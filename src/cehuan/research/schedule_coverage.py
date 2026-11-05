from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.fee_schedule import map_fee_schedule_to_sessions, normalize_fee_schedule
from cehuan.data.market_rules import normalize_market_rule_schedule


def audit_schedule_file(
    *, path: str | Path | None, label: str,
    required_pairs: set[tuple[str, str]], required_dates: set[str],
) -> tuple[dict[str, Any], pd.DataFrame | None, list[dict[str, Any]]]:
    """Validate optional schedule input and report coverage only for the requested scope."""
    if label not in {"market_rules", "fee_schedule"}:
        raise ValueError("label must be market_rules or fee_schedule")
    if path is None:
        return ({"status": "NOT_PROVIDED", "path": None, "rows": None, "scope_coverage": "NOT_CHECKED"}, None, [])
    resolved = Path(path).resolve()
    if not resolved.is_file():
        return ({"status": "MISSING", "path": str(resolved), "rows": None, "scope_coverage": "NOT_CHECKED"}, None, [_input_record(resolved, label)])
    input_record = _input_record(resolved, label)
    try:
        raw = pd.read_csv(resolved, dtype=str, keep_default_na=False)
        if label == "market_rules":
            schedule = normalize_market_rule_schedule(raw)
            missing = sorted(
                (symbol, day) for symbol, day in required_pairs
                if not _effective_rows(schedule, symbol=symbol, day=day)
            )
            required_count = len(required_pairs)
        else:
            schedule = normalize_fee_schedule(raw)
            mapped = map_fee_schedule_to_sessions(schedule, sorted(required_dates))
            missing = sorted(("A_SHARE", day) for day in required_dates if day not in mapped)
            required_count = len(required_dates)
        complete = not missing
        return ({
            "status": "VERIFIED_FOR_SCOPE" if complete else "AVAILABLE_BUT_UNVERIFIED",
            "path": str(resolved),
            "sha256": input_record["sha256"],
            "rows": int(len(schedule)),
            "scope_coverage": "COMPLETE" if complete else "INCOMPLETE",
            "required_pair_count": required_count,
            "covered_pair_count": required_count - len(missing),
            "missing_scope_examples": [{"symbol_or_market": row[0], "date": row[1]} for row in missing[:20]],
            "adapter_validation": "PASSED",
            "source_semantics_review": "NOT_INDEPENDENTLY_VERIFIED",
            "reviewed_source_fields_present": True,
            "warning": "VERIFIED_FOR_SCOPE means adapter schema, review fields, non-overlap and effective-date coverage passed; source interpretation and completeness remain separately auditable.",
        }, schedule, [input_record])
    except Exception as exc:
        return ({
            "status": "FAILED",
            "path": str(resolved),
            "sha256": input_record["sha256"],
            "rows": None,
            "scope_coverage": "FAILED",
            "reason": str(exc),
        }, None, [input_record])


def _effective_rows(schedule: pd.DataFrame, *, symbol: str, day: str) -> list[dict[str, Any]]:
    date_value = pd.Timestamp(day)
    matches = schedule.loc[
        schedule.symbol.astype(str).eq(symbol)
        & schedule.valid_from.le(date_value)
        & (schedule.valid_to.isna() | schedule.valid_to.ge(date_value))
    ]
    return matches.to_dict(orient="records")


def _input_record(path: Path, role: str) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path.resolve()), "role": role, "sha256": None, "status": "MISSING"}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "role": role, "sha256": digest.hexdigest(), "status": "AVAILABLE_BUT_UNVERIFIED"}
