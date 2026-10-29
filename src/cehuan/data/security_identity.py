from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.canonical import canonical_symbol


DEFAULT_SECURITY_IDENTITY_EVENTS = (
    Path(__file__).resolve().parents[3] / "configs" / "security_identity_events.csv"
)

IDENTITY_EVENT_COLUMNS = [
    "event_id",
    "old_symbol",
    "new_symbol",
    "effective_date",
    "share_ratio",
    "ratio_basis",
    "security_identity",
    "event_status",
    "evidence_type",
    "evidence_url",
    "evidence_summary",
]

OUT_DATE_EVIDENCE_COLUMNS = [
    "trade_date",
    "symbol",
    "basic_out_date",
    "confirmed_delisting_date",
    "evidence_type",
    "evidence_url",
    "evidence_summary",
    "additional_action_required",
    "review_status",
]


def normalize_security_identity_events(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Validate the explicit, evidence-backed point-in-time identity event table."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS)
    missing = set(IDENTITY_EVENT_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError("证券身份事件表缺少字段：" + ", ".join(sorted(missing)))
    events = frame[IDENTITY_EVENT_COLUMNS].copy()
    for column in IDENTITY_EVENT_COLUMNS:
        events[column] = events[column].astype("string").fillna("").str.strip()
    events["old_symbol"] = events["old_symbol"].map(canonical_symbol)
    events["new_symbol"] = events["new_symbol"].map(canonical_symbol)
    parsed_dates = pd.to_datetime(events["effective_date"], errors="coerce")
    if parsed_dates.isna().any():
        raise ValueError("证券身份事件表含无效 effective_date")
    events["effective_date"] = parsed_dates.dt.strftime("%Y-%m-%d")
    ratios = pd.to_numeric(events["share_ratio"], errors="coerce")
    if ratios.isna().any() or (~ratios.map(math.isfinite)).any() or (ratios <= 0).any():
        raise ValueError("证券身份事件表 share_ratio 必须为有限正数")
    events["share_ratio"] = ratios.astype(float)
    if events["event_id"].eq("").any() or events["security_identity"].eq("").any():
        raise ValueError("证券身份事件表 event_id 与 security_identity 不能为空")
    if events["old_symbol"].eq(events["new_symbol"]).any():
        raise ValueError("证券身份事件的旧代码和新代码不能相同")
    if not events["event_status"].eq("CONFIRMED").all():
        raise ValueError("只有 event_status=CONFIRMED 的事件可以进入回测")
    if not events["ratio_basis"].eq("old_shares_to_new_shares").all():
        raise ValueError("证券身份事件 share_ratio 必须按旧股数到新股数解释")
    if not events["evidence_url"].str.startswith("https://").all():
        raise ValueError("每个已确认证券身份事件都必须有 HTTPS 公告来源")
    if events.duplicated(["old_symbol", "effective_date"]).any():
        raise ValueError("同一旧代码和生效日不能登记多个身份事件")
    if events.duplicated(["new_symbol", "effective_date"]).any():
        raise ValueError("同一新代码和生效日不能对应多个旧代码事件")
    return events.sort_values(["effective_date", "old_symbol"]).reset_index(drop=True)


def read_security_identity_events(path: str | Path = DEFAULT_SECURITY_IDENTITY_EVENTS) -> pd.DataFrame:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"证券身份事件表不存在：{source}")
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    return normalize_security_identity_events(frame)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).expanduser().resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_security_identity_events(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    events = normalize_security_identity_events(frame)
    return events.to_dict(orient="records")


def audit_out_date_boundary_evidence(
    reconciliation_path: str | Path,
    evidence_path: str | Path | None,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Check row-by-row primary-source evidence for basic outDate/list boundary rows.

    This validates a reviewed evidence register against the immutable snapshot's
    reconciliation rows. It does not fetch the linked notices at runtime.
    """
    reconciliation_file = Path(reconciliation_path).expanduser().resolve()
    if not reconciliation_file.is_file():
        empty = pd.DataFrame(columns=OUT_DATE_EVIDENCE_COLUMNS + ["resolution_status"])
        return ({
            "healthy": False,
            "errors": ["点时股票池快照缺少 reconciliation.csv"],
            "source_rows": 0,
            "resolved_rows": 0,
            "unresolved_rows": 0,
            "evidence_file": None,
            "evidence_sha256": None,
        }, empty)
    source = pd.read_csv(reconciliation_file, dtype=str, keep_default_na=False)
    if source.empty:
        empty = pd.DataFrame(columns=OUT_DATE_EVIDENCE_COLUMNS + ["resolution_status"])
        evidence_file = Path(evidence_path).expanduser().resolve() if evidence_path else None
        digest = sha256_file(evidence_file) if evidence_file and evidence_file.is_file() else None
        return ({
            "healthy": True,
            "errors": [],
            "source_rows": 0,
            "resolved_rows": 0,
            "unresolved_rows": 0,
            "evidence_file": str(evidence_file) if evidence_file else None,
            "evidence_sha256": digest,
        }, empty)
    required_source = {"trade_date", "symbol", "outDate", "discrepancy"}
    if not required_source.issubset(source.columns):
        raise ValueError("reconciliation.csv 缺少 outDate 对账所需字段")
    expected = source.loc[
        source["discrepancy"].eq("BASIC_INTERVAL_SAYS_LISTED_BUT_NOT_IN_DATE_LIST")
    ].copy()
    if expected.empty:
        empty = pd.DataFrame(columns=OUT_DATE_EVIDENCE_COLUMNS + ["resolution_status"])
        return ({
            "healthy": True,
            "errors": [],
            "source_rows": 0,
            "resolved_rows": 0,
            "unresolved_rows": 0,
            "evidence_file": str(Path(evidence_path).expanduser().resolve()) if evidence_path else None,
            "evidence_sha256": sha256_file(evidence_path) if evidence_path and Path(evidence_path).is_file() else None,
        }, empty)

    evidence_file = Path(evidence_path).expanduser().resolve() if evidence_path else None
    evidence_hash = sha256_file(evidence_file) if evidence_file and evidence_file.is_file() else None
    missing_evidence = evidence_file is None or not evidence_file.is_file()
    if missing_evidence:
        evidence = pd.DataFrame(columns=OUT_DATE_EVIDENCE_COLUMNS)
    else:
        evidence = pd.read_csv(evidence_file, dtype=str, keep_default_na=False)
        missing_columns = set(OUT_DATE_EVIDENCE_COLUMNS).difference(evidence.columns)
        if missing_columns:
            raise ValueError("outDate 独立证据表缺少字段：" + ", ".join(sorted(missing_columns)))
        evidence = evidence[OUT_DATE_EVIDENCE_COLUMNS].copy()
        evidence["symbol"] = evidence["symbol"].map(canonical_symbol)
        if evidence.duplicated(["trade_date", "symbol"]).any():
            raise ValueError("outDate 独立证据表存在重复 (trade_date, symbol)")

    expected["symbol"] = expected["symbol"].map(canonical_symbol)
    evidence_index = evidence.set_index(["trade_date", "symbol"], drop=False) if not evidence.empty else None
    reviewed_rows: list[dict[str, str]] = []
    errors: list[str] = []
    resolved = 0
    for row in expected.to_dict(orient="records"):
        key = (str(row["trade_date"]), str(row["symbol"]))
        evidence_row = evidence_index.loc[key] if evidence_index is not None and key in evidence_index.index else None
        if isinstance(evidence_row, pd.DataFrame):
            raise ValueError(f"outDate 独立证据表匹配 {key} 不唯一")
        record = {column: str(evidence_row[column]) if evidence_row is not None else "" for column in OUT_DATE_EVIDENCE_COLUMNS}
        valid = bool(
            evidence_row is not None
            and str(row["outDate"]) == key[0]
            and record["basic_out_date"] == str(row["outDate"])
            and record["confirmed_delisting_date"] == key[0]
            and record["review_status"] in {"PRIMARY_NOTICE_CONFIRMED", "EXCHANGE_CIRCULAR_CONFIRMED"}
            and record["evidence_type"]
            and record["evidence_url"].startswith("https://")
            and record["evidence_summary"]
        )
        record.update({
            "trade_date": key[0],
            "symbol": key[1],
            "source_basic_out_date": str(row["outDate"]),
            "source_discrepancy": str(row["discrepancy"]),
            "resolution_status": "EXPLAINED_OUT_DATE_BOUNDARY" if valid else "UNRESOLVED_OUT_DATE_BOUNDARY",
        })
        reviewed_rows.append(record)
        if valid:
            resolved += 1
        else:
            errors.append(f"{key[0]} {key[1]} 的 outDate/日期名单差异缺少匹配的独立公告证据")

    expected_keys = set(zip(expected["trade_date"].astype(str), expected["symbol"].astype(str)))
    if not evidence.empty:
        extra_keys = set(zip(evidence["trade_date"].astype(str), evidence["symbol"].astype(str))) - expected_keys
        if extra_keys:
            errors.append("outDate 独立证据表含当前快照中不存在的记录：" + ", ".join(f"{day}/{symbol}" for day, symbol in sorted(extra_keys)))
    result_frame = pd.DataFrame(reviewed_rows)
    return ({
        "healthy": not errors and resolved == len(expected),
        "errors": errors,
        "source_rows": int(len(expected)),
        "resolved_rows": int(resolved),
        "unresolved_rows": int(len(expected) - resolved),
        "evidence_file": str(evidence_file) if evidence_file else None,
        "evidence_sha256": evidence_hash,
    }, result_frame)
