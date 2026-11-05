from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.canonical import normalize_bar_frame


H2_DECISION_TIME_LOCAL = "09:31:00"
H2_TIMEZONE = "Asia/Shanghai"
H1_LOOKBACK_SESSIONS = 20


def _day(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return ""
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()
        except ValueError:
            return ""
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def _flag(value: object) -> bool | None:
    if pd.isna(value) or value == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "否"}:
        return False
    return None


def _number_present(value: object) -> bool:
    if pd.isna(value):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(parsed)


def _positive_price_valid(value: object) -> bool:
    if not _number_present(value):
        return False
    return float(value) > 0


def _next_open_sessions(calendar: pd.DataFrame, signal_day: str, count: int = 2) -> list[str]:
    sessions = normalize_trading_calendar(calendar)
    open_days = [row.isoformat() for row in sessions.loc[sessions.is_open, "trade_date"]]
    try:
        index = open_days.index(signal_day)
    except ValueError as exc:
        raise ValueError(f"信号日期 {signal_day} 不在已知开市日中") from exc
    result = open_days[index + 1:index + count + 1]
    if len(result) != count:
        raise ValueError(f"交易日历没有覆盖 {signal_day} 后的 {count} 个开市日")
    calendar_days = {row.isoformat() for row in sessions.trade_date}
    expected_calendar_days = set(pd.date_range(signal_day, result[-1], freq="D").strftime("%Y-%m-%d"))
    missing_calendar_days = sorted(expected_calendar_days - calendar_days)
    if missing_calendar_days:
        raise ValueError("H2 后续交易日历自然日覆盖不完整: " + ", ".join(missing_calendar_days))
    return result


def _window_dates(calendar: pd.DataFrame, signal_day: str, count: int) -> list[str]:
    sessions = normalize_trading_calendar(calendar)
    open_days = [row.isoformat() for row in sessions.loc[sessions.is_open, "trade_date"] if row.isoformat() <= signal_day]
    if not open_days or open_days[-1] != signal_day:
        raise ValueError(f"H1 信号日不是已知开市日：{signal_day}")
    selected = open_days[-count:]
    if selected:
        calendar_days = {row.isoformat() for row in sessions.trade_date}
        expected_calendar_days = set(pd.date_range(selected[0], signal_day, freq="D").strftime("%Y-%m-%d"))
        missing_calendar_days = sorted(expected_calendar_days - calendar_days)
        if missing_calendar_days:
            raise ValueError("H1 窗口交易日历自然日覆盖不完整: " + ", ".join(missing_calendar_days))
    return selected


def _availability(raw: object) -> pd.Timestamp | None:
    if pd.isna(raw) or str(raw).strip() == "":
        return None
    parsed = pd.to_datetime(raw, errors="coerce", utc=True)
    return None if pd.isna(parsed) else parsed


def compute_hypothesis_input_coverage(
    bars: pd.DataFrame,
    universe_members: pd.DataFrame,
    trading_calendar: pd.DataFrame,
    candidate_pool: pd.DataFrame,
    *,
    config: dict[str, Any] | None = None,
    created_at_utc: str | None = None,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Summarize H1/H2 input availability without computing strategy returns.

    H1 is measured both over each signal-date stock-member universe and over
    the original ranked candidate rows. H2 checks the original candidate pool
    against decision/execution session bars and distinguishes recorded adapter
    timestamps from evidence of historical source availability.
    """
    normalized_bars = normalize_bar_frame(bars, compact=False)
    normalized_calendar = normalize_trading_calendar(trading_calendar)
    members = universe_members.copy()
    candidates = candidate_pool.copy()
    for frame, label, required in (
        (members, "universe_members", {"trade_date", "symbol", "pool_status", "type_from_latest_basic"}),
        (candidates, "candidate_pool", {"signal_date", "symbol", "rank"}),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{label} 缺少字段: {', '.join(sorted(missing))}")
    normalized_bars["trade_date"] = normalized_bars.trade_date.map(_day)
    normalized_bars["symbol"] = normalized_bars.symbol.astype(str)
    members["trade_date"] = members.trade_date.map(_day)
    members["symbol"] = members.symbol.astype(str)
    candidates["signal_date"] = candidates.signal_date.map(_day)
    candidates["symbol"] = candidates.symbol.astype(str)
    candidates["rank"] = pd.to_numeric(candidates["rank"], errors="coerce")
    if candidates["signal_date"].eq("").any() or candidates["rank"].isna().any():
        raise ValueError("候选池含无效 signal_date 或 rank")
    if members["trade_date"].eq("").any():
        raise ValueError("按日股票池含无效 trade_date")

    signal_days = sorted(candidates.signal_date.unique())
    if not signal_days:
        raise ValueError("候选池没有信号日")
    calendar_days = [row.isoformat() for row in normalized_calendar.trade_date]
    calendar_open = {
        row.trade_date.isoformat(): bool(row.is_open)
        for row in normalized_calendar.itertuples(index=False)
    }
    bar_rows_by_symbol: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for row in normalized_bars[["symbol", "trade_date", "is_suspended", "close", "adjusted_close"]].itertuples(index=False, name=None):
        bar_rows_by_symbol[str(row[0])].append((str(row[1]), row[2], row[3], row[4]))
    h1_rows: list[dict[str, Any]] = []
    h1_summary: list[dict[str, Any]] = []
    candidate_symbol_sets: dict[str, set[str]] = {}
    for signal_day in signal_days:
        expected = _window_dates(normalized_calendar, signal_day, H1_LOOKBACK_SESSIONS + 1)
        expected_set = set(expected)
        stock_rows = members.loc[
            members.trade_date.eq(signal_day) & members.pool_status.eq("STOCK_MEMBER")
        ]
        unknown_type_rows = members.loc[
            members.trade_date.eq(signal_day)
            & (members.pool_status.eq("UNKNOWN_TYPE") | members.type_from_latest_basic.isna())
        ]
        signal_candidates = candidates.loc[candidates.signal_date.eq(signal_day)].copy()
        candidate_symbols = set(signal_candidates.symbol.astype(str))
        candidate_symbol_sets[signal_day] = candidate_symbols
        scopes: list[tuple[str, list[str], dict[str, dict[str, Any]]]] = [
            ("FULL_STOCK_MEMBER_POOL", sorted(set(stock_rows.symbol.astype(str))), {}),
            ("ORIGINAL_CANDIDATE_POOL", sorted(candidate_symbols), {
                str(row.symbol): row._asdict() for row in signal_candidates.itertuples(index=False)
            }),
        ]
        for scope, symbols, candidate_info in scopes:
            scope_rows: list[dict[str, Any]] = []
            for symbol in symbols:
                symbol_window = [row for row in bar_rows_by_symbol.get(symbol, ()) if row[0] in expected_set]
                counts = Counter(row[0] for row in symbol_window)
                observed_dates = set(counts)
                missing_dates = sorted(expected_set - observed_dates)
                duplicate_dates = sorted(day for day, count in counts.items() if count > 1)
                suspension = [_flag(row[1]) for row in symbol_window]
                unknown_suspension = sum(value is None for value in suspension)
                suspended = sum(value is True for value in suspension)
                invalid_close = sum(not _positive_price_valid(row[2]) for row in symbol_window)
                invalid_adjusted_close = sum(not _positive_price_valid(row[3]) for row in symbol_window)
                reasons: list[str] = []
                if len(expected) < H1_LOOKBACK_SESSIONS + 1:
                    reasons.append("INSUFFICIENT_CALENDAR_HISTORY")
                if missing_dates:
                    reasons.append("MISSING_BAR_SESSIONS")
                if duplicate_dates:
                    reasons.append("DUPLICATE_BAR_SESSIONS")
                if unknown_suspension:
                    reasons.append("SUSPENSION_STATUS_UNKNOWN")
                if suspended:
                    reasons.append("SUSPENDED_IN_WINDOW")
                if invalid_close:
                    reasons.append("INVALID_CLOSE_IN_WINDOW")
                if invalid_adjusted_close:
                    reasons.append("INVALID_ADJUSTED_CLOSE_IN_WINDOW")
                extra: dict[str, Any] = {}
                if scope == "FULL_STOCK_MEMBER_POOL":
                    member_row = stock_rows.loc[stock_rows.symbol.astype(str).eq(symbol)]
                    extra["membership_rows"] = int(len(member_row))
                    extra["membership_source"] = ";".join(sorted(set(member_row.get("membership_source", pd.Series(dtype=str)).astype(str))))
                    extra["type_from_latest_basic"] = ";".join(sorted(set(member_row.type_from_latest_basic.astype(str))))
                else:
                    info = candidate_info.get(symbol, {})
                    extra.update({
                        "rank": int(info.get("rank", 0)) if pd.notna(info.get("rank")) else None,
                        "momentum": info.get("momentum"),
                        "selected_target": info.get("selected_target"),
                    })
                row = {
                    "scope": scope,
                    "signal_date": signal_day,
                    "symbol": symbol,
                    "expected_open_sessions": len(expected),
                    "expected_session_dates": ";".join(expected),
                    "observed_bar_rows": int(len(symbol_window)),
                    "observed_unique_sessions": len(observed_dates),
                    "missing_session_count": len(missing_dates),
                    "missing_session_dates": ";".join(missing_dates),
                    "duplicate_session_count": len(duplicate_dates),
                    "duplicate_session_dates": ";".join(duplicate_dates),
                    "unknown_suspension_rows": unknown_suspension,
                    "suspended_rows": suspended,
                    "invalid_close_rows": invalid_close,
                    "invalid_adjusted_close_rows": invalid_adjusted_close,
                    "h1_status": "ELIGIBLE_FOR_H1_DIAGNOSTIC" if not reasons else "EXCLUDED_OR_UNVERIFIED",
                    "reasons": ";".join(reasons),
                    **extra,
                }
                h1_rows.append(row)
                scope_rows.append(row)
            reason_counts = Counter(
                reason for row in scope_rows for reason in str(row["reasons"]).split(";") if reason
            )
            h1_summary.append({
                "signal_date": signal_day,
                "scope": scope,
                "universe_or_candidate_symbols": len(symbols),
                "fully_covered_valid_price_non_suspended_windows": sum(row["h1_status"] == "ELIGIBLE_FOR_H1_DIAGNOSTIC" for row in scope_rows),
                "excluded_or_unverified_windows": sum(row["h1_status"] != "ELIGIBLE_FOR_H1_DIAGNOSTIC" for row in scope_rows),
                "reason_counts": dict(sorted(reason_counts.items())),
                "unknown_type_member_rows_excluded_from_stock_pool": int(len(unknown_type_rows)) if scope == "FULL_STOCK_MEMBER_POOL" else None,
                "candidate_rank_rows": int(len(signal_candidates)) if scope == "ORIGINAL_CANDIDATE_POOL" else None,
                "candidate_symbols_unique": bool(signal_candidates["symbol"].is_unique) if scope == "ORIGINAL_CANDIDATE_POOL" else None,
                "candidate_rank_unique": bool(signal_candidates["rank"].is_unique) if scope == "ORIGINAL_CANDIDATE_POOL" else None,
            })

    h2_rows: list[dict[str, Any]] = []
    h2_summary: list[dict[str, Any]] = []
    availability_cache: dict[str, pd.Timestamp | None] = {}
    h2_needed_dates: set[str] = set()
    for signal_day in signal_days:
        h2_needed_dates.update(_next_open_sessions(normalized_calendar, signal_day, 2))
    h2_bars = normalized_bars.loc[normalized_bars.trade_date.isin(h2_needed_dates)]
    bars_by_key = {
        (str(row["trade_date"]), str(row["symbol"])): row
        for row in h2_bars.to_dict(orient="records")
    }
    for signal_day in signal_days:
        decision_day, execution_day = _next_open_sessions(normalized_calendar, signal_day, 2)
        decision_cutoff = pd.Timestamp(f"{decision_day} {H2_DECISION_TIME_LOCAL}", tz=H2_TIMEZONE).tz_convert("UTC")
        execution_cutoff = pd.Timestamp(f"{execution_day} {H2_DECISION_TIME_LOCAL}", tz=H2_TIMEZONE).tz_convert("UTC")
        signal_candidates = candidates.loc[candidates.signal_date.eq(signal_day)].sort_values("rank")
        for candidate in signal_candidates.itertuples(index=False):
            detail: dict[str, Any] = {
                "signal_date": signal_day,
                "decision_session_date": decision_day,
                "execution_session_date": execution_day,
                "symbol": str(candidate.symbol),
                "rank": int(candidate.rank),
                "momentum": getattr(candidate, "momentum", None),
            }
            for stage, day, cutoff in (("decision", decision_day, decision_cutoff), ("execution", execution_day, execution_cutoff)):
                bar = bars_by_key.get((day, str(candidate.symbol)))
                if bar is None:
                    detail.update({
                        f"{stage}_bar_present": False,
                        f"{stage}_open_price_present": False,
                        f"{stage}_recorded_available_at": None,
                        f"{stage}_recorded_available_by_0931": False,
                        f"{stage}_price_source": None,
                        f"{stage}_status_recorded": False,
                        f"{stage}_status_available_at_separate": False,
                        f"{stage}_can_buy_input_present": False,
                        f"{stage}_market_rule_complete": False,
                    })
                    continue
                raw_availability = str(bar.get("available_at", "")).strip()
                if raw_availability not in availability_cache:
                    availability_cache[raw_availability] = _availability(raw_availability)
                availability = availability_cache[raw_availability]
                detail.update({
                    f"{stage}_bar_present": True,
                    f"{stage}_open_price_present": _number_present(bar.get("open")) and float(bar.get("open")) > 0,
                    f"{stage}_recorded_available_at": None if availability is None else availability.isoformat(),
                    f"{stage}_recorded_available_by_0931": availability is not None and availability <= cutoff,
                    f"{stage}_price_source": str(bar.get("data_source")) if pd.notna(bar.get("data_source")) else None,
                    f"{stage}_status_recorded": all(_flag(bar.get(field)) is not None for field in ("is_listed", "is_suspended", "is_st")),
                    f"{stage}_status_available_at_separate": False,
                    f"{stage}_can_buy_input_present": _flag(bar.get("can_buy")) is not None,
                    f"{stage}_market_rule_complete": all(_number_present(bar.get(field)) for field in ("buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares", "price_limit_pct")) and pd.notna(bar.get("market_rule_source")),
                    f"{stage}_is_listed": _flag(bar.get("is_listed")),
                    f"{stage}_is_suspended": _flag(bar.get("is_suspended")),
                    f"{stage}_is_st": _flag(bar.get("is_st")),
                })
            h2_rows.append(detail)
        group_rows = [row for row in h2_rows if row["signal_date"] == signal_day]
        h2_summary.append({
            "signal_date": signal_day,
            "candidate_rows": len(group_rows),
            "candidate_symbols_unique": bool(signal_candidates["symbol"].is_unique),
            "candidate_ranks_unique": bool(signal_candidates["rank"].is_unique),
            "decision_bar_rows": sum(bool(row.get("decision_bar_present")) for row in group_rows),
            "decision_open_rows": sum(bool(row.get("decision_open_price_present")) for row in group_rows),
            "decision_rows_recorded_available_by_0931": sum(bool(row.get("decision_recorded_available_by_0931")) for row in group_rows),
            "decision_status_rows_recorded": sum(bool(row.get("decision_status_recorded")) for row in group_rows),
            "execution_bar_rows": sum(bool(row.get("execution_bar_present")) for row in group_rows),
            "execution_open_rows": sum(bool(row.get("execution_open_price_present")) for row in group_rows),
            "execution_rows_recorded_available_by_0931": sum(bool(row.get("execution_recorded_available_by_0931")) for row in group_rows),
            "execution_status_rows_recorded": sum(bool(row.get("execution_status_recorded")) for row in group_rows),
            "rows_with_separate_status_availability_time": 0,
            "decision_rows_with_explicit_can_buy": sum(bool(row.get("decision_can_buy_input_present")) for row in group_rows),
            "execution_rows_with_explicit_can_buy": sum(bool(row.get("execution_can_buy_input_present")) for row in group_rows),
            "decision_market_rule_complete_rows": sum(bool(row.get("decision_market_rule_complete")) for row in group_rows),
            "execution_market_rule_complete_rows": sum(bool(row.get("execution_market_rule_complete")) for row in group_rows),
            "decision_date": decision_day,
            "execution_date": execution_day,
            "decision_time_assumption_local": f"{decision_day} {H2_DECISION_TIME_LOCAL} {H2_TIMEZONE}",
            "execution_time_assumption_local": f"{execution_day} {H2_DECISION_TIME_LOCAL} {H2_TIMEZONE}",
        })

    bar_count = len(normalized_bars)
    rule_numeric = normalized_bars[[
        "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares", "price_limit_pct",
    ]].notna().all(axis=1)
    rule_complete = rule_numeric & normalized_bars["market_rule_source"].notna() & normalized_bars["market_rule_source"].astype(str).ne("")
    source_times = normalized_bars["available_at"].dropna().astype(str)
    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "READ_ONLY_HYPOTHESIS_INPUT_COVERAGE_PREFLIGHT_NO_PERFORMANCE_RESULTS",
        "created_at_utc": created_at_utc or datetime.now(timezone.utc).isoformat(),
        "scope": {
            "bar_date_range": [min(normalized_bars.trade_date), max(normalized_bars.trade_date)],
            "bar_rows": bar_count,
            "bar_symbols": int(normalized_bars.symbol.nunique()),
            "signal_dates": signal_days,
            "calendar_open_sessions": int(normalized_calendar.is_open.sum()),
            "data_scope": "Existing 68-open-session Shanghai/Shenzhen development snapshot; not an independent holdout and not a full-A-share completeness claim.",
        },
        "h1": {
            "lookback_sessions": H1_LOOKBACK_SESSIONS,
            "required_open_sessions_including_signal_day": H1_LOOKBACK_SESSIONS + 1,
            "summary_by_signal_and_scope": h1_summary,
            "detail_file": "h1-window-coverage.csv",
        },
        "h2": {
            "summary_by_signal": h2_summary,
            "detail_file": "h2-candidate-input-coverage.csv",
            "decision_and_execution_time_assumption": "09:31 Asia/Shanghai; this is the prototype's requested timing, not evidence that historical provider data were available then.",
            "ranking_availability_status": "UNVERIFIED_RECONSTRUCTED_AFTER_RUN",
            "quote_availability_status": "ADAPTER_TIMESTAMP_ONLY_NOT_VENDOR_PUBLICATION_EVIDENCE",
            "status_availability_status": "UNVERIFIED_NO_SEPARATE_STATUS_TIMESTAMP_OR_SOURCE_BINDING",
        },
        "rules_fees_and_status": {
            "market_rule_complete_bar_rows": int(rule_complete.sum()),
            "market_rule_missing_or_incomplete_bar_rows": int(bar_count - rule_complete.sum()),
            "historical_fee_schedule": "MISSING",
            "config_fallback_available_but_unverified": True,
            "config_fallback_values": (config or {}).get("costs", {}),
            "market_fallback_values": (config or {}).get("market", {}),
            "bar_status_fields_recorded_rows": int(normalized_bars[["is_listed", "is_suspended", "is_st"]].notna().all(axis=1).sum()),
            "bar_status_point_in_time_and_separate_availability": "UNVERIFIED",
            "adapter_available_at_min": None if source_times.empty else source_times.min(),
            "adapter_available_at_max": None if source_times.empty else source_times.max(),
            "adapter_available_at_semantics": "Adapter timestamp assumption; not historical provider publication evidence.",
        },
        "interpretation": {
            "strategy_returns_computed": False,
            "strategy_comparison_performance_claim": False,
            "active_strategy_modified": False,
            "broker_connected": False,
            "orders_generated": False,
            "formal_baseline": "NOT_READY",
            "can_continue": False,
        },
    }
    return report, pd.DataFrame(h1_rows), pd.DataFrame(h2_rows)
