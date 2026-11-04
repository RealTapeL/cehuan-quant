"""Version-one input digests shared by the backtest and proof rebuilders.

These functions intentionally preserve the serialization used by checkpoint
v1. A change to their normalization or encoding requires a new digest version
and an explicit checkpoint migration policy.
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
    """Hash a normalized event table using the checkpoint-v1 CSV convention."""
    canonical = frame.copy()
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_table_digest(frame: pd.DataFrame) -> str:
    """Hash a normalized, order-independent table using checkpoint-v1 rules."""
    canonical = frame.copy()
    canonical = canonical.sort_values(list(canonical.columns), kind="stable", na_position="first")
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalized_bars_digest(frame: pd.DataFrame) -> str:
    """Hash normalized bars exactly as checkpoint v1 records a window."""
    values = pd.util.hash_pandas_object(
        frame[BAR_COLUMNS].reset_index(drop=True), index=False,
    ).values.tobytes()
    return hashlib.sha256(values).hexdigest()


def compact_config_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: config[name]
        for name in ("data", "strategy", "market", "costs", "backtest", "benchmark")
        if name in config
    }


def compact_config_json(config: Mapping[str, Any]) -> str:
    return json.dumps(compact_config_payload(config), ensure_ascii=False, sort_keys=True, default=str)


def compact_config_digest(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(compact_config_json(config).encode("utf-8")).hexdigest()


def canonical_payload_digest(value: Mapping[str, Any]) -> str:
    """Hash a proof payload as deterministic compact JSON."""
    serialized = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def accounting_events_digest(
    events: Sequence[Any],
    dividend_tax_cny_by_event: Mapping[str, Any] | None = None,
    dividend_tax_treatment_by_event: Mapping[str, str] | None = None,
    dividend_payment_withholding_per_share_cny_by_event: Mapping[str, Any] | None = None,
    rights_offering_decisions_by_event: Mapping[str, Any] | None = None,
) -> str:
    """Hash the event and tax inputs with the exact checkpoint-v1 JSON format."""
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
    """Append one checkpoint-v1 window hash to the ordered history chain."""
    current = (
        f"{window_data_sha256}:{window_bars_sha256}:"
        f"{benchmark_data_sha256 or 'peer_from_input'}"
    )
    return hashlib.sha256(f"{previous_chain_sha256}:{current}".encode("utf-8")).hexdigest()
