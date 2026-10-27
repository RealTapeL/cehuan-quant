from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.baostock_adapter import (
    BaoStockSession,
    _canonical_vendor_symbol,
    _parse_date,
    normalize_baostock_calendar,
)
from cehuan.data.canonical import BAR_COLUMNS, canonical_symbol, normalize_bar_frame, parse_optional_bool


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flush_baostock_requests(
    session: BaoStockSession,
    request_log_path: Path,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
) -> None:
    if not session.request_log:
        return
    with request_log_path.open("a", encoding="utf-8") as handle:
        for request in session.request_log:
            handle.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
    checkpoint["request_count"] = int(checkpoint.get("request_count", 0)) + len(session.request_log)
    checkpoint["provider_package_version"] = session.package_version
    checkpoint["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session.request_log.clear()
    _atomic_json(checkpoint, checkpoint_path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _as_text(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _date_is_in_basic_interval(day: str, basic: pd.Series) -> bool | None:
    ipo = _as_text(basic.get("ipoDate"))
    out_date = _as_text(basic.get("outDate"))
    if not ipo:
        return None
    return ipo <= day and (not out_date or day <= out_date)


def _load_optional_bars(path: str | Path | None) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict[str, object]]]:
    if path is None:
        return set(), {}
    frame = pd.read_csv(path, dtype={"symbol": str, "trade_date": str})
    required = {"symbol", "trade_date"}
    if not required.issubset(frame.columns):
        raise ValueError("--bars 文件必须包含 symbol 和 trade_date 列")
    keys: set[tuple[str, str]] = set()
    details: dict[tuple[str, str], dict[str, object]] = {}
    for _, row in frame.iterrows():
        key = (str(row["trade_date"])[:10], _canonical_vendor_symbol(row["symbol"]))
        keys.add(key)
        details[key] = row.to_dict()
    return keys, details


def create_baostock_universe_snapshot(
    session: BaoStockSession,
    start_date: str,
    end_date: str,
    root: str | Path,
    snapshot_id: str,
    batch_size: int | None = None,
    resume: bool = False,
    bars_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a daily securities-list snapshot, keeping every date request resumable."""
    start, end = _parse_date(start_date), _parse_date(end_date)
    if start > end:
        raise ValueError("start_date 必须不晚于 end_date")
    if not snapshot_id or not re.fullmatch(r"[A-Za-z0-9._-]+", snapshot_id) or ".." in snapshot_id:
        raise ValueError("snapshot_id 只能包含字母、数字、点、下划线和连字符")
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size 必须大于 0")

    parent = Path(root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / snapshot_id
    work = parent / f".{snapshot_id}.partial"
    if destination.exists():
        raise FileExistsError(f"快照已完成，拒绝覆盖：{destination}")
    if resume and not work.exists():
        raise FileNotFoundError(f"找不到可续跑任务：{work}")
    if not resume and work.exists():
        raise FileExistsError(f"发现未完成任务；请使用 --resume 续跑：{work}")
    if not work.exists():
        work.mkdir()

    raw_dir = work / "raw"
    daily_dir = raw_dir / "daily_securities"
    daily_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = work / "checkpoint.json"
    request_log_path = raw_dir / "request_log.jsonl"
    request_spec = {"start_date": start, "end_date": end, "snapshot_id": snapshot_id}
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("request") != request_spec:
            raise ValueError("续跑参数与原任务不一致")
    else:
        checkpoint = {"request": request_spec, "completed_dates": [], "requests": []}
    legacy_requests = checkpoint.pop("requests", [])
    if legacy_requests and not request_log_path.exists():
        with request_log_path.open("w", encoding="utf-8") as handle:
            for request in legacy_requests:
                handle.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
        checkpoint["request_count"] = len(legacy_requests)
        _atomic_json(checkpoint, checkpoint_path)

    calendar_path = work / "trading_calendar.csv"
    basics_path = raw_dir / "stock_basic_all_types.csv"
    if not calendar_path.exists():
        raw_calendar = session.trade_calendar(start, end)
        if raw_calendar.empty:
            raise RuntimeError("BaoStock 未返回请求区间的交易日历")
        retrieved = session.request_log[-1]["completed_at_utc"]
        _atomic_csv(raw_calendar, raw_dir / "trade_calendar.csv")
        _atomic_csv(normalize_baostock_calendar(raw_calendar, retrieved), calendar_path)
    if not basics_path.exists():
        _atomic_csv(session.stock_basic(), basics_path)

    _flush_baostock_requests(session, request_log_path, checkpoint, checkpoint_path)

    calendar = pd.read_csv(calendar_path, dtype=str)
    if "is_open" not in calendar or "trade_date" not in calendar:
        raise RuntimeError("缓存交易日历字段不完整")
    open_dates = sorted(
        pd.to_datetime(calendar.loc[calendar["is_open"].astype(str).eq("True"), "trade_date"], errors="coerce")
        .dropna().dt.strftime("%Y-%m-%d").tolist()
    )
    if not open_dates:
        raise RuntimeError("请求区间没有开市日")
    basics = pd.read_csv(basics_path, dtype=str, keep_default_na=False)
    required_basic = {"code", "code_name", "ipoDate", "outDate", "type", "status"}
    if not required_basic.issubset(basics.columns):
        raise RuntimeError("BaoStock query_stock_basic 返回字段变化")
    basic_rows: dict[str, pd.Series] = {
        _canonical_vendor_symbol(row["code"]): row
        for _, row in basics.iterrows()
    }

    completed = set(checkpoint.get("completed_dates", []))
    pending = [day for day in open_dates if day not in completed]
    selected_dates = pending if batch_size is None else pending[:batch_size]
    for day in selected_dates:
        raw = session.all_stocks(day)
        required_day = {"code", "tradeStatus", "code_name"}
        if not required_day.issubset(raw.columns):
            raise RuntimeError(f"BaoStock query_all_stock({day}) 返回字段变化：{list(raw.columns)}")
        _atomic_csv(raw, daily_dir / f"{day}.csv")
        completed.add(day)
        checkpoint["completed_dates"] = sorted(completed)
        _flush_baostock_requests(session, request_log_path, checkpoint, checkpoint_path)

    # Persist metadata requests too, including when the first date batch is empty.
    if session.request_log:
        checkpoint["requests"] = list(checkpoint.get("requests", [])) + list(session.request_log)
        checkpoint["provider_package_version"] = session.package_version
        checkpoint["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _atomic_json(checkpoint, checkpoint_path)

    remaining = [day for day in open_dates if day not in completed]
    if remaining:
        return {
            "state": "partial",
            "snapshot_id": snapshot_id,
            "partial_path": str(work),
            "completed_dates": len(completed),
            "total_dates": len(open_dates),
            "next_dates": remaining[:5],
            "resume_command": f"cehuan universe-snapshot --start {start} --end {end} --snapshot-id {snapshot_id} --resume",
        }

    request_log = [json.loads(line) for line in request_log_path.read_text(encoding="utf-8").splitlines() if line]
    basic_retrieved = next(
        (entry["completed_at_utc"] for entry in request_log if entry["endpoint"] == "query_stock_basic"),
        None,
    )
    if basic_retrieved is None:
        # The basic table can be created on an earlier invocation and no longer
        # have its request log in the active session; its content hash still pins it.
        basic_retrieved = "recorded-in-checkpoint-request-log"
    bars_keys, bars_details = _load_optional_bars(bars_path)
    membership_temp = work / ".securities_by_date.csv.tmp"
    reconciliation_temp = work / ".reconciliation.csv.tmp"
    membership_temp.unlink(missing_ok=True)
    reconciliation_temp.unlink(missing_ok=True)
    membership_rows_total = 0
    unknown_type_rows_total = 0
    non_stock_rows_total = 0
    stock_rows_total = 0
    st_unknown_rows_total = 0
    daily_status_unknown_rows_total = 0
    daily_summary: list[dict[str, object]] = []
    membership_retrieved_at = {
        entry["endpoint"][len("query_all_stock("):-1]: entry["completed_at_utc"]
        for entry in request_log
        if entry.get("endpoint", "").startswith("query_all_stock(") and entry["endpoint"].endswith(")")
    }
    for day in open_dates:
        raw = pd.read_csv(daily_dir / f"{day}.csv", dtype=str, keep_default_na=False)
        raw_codes = set(raw["code"].map(_canonical_vendor_symbol))
        daily_memberships: list[dict[str, object]] = []
        daily_reconciliation: list[dict[str, object]] = []
        type_counts: dict[str, int] = {}
        stock_trade_counts: dict[str, int] = {}
        selected_stock_count = 0
        unknown_type_count = 0
        non_stock_count = 0
        st_known_count = 0
        quoted_stock_count = 0
        delisted_by_now_count = 0
        delisted_quoted_count = 0
        interval_conflict_count = 0
        for _, item in raw.iterrows():
            code = _canonical_vendor_symbol(item["code"])
            basic = basic_rows.get(code)
            security_type = _as_text(basic.get("type")) if basic is not None else None
            type_key = security_type or "UNKNOWN"
            type_counts[type_key] = type_counts.get(type_key, 0) + 1
            status = _as_text(item.get("tradeStatus"))
            is_stock = security_type == "1"
            if is_stock:
                selected_stock_count += 1
                status_key = status or "UNKNOWN"
                stock_trade_counts[status_key] = stock_trade_counts.get(status_key, 0) + 1
            elif security_type is None:
                unknown_type_count += 1
            else:
                non_stock_count += 1
            interval_match = _date_is_in_basic_interval(day, basic) if basic is not None else None
            if is_stock and interval_match is False:
                interval_conflict_count += 1
            bar = bars_details.get((day, code), {})
            has_quote = (day, code) in bars_keys
            if is_stock and has_quote:
                quoted_stock_count += 1
            is_st = bar.get("is_st") if has_quote else None
            if is_st is not None and not pd.isna(is_st):
                st_known_count += 1
                st_status = "FROM_CANONICAL_DAILY_BAR"
            else:
                is_st = pd.NA
                st_status = "UNKNOWN_NOT_RETURNED_BY_DAILY_SECURITIES_LIST"
            if is_stock and basic is not None and _as_text(basic.get("status")) == "0" and _as_text(basic.get("outDate")):
                if str(basic["outDate"]) >= day:
                    delisted_by_now_count += 1
                    delisted_quoted_count += int(has_quote)

            if not is_stock:
                pool_status = "UNKNOWN_TYPE" if security_type is None else "FILTERED_NON_STOCK"
                eligibility = "NOT_EQUITY_OR_UNKNOWN_TYPE"
            elif interval_match is False:
                pool_status = "STOCK_MEMBER_INTERVAL_CONFLICT"
                eligibility = "UNKNOWN_LISTING_INTERVAL_CONFLICT"
            else:
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

            daily_memberships.append({
                "trade_date": day,
                "symbol": code,
                "name_on_date_from_daily_list": _as_text(item.get("code_name")),
                "type_from_latest_basic": security_type,
                "basic_name_at_retrieval": _as_text(basic.get("code_name")) if basic is not None else None,
                "basic_ipo_date": _as_text(basic.get("ipoDate")) if basic is not None else None,
                "basic_out_date": _as_text(basic.get("outDate")) if basic is not None else None,
                "basic_status_at_retrieval": _as_text(basic.get("status")) if basic is not None else None,
                "basic_interval_matches_date": interval_match,
                "member_of_date_specific_list": True,
                "daily_trade_status": status,
                "daily_trade_status_meaning": "TRADING" if status == "1" else "SUSPENDED" if status == "0" else "UNKNOWN",
                "is_st": is_st,
                "st_status_source": st_status,
                "has_canonical_daily_bar": has_quote if bars_path is not None else pd.NA,
                "pool_status": pool_status,
                "eligibility_status": eligibility,
                "membership_source": "BaoStock.query_all_stock(day)",
                "membership_retrieved_at_utc": membership_retrieved_at.get(day),
                "basic_source": "BaoStock.query_stock_basic (retrieved metadata; not daily ST/board state)",
                "basic_retrieved_at_utc": basic_retrieved,
            })

        for code, basic in basic_rows.items():
            if str(basic.get("type")) != "1":
                continue
            interval_match = _date_is_in_basic_interval(day, basic)
            if interval_match is True and code not in raw_codes:
                daily_reconciliation.append({
                    "trade_date": day,
                    "symbol": code,
                    "code_name": _as_text(basic.get("code_name")),
                    "ipoDate": _as_text(basic.get("ipoDate")),
                    "outDate": _as_text(basic.get("outDate")),
                    "basic_status_at_retrieval": _as_text(basic.get("status")),
                    "daily_list_status": "ABSENT",
                    "discrepancy": "BASIC_INTERVAL_SAYS_LISTED_BUT_NOT_IN_DATE_LIST",
                    "interpretation": "UNRESOLVED; could reflect vendor list scope, date/status semantics, or metadata mismatch",
                })

        daily_membership_frame = pd.DataFrame(daily_memberships)
        daily_membership_frame.to_csv(
            membership_temp,
            mode="a",
            header=not membership_temp.exists(),
            index=False,
            encoding="utf-8",
        )
        if daily_reconciliation:
            pd.DataFrame(daily_reconciliation).to_csv(
                reconciliation_temp,
                mode="a",
                header=not reconciliation_temp.exists(),
                index=False,
                encoding="utf-8",
            )
        membership_rows_total += len(daily_membership_frame)
        unknown_type_rows_total += unknown_type_count
        non_stock_rows_total += non_stock_count
        stock_rows_total += selected_stock_count
        st_unknown_rows_total += selected_stock_count - st_known_count
        daily_status_unknown_rows_total += stock_trade_counts.get("UNKNOWN", 0)
        daily_summary.append({
            "trade_date": day,
            "date_list_total_rows": len(raw),
            "type_counts_from_basic": json.dumps(type_counts, ensure_ascii=False, sort_keys=True),
            "stock_type_1_count": selected_stock_count,
            "non_stock_count": non_stock_count,
            "unknown_type_count": unknown_type_count,
            "daily_trade_status_counts_stock_type_1": json.dumps(stock_trade_counts, ensure_ascii=False, sort_keys=True),
            "stock_interval_conflict_count": interval_conflict_count,
            "basic_interval_listed_but_absent_count": len(daily_reconciliation),
            "st_known_count": st_known_count,
            "st_unknown_count": selected_stock_count - st_known_count,
            "stock_with_price_rows": quoted_stock_count if bars_path is not None else pd.NA,
            "stock_price_coverage": quoted_stock_count / selected_stock_count if bars_path is not None and selected_stock_count else pd.NA,
            "eventually_delisted_member_count": delisted_by_now_count,
            "eventually_delisted_with_price_count": delisted_quoted_count if bars_path is not None else pd.NA,
            "eventually_delisted_price_coverage": delisted_quoted_count / delisted_by_now_count if bars_path is not None and delisted_by_now_count else pd.NA,
            "bar_coverage_source": str(Path(bars_path).expanduser()) if bars_path is not None else None,
        })

    os.replace(membership_temp, work / "securities_by_date.csv")
    reconciliation_count = sum(int(row["basic_interval_listed_but_absent_count"]) for row in daily_summary)
    if reconciliation_temp.exists():
        os.replace(reconciliation_temp, work / "reconciliation.csv")
    else:
        _atomic_csv(pd.DataFrame(columns=[
            "trade_date", "symbol", "code_name", "ipoDate", "outDate",
            "basic_status_at_retrieval", "daily_list_status", "discrepancy", "interpretation",
        ]), work / "reconciliation.csv")
    summary_frame = pd.DataFrame(daily_summary)
    _atomic_csv(summary_frame, work / "daily_coverage.csv")
    files = [p for p in sorted(work.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "checkpoint.json"}]
    hashes = {str(p.relative_to(work)): _sha256(p) for p in files}
    manifest = {
        "snapshot_id": snapshot_id,
        "provider": "BaoStock",
        "provider_package_version": checkpoint.get("provider_package_version"),
        "date_range": {"start": start, "end": end},
        "open_date_count": len(open_dates),
        "membership_source": "query_all_stock(day), one call per open trade date",
        "securities_type_source": "query_stock_basic() retrieved at snapshot time; type=1 is stock, 2=index, 3=other, 4=convertible bond, 5=ETF",
        "point_in_time_semantics": {
            "membership": "date-specific query_all_stock response",
            "daily_trade_status": "tradeStatus returned by the date-specific list; 0=suspended, 1=trading",
            "st_status": "not present in query_all_stock; unknown unless a matching canonical daily bar was supplied",
            "ipo_out_dates": "query_stock_basic metadata retrieved at snapshot time; retained separately from date-list membership",
            "name": "historical list name and retrieved basic name are stored separately",
        },
        "coverage": {
            "daily_list_rows_total": membership_rows_total,
            "type_1_stock_rows": stock_rows_total,
            "non_stock_rows_filtered": non_stock_rows_total,
            "unknown_type_rows": unknown_type_rows_total,
            "basic_interval_listed_but_absent_rows": reconciliation_count,
            "daily_status_unknown_rows": daily_status_unknown_rows_total,
            "st_unknown_rows": st_unknown_rows_total,
            "canonical_bar_coverage_supplied": bars_path is not None,
            "daily_coverage_file": "daily_coverage.csv",
        },
        "requests": request_log,
        "files_sha256": hashes,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "warnings": [
            "A daily-list member with unknown type or ST state is not confirmed eligible for trading.",
            "The securities list includes non-equity instruments; use pool_status=STOCK_MEMBER for the equity membership set.",
            "Basic IPO/outDate data is recorded separately and does not override date-specific list membership.",
            "This snapshot does not supply historical price-limit bands, board rules, lot-size rules, or corporate-action cash/share accounting.",
        ],
    }
    _atomic_json(manifest, work / "manifest.json")
    os.replace(work, destination)
    return {"state": "complete", "snapshot_path": str(destination), **manifest}


def filter_bars_to_point_in_time_universe(
    bars: pd.DataFrame,
    membership: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Keep only daily bars whose code is a confirmed type-1 daily-list member."""
    required = {"trade_date", "symbol", "pool_status", "daily_trade_status"}
    missing = required.difference(membership.columns)
    if missing:
        raise ValueError("股票池文件缺少字段：" + ", ".join(sorted(missing)))
    normalized_bars = normalize_bar_frame(bars)
    quotes = normalized_bars.copy()
    quotes["trade_date_key"] = pd.to_datetime(quotes["trade_date"]).dt.strftime("%Y-%m-%d")
    quotes["symbol_key"] = quotes["symbol"].map(canonical_symbol)
    pool = membership.copy()
    pool["trade_date_key"] = pd.to_datetime(pool["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    pool["symbol_key"] = pool["symbol"].map(canonical_symbol)
    pool = pool.loc[pool["pool_status"].astype(str).eq("STOCK_MEMBER")].copy()
    if pool.duplicated(["trade_date_key", "symbol_key"]).any():
        raise ValueError("每日股票池存在重复 trade_date/symbol")
    if quotes.duplicated(["trade_date_key", "symbol_key"]).any():
        raise ValueError("行情存在重复 trade_date/symbol")

    joined = pool.merge(
        quotes,
        on=["trade_date_key", "symbol_key"],
        how="left",
        suffixes=("_universe", "_bar"),
        indicator=True,
        validate="one_to_one",
    )
    joined["has_bar"] = joined["_merge"].eq("both")
    date_keys = set(pool["trade_date_key"].dropna())
    in_period_quotes = quotes.loc[quotes["trade_date_key"].isin(date_keys)]
    no_bar_columns = [
        column for column in (
            "trade_date_key", "symbol_key", "name_on_date_from_daily_list", "daily_trade_status",
        ) if column in joined.columns
    ]
    no_bar = joined.loc[~joined["has_bar"], no_bar_columns].copy()
    if not no_bar.empty:
        no_bar["discrepancy"] = "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR"
    daily_status = joined["daily_trade_status"].astype("string")
    bar_suspended = joined.get("is_suspended")
    known_status = joined["has_bar"] & daily_status.isin(["0", "1"])
    known_bar_suspension = bar_suspended.notna() if bar_suspended is not None else pd.Series(False, index=joined.index)
    comparable = known_status & known_bar_suspension
    expected_suspended = daily_status.eq("0")
    actual_suspended = bar_suspended.fillna(False).astype(bool) if bar_suspended is not None else pd.Series(False, index=joined.index)
    mismatch = comparable & expected_suspended.ne(actual_suspended)
    status_discrepancies = joined.loc[mismatch, [
        "trade_date_key", "symbol_key", "daily_trade_status", "is_suspended",
    ]].copy()
    if not status_discrepancies.empty:
        status_discrepancies["discrepancy"] = "DAILY_LIST_VS_DAILY_KLINE_TRADE_STATUS_MISMATCH"
    discrepancies = pd.concat([no_bar, status_discrepancies], ignore_index=True, sort=False)
    overlapping_columns = set(pool.columns).intersection(quotes.columns) - {"trade_date_key", "symbol_key"}
    bar_columns_after_merge = {
        column: f"{column}_bar" if column in overlapping_columns else column
        for column in BAR_COLUMNS
    }
    filtered = joined.loc[joined["has_bar"], list(bar_columns_after_merge.values())].rename(
        columns={value: key for key, value in bar_columns_after_merge.items()}
    ).copy()
    filtered = normalize_bar_frame(filtered)

    eventual_delisted = (
        joined.get("basic_status_at_retrieval", pd.Series(index=joined.index, dtype="string")).astype("string").eq("0")
        & joined.get("basic_out_date", pd.Series(index=joined.index, dtype="string")).astype("string").ne("")
    )
    summary = {
        "daily_stock_member_rows": int(len(pool)),
        "members_with_bars": int(joined["has_bar"].sum()),
        "members_without_bars": int((~joined["has_bar"]).sum()),
        "price_coverage_fraction": float(joined["has_bar"].mean()) if len(joined) else None,
        "st_known_member_rows": int(joined.loc[joined["has_bar"], bar_columns_after_merge["is_st"]].notna().sum()),
        "st_unknown_member_rows": int((~joined["has_bar"] | joined[bar_columns_after_merge["is_st"]].isna()).sum()),
        "daily_trade_status_comparable_rows": int(comparable.sum()),
        "daily_trade_status_mismatch_rows": int(mismatch.sum()),
        "eventually_delisted_member_rows": int(eventual_delisted.sum()),
        "eventually_delisted_member_rows_with_bars": int((eventual_delisted & joined["has_bar"]).sum()),
        "eventually_delisted_price_coverage_fraction": (
            float((eventual_delisted & joined["has_bar"]).sum() / eventual_delisted.sum())
            if eventual_delisted.sum() else None
        ),
        "quote_rows_outside_confirmed_daily_stock_pool": int(
            (~in_period_quotes.set_index(["trade_date_key", "symbol_key"]).index.isin(
                pool.set_index(["trade_date_key", "symbol_key"]).index
            )).sum()
        ),
    }
    return filtered, summary, discrepancies


def _iter_csv_groups(path: Path, key_column: str, usecols: list[str], chunksize: int = 50_000):
    """Yield adjacent date groups from a sorted CSV with bounded memory."""
    carry: pd.DataFrame | None = None
    previous_key: str | None = None
    for chunk in pd.read_csv(
        path,
        usecols=usecols,
        dtype={column: str for column in usecols},
        keep_default_na=False,
        chunksize=chunksize,
    ):
        if carry is not None:
            chunk = pd.concat((carry, chunk), ignore_index=True)
            carry = None
        if chunk.empty:
            continue
        keys = chunk[key_column].astype(str)
        last_key = str(keys.iloc[-1])
        complete = chunk.loc[keys.ne(last_key)]
        for key, group in complete.groupby(key_column, sort=False):
            current_key = str(key)
            if previous_key is not None and current_key < previous_key:
                raise ValueError(f"{path.name} 必须按 {key_column} 升序排列")
            previous_key = current_key
            yield current_key, group
        carry = chunk.loc[keys.eq(last_key)].copy()
    if carry is not None and not carry.empty:
        for key, group in carry.groupby(key_column, sort=False):
            current_key = str(key)
            if previous_key is not None and current_key < previous_key:
                raise ValueError(f"{path.name} 必须按 {key_column} 升序排列")
            yield current_key, group


def audit_baostock_universe_prices(
    universe_dir: str | Path,
    bars_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Compare a date-specific universe to bars using bounded-memory CSV groups."""
    universe_root = Path(universe_dir).expanduser().resolve()
    membership_path = universe_root / "securities_by_date.csv"
    manifest_path = universe_root / "manifest.json"
    if not membership_path.exists() or not manifest_path.exists():
        raise FileNotFoundError("--universe 需要指向包含 manifest.json 和 securities_by_date.csv 的快照目录")
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    bars_path = Path(bars_path).expanduser().resolve()
    header = set(pd.read_csv(bars_path, nrows=0).columns)
    bar_columns = [column for column in BAR_COLUMNS if column in header]
    required = {"trade_date", "symbol", "open", "high", "low", "close", "volume_shares"}
    if not required.issubset(bar_columns):
        raise ValueError("规范日线文件缺少必需字段：" + ", ".join(sorted(required - set(bar_columns))))
    member_columns = [
        "trade_date", "symbol", "pool_status", "daily_trade_status",
        "basic_status_at_retrieval", "basic_out_date",
    ]
    membership_groups = iter(_iter_csv_groups(membership_path, "trade_date", member_columns))
    quote_groups = iter(_iter_csv_groups(bars_path, "trade_date", bar_columns))
    current_members = next(membership_groups, None)
    current_quotes = next(quote_groups, None)
    coverage_rows: list[dict[str, Any]] = []
    member_total = bar_total = missing_total = 0
    st_known_total = status_comparable_total = status_mismatch_total = 0
    delisted_total = delisted_with_bars_total = quote_outside_total = 0
    discrepancy_count = 0
    filtered_temp = output / ".point_in_time_bars.csv.tmp"
    discrepancy_temp = output / ".universe_price_discrepancies.csv.tmp"
    filtered_temp.unlink(missing_ok=True)
    discrepancy_temp.unlink(missing_ok=True)
    while current_members is not None or current_quotes is not None:
        member_day = current_members[0] if current_members is not None else None
        quote_day = current_quotes[0] if current_quotes is not None else None
        day = min(value for value in (member_day, quote_day) if value is not None)
        member_frame = current_members[1] if member_day == day else pd.DataFrame(columns=member_columns)
        quote_frame = current_quotes[1] if quote_day == day else pd.DataFrame(columns=bar_columns)
        if member_day == day:
            current_members = next(membership_groups, None)
        if quote_day == day:
            current_quotes = next(quote_groups, None)

        stock_members = member_frame.loc[member_frame["pool_status"].astype(str).eq("STOCK_MEMBER")].copy()
        stock_members["symbol_key"] = stock_members["symbol"].map(canonical_symbol)
        if stock_members["symbol_key"].duplicated().any():
            raise ValueError(f"{day} 的每日股票池存在重复代码")
        quote_frame = quote_frame.copy()
        if not quote_frame.empty:
            quote_frame["symbol"] = quote_frame["symbol"].map(canonical_symbol)
            if quote_frame["symbol"].duplicated().any():
                raise ValueError(f"{day} 的行情存在重复证券代码")
        members_by_symbol = stock_members.set_index("symbol_key", drop=False)
        quotes_by_symbol = quote_frame.set_index("symbol", drop=False) if not quote_frame.empty else quote_frame
        member_symbols = set(members_by_symbol.index.astype(str))
        quote_symbols = set(quotes_by_symbol.index.astype(str)) if not quote_frame.empty else set()
        matching_symbols = member_symbols & quote_symbols
        missing_symbols = sorted(member_symbols - quote_symbols)
        extra_symbols = sorted(quote_symbols - member_symbols)
        discrepancies: list[dict[str, object]] = []
        for symbol in missing_symbols:
            discrepancies.append({
                "trade_date": day,
                "symbol": symbol,
                "daily_trade_status": members_by_symbol.loc[symbol, "daily_trade_status"],
                "discrepancy": "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR",
            })
        if matching_symbols:
            ordered_matches = sorted(matching_symbols)
            matched_members = members_by_symbol.loc[ordered_matches]
            matched_quotes = quotes_by_symbol.loc[ordered_matches]
            st_values = parse_optional_bool(matched_quotes["is_st"]) if "is_st" in matched_quotes else pd.Series(pd.NA, index=matched_quotes.index, dtype="boolean")
            st_known = int(st_values.notna().sum())
            daily_status = matched_members["daily_trade_status"].astype(str)
            suspended = parse_optional_bool(matched_quotes["is_suspended"]) if "is_suspended" in matched_quotes else pd.Series(pd.NA, index=matched_quotes.index, dtype="boolean")
            comparable = daily_status.isin(["0", "1"]) & suspended.notna()
            mismatch = comparable & daily_status.eq("0").ne(suspended.fillna(False).astype(bool))
            status_comparable = int(comparable.sum())
            status_mismatch = int(mismatch.sum())
            for symbol, quote_status in zip(matched_quotes.index[mismatch], suspended.loc[mismatch]):
                discrepancies.append({
                    "trade_date": day,
                    "symbol": str(symbol),
                    "daily_trade_status": daily_status.loc[symbol],
                    "is_suspended": quote_status,
                    "discrepancy": "DAILY_LIST_VS_DAILY_KLINE_TRADE_STATUS_MISMATCH",
                })
        else:
            st_known = status_comparable = status_mismatch = 0
        if discrepancies:
            pd.DataFrame(discrepancies).to_csv(
                discrepancy_temp,
                mode="a",
                header=not discrepancy_temp.exists(),
                index=False,
                encoding="utf-8",
            )
            discrepancy_count += len(discrepancies)
        if matching_symbols:
            filtered = quote_frame.loc[quote_frame["symbol"].isin(matching_symbols)].copy()
            filtered = normalize_bar_frame(filtered)
            filtered.to_csv(
                filtered_temp,
                mode="a",
                header=not filtered_temp.exists(),
                index=False,
                encoding="utf-8",
            )

        if member_day == day:
            delisted_mask = (
                stock_members["basic_status_at_retrieval"].astype(str).eq("0")
                & stock_members["basic_out_date"].astype(str).ne("")
                & stock_members["basic_out_date"].astype(str).ge(day)
            )
            eventually_delisted = int(delisted_mask.sum())
            delisted_symbols = set(stock_members.loc[delisted_mask, "symbol_key"].astype(str))
            delisted_with_bars = len(delisted_symbols & quote_symbols)
            coverage_rows.append({
                "trade_date": day,
                "stock_members": len(stock_members),
                "members_with_bars": len(matching_symbols),
                "members_without_bars": len(missing_symbols),
                "price_coverage_fraction": len(matching_symbols) / len(stock_members) if len(stock_members) else pd.NA,
                "eventually_delisted_members": eventually_delisted,
                "eventually_delisted_members_with_bars": delisted_with_bars,
                "eventually_delisted_price_coverage_fraction": delisted_with_bars / eventually_delisted if eventually_delisted else pd.NA,
                "st_status_known_members_with_bars": st_known,
                "st_status_unknown_members": len(stock_members) - st_known,
            })
            member_total += len(stock_members)
            bar_total += len(matching_symbols)
            missing_total += len(missing_symbols)
            st_known_total += st_known
            status_comparable_total += status_comparable
            status_mismatch_total += status_mismatch
            delisted_total += eventually_delisted
            delisted_with_bars_total += delisted_with_bars
        quote_outside_total += len(extra_symbols)

    if not filtered_temp.exists():
        pd.DataFrame(columns=BAR_COLUMNS).to_csv(filtered_temp, index=False, encoding="utf-8")
    if not discrepancy_temp.exists():
        pd.DataFrame(columns=["trade_date", "symbol", "daily_trade_status", "is_suspended", "discrepancy"]).to_csv(
            discrepancy_temp, index=False, encoding="utf-8",
        )
    os.replace(filtered_temp, output / "point_in_time_bars.csv")
    os.replace(discrepancy_temp, output / "universe_price_discrepancies.csv")
    coverage = pd.DataFrame(coverage_rows)
    _atomic_csv(coverage, output / "daily_price_coverage.csv")
    universe_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_quality_path = bars_path.parent / "quality.json"
    source_quality = json.loads(source_quality_path.read_text(encoding="utf-8")) if source_quality_path.exists() else None
    filtered_path = output / "point_in_time_bars.csv"
    filtered_hash = _sha256(filtered_path)
    prevalidated_warnings = list(source_quality.get("warnings", [])) if source_quality else []
    if missing_total:
        prevalidated_warnings.append(f"点时股票池有 {missing_total} 个股票日缺少日线")
    if status_mismatch_total:
        prevalidated_warnings.append(f"日期名单与日线停牌状态有 {status_mismatch_total} 行不一致")
    prevalidated_quality = {
        "healthy": bool(source_quality and source_quality.get("healthy")),
        "errors": source_quality.get("errors", []) if source_quality else [
            "来源快照没有可验证的 quality.json；不能用于紧凑内存预验证回测",
        ],
        "warnings": prevalidated_warnings,
        "source_quality_file": str(source_quality_path) if source_quality else None,
        "source_bars_sha256": _sha256(bars_path),
        "data_sha256": filtered_hash,
        "result_type": "prevalidated_source_snapshot_after_streaming_point_in_time_audit",
        "universe_snapshot_id": universe_manifest.get("snapshot_id"),
        "point_in_time_rows": bar_total,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(prevalidated_quality, output / "point_in_time_quality.json")
    result = {
        "universe_snapshot_id": universe_manifest.get("snapshot_id"),
        "universe_date_range": universe_manifest.get("date_range"),
        "bars_source": str(Path(bars_path).expanduser().resolve()),
        "daily_coverage_rows": int(len(coverage)),
        "daily_price_coverage_min": float(coverage["price_coverage_fraction"].min()) if len(coverage) else None,
        "daily_price_coverage_max": float(coverage["price_coverage_fraction"].max()) if len(coverage) else None,
        "discrepancy_rows": discrepancy_count,
        "daily_stock_member_rows": member_total,
        "members_with_bars": bar_total,
        "members_without_bars": missing_total,
        "price_coverage_fraction": bar_total / member_total if member_total else None,
        "st_known_member_rows": st_known_total,
        "st_unknown_member_rows": member_total - st_known_total,
        "daily_trade_status_comparable_rows": status_comparable_total,
        "daily_trade_status_mismatch_rows": status_mismatch_total,
        "eventually_delisted_member_rows": delisted_total,
        "eventually_delisted_member_rows_with_bars": delisted_with_bars_total,
        "eventually_delisted_price_coverage_fraction": delisted_with_bars_total / delisted_total if delisted_total else None,
        "quote_rows_outside_confirmed_daily_stock_pool": quote_outside_total,
        "daily_coverage_file": str(output / "daily_price_coverage.csv"),
        "discrepancy_file": str(output / "universe_price_discrepancies.csv"),
        "prevalidated_quality_file": str(output / "point_in_time_quality.json"),
    }
    _atomic_json(result, output / "audit.json")
    return result
