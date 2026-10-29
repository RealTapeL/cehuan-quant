from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.baostock_adapter import _canonical_vendor_symbol
from cehuan.data.canonical import parse_optional_bool


REQUIRED_COLUMNS = {
    "trade_date",
    "symbol",
    "source",
    "source_record_date",
    "source_retrieved_at_utc",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def import_historical_universe_csv(
    input_path: str | Path,
    root: str | Path,
    snapshot_id: str,
) -> dict[str, Any]:
    """Import dated membership evidence without synthesizing history from a current list."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", snapshot_id) or ".." in snapshot_id:
        raise ValueError("snapshot_id 只能包含字母、数字、点、下划线和连字符")
    source_file = Path(input_path).expanduser().resolve()
    if not source_file.is_file():
        raise FileNotFoundError(source_file)
    frame = pd.read_csv(source_file, dtype=str, keep_default_na=False)
    missing = REQUIRED_COLUMNS.difference(frame.columns)
    if missing:
        raise ValueError("历史股票池 CSV 缺少字段：" + ", ".join(sorted(missing)))
    if frame.empty:
        raise ValueError("历史股票池 CSV 为空")

    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    frame["source_record_date"] = pd.to_datetime(frame["source_record_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if frame["trade_date"].isna().any() or frame["source_record_date"].isna().any():
        raise ValueError("trade_date 和 source_record_date 必须是有效日期")
    conflict = frame["trade_date"].ne(frame["source_record_date"])
    if conflict.any():
        rows = frame.index[conflict].tolist()[:10]
        raise ValueError(f"存在非点时记录：source_record_date 必须等于 trade_date；行号 {rows}")

    frame["symbol"] = frame["symbol"].map(_canonical_vendor_symbol)
    if frame["symbol"].astype(str).str.fullmatch(r"(?:SH|SZ|BJ)\d{6}").eq(False).any():
        raise ValueError("symbol 必须能规范化为 SH/SZ/BJ + 六位证券代码")
    key_columns = ["trade_date", "symbol"]
    duplicate = frame.duplicated(key_columns, keep=False)
    if duplicate.any():
        keys = frame.loc[duplicate, key_columns].drop_duplicates().head(10).to_dict(orient="records")
        raise ValueError(f"存在重复每日成员，拒绝静默去重：{keys}")
    if frame["source"].astype(str).str.strip().eq("").any():
        raise ValueError("source 不得为空")
    retrieved = pd.to_datetime(frame["source_retrieved_at_utc"], utc=True, errors="coerce")
    if retrieved.isna().any():
        raise ValueError("source_retrieved_at_utc 必须包含可解析的时区时间")
    if retrieved.gt(pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=5)).any():
        raise ValueError("source_retrieved_at_utc 位于未来，拒绝导入")
    frame["source_retrieved_at_utc"] = retrieved.dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    for column in ("security_type", "daily_trade_status", "is_st", "is_suspended", "st_status_source"):
        if column not in frame:
            frame[column] = ""
    frame["security_type"] = frame["security_type"].astype(str).str.strip()
    frame["daily_trade_status"] = frame["daily_trade_status"].astype(str).str.strip()
    suspended = parse_optional_bool(frame["is_suspended"])
    frame["is_st"] = parse_optional_bool(frame["is_st"])
    blank_status = frame["daily_trade_status"].eq("")
    frame.loc[blank_status & suspended.eq(True).fillna(False), "daily_trade_status"] = "0"
    frame.loc[blank_status & suspended.eq(False).fillna(False), "daily_trade_status"] = "1"
    frame.loc[~frame["daily_trade_status"].isin(["", "0", "1"]), "daily_trade_status"] = ""

    output_rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        security_type = row["security_type"] or None
        status = row["daily_trade_status"] or None
        is_st = row["is_st"]
        is_stock = security_type == "1"
        if is_stock:
            pool_status = "STOCK_MEMBER"
            if status == "0":
                eligibility = "SUSPENDED_ON_DATE"
            elif status != "1":
                eligibility = "UNKNOWN_DAILY_TRADE_STATUS"
            elif is_st is True or str(is_st).lower() == "true":
                eligibility = "EXCLUDED_ST"
            elif pd.isna(is_st):
                eligibility = "UNKNOWN_ST_STATUS"
            else:
                eligibility = "CANDIDATE_PENDING_OTHER_RULES"
        elif security_type:
            pool_status = "FILTERED_NON_STOCK"
            eligibility = "NOT_EQUITY"
        else:
            pool_status = "UNKNOWN_TYPE"
            eligibility = "UNKNOWN_SECURITY_TYPE"
        output_rows.append({
            "trade_date": row["trade_date"],
            "symbol": row["symbol"],
            "name_on_date_from_daily_list": row.get("name", "") or None,
            "security_type_from_source": security_type,
            "member_of_date_specific_list": True,
            "daily_trade_status": status,
            "daily_trade_status_meaning": "TRADING" if status == "1" else "SUSPENDED" if status == "0" else "UNKNOWN",
            "is_st": is_st,
            "st_status_source": row["st_status_source"] or ("SOURCE_CSV" if not pd.isna(is_st) else "UNKNOWN"),
            "pool_status": pool_status,
            "eligibility_status": eligibility,
            "membership_source": row["source"],
            "source_record_date": row["source_record_date"],
            "membership_retrieved_at_utc": row["source_retrieved_at_utc"],
        })
    membership = pd.DataFrame(output_rows).sort_values(["trade_date", "symbol"], kind="stable")

    parent = Path(root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / snapshot_id
    if destination.exists():
        raise FileExistsError(f"快照已存在，拒绝覆盖：{destination}")
    work = parent / f".{snapshot_id}.partial"
    if work.exists():
        raise FileExistsError(f"发现同名未完成导入任务，拒绝覆盖：{work}")
    work.mkdir()
    try:
        _atomic_csv(membership, work / "securities_by_date.csv")
        daily = membership.groupby("trade_date", sort=True).agg(
            daily_list_total_rows=("symbol", "size"),
            stock_type_1_count=("pool_status", lambda values: int(values.eq("STOCK_MEMBER").sum())),
            non_stock_count=("pool_status", lambda values: int(values.eq("FILTERED_NON_STOCK").sum())),
            unknown_type_count=("pool_status", lambda values: int(values.eq("UNKNOWN_TYPE").sum())),
            st_unknown_count=("is_st", lambda values: int(values.isna().sum())),
            daily_status_unknown_count=("daily_trade_status", lambda values: int((values.isna() | values.eq("")).sum())),
        ).reset_index()
        _atomic_csv(daily, work / "daily_coverage.csv")
        source_copy = work / "raw_source.csv"
        source_copy.write_bytes(source_file.read_bytes())
        manifest = {
            "snapshot_id": snapshot_id,
            "provider": "external_csv_import",
            "source_file_name": source_file.name,
            "source_file_sha256": _sha256(source_file),
            "files_sha256": {
                "raw_source.csv": _sha256(source_copy),
                "securities_by_date.csv": _sha256(work / "securities_by_date.csv"),
                "daily_coverage.csv": _sha256(work / "daily_coverage.csv"),
            },
            "date_range": {
                "start": str(membership["trade_date"].min()),
                "end": str(membership["trade_date"].max()),
                "date_count": int(membership["trade_date"].nunique()),
            },
            "coverage": {
                "membership_rows": int(len(membership)),
                "stock_rows": int(membership["pool_status"].eq("STOCK_MEMBER").sum()),
                "unknown_type_rows": int(membership["pool_status"].eq("UNKNOWN_TYPE").sum()),
                "st_unknown_rows": int(membership["is_st"].isna().sum()),
                "trade_status_unknown_rows": int(membership["daily_trade_status"].isna().sum()),
                "bars_attached": False,
            },
            "point_in_time_policy": "Every row must include an explicit source_record_date equal to trade_date; rows from a current list cannot be used to fill historical dates.",
            "status_policy": "Missing type, ST, or suspension data remains unknown and is not inferred from current status.",
            "imported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _atomic_json(manifest, work / "manifest.json")
        os.replace(work, destination)
    except Exception:
        # Preserve failed work as an inspectable partial import; never overwrite it.
        raise
    return {"state": "complete", "snapshot_path": str(destination), **manifest}
