"""AKShare 分钟行情只读探测。模块对单证券、单日期、有限次数请求留存 SDK 序列化响应，验证原件到规范载荷的重建关系；不提供状态数据、前瞻排名或可交易结论。"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from cehuan.research.observation_parsers import (
    AKSHARE_MINUTE_PARSER,
    PARSER_VERSION,
    normalize_akshare_minute_frame,
    normalize_akshare_minute_records,
)


def capture_akshare_minute_probe(
    *, symbol: str, trade_date: str, output_dir: str | Path, max_attempts: int = 1,
) -> dict[str, Any]:
    """对一个证券和交易日执行有限 AKShare 分钟行情探测。参数：symbol/trade_date 定义查询，output_dir 必须是新目录，max_attempts 仅允许有限尝试。返回：试取报告和校验摘要。副作用：网络请求、写私有响应及来源哈希；保存的是 SDK 返回序列化，不是原始 HTTP 字节。异常：证券/日期/目录或响应校验不合约定时抛出异常。"""
    target = Path(output_dir).resolve()
    normalized_symbol = _normalize_symbol(symbol)
    parsed_date = date.fromisoformat(trade_date)
    if max_attempts not in {1, 2}:
        raise ValueError("max_attempts 仅允许 1 或 2，避免无界请求")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"拒绝覆盖已有数据源试取目录: {target}")

    import akshare as ak

    try:
        akshare_version = importlib.metadata.version("akshare")
    except importlib.metadata.PackageNotFoundError:
        akshare_version = str(getattr(ak, "__version__", "UNKNOWN"))

    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    attempts: list[dict[str, Any]] = []
    try:
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        for attempt_no in range(1, max_attempts + 1):
            request_started = _now_utc()
            try:
                frame = ak.stock_zh_a_hist_min_em(
                    symbol=normalized_symbol[2:],
                    start_date=f"{parsed_date.isoformat()} 09:30:00",
                    end_date=f"{parsed_date.isoformat()} 15:00:00",
                    period="1",
                    adjust="",
                )
                response_received = _now_utc()
                if frame is None:
                    records: list[dict[str, Any]] = []
                elif not hasattr(frame, "to_json"):
                    records = []
                    response_error = f"AKShare 返回类型不是 DataFrame: {type(frame).__name__}"
                else:
                    records = json.loads(frame.to_json(orient="records", force_ascii=False, date_format="iso"))
                    if not isinstance(records, list):
                        response_error = "AKShare DataFrame 序列化结果不是 JSON 数组"
                        records = []
                    else:
                        response_error = None
                if frame is None:
                    response_error = None
                raw_bytes = (json.dumps(records, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                raw_name = f"akshare-minute-attempt-{attempt_no}.json"
                (raw_dir / raw_name).write_bytes(raw_bytes)
                if response_error:
                    result_rows, outcome = [], "PARSE_FAILED"
                    parse_error = response_error
                elif records:
                    try:
                        result_rows = normalize_akshare_minute_frame(_FrameAdapter(records), normalized_symbol)
                        outcome, parse_error = ("AVAILABLE" if result_rows else "MISSING"), None
                    except Exception as exc:
                        result_rows, outcome = [], "PARSE_FAILED"
                        parse_error = _redact_error(f"{type(exc).__name__}: {exc}")
                else:
                    result_rows, outcome, parse_error = [], "MISSING", None
                attempts.append({
                    "attempt_no": attempt_no,
                    "attempt_id": f"akshare-min-{attempt_no}",
                    "request_started_at_utc": request_started,
                    "response_received_at_utc": response_received,
                    "outcome": outcome,
                    "error_category": None,
                    "error_message": parse_error,
                    "raw_response": _binding(staging / "raw" / raw_name, raw_name, "SDK_SERIALIZED_RETURN"),
                    "normalized_rows": result_rows,
                })
                if result_rows:
                    break
            except Exception as exc:
                response_received = _now_utc()
                error = {
                    "error_type": type(exc).__name__,
                    "error_message": _redact_error(str(exc)),
                    "classification": "PROVIDER_OR_NETWORK_ERROR_CAUSE_NOT_INFERRED",
                }
                raw_name = f"akshare-error-attempt-{attempt_no}.json"
                raw_bytes = (json.dumps(error, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                (raw_dir / raw_name).write_bytes(raw_bytes)
                attempts.append({
                    "attempt_no": attempt_no,
                    "attempt_id": f"akshare-min-{attempt_no}",
                    "request_started_at_utc": request_started,
                    "response_received_at_utc": response_received,
                    "outcome": "ERROR",
                    "error_category": "PROVIDER_OR_NETWORK_ERROR_CAUSE_NOT_INFERRED",
                    "error_message": error["error_message"],
                    "raw_response": _binding(staging / "raw" / raw_name, raw_name, "LOCAL_ERROR_RECORD"),
                    "normalized_rows": [],
                })
                if attempt_no < max_attempts:
                    time.sleep(0.5 * (2 ** (attempt_no - 1)))

        all_rows = [row for attempt in attempts for row in attempt["normalized_rows"]]
        requested_session = [
            row for row in all_rows
            if row["event_time_utc"].startswith(parsed_date.isoformat())
        ]
        report = {
            "schema_version": 1,
            "record_type": "READ_ONLY_PROVIDER_PROBE",
            "classification": "PROVIDER_PROBE_NOT_PAPER_SIGNAL",
            "probe_id": target.name,
            "provider": "AKShare / Eastmoney minute history adapter",
            "endpoint": "stock_zh_a_hist_min_em",
            "provider_sdk_version": akshare_version,
            "parser_name": AKSHARE_MINUTE_PARSER,
            "parser_version": PARSER_VERSION,
            "symbol": normalized_symbol,
            "provider_symbol": normalized_symbol[2:],
            "requested_trade_date": parsed_date.isoformat(),
            "request_parameters": {
                "start_date": f"{parsed_date.isoformat()} 09:30:00",
                "end_date": f"{parsed_date.isoformat()} 15:00:00",
                "period": "1",
                "adjust": "",
                "max_attempts": max_attempts,
            },
            "attempt_count": len(attempts),
            "attempts": [{key: value for key, value in attempt.items() if key != "normalized_rows"} | {
                "normalized_row_count": len(attempt["normalized_rows"]),
                "normalized_payloads_sha256": _sha256_json(attempt["normalized_rows"]),
            } for attempt in attempts],
            "overall_status": "AVAILABLE" if requested_session else attempts[-1]["outcome"],
            "response_row_count_for_requested_date": len(requested_session),
            "first_event_time_utc": requested_session[0]["event_time_utc"] if requested_session else None,
            "last_event_time_utc": requested_session[-1]["event_time_utc"] if requested_session else None,
            "payload_rebuilt_from_archived_sdk_return": "VERIFIED" if requested_session else "NOT_APPLICABLE",
            "response_capture_layer": "SDK_SERIALIZED_RETURN_NOT_ORIGINAL_HTTP_BYTES",
            "event_time_timezone_assumption": "Naive AKShare 时间字段 interpreted as Asia/Shanghai, then normalized to UTC",
            "provider_published_at_utc": None,
            "provider_publication_time_status": "UNVERIFIED_NOT_RETURNED_BY_ADAPTER",
            "status_data": "NOT_REQUESTED_BY_THIS_PROBE",
            "research_eligibility": "UNKNOWN",
            "historical_point_in_time_claim": "NOT_ESTABLISHED; local fetch records only when this probe received the returned data",
            "code_revision": _git_revision(),
            "worktree_dirty": _git_dirty(),
            "broker_connected": False,
            "account_accessed": False,
            "orders_generated": False,
            "private_raw_files": True,
        }
        normalized_path = staging / "normalized-rows.json"
        normalized_path.write_text(json.dumps(requested_session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["normalized_rows_binding"] = {
            "path": normalized_path.name,
            "sha256": _sha256(normalized_path),
        }
        (staging / "probe-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validation = validate_akshare_probe_directory(staging)
        (staging / "validation-report.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.exists():
            raise FileExistsError(f"发布前目标已出现，拒绝覆盖: {target}")
        os.rename(staging, target)
        return {"output_dir": str(target), "report": report, "validation": validation}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def validate_akshare_probe_directory(directory: str | Path) -> dict[str, Any]:
    """从归档 SDK 返回重建分钟载荷并核对全部绑定。参数：directory 为试取目录。返回：原件哈希和规范载荷核验状态。异常：路径越界、文件摘要不匹配、解析结果与报告冲突时阻断；不证明供应商发布时间。"""
    root = Path(directory).resolve()
    report = json.loads((root / "probe-report.json").read_text(encoding="utf-8"))
    if report.get("record_type") != "READ_ONLY_PROVIDER_PROBE":
        raise ValueError("试取报告类型错误")
    rows_binding = report.get("normalized_rows_binding", {})
    rows_path = _verified_relative_file(root, rows_binding, "normalized rows")
    normalized_rows = json.loads(rows_path.read_text(encoding="utf-8"))
    if not isinstance(normalized_rows, list):
        raise ValueError("normalized-rows.json 必须是数组")
    rebuilt_for_requested_date: list[dict[str, Any]] = []
    verified_raw = 0
    for attempt in report.get("attempts", []):
        binding = attempt.get("raw_response", {})
        raw_path = _verified_relative_file(root, binding, "raw response")
        outcome = attempt.get("outcome")
        if outcome == "AVAILABLE":
            rebuilt = normalize_akshare_minute_records(raw_path.read_bytes(), report["symbol"])
            if _sha256_json(rebuilt) != attempt.get("normalized_payloads_sha256"):
                raise ValueError(f"attempt {attempt.get('attempt_id')} 原始响应规范化摘要不匹配")
            verified_raw += 1
            requested_date = report["requested_trade_date"]
            rebuilt_for_requested_date.extend(
                row for row in rebuilt if row["event_time_utc"].startswith(requested_date)
            )
        elif outcome == "MISSING":
            if normalize_akshare_minute_records(raw_path.read_bytes(), report["symbol"]):
                raise ValueError(f"attempt {attempt.get('attempt_id')} 标记 MISSING，但原始响应有分钟记录")
        elif outcome == "PARSE_FAILED":
            try:
                normalize_akshare_minute_records(raw_path.read_bytes(), report["symbol"])
            except Exception:
                pass
            else:
                raise ValueError(f"attempt {attempt.get('attempt_id')} 标记 PARSE_FAILED，但解析器能成功重建")
        elif outcome != "ERROR":
            raise ValueError(f"未知试取状态: {outcome}")
    if _sha256_json(rebuilt_for_requested_date) != _sha256_json(normalized_rows):
        raise ValueError("normalized-rows 与已归档响应重建结果不一致")
    return {
        "status": "VALID",
        "raw_file_hashes_valid": True,
        "normalized_payloads_rebuilt_from_raw": True,
        "verified_successful_raw_responses": verified_raw,
        "normalized_requested_date_rows": len(normalized_rows),
        "provider_published_at_utc": report.get("provider_published_at_utc"),
        "historical_point_in_time_claim": "NOT_ESTABLISHED",
        "orders_generated": False,
    }


def _normalize_symbol(value: str) -> str:
    """检查并规范沪深北六位代码。参数：value 为输入证券代码。返回：大写 SH/SZ/BJ 前缀代码。异常：代码格式不合约定时抛 ValueError。"""
    symbol = str(value).strip().upper()
    if re.fullmatch(r"(SH|SZ|BJ)[0-9]{6}", symbol):
        return symbol
    raise ValueError("symbol 必须使用 SH/SZ/BJ 加六位代码，例如 SH600000")


def _now_utc() -> str:
    """读取当前 UTC 时间并序列化。返回：Z 后缀 ISO 字符串；用于记录本机请求时点，不代表供应商发布时间。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _binding(path: Path, relative: str, layer: str) -> dict[str, Any]:
    """为目录内响应构造路径、SHA-256、格式和捕获层绑定。参数：path 为响应文件，relative 为相对名，layer 标明 SDK 序列化或本地错误记录。返回：绑定字典。"""
    return {"path": f"raw/{relative}", "sha256": _sha256(path), "format": "JSON", "capture_layer": layer}


def _verified_relative_file(root: Path, binding: dict[str, Any], label: str) -> Path:
    """解析归档目录内相对文件并验证摘要。参数：root 为归档根，binding 为路径/哈希记录，label 用于错误定位。返回：已核验文件路径。异常：绝对/越界路径、文件缺失或摘要不符时阻断。"""
    relative = Path(str(binding.get("path", "")))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label} 路径无效")
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError(f"{label} 文件缺失或路径越界")
    expected = str(binding.get("sha256", "")).lower()
    if _sha256(path) != expected:
        raise ValueError(f"{label} SHA-256 不匹配")
    return path


def _sha256(path: Path) -> str:
    """流式计算文件 SHA-256。参数：path 为文件路径。返回：十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    """按稳定 JSON 序列化计算对象摘要。参数：value 为 JSON 兼容对象。返回：SHA-256；不可规范化值会被拒绝。"""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _redact_error(message: str) -> str:
    """清理错误文本中的常见凭据字段并截断长度。参数：message 为供应商/解析错误。返回：有限长度安全文本；不能保证识别所有秘密格式。"""
    text_value = re.sub(r"(?i)(token|authorization|password|secret)=([^&\s]+)", r"\1=[REDACTED]", message)
    return text_value[:2000]


def _git_revision() -> str | None:
    """读取当前 Git 提交号。返回：哈希或 None；只执行只读 Git 查询。"""
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    """检查工作区是否有未提交变化。返回：布尔值或 None；只执行只读 Git 查询。"""
    try:
        return bool(subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True).strip())
    except Exception:
        return None


class _FrameAdapter:
    """给解析器测试复用的 DataFrame 形状适配器。状态只包含传入记录；不连接 AKShare。"""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        """保存要序列化的记录。参数：records 为规范化字典列表。副作用：保存在实例字段中。"""
        self._records = records

    def to_json(self, **_: Any) -> str:
        """提供 DataFrame 风格的 JSON 序列化接口。参数：关键字参数仅为兼容调用签名。返回：records 的 UTF-8 JSON 字符串。"""
        return json.dumps(self._records, ensure_ascii=False)
