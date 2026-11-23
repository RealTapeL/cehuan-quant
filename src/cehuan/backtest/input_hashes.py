"""回测与输入证明重建共用的 checkpoint-v1 摘要函数。

这些函数有意保留 checkpoint v1 的规范化和序列化口径。修改字段范围、排序或编码都
必须升级摘要版本并制定 checkpoint 迁移策略，不能悄悄改变既有哈希含义。
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from typing import Any, Mapping, Sequence

import pandas as pd

from cehuan.data.canonical import BAR_COLUMNS


CHECKPOINT_INPUT_HASH_VERSION = "checkpoint-v1"


def normalized_event_table_digest(frame: pd.DataFrame) -> str:
    """按 checkpoint-v1 CSV 规则摘要规范化事件表，保留当前列顺序。"""
    canonical = frame.copy()
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_table_digest(frame: pd.DataFrame) -> str:
    """按 checkpoint-v1 规则摘要规范化表；先排序行，因此不受输入行序影响。"""
    canonical = frame.copy()
    canonical = canonical.sort_values(list(canonical.columns), kind="stable", na_position="first")
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_bars_digest(frame: pd.DataFrame) -> str:
    """按 checkpoint v1 的行情列和 pandas 行哈希口径摘要一个窗口。"""
    values = pd.util.hash_pandas_object(
        frame[BAR_COLUMNS].reset_index(drop=True), index=False,
    ).values.tobytes()
    return hashlib.sha256(values).hexdigest()


def compact_config_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    """提取会影响回测的配置分区，排除无关运行配置。"""
    return {
        name: config[name]
        for name in ("data", "strategy", "market", "costs", "backtest", "benchmark")
        if name in config
    }


def compact_config_json(config: Mapping[str, Any]) -> str:
    """将回测相关配置序列化为排序键的 JSON 字符串。"""
    return json.dumps(compact_config_payload(config), ensure_ascii=False, sort_keys=True, default=str)


def compact_config_digest(config: Mapping[str, Any]) -> str:
    """计算回测相关配置的 UTF-8 JSON SHA-256。"""
    return hashlib.sha256(compact_config_json(config).encode("utf-8")).hexdigest()


def canonical_payload_digest(value: Mapping[str, Any]) -> str:
    """将证明 payload 按确定性紧凑 JSON 序列化后计算 SHA-256。"""
    serialized = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def accounting_events_digest(
    events: Sequence[Any],
    dividend_tax_cny_by_event: Mapping[str, Any] | None = None,
    dividend_tax_treatment_by_event: Mapping[str, str] | None = None,
    dividend_payment_withholding_per_share_cny_by_event: Mapping[str, Any] | None = None,
    rights_offering_decisions_by_event: Mapping[str, Any] | None = None,
) -> str:
    """按 checkpoint-v1 JSON 口径摘要公司行动、分红税务和配股决策输入。"""
    payload = {
        "events": [asdict(event) for event in sorted(events, key=lambda event: event.event_id)],
        "dividend_tax_cny_by_event": {
            event_id: None if amount is None else str(amount)
            for event_id, amount in sorted((dividend_tax_cny_by_event or {}).items())
        },
        "dividend_tax_treatment_by_event": dict(sorted((dividend_tax_treatment_by_event or {}).items())),
        "dividend_payment_withholding_per_share_cny_by_event": {
            event_id: None if amount is None else str(amount)
            for event_id, amount in sorted((dividend_payment_withholding_per_share_cny_by_event or {}).items())
        },
    }
    decisions = rights_offering_decisions_by_event or {}
    if decisions:
        payload["rights_offering_decisions_by_event"] = {
            event_id: asdict(decision) if hasattr(decision, "__dataclass_fields__") else dict(decision)
            for event_id, decision in sorted(decisions.items())
        }
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def chain_data_window_digest(
    previous_chain_sha256: str,
    window_data_sha256: str,
    window_bars_sha256: str,
    benchmark_data_sha256: str | None,
) -> str:
    """将当前窗口行情摘要追加到有序历史链，生成新的 checkpoint-v1 链摘要。"""
    current = (
        f"{window_data_sha256}:{window_bars_sha256}:"
        f"{benchmark_data_sha256 or 'peer_from_input'}"
    )
    return hashlib.sha256(f"{previous_chain_sha256}:{current}".encode("utf-8")).hexdigest()
