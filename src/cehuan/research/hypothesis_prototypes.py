from __future__ import annotations

from collections.abc import Mapping
from math import floor, isfinite
from typing import Any

import pandas as pd

from cehuan.data.canonical import parse_optional_bool
from cehuan.data.calendar import normalize_trading_calendar


def _date_text(value: object) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(parsed) else parsed.strftime("%Y-%m-%d")


def _aware_utc(value: object, *, field: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须带时区")
    return parsed.tz_convert("UTC")


def _optional_flag(value: object) -> bool | None:
    parsed = parse_optional_bool(pd.Series([value])).iloc[0]
    return None if pd.isna(parsed) else bool(parsed)


def _positive_number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def evaluate_h1_window(
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    symbol: str,
    signal_date: str,
    lookback_sessions: int = 20,
) -> dict[str, Any]:
    """Evaluate the H1 research filter using exactly lookback+1 open sessions.

    The function is read-only and only uses bars and calendar rows dated on or
    before the signal date. It does not replace the active signal pipeline.
    """
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    signal_day = _date_text(signal_date)
    if not signal_day:
        raise ValueError("signal_date 无效")
    required_rows = lookback_sessions + 1

    calendar_rows = calendar.copy()
    if not {"trade_date", "is_open"}.issubset(calendar_rows.columns):
        raise ValueError("calendar 必须包含 trade_date 和 is_open")
    calendar_rows["trade_date"] = calendar_rows["trade_date"].map(_date_text)
    calendar_rows = calendar_rows.loc[
        calendar_rows.trade_date.ne("") & calendar_rows.trade_date.le(signal_day)
    ].copy()
    calendar_states = parse_optional_bool(calendar_rows["is_open"])
    calendar_rows["_is_open"] = calendar_states
    open_dates = sorted(calendar_rows.loc[calendar_rows._is_open.eq(True).fillna(False), "trade_date"].unique())
    expected_dates = open_dates[-required_rows:]
    reasons: list[str] = []
    if signal_day not in set(open_dates):
        reasons.append("SIGNAL_DATE_NOT_OPEN_SESSION")
    if len(expected_dates) < required_rows:
        reasons.append("INSUFFICIENT_HISTORY")
    if expected_dates:
        first_expected = expected_dates[0]
        unknown_calendar_dates = sorted(calendar_rows.loc[
            calendar_rows.trade_date.ge(first_expected) & calendar_rows["_is_open"].isna(), "trade_date"
        ].unique())
        if unknown_calendar_dates:
            reasons.append("CALENDAR_STATUS_UNKNOWN")
        window_calendar_rows = calendar_rows.loc[
            calendar_rows.trade_date.ge(first_expected) & calendar_rows.trade_date.le(signal_day)
        ]
        calendar_counts = window_calendar_rows["trade_date"].value_counts()
        duplicate_calendar_dates = sorted(day for day, count in calendar_counts.items() if int(count) > 1)
        if duplicate_calendar_dates:
            reasons.append("DUPLICATE_CALENDAR_SESSION")
    else:
        unknown_calendar_dates = []
        duplicate_calendar_dates = []

    symbol_rows = bars.loc[bars["symbol"].astype(str).eq(str(symbol))].copy()
    if not {"trade_date", "is_suspended"}.issubset(symbol_rows.columns):
        raise ValueError("bars 必须包含 trade_date 和 is_suspended")
    symbol_rows["trade_date"] = symbol_rows["trade_date"].map(_date_text)
    symbol_rows = symbol_rows.loc[
        symbol_rows.trade_date.ne("") & symbol_rows.trade_date.le(signal_day)
    ].copy()
    if expected_dates:
        window_rows = symbol_rows.loc[symbol_rows.trade_date.isin(expected_dates)].copy()
        off_calendar_dates = sorted(set(
            symbol_rows.loc[
                symbol_rows.trade_date.ge(expected_dates[0]) & symbol_rows.trade_date.le(signal_day), "trade_date"
            ]
        ) - set(open_dates))
    else:
        window_rows = symbol_rows.iloc[0:0].copy()
        off_calendar_dates = []
    row_counts = window_rows.trade_date.value_counts() if not window_rows.empty else pd.Series(dtype=int)
    duplicate_bar_dates = sorted(day for day, count in row_counts.items() if int(count) > 1)
    observed_dates = set(window_rows.trade_date.unique())
    missing_dates = [day for day in expected_dates if day not in observed_dates]
    leading_history_gap = bool(missing_dates) and missing_dates == expected_dates[: len(missing_dates)]
    if len(expected_dates) < required_rows or leading_history_gap:
        reasons.append("INSUFFICIENT_HISTORY")
    if missing_dates:
        reasons.append("MISSING_SESSION")
    if duplicate_bar_dates:
        reasons.append("DUPLICATE_SESSION")
    if off_calendar_dates:
        reasons.append("OFF_CALENDAR_BAR")

    suspension = parse_optional_bool(window_rows["is_suspended"]) if not window_rows.empty else pd.Series(dtype="boolean")
    if suspension.isna().any():
        reasons.append("SUSPENSION_UNKNOWN")
    if suspension.eq(True).fillna(False).any():
        reasons.append("SUSPENDED_IN_WINDOW")
    reasons = list(dict.fromkeys(reasons))
    return {
        "symbol": str(symbol),
        "signal_date": signal_day,
        "lookback_sessions": int(lookback_sessions),
        "required_open_session_count": required_rows,
        "expected_open_session_dates": expected_dates,
        "observed_bar_row_count": int(len(window_rows)),
        "observed_bar_dates": window_rows.trade_date.astype(str).tolist(),
        "missing_session_dates": missing_dates,
        "duplicate_bar_dates": duplicate_bar_dates,
        "duplicate_calendar_dates": duplicate_calendar_dates,
        "unknown_calendar_dates": unknown_calendar_dates,
        "off_calendar_bar_dates": off_calendar_dates,
        "suspended_rows": int(suspension.eq(True).fillna(False).sum()),
        "not_suspended_rows": int(suspension.eq(False).fillna(False).sum()),
        "unknown_suspension_rows": int(suspension.isna().sum()),
        "reasons": reasons,
        "status": "ELIGIBLE" if not reasons else "EXCLUDED",
    }


def compare_h1_candidate_pool(
    baseline_candidates: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    lookback_sessions: int = 20,
) -> pd.DataFrame:
    """Keep baseline candidates unchanged and recompute only H1 pool/ranks."""
    required = {"symbol", "signal_date", "momentum", "rank"}
    if not required.issubset(baseline_candidates.columns):
        raise ValueError("baseline_candidates 缺少 symbol/signal_date/momentum/rank")
    rows: list[dict[str, Any]] = []
    for candidate in baseline_candidates.to_dict(orient="records"):
        audit = evaluate_h1_window(
            bars, calendar, symbol=str(candidate["symbol"]),
            signal_date=str(candidate["signal_date"]), lookback_sessions=lookback_sessions,
        )
        rows.append({
            **candidate,
            "baseline_rank": int(candidate["rank"]),
            "h1_status": audit["status"],
            "h1_reasons": ";".join(audit["reasons"]),
            "h1_expected_sessions": audit["required_open_session_count"],
            "h1_observed_rows": audit["observed_bar_row_count"],
            "h1_missing_dates": ";".join(audit["missing_session_dates"]),
            "h1_duplicate_dates": ";".join(audit["duplicate_bar_dates"]),
            "h1_suspended_rows": audit["suspended_rows"],
            "h1_unknown_suspension_rows": audit["unknown_suspension_rows"],
        })
    result = pd.DataFrame(rows)
    result["h1_rank"] = pd.NA
    for _, indices in result.groupby("signal_date", sort=False).groups.items():
        eligible = result.loc[list(indices)].loc[lambda frame: frame.h1_status.eq("ELIGIBLE")]
        eligible = eligible.sort_values(["momentum", "symbol"], ascending=[False, True], kind="stable")
        result.loc[eligible.index, "h1_rank"] = range(1, len(eligible) + 1)
    return result


def _round_lot(shares: int, minimum: int, step: int) -> int:
    if minimum < 1 or step < 1:
        raise ValueError("minimum 和 step 必须为正整数")
    if shares < minimum:
        return 0
    return minimum + ((shares - minimum) // step) * step


def _quote_lookup(frame: pd.DataFrame, *, trade_date: str, field: str) -> dict[str, dict[str, Any]]:
    required = {
        "symbol", "trade_date", "open_price_cny", "price_available_at_utc", "price_source",
        "status_available_at_utc", "status_source", "is_suspended", "is_listed", "can_buy",
    }
    if not required.issubset(frame.columns):
        raise ValueError(f"{field} quotes 缺少字段：{', '.join(sorted(required - set(frame.columns)))}")
    data = frame.copy()
    data["trade_date"] = data["trade_date"].map(_date_text)
    data = data.loc[data.trade_date.eq(trade_date)].copy()
    duplicates = data.symbol.astype(str).duplicated(keep=False)
    if duplicates.any():
        symbols = sorted(data.loc[duplicates, "symbol"].astype(str).unique())
        raise ValueError(f"{field} quotes 存在重复证券日：{symbols}")
    return {str(row["symbol"]): row for row in data.to_dict(orient="records")}


def _quote_block_reason(quote: Mapping[str, Any], *, decision_at: pd.Timestamp) -> str | None:
    price_available = _aware_utc(quote["price_available_at_utc"], field="quote.price_available_at_utc")
    status_available = _aware_utc(quote["status_available_at_utc"], field="quote.status_available_at_utc")
    if price_available > decision_at:
        return "PRICE_NOT_AVAILABLE_AT_DECISION"
    if price_available.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") != _date_text(quote.get("trade_date")):
        return "QUOTE_AVAILABILITY_DATE_MISMATCH"
    if status_available > decision_at:
        return "SECURITY_STATUS_NOT_AVAILABLE_AT_DECISION"
    if not str(quote.get("price_source", "")).strip():
        return "PRICE_SOURCE_UNKNOWN"
    if not str(quote.get("status_source", "")).strip():
        return "SECURITY_STATUS_SOURCE_UNKNOWN"
    listed = _optional_flag(quote["is_listed"])
    if listed is None:
        return "LISTING_STATUS_UNKNOWN"
    if not listed:
        return "NOT_LISTED_AT_DECISION"
    suspended = _optional_flag(quote["is_suspended"])
    if suspended is None:
        return "SUSPENSION_STATUS_UNKNOWN"
    if suspended:
        return "SUSPENDED_AT_DECISION"
    can_buy = _optional_flag(quote["can_buy"])
    if can_buy is None:
        return "BUYABILITY_UNKNOWN"
    if not can_buy:
        return "NOT_BUYABLE_AT_DECISION"
    if _positive_number(quote["open_price_cny"]) is None:
        return "INVALID_DECISION_OPEN_PRICE"
    return None


def _execution_block_reason(
    quote: Mapping[str, Any], *, decision_at: pd.Timestamp, execution_at: pd.Timestamp,
) -> str | None:
    price_available = _aware_utc(quote["price_available_at_utc"], field="execution_quote.price_available_at_utc")
    status_available = _aware_utc(quote["status_available_at_utc"], field="execution_quote.status_available_at_utc")
    if price_available <= decision_at:
        return "EXECUTION_QUOTE_NOT_FUTURE_RELATIVE_TO_DECISION"
    if price_available > execution_at:
        return "EXECUTION_PRICE_NOT_AVAILABLE_AT_EXECUTION"
    if price_available.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") != _date_text(quote.get("trade_date")):
        return "EXECUTION_QUOTE_AVAILABILITY_DATE_MISMATCH"
    if status_available > execution_at:
        return "EXECUTION_SECURITY_STATUS_NOT_AVAILABLE"
    if not str(quote.get("price_source", "")).strip():
        return "EXECUTION_PRICE_SOURCE_UNKNOWN"
    if not str(quote.get("status_source", "")).strip():
        return "EXECUTION_SECURITY_STATUS_SOURCE_UNKNOWN"
    listed = _optional_flag(quote["is_listed"])
    if listed is None:
        return "EXECUTION_LISTING_STATUS_UNKNOWN"
    if not listed:
        return "NOT_LISTED_AT_EXECUTION"
    suspended = _optional_flag(quote["is_suspended"])
    if suspended is None:
        return "EXECUTION_SUSPENSION_STATUS_UNKNOWN"
    if suspended:
        return "SUSPENDED_AT_EXECUTION"
    can_buy = _optional_flag(quote["can_buy"])
    if can_buy is None:
        return "EXECUTION_BUYABILITY_UNKNOWN"
    if not can_buy:
        return "NOT_BUYABLE_AT_EXECUTION"
    if _positive_number(quote["open_price_cny"]) is None:
        return "INVALID_EXECUTION_OPEN_PRICE"
    return None


def validate_h2_session_sequence(
    trading_calendar: pd.DataFrame,
    *,
    signal_date: str,
    decision_session_date: str,
    execution_session_date: str,
) -> list[str]:
    """Require three consecutive open sessions using the canonical calendar."""
    signal_day = _date_text(signal_date)
    decision_day = _date_text(decision_session_date)
    execution_day = _date_text(execution_session_date)
    if not signal_day or not decision_day or not execution_day:
        raise ValueError("H2 信号日、决策日和执行日必须是有效日期")
    if not signal_day < decision_day < execution_day:
        raise ValueError("H2 日期必须满足 signal_date < decision_session_date < execution_session_date")
    try:
        normalized = normalize_trading_calendar(trading_calendar)
    except ValueError as exc:
        raise ValueError(f"H2 交易日历无效或状态不明确：{exc}") from exc
    dates = normalized["trade_date"].map(lambda value: value.isoformat())
    is_open_by_date = dict(zip(dates, normalized["is_open"].astype(bool)))
    required_days = pd.date_range(signal_day, execution_day, freq="D").strftime("%Y-%m-%d").tolist()
    missing = [day for day in required_days if day not in is_open_by_date]
    if missing:
        raise ValueError("H2 交易日历日覆盖不完整：" + ", ".join(missing))
    if not is_open_by_date[signal_day]:
        raise ValueError("signal_date 必须是已知开市日")
    if not is_open_by_date[decision_day]:
        raise ValueError("decision_session_date 必须是已知开市日")
    if not is_open_by_date[execution_day]:
        raise ValueError("execution_session_date 必须是已知开市日")
    open_days = [day for day in required_days if is_open_by_date[day]]
    if len(open_days) < 3:
        raise ValueError("交易日历未覆盖信号、决策和执行所需的三个开市日")
    if decision_day != open_days[1]:
        raise ValueError(f"decision_session_date 必须是信号后的下一开市日：expected={open_days[1]}")
    if execution_day != open_days[2]:
        raise ValueError(f"execution_session_date 必须是决策后的下一开市日：expected={open_days[2]}")
    return open_days[:3]


def build_h2_open_based_replacement_plan(
    ranked_candidates: pd.DataFrame,
    decision_open_quotes: pd.DataFrame,
    next_session_open_quotes: pd.DataFrame,
    trading_calendar: pd.DataFrame,
    *,
    signal_date: str,
    ranking_available_at_utc: str,
    decision_session_date: str,
    execution_session_date: str,
    decision_at_utc: str,
    execution_at_utc: str,
    max_positions: int,
    target_amount_per_slot_cny: float,
    portfolio_risk_budget_cny: float,
    reserved_risk_cny: float,
    available_cash_cny: float,
    buy_min_shares: int,
    buy_step_shares: int,
    commission_rate: float,
    minimum_commission_cny: float,
    existing_positions: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Prototype H2 with an explicit delayed execution boundary.

    The trading calendar proves that signal, decision, and execution sessions
    are consecutive open sessions. Price and status availability are checked
    separately. The execution open is an explicit synthetic fill assumption,
    not evidence that a real order could fill at that price.
    """
    if max_positions < 1:
        raise ValueError("max_positions 必须大于 0")
    for name, value in {
        "target_amount_per_slot_cny": target_amount_per_slot_cny,
        "portfolio_risk_budget_cny": portfolio_risk_budget_cny,
        "reserved_risk_cny": reserved_risk_cny,
        "available_cash_cny": available_cash_cny,
        "commission_rate": commission_rate,
        "minimum_commission_cny": minimum_commission_cny,
    }.items():
        if not isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"{name} 必须为有限非负数")
    if target_amount_per_slot_cny <= 0 or portfolio_risk_budget_cny <= 0:
        raise ValueError("目标金额和总风险预算必须为正数")
    if target_amount_per_slot_cny * max_positions > portfolio_risk_budget_cny + 1e-9:
        raise ValueError("max_positions × 单槽目标金额超过组合风险预算")
    if reserved_risk_cny > portfolio_risk_budget_cny + 1e-9:
        raise ValueError("已有目标占用的风险预算超过组合上限")
    decision_at = _aware_utc(decision_at_utc, field="decision_at_utc")
    execution_at = _aware_utc(execution_at_utc, field="execution_at_utc")
    ranking_available_at = _aware_utc(ranking_available_at_utc, field="ranking_available_at_utc")
    signal_day = _date_text(signal_date)
    decision_day = _date_text(decision_session_date)
    execution_day = _date_text(execution_session_date)
    open_days = validate_h2_session_sequence(
        trading_calendar,
        signal_date=signal_day,
        decision_session_date=decision_day,
        execution_session_date=execution_day,
    )
    if ranking_available_at.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") != signal_day:
        raise ValueError("候选排名必须在 signal_date 当日可得，不能由后续数据生成")
    signal_close_assumption = pd.Timestamp(f"{signal_day} 15:00:00", tz="Asia/Shanghai").tz_convert("UTC")
    if ranking_available_at < signal_close_assumption:
        raise ValueError("日线收盘信号的候选排名不能早于 signal_date 15:00 Asia/Shanghai")
    if ranking_available_at > decision_at:
        raise ValueError("候选排名在替补决策时点尚不可得")
    if decision_at.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") != decision_day:
        raise ValueError("decision_at_utc 必须落在 decision_session_date")
    if execution_at.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d") != execution_day:
        raise ValueError("execution_at_utc 必须落在 execution_session_date")
    if execution_at <= decision_at:
        raise ValueError("替补执行时点必须晚于开盘观察后的决策时点")
    required = {"symbol", "rank", "momentum"}
    if not required.issubset(ranked_candidates.columns):
        raise ValueError("ranked_candidates 缺少 symbol/rank/momentum")
    candidates = ranked_candidates.copy()
    candidates["symbol"] = candidates.symbol.astype(str)
    candidates["rank"] = pd.to_numeric(candidates["rank"], errors="raise").astype(int)
    if candidates.symbol.duplicated().any() or candidates["rank"].duplicated().any():
        raise ValueError("ranked_candidates 的证券代码和排名必须唯一")
    candidates = candidates.sort_values("rank", kind="stable")
    decision_quotes = _quote_lookup(decision_open_quotes, trade_date=_date_text(decision_session_date), field="decision")
    execution_quotes = _quote_lookup(next_session_open_quotes, trade_date=_date_text(execution_session_date), field="execution")
    held = {str(symbol): int(shares) for symbol, shares in (existing_positions or {}).items()}
    primary = candidates.head(max_positions)
    initial_zero_slots: list[dict[str, Any]] = []
    primary_reviews: list[dict[str, Any]] = []
    for row in primary.to_dict(orient="records"):
        symbol = str(row["symbol"])
        quote = decision_quotes.get(symbol)
        reason = "DECISION_OPEN_QUOTE_MISSING" if quote is None else _quote_block_reason(quote, decision_at=decision_at)
        if reason:
            primary_reviews.append({
                "symbol": symbol, "rank": int(row["rank"]), "status": "NOT_EVALUATED_FOR_ZERO", "reason": reason,
                "decision_price_available_at_utc": quote.get("price_available_at_utc") if quote is not None else None,
                "decision_status_available_at_utc": quote.get("status_available_at_utc") if quote is not None else None,
            })
            continue
        price = float(quote["open_price_cny"])
        target_shares = _round_lot(floor(target_amount_per_slot_cny / price), buy_min_shares, buy_step_shares)
        if target_shares > 0:
            primary_reviews.append({
                "symbol": symbol, "rank": int(row["rank"]), "status": "PRIMARY_TARGET_HAS_VALID_QUANTITY",
                "planned_shares": target_shares,
                "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
                "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
            })
            continue
        if held.get(symbol, 0) > 0:
            primary_reviews.append({
                "symbol": symbol, "rank": int(row["rank"]),
                "status": "EXISTING_POSITION_ZERO_TARGET_REBALANCE_OUT_OF_SCOPE",
                "held_shares": held[symbol],
                "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
                "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
            })
            continue
        initial_zero_slots.append({"primary_symbol": symbol, "primary_rank": int(row["rank"])})
        primary_reviews.append({
            "symbol": symbol, "rank": int(row["rank"]), "status": "ZERO_TARGET_REPLACEMENT_SLOT",
            "reference_price_cny": price,
            "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
            "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
        })

    candidate_evaluations: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    used_symbols = set(candidates.head(max_positions).symbol.astype(str)) | {symbol for symbol, shares in held.items() if shares > 0}
    estimated_cash_remaining = float(available_cash_cny)
    estimated_risk_remaining = max(0.0, portfolio_risk_budget_cny - reserved_risk_cny)
    for slot_index, slot in enumerate(initial_zero_slots, start=1):
        chosen: dict[str, Any] | None = None
        for row in candidates.loc[candidates["rank"].gt(max_positions)].to_dict(orient="records"):
            symbol = str(row["symbol"])
            if symbol in used_symbols:
                candidate_evaluations.append({"replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]), "status": "SKIPPED_ALREADY_SELECTED_OR_HELD"})
                continue
            quote = decision_quotes.get(symbol)
            if quote is None:
                candidate_evaluations.append({"replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]), "status": "SKIPPED_DECISION_OPEN_QUOTE_MISSING"})
                continue
            block_reason = _quote_block_reason(quote, decision_at=decision_at)
            if block_reason:
                candidate_evaluations.append({
                    "replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]),
                    "status": "SKIPPED", "reason": block_reason,
                    "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
                    "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
                    "decision_price_source": str(quote["price_source"]),
                    "decision_status_source": str(quote["status_source"]),
                })
                continue
            reference_price = float(quote["open_price_cny"])
            quantity = _round_lot(floor(target_amount_per_slot_cny / reference_price), buy_min_shares, buy_step_shares)
            if quantity == 0:
                candidate_evaluations.append({
                    "replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]),
                    "status": "SKIPPED_ZERO_AFTER_LOT_AT_DECISION_OPEN",
                    "reference_price_cny": reference_price,
                })
                continue
            estimated_notional = quantity * reference_price
            estimated_fee = max(minimum_commission_cny, estimated_notional * commission_rate)
            if (
                estimated_notional > target_amount_per_slot_cny + 1e-9
                or estimated_notional > estimated_risk_remaining + 1e-9
                or estimated_notional + estimated_fee > estimated_cash_remaining + 1e-9
            ):
                candidate_evaluations.append({
                    "replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]),
                    "status": "SKIPPED_DECISION_ESTIMATE_BUDGET_OR_CASH",
                    "estimated_cost_cny": estimated_notional + estimated_fee,
                })
                continue
            chosen = {
                "replacement_slot": slot_index,
                "replaces_primary_symbol": slot["primary_symbol"],
                "symbol": symbol,
                "rank": int(row["rank"]),
                "planned_shares": quantity,
                "decision_reference_price_cny": reference_price,
                "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
                "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
                "decision_price_source": str(quote["price_source"]),
                "decision_status_source": str(quote["status_source"]),
                "ranking_available_at_utc": ranking_available_at.isoformat(),
                "decision_at_utc": decision_at.isoformat(),
                "order_status": "SCHEDULED_SIMULATED_ORDER",
                "fill_assumption": "NEXT_SESSION_OPEN_PRICE_ASSUMPTION",
                "real_market_fill_proven": False,
                "execution_session_date": _date_text(execution_session_date),
                "execution_at_utc": execution_at.isoformat(),
                "same_decision_session_open_fill_allowed": False,
                "selection_uses_execution_quote": False,
            }
            candidate_evaluations.append({
                "replacement_slot": slot_index, "symbol": symbol, "rank": int(row["rank"]),
                "status": "SELECTED_FOR_NEXT_SESSION_OPEN", "planned_shares": quantity,
                "decision_price_available_at_utc": str(quote["price_available_at_utc"]),
                "decision_status_available_at_utc": str(quote["status_available_at_utc"]),
            })
            used_symbols.add(symbol)
            estimated_cash_remaining -= estimated_notional + estimated_fee
            estimated_risk_remaining -= estimated_notional
            break
        if chosen is None:
            orders.append({
                "replacement_slot": slot_index,
                "replaces_primary_symbol": slot["primary_symbol"],
                "order_status": "NO_REPLACEMENT_CANDIDATE_REMAINS_CASH",
                "same_decision_session_open_fill_allowed": False,
                "selection_uses_execution_quote": False,
            })
        else:
            orders.append(chosen)

    remaining_risk = max(0.0, portfolio_risk_budget_cny - reserved_risk_cny)
    remaining_cash = float(available_cash_cny)
    execution_results: list[dict[str, Any]] = []
    for order in orders:
        if order.get("order_status") != "SCHEDULED_SIMULATED_ORDER":
            continue
        symbol = str(order["symbol"])
        quote = execution_quotes.get(symbol)
        reason = "NEXT_SESSION_OPEN_QUOTE_MISSING" if quote is None else _execution_block_reason(
            quote, decision_at=decision_at, execution_at=execution_at,
        )
        if reason:
            execution_results.append({
                "symbol": symbol, "status": "BLOCKED", "reason": reason, "filled_shares": 0,
                "execution_price_available_at_utc": str(quote["price_available_at_utc"]) if quote is not None else None,
                "execution_status_available_at_utc": str(quote["status_available_at_utc"]) if quote is not None else None,
                "execution_price_source": str(quote["price_source"]) if quote is not None else None,
                "execution_status_source": str(quote["status_source"]) if quote is not None else None,
                "fill_assumption": "NEXT_SESSION_OPEN_PRICE_ASSUMPTION",
                "real_market_fill_proven": False,
            })
            continue
        price = float(quote["open_price_cny"])
        quantity = int(order["planned_shares"])
        notional = price * quantity
        commission = max(float(minimum_commission_cny), notional * float(commission_rate))
        total_cost = notional + commission
        if notional > remaining_risk + 1e-9:
            execution_results.append({"symbol": symbol, "status": "BLOCKED", "reason": "PORTFOLIO_RISK_BUDGET_EXCEEDED_AT_EXECUTION", "filled_shares": 0, "execution_notional_cny": notional, "risk_budget_remaining_cny": remaining_risk})
            continue
        if total_cost > remaining_cash + 1e-9:
            execution_results.append({"symbol": symbol, "status": "BLOCKED", "reason": "CASH_INSUFFICIENT_AFTER_FEES_AT_EXECUTION", "filled_shares": 0, "execution_cost_cny": total_cost, "cash_available_cny": remaining_cash})
            continue
        if notional > target_amount_per_slot_cny + 1e-9:
            execution_results.append({"symbol": symbol, "status": "BLOCKED", "reason": "SLOT_BUDGET_EXCEEDED_AT_EXECUTION", "filled_shares": 0, "execution_notional_cny": notional, "slot_budget_cny": target_amount_per_slot_cny})
            continue
        remaining_risk -= notional
        remaining_cash -= total_cost
        execution_results.append({
            "symbol": symbol,
            "status": "SIMULATED_FILL_UNDER_OPEN_PRICE_ASSUMPTION",
            "reason": "PRECOMMITTED_QUANTITY_PRICED_USING_SYNTHETIC_NEXT_OPEN_ASSUMPTION",
            "filled_shares": quantity,
            "execution_price_cny": price,
            "execution_notional_cny": notional,
            "estimated_commission_cny": commission,
            "execution_cost_cny": total_cost,
            "execution_price_source": str(quote["price_source"]),
            "execution_price_observed_at_utc": str(quote["price_available_at_utc"]),
            "execution_status_source": str(quote["status_source"]),
            "execution_status_available_at_utc": str(quote["status_available_at_utc"]),
            "fill_assumption": "NEXT_SESSION_OPEN_PRICE_ASSUMPTION",
            "real_market_fill_proven": False,
            "cash_remaining_cny": remaining_cash,
            "risk_budget_remaining_cny": remaining_risk,
            "actual_execution_quote_used_for_reselection": False,
        })

    return {
        "result_type": "SYNTHETIC_RESEARCH_PROTOTYPE_NOT_BACKTEST",
        "control_policy": "SELECT_TOP_N_THEN_ROUND_KEEP_CASH",
        "candidate_policy": "OBSERVE_DECISION_SESSION_OPEN_THEN_SELECT; SCHEDULE_REPLACEMENT_FOR_NEXT_SESSION_OPEN",
        "signal_date": _date_text(signal_date),
        "ranking_available_at_utc": ranking_available_at.isoformat(),
        "decision_session_date": _date_text(decision_session_date),
        "decision_at_utc": decision_at.isoformat(),
        "decision_quote_availability_fields": ["price_available_at_utc", "status_available_at_utc"],
        "decision_status_observation_at_utc": decision_at.isoformat(),
        "execution_session_date": _date_text(execution_session_date),
        "execution_at_utc": execution_at.isoformat(),
        "validated_open_sessions": open_days,
        "decision_price_source": "per-quote price_source; must be available by decision_at_utc",
        "decision_security_status_source": "per-quote status_source; must be available by decision_at_utc",
        "replacement_execution_price_source": "per-quote source; explicitly treated as an open-price fill assumption only",
        "execution_fill_model": "SIMULATED_FILL_UNDER_OPEN_PRICE_ASSUMPTION",
        "real_market_fill_evidence": False,
        "same_session_open_replacement_fill_allowed": False,
        "max_positions": int(max_positions),
        "target_amount_per_slot_cny": float(target_amount_per_slot_cny),
        "portfolio_risk_budget_cny": float(portfolio_risk_budget_cny),
        "reserved_risk_cny": float(reserved_risk_cny),
        "available_cash_cny": float(available_cash_cny),
        "primary_target_reviews": primary_reviews,
        "candidate_evaluations": candidate_evaluations,
        "replacement_orders": orders,
        "execution_results": execution_results,
        "execution_reselection_after_price_change": False,
    }
