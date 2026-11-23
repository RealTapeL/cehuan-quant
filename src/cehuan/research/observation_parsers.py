"""观察证据解析器。把归档的报价/状态 JSON 或 AKShare SDK 序列化结果规范为有限字段，并核对证券和事件时间；SDK JSON 不等同于供应商原始 HTTP 响应。"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo


PARSER_VERSION = "1"
SYNTHETIC_QUOTE_PARSER = "cehuan.synthetic.quote_json"
SYNTHETIC_STATUS_PARSER = "cehuan.synthetic.status_json"
AKSHARE_MINUTE_PARSER = "cehuan.akshare.stock_minute_json"


def parse_observation_payload(
    raw_bytes: bytes,
    *,
    parser_name: str,
    parser_version: str,
    observation_type: str,
    symbol: str,
    expected_event_time_utc: str | None,
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """根据已登记解析器重建规范化报价或状态观察。参数：raw_bytes 为归档字节，parser_name/version 标识解析契约，observation_type 和 symbol 定义预期记录，expected_event_time_utc 可指定事件时点，source_metadata 提供来源字段映射。返回：规范化字典；没有对应分钟记录时可返回 None。异常：版本、JSON、证券、时间或解析器类型不匹配时抛出 ValueError。"""
    if parser_version != PARSER_VERSION:
        raise ValueError(f"不支持的解析器版本: {parser_name}@{parser_version}")
    metadata = source_metadata or {}
    try:
        decoded = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"原始响应不是可解析 UTF-8 JSON: {exc}") from exc

    if parser_name == SYNTHETIC_QUOTE_PARSER and observation_type == "QUOTE":
        return _normalize_generic_quote(decoded, symbol, expected_event_time_utc)
    if parser_name == SYNTHETIC_STATUS_PARSER and observation_type == "STATUS":
        return _normalize_generic_status(decoded, symbol, expected_event_time_utc)
    if parser_name == AKSHARE_MINUTE_PARSER and observation_type == "QUOTE":
        return _normalize_akshare_quote(decoded, symbol, expected_event_time_utc, metadata)
    raise ValueError(f"解析器与观察类型不匹配或未注册: {parser_name} / {observation_type}")


def canonical_payload_sha256(payload: dict[str, Any]) -> str:
    """计算规范 JSON 载荷的稳定 SHA-256。参数：payload 为规范字段字典。返回：小写十六进制摘要。异常：非 JSON 值或 NaN 等不可规范化内容会被拒绝。"""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_akshare_minute_frame(frame: Any, symbol: str) -> list[dict[str, Any]]:
    """将 AKShare 分钟 DataFrame 逐行规范化为报价观察。参数：frame 为 SDK 返回表，symbol 为项目规范代码。返回：逐分钟规范记录列表。异常：时间/开盘字段无效或无法规范化时阻断；输入是 SDK 序列化结果而非 HTTP 原文。"""
    records = json.loads(frame.to_json(orient="records", force_ascii=False, date_format="iso"))
    if not isinstance(records, list):
        raise ValueError("AKShare 分钟结果必须序列化为 JSON 数组")
    result = []
    for record in records:
        event_time = _local_event_to_utc(record.get("时间"))
        result.append(_normalize_akshare_quote([record], symbol, event_time, {}))
    return result


def normalize_akshare_minute_records(raw_bytes: bytes, symbol: str) -> list[dict[str, Any]]:
    """将已归档的 AKShare 分钟 JSON 记录规范化。参数：raw_bytes 为 JSON 数组字节，symbol 为规范代码。返回：逐分钟规范记录列表。异常：JSON 结构错误、重复事件时间或记录无法规范化时抛出 ValueError。"""
    try:
        records = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"AKShare 归档不是可解析 UTF-8 JSON: {exc}") from exc
    if not isinstance(records, list):
        raise ValueError("AKShare 归档必须是 JSON 记录数组")
    normalized = []
    seen: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("AKShare 分钟记录必须是对象")
        event_time = _local_event_to_utc(row.get("时间"))
        if event_time in seen:
            raise ValueError(f"AKShare 原始响应包含重复分钟: {event_time}")
        seen.add(event_time)
        parsed = _normalize_akshare_quote([row], symbol, event_time, {})
        if parsed is None:
            raise ValueError("AKShare 分钟记录未能规范化")
        normalized.append(parsed)
    return normalized


def _normalize_generic_quote(raw: Any, symbol: str, expected_event_time_utc: str | None) -> dict[str, Any]:
    """校验通用报价对象中的证券、事件时间和开盘价。参数：raw 为解码对象，symbol/expected_event_time_utc 为预期身份及时点。返回：规范报价字段。异常：对象结构、证券、时间或价格不符时拒绝。"""
    if not isinstance(raw, dict):
        raise ValueError("报价 JSON 必须是对象")
    raw_symbol = str(raw.get("symbol", "")).strip().upper()
    if raw_symbol != symbol.upper():
        raise ValueError(f"原始证券代码 {raw_symbol!r} 与记录代码 {symbol!r} 不一致")
    raw_event = _canonical_utc(raw.get("event_time_utc"))
    _require_expected_event(raw_event, expected_event_time_utc)
    price = _positive_price(raw.get("open_price_cny"))
    return {
        "symbol": symbol.upper(),
        "event_time_utc": raw_event,
        "open_price_cny": price,
        "price_semantics": str(raw.get("price_semantics", "OBSERVED_OPEN_PRICE")),
    }


def _normalize_generic_status(raw: Any, symbol: str, expected_event_time_utc: str | None) -> dict[str, Any]:
    """校验通用证券状态对象。参数：raw 为解码对象，symbol/expected_event_time_utc 为预期身份及时点。返回：明确包含上市、停牌和 ST 三态字段的记录。异常：字段不是明确布尔值时拒绝；不接受由状态推导的 can_buy。"""
    if not isinstance(raw, dict):
        raise ValueError("状态 JSON 必须是对象")
    raw_symbol = str(raw.get("symbol", "")).strip().upper()
    if raw_symbol != symbol.upper():
        raise ValueError(f"原始证券代码 {raw_symbol!r} 与记录代码 {symbol!r} 不一致")
    raw_event = _canonical_utc(raw.get("event_time_utc"))
    _require_expected_event(raw_event, expected_event_time_utc)
    fields = ("is_listed", "is_suspended", "is_st")
    if any(not isinstance(raw.get(field), bool) for field in fields):
        raise ValueError("状态响应必须明确包含 is_listed、is_suspended、is_st 布尔值")
    # can_buy is intentionally not accepted as a source fact: it is a derived
    # research eligibility conclusion that needs rules, quotes and account state.
    return {"symbol": symbol.upper(), "event_time_utc": raw_event, **{field: raw[field] for field in fields}}


def _normalize_akshare_quote(
    raw: Any,
    symbol: str,
    expected_event_time_utc: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """从 AKShare 分钟记录中选取且仅选取目标事件时点。参数：raw 为记录数组，symbol 为规范代码，expected_event_time_utc 为可选目标时点，metadata 保存供应商代码映射。返回：规范开盘报价；未找到目标记录时返回 None。异常：重复时点、必需列或代码映射不一致时抛出 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError("AKShare 分钟结果 JSON 必须是记录数组")
    expected = _canonical_utc(expected_event_time_utc) if expected_event_time_utc else None
    matching = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("AKShare 分钟记录必须是对象")
        if "时间" not in row or "开盘" not in row:
            raise ValueError("AKShare 分钟记录缺少 时间 或 开盘 字段")
        event_time = _local_event_to_utc(row["时间"])
        if expected is None or event_time == expected:
            matching.append((event_time, row))
    if not matching:
        return None
    if len(matching) != 1:
        raise ValueError("AKShare 原始响应中目标事件时间重复")
    event_time, row = matching[0]
    provider_symbol = str(metadata.get("provider_symbol", "")).strip()
    expected_provider_symbol = symbol[2:] if symbol.upper().startswith(("SH", "SZ", "BJ")) else symbol
    if provider_symbol and provider_symbol != expected_provider_symbol:
        raise ValueError("AKShare provider_symbol 与规范证券代码不一致")
    return {
        "symbol": symbol.upper(),
        "event_time_utc": event_time,
        "open_price_cny": _positive_price(row["开盘"]),
        "price_semantics": "MINUTE_BAR_OPEN",
        "provider_time_field": "时间",
        "provider_price_field": "开盘",
    }


def _positive_price(value: Any) -> float:
    """解析有限正价格。参数：value 为原始价格值。返回：float 价格。异常：无法解析、非有限或非正数时抛出 ValueError。"""
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("报价缺少数值价格") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("报价必须是有限正数")
    return price


def _local_event_to_utc(value: Any) -> str:
    """将本地事件时间解释为上海时间（若未带时区）并转 UTC。参数：value 为来源事件时间。返回：Z 后缀的 UTC 时间字符串。异常：缺失或格式错误时抛出解析异常。"""
    if value is None or str(value).strip() == "":
        raise ValueError("行情记录缺少事件时间")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_utc(value: Any) -> str:
    """校验并规范带时区的事件时间。参数：value 为时间值。返回：Z 后缀 UTC 字符串。异常：缺失、格式错误或无时区时抛出 ValueError。"""
    if value is None or str(value).strip() == "":
        raise ValueError("事件时间缺失")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("事件时间必须带时区")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_expected_event(actual: str, expected: str | None) -> None:
    """将实际事件时间与登记的预期时间逐字规范后比较。参数：actual 为规范实时时间，expected 为可选登记时间。返回：匹配时 None。异常：时点不匹配时抛出 ValueError。"""
    if expected is not None and actual != _canonical_utc(expected):
        raise ValueError(f"原始事件时间 {actual} 与登记事件时间 {_canonical_utc(expected)} 不一致")
