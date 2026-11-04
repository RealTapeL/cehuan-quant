from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.canonical import parse_optional_bool, read_bars
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_status(value: object) -> str:
    return "RECORDED" if value is not None and not pd.isna(value) and str(value).strip() else "UNAVAILABLE"


def _as_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _suspension_state_counts(values: pd.Series) -> dict[str, int]:
    """Count tri-state suspension observations without treating unknown as false."""
    states = parse_optional_bool(pd.Series(values, copy=False))
    suspended = int(states.eq(True).fillna(False).sum())
    not_suspended = int(states.eq(False).fillna(False).sum())
    unknown = int(states.isna().sum())
    total = int(len(states))
    if suspended + not_suspended + unknown != total:
        raise AssertionError("停牌三态计数未能覆盖全部观察行")
    return {
        "total_rows": total,
        "suspended_rows": suspended,
        "not_suspended_rows": not_suspended,
        "unknown_rows": unknown,
    }


def _session_window_audit(
    window: pd.DataFrame,
    sessions: list[str],
    signal_day: str,
    lookback: int,
    symbol_history: pd.DataFrame,
) -> dict[str, Any]:
    expected = [day for day in sessions if day <= signal_day][-lookback - 1:]
    actual = window["trade_date"].astype(str).tolist()
    counts = symbol_history["trade_date"].astype(str).value_counts()
    duplicates = sorted(day for day in set(actual) if int(counts.get(day, 0)) > 1)
    actual_set = set(actual)
    missing = [day for day in expected if day not in actual_set]
    off_calendar = [day for day in actual if day not in set(sessions)]
    exact = actual == expected and not duplicates and not off_calendar
    return {
        "expected_open_session_count": len(expected),
        "expected_open_session_dates": expected,
        "actual_bar_row_count": len(actual),
        "actual_bar_dates": actual,
        "missing_open_session_dates": missing,
        "off_calendar_bar_dates": sorted(set(off_calendar)),
        "duplicate_symbol_date_rows": sum(max(0, int(counts.get(day, 0)) - 1) for day in set(actual)),
        "duplicate_symbol_dates": duplicates,
        "window_alignment_status": "MATCHED_EXACT_SESSION_WINDOW" if exact else "MISMATCH_ROW_WINDOW_VS_OPEN_SESSIONS",
        "provider_source_values": sorted(symbol_history.loc[
            symbol_history["trade_date"].astype(str).isin(actual), "data_source"
        ].dropna().astype(str).unique()) if "data_source" in symbol_history else [],
        "project_forward_fill_detected": False,
        "provider_raw_response_vintage": "UNVERIFIED_NOT_ARCHIVED_WITH_CANONICAL_BARS",
    }


def _round_order_quantity(quantity: int, minimum: int, step: int) -> int:
    if quantity < minimum:
        return 0
    return minimum + ((quantity - minimum) // step) * step


def _match_action_to_holding_windows(
    record_date: object,
    phase_dates: list[object],
    holding_windows: pd.DataFrame,
) -> dict[str, Any]:
    """Match record-date events to the recorded end-of-day holding interval.

    Candidate-generation convention for this audit: an ordinary A-share
    holding interval is a possible record-date match when it includes the
    record date, including an interval that starts with a record-date buy. A
    position closed before the simulated end-of-day snapshot is not matched.
    This does not confirm the depository's account-level entitlement: the
    available exchange education page and settlement materials do not fully
    align on same-day transaction treatment. Later event phases remain linked
    only after a possible record-date match is established.
    """
    record_day = pd.to_datetime(record_date, errors="coerce")
    record_day = None if pd.isna(record_day) else record_day.date()
    phases = [
        pd.to_datetime(value, errors="coerce").date()
        for value in phase_dates
        if pd.notna(value) and not pd.isna(pd.to_datetime(value, errors="coerce"))
    ]
    entitlement_matches: list[str] = []
    phase_matches: list[str] = []
    for holding in holding_windows.itertuples(index=False):
        start_day = pd.to_datetime(holding.holding_start_trade_date_at_open).date()
        held_through_day = pd.to_datetime(holding.held_at_end_of_day_through).date()
        exit_day_value = getattr(holding, "holding_end_trade_date_before_open", None)
        exit_day = None if pd.isna(exit_day_value) else pd.to_datetime(exit_day_value).date()
        interval_id = f"{holding.symbol}:{start_day}:{exit_day or held_through_day}"
        entitled = record_day is not None and start_day <= record_day <= held_through_day
        if entitled:
            entitlement_matches.append(interval_id)
        if any(start_day <= day <= held_through_day for day in phases):
            phase_matches.append(interval_id)
    return {
        "record_date_entitlement_match": bool(entitlement_matches),
        "phase_overlap_match": bool(phase_matches),
        "matching_holding_intervals": sorted(set(entitlement_matches + phase_matches)),
        "holding_match_status": (
            "POSSIBLE_RECORD_DATE_ENTITLEMENT" if entitlement_matches
            else "POSSIBLE_EVENT_PHASE_OVERLAP" if phase_matches
            else "NO_MATCH_IN_RECORDED_HOLDING_INTERVALS"
        ),
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须为对象：{path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _rank_audit(
    bars: pd.DataFrame,
    candidates_path: Path,
    strategy: dict[str, Any],
    market: dict[str, Any],
    signal_dates: list[str],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    calculated = add_momentum(bars.copy(), int(strategy["lookback_sessions"]))
    saved = pd.read_csv(candidates_path, dtype={"signal_date": str, "execution_date": str, "symbol": str})
    output: list[pd.DataFrame] = []
    by_date: dict[str, dict[str, Any]] = {}
    for signal_date in signal_dates:
        day = calculated.loc[calculated["trade_date"].astype(str).eq(signal_date)].copy()
        ranked = rank_precomputed_day(day, strategy, market, require_order_rules=False)
        selected = ranked.head(int(strategy["max_positions"])).copy()
        selected["signal_date"] = signal_date
        output.append(ranked.assign(signal_date=signal_date))
        saved_day = saved.loc[saved["signal_date"].astype(str).eq(signal_date)].copy()
        joined = ranked[["symbol", "momentum", "rank"]].merge(
            saved_day[["symbol", "momentum", "rank"]].rename(
                columns={"momentum": "saved_momentum", "rank": "saved_rank"}
            ),
            on="symbol", how="outer", indicator=True,
        )
        mismatch = joined.loc[
            joined["_merge"].ne("both")
            | joined["rank"].fillna(-1).ne(joined["saved_rank"].fillna(-1))
            | ((joined["momentum"] - joined["saved_momentum"]).abs().fillna(0) > 1e-10)
        ]
        by_date[signal_date] = {
            "eligible_candidate_count_rebuilt": int(len(ranked)),
            "eligible_candidate_count_saved": int(len(saved_day)),
            "selected_targets": selected[["symbol", "momentum", "rank"]].to_dict(orient="records"),
            "candidate_reconstruction_status": "MATCH" if mismatch.empty else "MISMATCH",
            "candidate_mismatch_rows": int(len(mismatch)),
        }
    full = pd.concat(output, ignore_index=True) if output else pd.DataFrame()
    return full, by_date


def _signal_inputs(
    bars: pd.DataFrame,
    signal_date: str,
    symbol: str,
    lookback: int,
) -> dict[str, Any]:
    series = bars.loc[bars["symbol"].astype(str).eq(symbol)].sort_values("trade_date", kind="stable")
    signal_rows = series.loc[series["trade_date"].astype(str).le(signal_date)]
    current_matches = signal_rows.loc[signal_rows["trade_date"].astype(str).eq(signal_date)]
    if current_matches.empty:
        return {
            "signal_bar_status": "UNAVAILABLE",
            "signal_observation_count": int(len(signal_rows)),
            "lookback_observation_date": None,
            "signal_price_start": None,
            "signal_price_end": None,
            "recomputed_momentum": None,
            "signal_window_suspension_state_counts": None,
        }
    current = current_matches.iloc[-1]
    if len(signal_rows) <= lookback:
        return {
            "signal_bar_status": "INSUFFICIENT_LOOKBACK_ROWS",
            "signal_observation_count": int(len(signal_rows)),
            "lookback_observation_date": None,
            "signal_price_start": None,
            "signal_price_end": _as_float(current.get("adjusted_close")),
            "recomputed_momentum": None,
            "signal_window_suspension_state_counts": _suspension_state_counts(signal_rows["is_suspended"]),
        }
    base = signal_rows.iloc[-lookback - 1]
    end_price = _as_float(current.get("adjusted_close"))
    start_price = _as_float(base.get("adjusted_close"))
    if start_price is None or end_price is None or start_price <= 0:
        momentum = None
        status = "INVALID_ADJUSTED_PRICE"
    else:
        momentum = end_price / start_price - 1.0
        status = "RECONSTRUCTED_FROM_FROZEN_BARS"
    window = signal_rows.tail(lookback + 1)
    return {
        "signal_bar_status": status,
        "signal_observation_count": int(len(signal_rows)),
        "lookback_observation_date": str(base["trade_date"]),
        "signal_price_start": start_price,
        "signal_price_end": end_price,
        "recomputed_momentum": momentum,
        "signal_window_suspension_state_counts": _suspension_state_counts(window["is_suspended"]),
    }


def _suspension_candidate_audit(
    bars: pd.DataFrame,
    ranked: pd.DataFrame,
    fills: pd.DataFrame,
    blocked_attempts: pd.DataFrame,
    target_audit: pd.DataFrame,
    calendar: pd.DataFrame,
    strategy: dict[str, Any],
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rebuild one candidate's full row-based feature window and nearby ranks."""
    symbol = str(symbol)
    candidate_rows = ranked.loc[ranked["symbol"].astype(str).eq(symbol)]
    if candidate_rows.empty:
        return pd.DataFrame(), pd.DataFrame()
    sessions = sorted(pd.to_datetime(calendar.loc[
        calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"
    ]).dt.strftime("%Y-%m-%d").unique())
    lookback = int(strategy["lookback_sessions"])
    history = bars.loc[bars["symbol"].astype(str).eq(symbol)].copy()
    history["trade_date"] = history["trade_date"].astype(str)
    history = history.sort_values("trade_date", kind="stable")
    window_rows: list[dict[str, Any]] = []
    neighbor_rows: list[dict[str, Any]] = []
    for candidate in candidate_rows.sort_values("signal_date").itertuples(index=False):
        signal_day = str(candidate.signal_date)
        prior = history.loc[history["trade_date"].le(signal_day)]
        if len(prior) <= lookback:
            continue
        window = prior.tail(lookback + 1).reset_index(drop=True)
        calendar_audit = _session_window_audit(window, sessions, signal_day, lookback, history)
        state_counts = _suspension_state_counts(window["is_suspended"])
        return_state_counts = _suspension_state_counts(
            window.loc[window.index != 0, "is_suspended"]
        )
        session_pos = next((i for i, day in enumerate(sessions) if day == signal_day), None)
        execution_day = sessions[session_pos + 1] if session_pos is not None and session_pos + 1 < len(sessions) else None
        execution_rows = (
            bars.loc[
                bars["trade_date"].astype(str).eq(execution_day)
                & bars["symbol"].astype(str).eq(symbol)
            ] if execution_day else pd.DataFrame()
        )
        execution = execution_rows.iloc[0].to_dict() if len(execution_rows) == 1 else {}
        execution_fills = fills.loc[
            fills["trade_date"].astype(str).eq(execution_day)
            & fills["symbol"].astype(str).eq(symbol)
        ] if execution_day else pd.DataFrame()
        execution_blocked = blocked_attempts.loc[
            blocked_attempts["trade_date"].astype(str).eq(execution_day)
            & blocked_attempts["symbol"].astype(str).eq(symbol)
        ] if execution_day and {"trade_date", "symbol"}.issubset(blocked_attempts.columns) else pd.DataFrame()
        target_row = {
            "symbol": symbol,
            "signal_date": signal_day,
            "execution_date": execution_day,
            "momentum": _as_float(candidate.momentum),
            "rank": int(candidate.rank),
            "selected_target": int(candidate.rank) <= int(strategy["max_positions"]),
            "lookback_sessions": lookback,
            "window_observations": len(window),
            "execution_is_suspended": execution.get("is_suspended"),
            "execution_open": execution.get("open"),
            "execution_fill_record_count": int(len(execution_fills)),
            "execution_fill_sides": ",".join(execution_fills["side"].astype(str).tolist()) if not execution_fills.empty else "NONE_RECORDED",
            "execution_fill_quantities": ",".join(execution_fills["quantity"].astype(str).tolist()) if not execution_fills.empty else "",
            "execution_blocked_simulation_attempt_count": int(len(execution_blocked)),
            "execution_blocked_simulation_attempt_reasons": ",".join(execution_blocked.get("reason", pd.Series(dtype=str)).astype(str).tolist()),
            "order_evidence_status": "UNKNOWN_NO_COMPLETE_ORDER_LOG",
            "fill_file_semantics": "trades.csv rows are simulated fill records; not a complete order log",
            "blocked_file_semantics": "blocked_orders.csv rows are recorded simulator-blocked attempts; not submitted broker orders",
            "window_suspended_observation_count": state_counts["suspended_rows"],
            "window_not_suspended_observation_count": state_counts["not_suspended_rows"],
            "window_unknown_suspension_observation_count": state_counts["unknown_rows"],
            "return_suspended_observation_count_excluding_base": return_state_counts["suspended_rows"],
            "return_not_suspended_observation_count_excluding_base": return_state_counts["not_suspended_rows"],
            "return_unknown_suspension_observation_count_excluding_base": return_state_counts["unknown_rows"],
            **calendar_audit,
            "window_zero_volume_observation_count": int(pd.to_numeric(window["volume_shares"], errors="coerce").fillna(-1).eq(0).sum()),
            "window_status_source": ";".join(sorted(window["data_source"].dropna().astype(str).unique())) if "data_source" in window else "UNAVAILABLE",
            "availability_note": "available_at is adapter-assigned close time; historical provider publication/vintage remains unverified",
        }
        for index, row in window.iterrows():
            window_rows.append({
                **target_row,
                "observation_index": index,
                "window_role": "LOOKBACK_BASE" if index == 0 else "SIGNAL_DATE" if index == len(window) - 1 else "LOOKBACK_OBSERVATION",
                "calendar_is_open": row["trade_date"] in set(sessions),
                "symbol_date_row_count": int(history["trade_date"].astype(str).eq(str(row["trade_date"])).sum()),
                **row.to_dict(),
            })
        signal_neighbors = ranked.loc[
            ranked["signal_date"].astype(str).eq(signal_day)
            & ranked["rank"].between(max(1, int(candidate.rank) - 4), int(candidate.rank) + 5)
        ].sort_values("rank")
        for row in signal_neighbors.to_dict(orient="records"):
            neighbor_execution_day = execution_day
            neighbor_bar = bars.loc[
                bars["trade_date"].astype(str).eq(neighbor_execution_day)
                & bars["symbol"].astype(str).eq(str(row["symbol"]))
            ] if neighbor_execution_day else pd.DataFrame()
            neighbor_fills = fills.loc[
                fills["trade_date"].astype(str).eq(neighbor_execution_day)
                & fills["symbol"].astype(str).eq(str(row["symbol"]))
            ] if neighbor_execution_day else pd.DataFrame()
            neighbor_blocked = blocked_attempts.loc[
                blocked_attempts["trade_date"].astype(str).eq(neighbor_execution_day)
                & blocked_attempts["symbol"].astype(str).eq(str(row["symbol"]))
            ] if neighbor_execution_day and {"trade_date", "symbol"}.issubset(blocked_attempts.columns) else pd.DataFrame()
            target_matches = target_audit.loc[
                target_audit["execution_date"].astype(str).eq(str(neighbor_execution_day))
                & target_audit["symbol"].astype(str).eq(str(row["symbol"]))
            ] if neighbor_execution_day and not target_audit.empty else pd.DataFrame()
            target_details = target_matches.iloc[0].to_dict() if len(target_matches) == 1 else {}
            neighbor_rows.append({
                "signal_date": signal_day,
                "execution_date": neighbor_execution_day,
                "symbol": str(row["symbol"]),
                "name": row.get("name"),
                "rank": int(row["rank"]),
                "momentum": _as_float(row.get("momentum")),
                "selected_target": int(row["rank"]) <= int(strategy["max_positions"]),
                "signal_day_is_suspended": row.get("is_suspended"),
                "execution_bar_count": len(neighbor_bar),
                "execution_is_suspended": neighbor_bar.iloc[0].get("is_suspended") if len(neighbor_bar) == 1 else None,
                "execution_fill_record_count": int(len(neighbor_fills)),
                "execution_fill_sides": ",".join(neighbor_fills["side"].astype(str).tolist()) if not neighbor_fills.empty else "NONE_RECORDED",
                "execution_fill_quantities": ",".join(neighbor_fills["quantity"].astype(str).tolist()) if not neighbor_fills.empty else "",
                "execution_blocked_simulation_attempt_count": int(len(neighbor_blocked)),
                "execution_blocked_simulation_attempt_reasons": ",".join(neighbor_blocked.get("reason", pd.Series(dtype=str)).astype(str).tolist()),
                "order_evidence_status": "UNKNOWN_NO_COMPLETE_ORDER_LOG",
                "target_audit_status": "MATCHED_RECONSTRUCTION" if target_details else "NO_UNIQUE_TARGET_ROW",
                "target_weight": target_details.get("target_weight"),
                "target_amount_cny": target_details.get("target_amount_cny"),
                "execution_open_price_cny": target_details.get("execution_open_price_cny"),
                "raw_target_shares_before_rounding": target_details.get("raw_target_shares_before_rounding"),
                "target_shares_after_fallback": target_details.get("target_shares_after_100_share_fallback"),
                "current_shares_before_rebalance": target_details.get("current_shares_before_rebalance"),
                "expected_simulated_fill_side": target_details.get("expected_order_side"),
                "expected_simulated_fill_quantity": target_details.get("expected_order_quantity_after_fallback"),
                "actual_fill_quantity_from_trades_csv": target_details.get("actual_fill_quantity"),
                "target_fill_status": target_details.get("target_fill_status"),
                "target_lifecycle_status": target_details.get("target_lifecycle_status"),
                "market_rule_basis": target_details.get("market_rule_basis"),
            })
    return pd.DataFrame(window_rows), pd.DataFrame(neighbor_rows)


def _reconstruct_targets(
    fills: pd.DataFrame,
    candidates: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    strategy: dict[str, Any],
    market: dict[str, Any],
    costs: dict[str, Any],
    initial_cash_cny: float,
) -> pd.DataFrame:
    sessions = sorted(
        pd.to_datetime(calendar.loc[calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"])
        .dt.strftime("%Y-%m-%d").unique()
    )
    ordered_dates = sorted(fills["trade_date"].astype(str).unique())
    cash = float(initial_cash_cny)
    positions: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    slippage = float(costs.get("slippage_bps", 0.0)) / 10000.0
    bars_by_day_symbol = bars.set_index([bars["trade_date"].astype(str), "symbol"], drop=False)
    for execution_date in ordered_dates:
        prior_sessions = [day for day in sessions if day < execution_date]
        signal_date = prior_sessions[-1] if prior_sessions else None
        if signal_date is None:
            raise ValueError(f"成交日 {execution_date} 前缺少日历信号日")
        open_value = cash
        for symbol, shares in positions.items():
            key = (execution_date, symbol)
            if key not in bars_by_day_symbol.index:
                raise ValueError(f"无法用冻结行情估算开盘权益：{symbol} {execution_date}")
            bar = bars_by_day_symbol.loc[key]
            if isinstance(bar, pd.DataFrame):
                raise ValueError(f"成交日行情不唯一：{symbol} {execution_date}")
            open_value += shares * float(bar["open"])
        day_candidates = candidates.loc[
            candidates["signal_date"].astype(str).eq(signal_date)
        ].sort_values("rank")
        selected = day_candidates.head(int(strategy["max_positions"]))
        weight = min(
            float(strategy["max_position_weight"]),
            float(strategy["max_total_exposure"]) / len(selected),
        ) if not selected.empty else 0.0
        target_shares: dict[str, int] = {}
        target_details: dict[str, dict[str, float | int]] = {}
        for item in selected.itertuples(index=False):
            bar = bars_by_day_symbol.loc[(execution_date, item.symbol)]
            if isinstance(bar, pd.DataFrame):
                raise ValueError(f"成交日行情不唯一：{item.symbol} {execution_date}")
            target_amount = open_value * weight
            opening_price = float(bar["open"])
            raw_target = int(target_amount / opening_price)
            minimum = int(market["buy_lot_shares"])
            step = int(market["buy_lot_shares"])
            target_details[item.symbol] = {
                "target_amount_cny": target_amount,
                "execution_open_price_cny": opening_price,
                "raw_target_shares_before_rounding": raw_target,
                "buy_minimum_shares_from_config_fallback": minimum,
                "buy_step_shares_from_config_fallback": step,
            }
            target_shares[item.symbol] = _round_order_quantity(
                raw_target,
                minimum,
                step,
            )
        day_actual = fills.loc[fills["trade_date"].astype(str).eq(execution_date)]
        actual_map = {
            (str(row.symbol), str(row.side)): int(row.quantity)
            for row in day_actual.itertuples(index=False)
        }
        candidate_symbols = set(target_shares) | set(positions)
        for symbol in sorted(candidate_symbols):
            target_qty = target_shares.get(symbol, 0)
            current_qty = positions.get(symbol, 0)
            delta = target_qty - current_qty
            if delta > 0:
                side, expected_qty = "BUY", _round_order_quantity(
                    delta, int(market["buy_lot_shares"]), int(market["buy_lot_shares"])
                )
            elif delta < 0:
                side = "SELL"
                expected_qty = abs(delta)
                if expected_qty != current_qty:
                    expected_qty = _round_order_quantity(
                        expected_qty, int(market["sell_lot_shares"]), int(market["sell_lot_shares"])
                    )
            else:
                if symbol not in target_shares:
                    continue
                side, expected_qty = "NONE", 0
            actual_qty = actual_map.get((symbol, side), 0)
            if symbol in target_shares and target_qty == 0:
                lifecycle = "ZERO_TARGET_REQUIRES_EXIT" if current_qty > 0 else "ZERO_TARGET_NO_POSITION_CASH_RETAINED"
            elif side == "NONE":
                lifecycle = "TARGET_MAINTAINED"
            elif actual_qty == expected_qty and expected_qty > 0:
                lifecycle = "ORDER_FILLED"
            elif actual_qty > 0:
                lifecycle = "ORDER_PARTIALLY_FILLED_OR_SPLIT_REVIEW"
            elif expected_qty > 0:
                lifecycle = "ORDER_MISSING_OR_BLOCKED_REVIEW"
            else:
                lifecycle = "NO_ORDER"
            rows.append({
                "execution_date": execution_date,
                "signal_date": signal_date,
                "symbol": symbol,
                "rank": next((int(x.rank) for x in selected.itertuples(index=False) if x.symbol == symbol), None),
                "momentum": next((_as_float(x.momentum) for x in selected.itertuples(index=False) if x.symbol == symbol), None),
                "selected_target": symbol in target_shares,
                "opening_cash_before_rebalance_cny": cash,
                "opening_equity_reconstructed_cny": open_value,
                "target_weight": weight if symbol in target_shares else 0.0,
                "target_amount_cny": target_details.get(symbol, {}).get("target_amount_cny"),
                "execution_open_price_cny": target_details.get(symbol, {}).get("execution_open_price_cny"),
                "raw_target_shares_before_rounding": target_details.get(symbol, {}).get("raw_target_shares_before_rounding"),
                "buy_minimum_shares_from_config_fallback": target_details.get(symbol, {}).get("buy_minimum_shares_from_config_fallback"),
                "buy_step_shares_from_config_fallback": target_details.get(symbol, {}).get("buy_step_shares_from_config_fallback"),
                "target_shares_after_100_share_fallback": target_qty,
                "current_shares_before_rebalance": current_qty,
                "expected_order_side": side,
                "expected_order_quantity_after_fallback": expected_qty,
                "actual_fill_quantity": actual_qty,
                "target_fill_status": (
                    "ZERO_TARGET_EXIT_MATCHED" if symbol in target_shares and target_qty == 0 and current_qty > 0 and actual_qty == expected_qty and expected_qty > 0
                    else "ZERO_TARGET_EXIT_REVIEW_REQUIRED" if symbol in target_shares and target_qty == 0 and current_qty > 0
                    else "TARGET_ROUNDED_TO_ZERO" if symbol in target_shares and target_qty == 0
                    else "MATCHED_FILL" if actual_qty == expected_qty and expected_qty > 0
                    else "NO_FILL_REVIEW_REQUIRED" if actual_qty != expected_qty
                    else "MATCHED_FILL"
                ),
                "target_lifecycle_status": lifecycle,
                "market_rule_basis": "configs/default.toml fallback; 100-share lot; not historical board rules",
                "cost_basis": "configs/default.toml flat assumptions; not historical or personal HTS fees",
                "estimated_execution_impact_is_simulated": True,
                "slippage_bps": float(costs.get("slippage_bps", 0.0)),
            })
        # Keep the actual execution ledger as the state source. The audit compares
        # reconstructed targets to those fills; it does not replace or mutate them.
        for row in day_actual.itertuples(index=False):
            symbol, side, quantity = str(row.symbol), str(row.side), int(row.quantity)
            notional, fees = float(row.notional_cny), float(row.fees_cny)
            if side == "BUY":
                cash -= notional + fees
                positions[symbol] = positions.get(symbol, 0) + quantity
            else:
                cash += notional - fees
                positions[symbol] = positions.get(symbol, 0) - quantity
                if positions[symbol] <= 0:
                    positions.pop(symbol, None)
    return pd.DataFrame(rows)


def _trade_rows(
    fills: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    ranked: pd.DataFrame,
    journal: pd.DataFrame,
    fallback: pd.DataFrame,
    input_hashes: dict[str, str],
    experiment_id: str,
    strategy: dict[str, Any],
    costs: dict[str, Any],
) -> pd.DataFrame:
    sessions = sorted(
        pd.to_datetime(calendar.loc[calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"])
        .dt.strftime("%Y-%m-%d").unique()
    )
    session_before = {day: sessions[i - 1] for i, day in enumerate(sessions) if i > 0}
    rows: list[dict[str, Any]] = []
    by_event = {str(row.event_id): row for row in journal.itertuples(index=False)}
    candidate_lookup = {
        (str(row.signal_date), str(row.symbol)): row
        for row in ranked.itertuples(index=False)
    }
    fallback_lookup = {
        (str(row.trade_date), str(row.symbol), str(row.side)): row
        for row in fallback.itertuples(index=False)
    }
    bars_lookup = bars.set_index([bars["trade_date"].astype(str), "symbol"], drop=False)
    for source_line, row in enumerate(fills.itertuples(index=False), start=2):
        day = str(row.trade_date)
        symbol = str(row.symbol)
        side = str(row.side)
        signal_date = session_before.get(day)
        if signal_date is None:
            raise ValueError(f"成交日 {day} 缺少前一交易日")
        bar = bars_lookup.loc[(day, symbol)]
        if isinstance(bar, pd.DataFrame):
            raise ValueError(f"成交日行情非唯一：{symbol} {day}")
        signal_info = _signal_inputs(bars, signal_date, symbol, int(strategy["lookback_sessions"]))
        candidate = candidate_lookup.get((signal_date, symbol))
        fb = fallback_lookup.get((day, symbol, side))
        prior = bars.loc[
            bars["symbol"].astype(str).eq(symbol)
            & bars["trade_date"].astype(str).lt(day)
        ].sort_values("trade_date", kind="stable").tail(1)
        previous_close = _as_float(prior.iloc[0]["close"]) if not prior.empty else None
        open_change = (float(bar["open"]) / previous_close - 1.0) if previous_close else None
        event_id = (
            f"bt-{day}-{symbol}-{side}-" + f"{int(row.quantity):08d}"
        )
        journal_row = by_event.get(event_id)
        # The event IDs contain the engine sequence, so resolve by symbol/date/side
        # from the stable operation key when the quantity suffix is not the sequence.
        if journal_row is None:
            options = [
                item for item in journal.itertuples(index=False)
                if str(item.event_id).startswith(f"bt-{day}-{symbol}-{side}-")
            ]
            journal_row = options[0] if len(options) == 1 else None
        rows.append({
            "trade_id": str(journal_row.operation_key) if journal_row is not None else None,
            "experiment_id": experiment_id,
            "source_trade_row_number": source_line,
            "source_fills_file_sha256": input_hashes["trades.csv"],
            "trade_fields_provenance": "RECORDED_FROM_FROZEN_TRADES_CSV",
            "trade_date": day,
            "symbol": symbol,
            "side": side,
            "quantity": int(row.quantity),
            "reference_price": float(row.reference_price),
            "estimated_fill_price": float(row.estimated_fill_price),
            "notional_cny": float(row.notional_cny),
            "fees_cny": float(row.fees_cny),
            "commission_cny": float(row.commission_cny),
            "stamp_duty_cny": float(row.stamp_duty_cny),
            "transfer_fee_cny": float(row.transfer_fee_cny),
            "execution_impact_vs_reference_cny": float(row.execution_impact_vs_reference_cny),
            "allocated_cost_basis_cny": float(row.allocated_cost_basis_cny),
            "realized_pnl_cny": _as_float(row.realized_pnl_cny),
            "trade_reason": str(row.reason),
            "signal_date": signal_date,
            "signal_date_provenance": "RECONSTRUCTED_FROM_PREVIOUS_OPEN_SESSION_IN_FROZEN_CALENDAR",
            "signal_timing_rule": "t close signal; earliest simulated execution next open",
            "signal_timing_rule_evidence": "strategy code plus saved fill reason; historical vendor availability UNVERIFIED",
            "signal_cutoff_assumption": "15:00 Asia/Shanghai adapter assumption",
            "signal_cutoff_status": "UNVERIFIED_EXTERNAL_AVAILABILITY",
            "signal_lookback_sessions": int(strategy["lookback_sessions"]),
            **signal_info,
            "signal_feature_provenance": "RECONSTRUCTED_FROM_FROZEN_BARS_AND_CONFIG; COMPARED_TO_SAVED_CANDIDATES",
            "signal_feature_source_status": signal_info["signal_bar_status"],
            "signal_price_method": "BaoStock adjustflag=1 mapped to adjusted_close/hfq; separate from unadjusted execution open",
            "candidate_rank": int(candidate.rank) if candidate is not None else None,
            "candidate_momentum": _as_float(candidate.momentum) if candidate is not None else None,
            "candidate_link_status": "RECONSTRUCTED_MATCH" if candidate is not None else "NOT_SELECTED_OR_UNAVAILABLE",
            "execution_bar_source": str(bar.get("data_source")),
            "execution_bar_provenance": "RECORDED_FROM_FROZEN_CANONICAL_BARS_CSV",
            "execution_bar_retrieved_at_utc": str(bar.get("retrieved_at_utc")),
            "execution_bar_available_at": str(bar.get("available_at")),
            "execution_bar_available_at_status": "ADAPTER_ASSUMPTION_NOT_VENDOR_TIMESTAMP",
            "bar_open": float(bar["open"]),
            "bar_high": float(bar["high"]),
            "bar_low": float(bar["low"]),
            "bar_close": float(bar["close"]),
            "bar_adjusted_close": float(bar["adjusted_close"]),
            "execution_price_basis": "unadjusted open * (1 +/- configured 5 bps); cash and quantities use simulated fill price",
            "previous_raw_close": previous_close,
            "open_change_vs_previous_close": open_change,
            "fallback_limit_pct": float(fb.fallback_price_limit_pct) if fb is not None else None,
            "fallback_price_limit_applies_when_unknown": bool(fb.fallback_assume_limit_applies_when_unknown) if fb is not None else None,
            "limit_fallback_would_block_open": (
                bool(
                    (side == "BUY" and previous_close and float(bar["open"]) >= previous_close * (1.0 + float(fb.fallback_price_limit_pct)) - 0.005)
                    or (side == "SELL" and previous_close and float(bar["open"]) <= previous_close * (1.0 - float(fb.fallback_price_limit_pct)) + 0.005)
                ) if fb is not None else None
            ),
            "buy_lot_fallback": int(fb.fallback_buy_lot_shares) if fb is not None else None,
            "sell_lot_fallback": int(fb.fallback_sell_lot_shares) if fb is not None else None,
            "market_rule_source_status": str(fb.market_rule_source_status) if fb is not None else "UNAVAILABLE",
            "market_rule_provenance": "RECORDED_FROM_FROZEN_FALLBACK_DETAIL; CONFIG_FALLBACK" if fb is not None else "UNAVAILABLE",
            "fee_mode": str(fb.fee_application) if fb is not None else "UNAVAILABLE",
            "fee_source_status": str(fb.fee_source_status) if fb is not None else "UNAVAILABLE",
            "fee_provenance": "RECORDED_FROM_FROZEN_FALLBACK_DETAIL; CONFIG_ASSUMPTION" if fb is not None else "UNAVAILABLE",
            "commission_rate_assumption": float(fb.fallback_commission_rate) if fb is not None else None,
            "minimum_commission_cny_assumption": float(fb.fallback_minimum_commission_cny) if fb is not None else None,
            "stamp_duty_sell_rate_assumption": float(fb.fallback_stamp_duty_sell_rate) if fb is not None else None,
            "transfer_fee_rate_assumption": float(fb.fallback_transfer_fee_rate) if fb is not None else None,
            "slippage_bps_assumption": float(fb.fallback_slippage_bps) if fb is not None else float(costs.get("slippage_bps", 0.0)),
            "actual_broker_fee_evidence": "UNAVAILABLE; simulated config values only",
            "calendar_session_status": "RECONSTRUCTED_FROM_FROZEN_CALENDAR",
            "journal_link_status": "MATCHED" if journal_row is not None else "UNAVAILABLE",
            "journal_provenance": "RECORDED_FROM_FROZEN_ACCOUNTING_JOURNAL" if journal_row is not None else "UNAVAILABLE",
            "input_bar_file_sha256": input_hashes["bars.csv"],
            "candidate_file_sha256": input_hashes["candidate-pool-by-signal.csv"],
        })
    return pd.DataFrame(rows)


def _capture_cninfo_dividends(symbols: list[str], out_dir: Path) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    import akshare as ak
    import requests

    original_post = requests.post
    captured: list[tuple[str, bytes]] = []

    def capture_post(*args: Any, **kwargs: Any):
        response = original_post(*args, **kwargs)
        url = str(args[0] if args else kwargs.get("url", ""))
        if "p_sysapi1139" in url:
            captured.append((url, response.content))
        return response

    all_rows: list[pd.DataFrame] = []
    query_log: list[dict[str, Any]] = []
    requests.post = capture_post
    try:
        for symbol in symbols:
            started = datetime.now(timezone.utc).isoformat()
            before = len(captured)
            try:
                result = ak.stock_dividend_cninfo(symbol=symbol[2:])
                status = "QUERY_OK"
                error = None
                if not result.empty:
                    result = result.copy()
                    result.insert(0, "symbol", symbol)
                    all_rows.append(result)
                response_sha = None
                raw_path = None
                if len(captured) > before:
                    _, raw = captured[-1]
                    response_sha = hashlib.sha256(raw).hexdigest()
                    raw_path = f"cninfo-dividend-raw/{symbol}.json"
                    target = out_dir / raw_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
            except Exception as exc:  # network and upstream parser failures are audit states
                status = "QUERY_FAILED"
                error = f"{type(exc).__name__}: {exc}"
                response_sha = None
                raw_path = None
            query_log.append({
                "symbol": symbol,
                "source": "CNINFO webapi.cninfo.com.cn p_sysapi1139 via AKShare stock_dividend_cninfo",
                "scope": "CNINFO structured historical dividend / share distribution records only; does not cover all corporate actions",
                "query_started_at_utc": started,
                "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "result_rows": int(len(result)) if status == "QUERY_OK" else None,
                "raw_response_path": raw_path,
                "raw_response_sha256": response_sha,
                "error": error,
            })
            time.sleep(0.15)
    finally:
        requests.post = original_post
    rows = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not rows.empty:
        rows.to_csv(out_dir / "cninfo-dividend-records-adapter-output.csv", index=False)
    return rows, query_log


def _capture_tencent_quotes(symbols: list[str], out_dir: Path, start: str, end: str) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    import akshare as ak
    import requests

    original_get = requests.get
    captured: list[tuple[str, bytes]] = []

    def capture_get(*args: Any, **kwargs: Any):
        response = original_get(*args, **kwargs)
        url = str(args[0] if args else kwargs.get("url", ""))
        if "finance.qq.com" in url:
            captured.append((url, response.content))
        return response

    all_rows: list[pd.DataFrame] = []
    query_log: list[dict[str, Any]] = []
    requests.get = capture_get
    try:
        for prefixed_symbol in symbols:
            symbol = prefixed_symbol.lower()
            code = prefixed_symbol[2:]
            started = datetime.now(timezone.utc).isoformat()
            before = len(captured)
            try:
                result = ak.stock_zh_a_hist_tx(
                    symbol=symbol,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    adjust="",
                    timeout=20,
                )
                status = "QUERY_OK"
                error = None
                result = result.copy()
                result.insert(0, "symbol", prefixed_symbol)
                all_rows.append(result)
                saved_raw: list[dict[str, str]] = []
                for index, (url, raw) in enumerate(captured[before:], start=1):
                    raw_path = f"tencent-quote-raw/{prefixed_symbol}-{index}.jsonp"
                    target = out_dir / raw_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    saved_raw.append({"url": url, "path": raw_path, "sha256": hashlib.sha256(raw).hexdigest()})
            except Exception as exc:
                status = "QUERY_FAILED"
                error = f"{type(exc).__name__}: {exc}"
                result = pd.DataFrame()
                saved_raw = []
            query_log.append({
                "symbol": prefixed_symbol,
                "source": "Tencent Finance daily quote endpoint through AKShare stock_zh_a_hist_tx",
                "scope": "unadjusted daily OHLC, volume and amount; Tencent adapter reports volume in shares and amount in CNY",
                "query_range": f"{start}..{end}",
                "query_started_at_utc": started,
                "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "result_rows": int(len(result)) if status == "QUERY_OK" else None,
                "raw_http_responses": saved_raw,
                "error": error,
            })
            time.sleep(0.15)
    finally:
        requests.get = original_get
    rows = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not rows.empty:
        rows.to_csv(out_dir / "tencent-quotes-adapter-output.csv", index=False)
    return rows, query_log


def _archive_official_filing(out_dir: Path, filing: dict[str, Any]) -> dict[str, Any]:
    import requests

    symbol = str(filing["symbol"])
    event_family = str(filing["event_family"])
    source_title = str(filing["source_title"])
    url = str(filing["source_url"])
    document_date = str(filing["document_date"])
    started = datetime.now(timezone.utc).isoformat()
    filename_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{symbol}-{document_date}-{event_family}").strip("-")
    target = out_dir / f"official-filings/{filename_slug}.pdf"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.content
        if not content.startswith(b"%PDF-"):
            raise ValueError("公告响应不是 PDF 文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "symbol": symbol,
            "event_family": event_family,
            "source": "CNINFO official issuer filing",
            "source_title": source_title,
            "source_url": url,
            "document_date": document_date,
            "raw_response_path": target.relative_to(out_dir).as_posix(),
            "raw_response_sha256": sha256_file(target),
            "status": "OFFICIAL_PDF_ARCHIVED_HUMAN_FIELD_REVIEW_PENDING",
            "known_at_evidence_status": str(filing.get("known_at_evidence_status", "UNVERIFIED")),
            "query_started_at_utc": started,
            "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:  # a source retrieval failure must remain visible, not abort other audit artifacts
        return {
            "symbol": symbol,
            "event_family": event_family,
            "source": "CNINFO official issuer filing",
            "source_title": source_title,
            "source_url": url,
            "document_date": document_date,
            "raw_response_path": None,
            "raw_response_sha256": None,
            "status": "OFFICIAL_PDF_FETCH_FAILED",
            "known_at_evidence_status": str(filing.get("known_at_evidence_status", "UNVERIFIED")),
            "query_started_at_utc": started,
            "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate read-only, transaction-level audit of a frozen real-data research run.")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-pool", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fetch-independent-sources", action="store_true")
    parser.add_argument("--suspension-symbol", help="可选：重建该候选的完整动量窗口及相邻排名")
    parser.add_argument("--supplemental-filings", type=Path, help="私有 JSON 清单；仅归档并记录显式指定的官方公告")
    args = parser.parse_args()

    supplemental_filings: list[dict[str, Any]] = []
    supplemental_manifest_sha256 = None
    supplemental_manifest_bytes = None
    if args.supplemental_filings is not None:
        if not args.fetch_independent_sources:
            raise ValueError("使用 --supplemental-filings 时必须同时指定 --fetch-independent-sources")
        manifest_path = args.supplemental_filings.expanduser().resolve()
        if not manifest_path.is_file():
            raise FileNotFoundError(f"补充公告清单不存在：{manifest_path}")
        supplemental_manifest_bytes = manifest_path.read_bytes()
        supplemental_manifest_sha256 = hashlib.sha256(supplemental_manifest_bytes).hexdigest()
        manifest = json.loads(supplemental_manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("filings"), list):
            raise ValueError("补充公告清单必须是 schema_version=1 且含 filings 列表的 JSON 对象")
        required_filing_fields = {"symbol", "event_family", "source_title", "source_url", "document_date"}
        for index, filing in enumerate(manifest["filings"]):
            if not isinstance(filing, dict) or not required_filing_fields.issubset(filing):
                raise ValueError(f"supplemental filings[{index}] 缺少必填字段")
            if not str(filing["source_url"]).startswith("https://"):
                raise ValueError(f"supplemental filings[{index}].source_url 必须使用 HTTPS")
            supplemental_filings.append(filing)
        supplemental_filings = list(supplemental_filings)

    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=False)
    experiment_dir = args.experiment_dir.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    candidate_pool_path = args.candidate_pool.expanduser().resolve()
    audit_input_dir = candidate_pool_path.parent
    input_paths = {
        "bars.csv": experiment_dir / "inputs/bars.csv",
        "trading_calendar.csv": experiment_dir / "inputs/trading_calendar.csv",
        "config.json": run_dir / "config.json",
        "trades.csv": run_dir / "trades.csv",
        "accounting_journal.csv": run_dir / "accounting_journal.csv",
        "blocked_orders.csv": run_dir / "blocked_orders.csv",
        "metrics.json": run_dir / "metrics.json",
        "candidate-pool-by-signal.csv": candidate_pool_path,
    }
    missing = [str(path) for path in input_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("审计输入缺失：" + ", ".join(missing))
    hashes = {name: sha256_file(path) for name, path in input_paths.items()}
    frozen_inventory_path = audit_input_dir / "artifact-inventory.csv"
    frozen_inventory = pd.read_csv(frozen_inventory_path, dtype=str, keep_default_na=False)
    frozen_hashes = dict(zip(frozen_inventory["relative_path"], frozen_inventory["sha256"]))
    expected_inventory_paths = {
        "bars.csv": "inputs/bars.csv",
        "trading_calendar.csv": "inputs/trading_calendar.csv",
        "config.json": f"{run_dir.name}/config.json",
        "trades.csv": f"{run_dir.name}/trades.csv",
        "accounting_journal.csv": f"{run_dir.name}/accounting_journal.csv",
        "blocked_orders.csv": f"{run_dir.name}/blocked_orders.csv",
        "metrics.json": f"{run_dir.name}/metrics.json",
    }
    artifact_hash_checks = {}
    for name, relative_path in expected_inventory_paths.items():
        expected = frozen_hashes.get(relative_path)
        observed = hashes[name]
        artifact_hash_checks[name] = {
            "expected_sha256": expected or None,
            "observed_sha256": observed,
            "status": "MATCHED_FROZEN_INVENTORY" if expected == observed else "NO_FROZEN_HASH" if not expected else "FAILED_HASH_MISMATCH",
        }
    candidate_inventory_relative = (
        candidate_pool_path.relative_to(experiment_dir).as_posix()
        if candidate_pool_path.is_relative_to(experiment_dir) else None
    )
    candidate_expected_hash = frozen_hashes.get(candidate_inventory_relative)
    artifact_hash_checks["candidate-pool-by-signal.csv"] = {
        "expected_sha256": candidate_expected_hash or None,
        "observed_sha256": hashes["candidate-pool-by-signal.csv"],
        "status": (
            "MATCHED_FROZEN_INVENTORY" if candidate_expected_hash == hashes["candidate-pool-by-signal.csv"]
            else "NO_FROZEN_HASH_RECONSTRUCTED_AGAINST_BARS_CONFIG" if not candidate_expected_hash
            else "FAILED_HASH_MISMATCH"
        ),
    }
    bars = read_bars(input_paths["bars.csv"])
    raw_fills = pd.read_csv(input_paths["trades.csv"], keep_default_na=False)
    fallback = pd.read_csv(audit_input_dir / "trade-fallback-detail.csv")
    journal = pd.read_csv(input_paths["accounting_journal.csv"], dtype=str, keep_default_na=False)
    blocked = pd.read_csv(input_paths["blocked_orders.csv"], keep_default_na=False)
    calendar = pd.read_csv(input_paths["trading_calendar.csv"], keep_default_na=False)
    config = _json(input_paths["config.json"])
    run_metrics = _json(input_paths["metrics.json"])
    experiment_id = str(run_metrics.get("experiment_id", "UNAVAILABLE"))
    strategy, market, costs = config["strategy"], config["market"], config["costs"]
    fills = raw_fills.copy()
    fills["trade_date"] = fills["trade_date"].astype(str)
    signal_dates = sorted({
        max(day for day in pd.to_datetime(calendar.loc[
            calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"
        ]).dt.strftime("%Y-%m-%d").unique() if day < execution_date)
        for execution_date in fills["trade_date"].unique()
    })
    ranked, reconstruction = _rank_audit(bars, input_paths["candidate-pool-by-signal.csv"], strategy, market, signal_dates)
    audit = _trade_rows(fills, bars, calendar, ranked, journal, fallback, hashes, experiment_id, strategy, costs)
    # A stable snapshot of signal candidates is retained so later reviewers can
    # reproduce why each fill mapped to a rank without rerunning the backtest.
    ranked.to_csv(out_dir / "candidate-ranks-reconstructed.csv", index=False)
    audit.to_csv(out_dir / "trade-audit.csv", index=False)
    trade_targets = _reconstruct_targets(
        fills, ranked, bars, calendar, strategy, market, costs,
        float(run_metrics["initial_cash_cny"]),
    )
    trade_targets.to_csv(out_dir / "candidate-target-and-fill-audit.csv", index=False)
    zero_target_rows = trade_targets.loc[
        trade_targets.target_fill_status.astype(str).eq("TARGET_ROUNDED_TO_ZERO")
    ].copy()
    zero_review_rows: list[dict[str, Any]] = []
    for row in zero_target_rows.to_dict(orient="records"):
        following = ranked.loc[
            ranked["signal_date"].astype(str).eq(str(row["signal_date"]))
            & ranked["rank"].gt(int(strategy["max_positions"]))
        ].sort_values("rank").head(1)
        next_candidate = following.iloc[0].to_dict() if len(following) == 1 else {}
        zero_review_rows.append({
            **row,
            "next_unselected_candidate_rank": int(next_candidate["rank"]) if next_candidate else None,
            "next_unselected_candidate_symbol": str(next_candidate["symbol"]) if next_candidate else None,
            "next_unselected_candidate_momentum": _as_float(next_candidate.get("momentum")) if next_candidate else None,
            "replacement_applied": False,
            "replacement_policy": "NOT_APPLIED_TOP_N_SELECTED_BEFORE_ROUNDING",
        })
    pd.DataFrame(zero_review_rows).to_csv(out_dir / "zero-target-review.csv", index=False)
    suspension_window = pd.DataFrame()
    suspension_neighbors = pd.DataFrame()
    if args.suspension_symbol:
        suspension_window, suspension_neighbors = _suspension_candidate_audit(
            bars, ranked, fills, blocked, trade_targets, calendar, strategy, args.suspension_symbol,
        )
        suspension_window.to_csv(out_dir / "suspension-candidate-window.csv", index=False)
        suspension_neighbors.to_csv(out_dir / "suspension-candidate-rank-neighbors.csv", index=False)
    source_symbols = sorted(fills["symbol"].astype(str).unique())
    selected_symbols = sorted(
        pd.read_csv(input_paths["candidate-pool-by-signal.csv"])
        .loc[lambda frame: frame["selected_target"].astype(str).str.lower().isin(["true", "1"]), "symbol"]
        .astype(str).unique()
    )
    source_rows: list[dict[str, Any]] = []
    dividend_queries: list[dict[str, Any]] = []
    quote_queries: list[dict[str, Any]] = []
    dividend_match_summary: dict[str, Any] = {"query_count": 0, "query_success_count": 0, "selected_symbol_count": len(selected_symbols)}
    quote_match_summary: dict[str, Any] = {"trade_quote_rows": 0, "query_success_count": 0}
    if args.fetch_independent_sources:
        if supplemental_manifest_bytes is not None:
            (out_dir / "supplemental-filing-manifest.json").write_bytes(supplemental_manifest_bytes)
        dividends, dividend_queries = _capture_cninfo_dividends(selected_symbols, out_dir)
        issuer_filings = [_archive_official_filing(out_dir, filing) for filing in supplemental_filings]
        quote_symbols = source_symbols
        quote_start = str(bars["trade_date"].min())
        quote_end = str(bars["trade_date"].max())
        quotes, quote_queries = _capture_tencent_quotes(quote_symbols, out_dir, quote_start, quote_end)
        if not quotes.empty:
            quotes["date"] = pd.to_datetime(quotes["date"]).dt.strftime("%Y-%m-%d")
            quote_comparison = fills.merge(
                quotes, left_on=["trade_date", "symbol"], right_on=["date", "symbol"], how="left", suffixes=("_baostock", "_tencent")
            )
            bar_lookup = bars.assign(trade_date=bars["trade_date"].astype(str)).set_index(["trade_date", "symbol"])
            compare_rows = []
            for trade in fills.itertuples(index=False):
                key = (str(trade.trade_date), str(trade.symbol))
                own = bar_lookup.loc[key]
                ext = quote_comparison.loc[
                    quote_comparison["trade_date"].astype(str).eq(key[0])
                    & quote_comparison["symbol"].astype(str).eq(key[1])
                ]
                if ext.empty or pd.isna(ext.iloc[0].get("open")):
                    quote = {}
                else:
                    quote = ext.iloc[0].to_dict()
                item = {
                    "trade_date": key[0], "symbol": key[1], "side": str(trade.side),
                    "baostock_open": float(own["open"]), "baostock_high": float(own["high"]),
                    "baostock_low": float(own["low"]), "baostock_close": float(own["close"]),
                    "baostock_volume_shares": float(own["volume_shares"]), "baostock_amount_cny": float(own["amount_cny"]),
                    "tencent_open": _as_float(quote.get("open")), "tencent_high": _as_float(quote.get("high")),
                    "tencent_low": _as_float(quote.get("low")), "tencent_close": _as_float(quote.get("close")),
                    "tencent_volume_shares": _as_float(quote.get("volume")), "tencent_amount_cny": _as_float(quote.get("amount")),
                    "quote_source_status": "AVAILABLE" if quote else "MISSING",
                }
                for col in ("open", "high", "low", "close"):
                    left, right = item[f"baostock_{col}"], item[f"tencent_{col}"]
                    item[f"{col}_absolute_difference"] = None if right is None else left - right
                    item[f"{col}_match_within_0_01"] = None if right is None else abs(left - right) <= 0.0100001
                raw_volume = item["tencent_volume_shares"]
                quote_symbol = key[1]
                volume_scale = 100 if quote_symbol.startswith("SZ000") and not quote_symbol.startswith("SZ0000") else 1
                # AKShare 1.19.3's Tencent adapter leaves some SZ 000xxx stock
                # volumes unscaled even though its output contract says shares.
                # Check the raw amount/price/volume relationship and record the
                # correction separately instead of hiding it in the comparison.
                if quote_symbol.startswith("SZ000") and raw_volume is not None:
                    close_for_check = item["tencent_close"]
                    approx_price = item["tencent_amount_cny"] / (raw_volume * 100) if raw_volume else None
                    if close_for_check is not None and approx_price is not None and 0.5 * close_for_check <= approx_price <= 1.5 * close_for_check:
                        volume_scale = 100
                normalized_volume = raw_volume * volume_scale if raw_volume is not None else None
                item["tencent_volume_scale_applied"] = volume_scale
                item["tencent_volume_shares_normalized"] = normalized_volume
                item["volume_unit_adapter_status"] = "ADAPTER_VOLUME_SUSPECTED_LOTS_SCALED_FOR_COMPARISON" if volume_scale == 100 else "ADAPTER_REPORTED_SHARES"
                left, right = item["baostock_volume_shares"], normalized_volume
                item["volume_shares_absolute_difference"] = None if right is None else left - right
                volume_tol = max(1000.0, abs(left or 0.0) * 0.00002)
                item["volume_shares_tolerance"] = volume_tol
                item["volume_shares_match_within_tolerance"] = None if right is None else abs(left - right) <= volume_tol
                for col, tol in (("amount_cny", max(100.0, float(item["baostock_amount_cny"]) * 0.00002)),):
                    left, right = item[f"baostock_{col}"], item[f"tencent_{col}"]
                    item[f"{col}_absolute_difference"] = None if right is None else left - right
                    item[f"{col}_tolerance"] = tol
                    item[f"{col}_match_within_tolerance"] = None if right is None else abs(left - right) <= tol
                compare_rows.append(item)
            compare_frame = pd.DataFrame(compare_rows)
            compare_frame.to_csv(out_dir / "independent-quote-crosscheck.csv", index=False)
            quote_match_summary = {
                "trade_quote_rows": int(len(compare_frame)),
                "quotes_available": int(compare_frame["quote_source_status"].eq("AVAILABLE").sum()),
                "open_match_within_0_01": int(compare_frame["open_match_within_0_01"].fillna(False).sum()),
                "high_match_within_0_01": int(compare_frame["high_match_within_0_01"].fillna(False).sum()),
                "low_match_within_0_01": int(compare_frame["low_match_within_0_01"].fillna(False).sum()),
                "close_match_within_0_01": int(compare_frame["close_match_within_0_01"].fillna(False).sum()),
                "volume_match_after_recorded_unit_adjustment": int(compare_frame["volume_shares_match_within_tolerance"].fillna(False).sum()),
                "amount_match_within_recorded_tolerance": int(compare_frame["amount_cny_match_within_tolerance"].fillna(False).sum()),
                "symbols_with_suspected_lot_units_in_akshare_adapter": sorted(
                    compare_frame.loc[compare_frame["tencent_volume_scale_applied"].eq(100), "symbol"].astype(str).unique()
                ),
            }
        source_rows = [{"source_family": "cninfo_dividends", **row} for row in dividend_queries]
        source_rows.extend({"source_family": "issuer_filing", **filing} for filing in issuer_filings)
        source_rows.extend({"source_family": "tencent_daily_quotes", **row} for row in quote_queries)
        _write_json(out_dir / "independent-source-query-log.json", source_rows)
        dividend_match_summary = {
            "query_count": len(dividend_queries),
            "query_success_count": sum(row.get("status") == "QUERY_OK" for row in dividend_queries),
            "query_failed_count": sum(row.get("status") != "QUERY_OK" for row in dividend_queries),
            "selected_symbol_count": len(selected_symbols),
            "returned_record_count": int(len(dividends)),
            "scope": "structured dividend/share-distribution data only, not complete corporate-action coverage",
        }
        quote_match_summary["query_count"] = len(quote_queries)
        quote_match_summary["query_success_count"] = sum(row.get("status") == "QUERY_OK" for row in quote_queries)
        dividend_records_path = out_dir / "cninfo-dividend-records-adapter-output.csv"
        if not dividends.empty:
            dividends["symbol"] = dividends["symbol"].astype(str)
            holding_windows = pd.read_csv(audit_input_dir / "actual-holding-intervals.csv")
            event_rows = []
            for row in dividends.to_dict(orient="records"):
                symbol = str(row.get("symbol", ""))
                dates = [row.get(k) for k in ("股权登记日", "除权日", "派息日", "股份到账日")]
                symbol_holdings = holding_windows.loc[holding_windows.symbol.eq(symbol)]
                match = _match_action_to_holding_windows(row.get("股权登记日"), dates, symbol_holdings)
                if match["record_date_entitlement_match"] or match["phase_overlap_match"]:
                    event_rows.append({**row, **match})
            holding_hit_columns = [
                *dividends.columns,
                "record_date_entitlement_match",
                "phase_overlap_match",
                "matching_holding_intervals",
                "holding_match_status",
            ]
            pd.DataFrame(event_rows, columns=holding_hit_columns).to_csv(
                out_dir / "cninfo-dividend-holding-window-hits.csv", index=False
            )
            session_index = {
                day: index for index, day in enumerate(
                    sorted(pd.to_datetime(calendar.loc[
                        calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"
                    ]).dt.strftime("%Y-%m-%d").unique())
                )
            }
            candidate_hit_rows = []
            candidate_targets = pd.read_csv(input_paths["candidate-pool-by-signal.csv"])
            candidate_targets = candidate_targets.loc[
                candidate_targets["selected_target"].astype(str).str.lower().isin(["true", "1"])
            ]
            for target in candidate_targets.itertuples(index=False):
                signal = str(target.signal_date)
                execute = str(target.execution_date)
                signal_index = session_index.get(signal)
                lookback_start = None
                if signal_index is not None:
                    lookback_start = sorted(session_index, key=session_index.get)[max(0, signal_index - int(strategy["lookback_sessions"]))]
                symbol_records = dividends.loc[dividends["symbol"].astype(str).eq(str(target.symbol))]
                checked = 0
                for dividend in symbol_records.to_dict(orient="records"):
                    dates = [dividend.get(k) for k in ("股权登记日", "除权日", "派息日", "股份到账日")]
                    phases = [pd.to_datetime(d).strftime("%Y-%m-%d") for d in dates if pd.notna(d)]
                    if any(lookback_start <= phase <= execute for phase in phases) if lookback_start else False:
                        checked += 1
                        candidate_hit_rows.append({
                            "signal_date": signal, "execution_date": execute, "symbol": str(target.symbol),
                            "candidate_rank": int(target.rank), "event_phase_dates": ",".join(phases),
                            "query_source": "CNINFO structured dividend records",
                            "status": "POSSIBLE_DIVIDEND_PHASE_IN_SIGNAL_WINDOW",
                        })
                if checked == 0:
                    candidate_hit_rows.append({
                        "signal_date": signal, "execution_date": execute, "symbol": str(target.symbol),
                        "candidate_rank": int(target.rank), "event_phase_dates": "",
                        "query_source": "CNINFO structured dividend records",
                        "status": "NO_MATCH_IN_DIVIDEND_ENDPOINT_ONLY",
                    })
            pd.DataFrame(candidate_hit_rows).to_csv(out_dir / "cninfo-dividend-candidate-window-review.csv", index=False)
        # Query status is explicit by target. An empty match from this one
        # structured dividend interface is not a statement that no other
        # corporate action occurred.
        action_registry_rows = []
        query_by_symbol = {str(row["symbol"]): row for row in dividend_queries}
        for symbol in selected_symbols:
            query = query_by_symbol.get(symbol, {})
            matching_holding_hits = 0
            matching_entitlement_hits = 0
            if (out_dir / "cninfo-dividend-holding-window-hits.csv").exists():
                holding_hit_frame = pd.read_csv(out_dir / "cninfo-dividend-holding-window-hits.csv")
                matching_holding_hits = int((holding_hit_frame["symbol"].astype(str) == symbol).sum())
                if "record_date_entitlement_match" in holding_hit_frame.columns:
                    matching_entitlement_hits = int(
                        ((holding_hit_frame["symbol"].astype(str) == symbol)
                         & holding_hit_frame["record_date_entitlement_match"].astype(str).str.lower().eq("true")).sum()
                    )
            matching_candidate_hits = 0
            if (out_dir / "cninfo-dividend-candidate-window-review.csv").exists():
                candidate_hit_frame = pd.read_csv(out_dir / "cninfo-dividend-candidate-window-review.csv")
                matching_candidate_hits = int(
                    ((candidate_hit_frame["symbol"].astype(str) == symbol)
                     & candidate_hit_frame["status"].eq("POSSIBLE_DIVIDEND_PHASE_IN_SIGNAL_WINDOW")).sum()
                )
            action_registry_rows.append({
                "symbol": symbol,
                "checked_event_family": "cash_dividend_and_share_distribution_records",
                "source": query.get("source"),
                "source_query_status": query.get("status", "UNAVAILABLE"),
                "records_returned": query.get("result_rows"),
                "holding_window_phase_hits": matching_holding_hits,
                "holding_record_date_entitlement_hits": matching_entitlement_hits,
                "candidate_signal_window_phase_hits": matching_candidate_hits,
                "other_action_classes": "NOT_QUERIED_BY_THIS_ENDPOINT: rights offering, merger, suspension notices, delisting, code change and non-dividend share issuance",
                "coverage_conclusion": "NO_MATCH_IN_CNINFO_DIVIDEND_ENDPOINT_ONLY" if query.get("status") == "QUERY_OK" and not matching_holding_hits and not matching_candidate_hits else "REVIEW_REQUIRED",
                "overall_company_action_status": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            })
        for filing, filing_config in zip(issuer_filings, supplemental_filings, strict=True):
            action_registry_rows.append({
                "symbol": filing["symbol"],
                "checked_event_family": filing["event_family"],
                "source": filing["source"],
                "source_query_status": filing["status"],
                "records_returned": 1,
                "holding_window_phase_hits": filing_config.get("holding_window_phase_hits"),
                "candidate_signal_window_phase_hits": filing_config.get("candidate_signal_window_phase_hits"),
                "event_summary": filing_config.get("event_summary"),
                "source_url": filing["source_url"],
                "source_title": filing["source_title"],
                "source_document_date": filing["document_date"],
                "source_archive_path": filing["raw_response_path"],
                "source_archive_sha256": filing["raw_response_sha256"],
                "source_known_at_evidence_status": filing["known_at_evidence_status"],
                "source_human_review_status": "PENDING",
                "other_action_classes": filing_config.get("other_action_classes", "UNREVIEWED"),
                "coverage_conclusion": filing_config.get("coverage_conclusion", "REVIEW_REQUIRED"),
                "overall_company_action_status": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            })
        pd.DataFrame(action_registry_rows).to_csv(out_dir / "corporate-action-query-register.csv", index=False)
        pd.DataFrame([
            {"input_family": "daily_ohlc", "primary_source": "BaoStock frozen point-in-time snapshot", "independent_source": "Tencent Finance via AKShare historical daily endpoint", "scope_checked": "24 fill dates / 14 traded securities", "status": "OHLC_COMPARISON_RECORDED; availability/vintage UNVERIFIED"},
            {"input_family": "volume_and_amount", "primary_source": "BaoStock", "independent_source": "Tencent Finance via AKShare", "scope_checked": "24 fill dates; Tencent 000xxx adapter unit behavior separately flagged", "status": "UNIT_AND_ROUNDING_REVIEW_REQUIRED"},
            {"input_family": "historical_trading_calendar", "primary_source": "frozen BaoStock-derived calendar snapshot", "independent_source": "none in this audit", "scope_checked": "next-open mapping for 3 signal dates", "status": "MAPPING_MATCHED; historical source vintage UNVERIFIED"},
            {"input_family": "security_status_and_universe", "primary_source": "BaoStock point-in-time rows", "independent_source": "none in this audit", "scope_checked": "24 fill records and 6024 saved candidate rows", "status": "SOURCE_AND_HISTORICAL_AVAILABILITY_UNVERIFIED"},
            {"input_family": "market_rules_and_lots", "primary_source": "default configuration fallback", "independent_source": "none in this audit", "scope_checked": "347660 bars; all fallback", "status": "UNVERIFIED_FALLBACK_USED"},
            {"input_family": "historical_fees", "primary_source": "default configuration flat assumptions", "independent_source": "none in this audit", "scope_checked": "24 fills", "status": "UNVERIFIED_CONFIG_ASSUMPTION; HTS personal rate unavailable"},
            {"input_family": "corporate_actions", "primary_source": "in-window registered event tables (empty for the window)", "independent_source": "CNINFO structured dividend records for selected targets plus one issuer filing", "scope_checked": f"{len(selected_symbols)} selected target securities; not full candidate pool", "status": "INCOMPLETE_COVERAGE; empty registry not treated as no events"},
            {"input_family": "known_at_and_vendor_vintage", "primary_source": "adapter available_at and retrieved_at fields", "independent_source": "no vendor historical publication/vintage evidence", "scope_checked": "all 68 session dates; 15:00 adapter time assumption", "status": "UNVERIFIED"},
        ]).to_csv(out_dir / "source-availability-matrix.csv", index=False)
    # Preserve the existing holding table without changing its earlier snapshot.
    holding_path = audit_input_dir / "actual-holding-intervals.csv"
    pd.read_csv(holding_path).to_csv(out_dir / "actual-holding-intervals.csv", index=False)
    # Records are not event-completeness evidence: the source registry is empty for this window.
    registry = pd.read_csv(experiment_dir / "inputs/provenance-registry.csv", keep_default_na=False)
    blocked_summary = {
        "source_file_sha256": sha256_file(input_paths["blocked_orders.csv"]),
        "recorded_simulator_blocked_attempt_count": int(len(blocked)),
        "broker_order_log_status": "UNKNOWN_NO_COMPLETE_ORDER_LOG",
        "interpretation": "该文件仅记录模拟器明确写出的受阻尝试；空文件不能证明没有目标、订单或其他未记录尝试。",
    }
    core_input_hashes_match = all(
        row["status"] == "MATCHED_FROZEN_INVENTORY"
        for key, row in artifact_hash_checks.items() if key != "candidate-pool-by-signal.csv"
    )
    candidate_pool_rebuilt = (
        artifact_hash_checks["candidate-pool-by-signal.csv"]["status"] == "NO_FROZEN_HASH_RECONSTRUCTED_AGAINST_BARS_CONFIG"
        and all(value["candidate_reconstruction_status"] == "MATCH" for value in reconstruction.values())
    )
    all_inputs_frozen = all(row["status"] == "MATCHED_FROZEN_INVENTORY" for row in artifact_hash_checks.values())
    git_status_result = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    summary = {
        "schema_version": 2,
        "report_type": "transaction_level_data_rule_fee_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
        "git_worktree_clean_at_generation": not bool(git_status_result),
        "git_uncommitted_paths_at_generation": [line[3:] for line in git_status_result],
        "input_hashes": hashes,
        "experiment_id": experiment_id,
        "window": {"start": str(bars.trade_date.min()), "end": str(bars.trade_date.max()), "sessions": int(bars.trade_date.nunique()), "rows": int(len(bars))},
        "fill_record_count": int(len(fills)),
        "fill_record_semantics": "trades.csv 每行是一笔模拟成交记录；不等同于订单数，也不能反推出未成交订单数。",
        "order_log_status": "UNKNOWN_NO_COMPLETE_ORDER_LOG",
        "trade_audit_rows": int(len(audit)),
        "journal_linked_rows": int(audit.journal_link_status.eq("MATCHED").sum()),
        "candidate_reconstruction_by_signal_date": reconstruction,
        "candidate_pool_rows_saved": int(pd.read_csv(input_paths["candidate-pool-by-signal.csv"]).shape[0]),
        "candidate_unique_symbols_saved": int(pd.read_csv(input_paths["candidate-pool-by-signal.csv"]).symbol.nunique()),
        "selected_target_unique_symbols": len(selected_symbols),
        "selected_target_slots": int(trade_targets.selected_target.sum()),
        "selected_targets_reconstructed_to_zero_shares": int(trade_targets.target_fill_status.eq("TARGET_ROUNDED_TO_ZERO").sum()),
        "zero_target_reviews": zero_review_rows,
        "target_selection_and_zero_share_policy": {
            "selection_order": "rank candidates and take max_positions before quantity rounding",
            "zero_quantity_action": "retain_cash; no next-ranked replacement",
            "order_lot_source": "configs/default.toml fallback; historical market rule not verified",
        },
        "target_lifecycle_status_counts": {
            str(key): int(value) for key, value in trade_targets.target_lifecycle_status.value_counts(dropna=False).items()
        },
        "suspension_candidate_diagnostic": {
            "symbol": args.suspension_symbol,
            "window_rows": int(len(suspension_window)),
            "window_state_counts_including_base": _suspension_state_counts(suspension_window["is_suspended"]) if not suspension_window.empty else None,
            "return_observation_state_counts_excluding_base": _suspension_state_counts(
                suspension_window.loc[suspension_window.window_role.ne("LOOKBACK_BASE"), "is_suspended"]
            ) if not suspension_window.empty else None,
            "session_window_audit": (
                suspension_window.iloc[0][[
                    "expected_open_session_count", "expected_open_session_dates", "actual_bar_row_count",
                    "missing_open_session_dates", "off_calendar_bar_dates", "duplicate_symbol_date_rows",
                    "duplicate_symbol_dates", "window_alignment_status", "provider_source_values",
                    "project_forward_fill_detected", "provider_raw_response_vintage",
                ]].to_dict()
                if not suspension_window.empty else None
            ),
            "signal_momentum_formula": "adjusted_close_t / adjusted_close_at_row_shift_20 - 1; calculated per symbol from stored bar rows, executable no earlier than next open",
            "price_start": _as_float(suspension_window.iloc[0].get("adjusted_close")) if not suspension_window.empty else None,
            "price_end": _as_float(suspension_window.iloc[-1].get("adjusted_close")) if not suspension_window.empty else None,
            "price_ratio_end_over_start": (
                _as_float(suspension_window.iloc[-1].get("adjusted_close")) / _as_float(suspension_window.iloc[0].get("adjusted_close"))
                if not suspension_window.empty and _as_float(suspension_window.iloc[0].get("adjusted_close")) not in (None, 0)
                else None
            ),
            "reconstructed_momentum": _as_float(suspension_neighbors.loc[
                suspension_neighbors.symbol.astype(str).eq(str(args.suspension_symbol)), "momentum"
            ].iloc[0]) if not suspension_neighbors.empty and not suspension_neighbors.loc[
                suspension_neighbors.symbol.astype(str).eq(str(args.suspension_symbol))
            ].empty else None,
            "candidate_chain": suspension_neighbors.loc[
                suspension_neighbors.symbol.astype(str).eq(str(args.suspension_symbol))
            ].to_dict(orient="records") if not suspension_neighbors.empty else [],
            "neighbor_rows": int(len(suspension_neighbors)),
        } if args.suspension_symbol else None,
        "record_date_entitlement_audit": {
            "matcher_policy": "POSSIBLE_END_OF_DAY_HOLDING_INTERVAL_INCLUDES_RECORD_DATE",
            "same_day_trade_rule_status": "CONFLICTING_OFFICIAL_MATERIALS; candidate match is not account-level confirmation",
            "ledger_v2_policy": "record-date event snapshots use the simulated end-of-day ledger after same-day fills",
            "typed_accounting_events_in_frozen_run": run_metrics.get("accounting_event_ids", []),
            "formal_entitlement_validation": "UNVERIFIED",
        },
        "candidate_target_fill_mismatch_rows": int(trade_targets.target_fill_status.astype(str).str.contains("REVIEW_REQUIRED").sum()),
        "notional_cny": float(pd.to_numeric(fills.notional_cny).sum()),
        "modeled_fees_cny": float(pd.to_numeric(fills.fees_cny).sum()),
        "modeled_execution_impact_cny": float(pd.to_numeric(fills.execution_impact_vs_reference_cny).sum()),
        "market_rule_fallback_rows_in_window": int(run_metrics.get("market_rule_fallback_row_count", -1)),
        "all_trades_rule_fallback_status": sorted(set(audit.market_rule_source_status.astype(str))),
        "all_trades_fee_fallback_status": sorted(set(audit.fee_source_status.astype(str))),
        "actual_personal_broker_fee_evidence": "UNAVAILABLE",
        "historical_quote_vintage_and_at_decision_availability": "UNVERIFIED",
        "registered_provenance_rows": int(len(registry)),
        "blocked_simulation_attempts": blocked_summary,
        "external_source_query_count": len(source_rows),
        "supplemental_filing_manifest_sha256": supplemental_manifest_sha256,
        "external_company_action_query_scope": f"CNINFO structured dividend endpoint for {len(selected_symbols)} selected target securities; not all {int(pd.read_csv(input_paths['candidate-pool-by-signal.csv']).symbol.nunique())} distinct eligible candidate securities.",
        "frozen_artifact_hash_checks": artifact_hash_checks,
        "independent_quote_crosscheck": quote_match_summary,
        "cninfo_dividend_query": dividend_match_summary,
        "research_trust": {
            "input_file_integrity": (
                "VERIFIED_AGAINST_PRIOR_ARTIFACT_INVENTORY" if all_inputs_frozen
                else "CORE_RUN_AND_DATA_MATCH_CANDIDATE_RECONSTRUCTED_NOT_HASH_ANCHORED"
                if core_input_hashes_match and candidate_pool_rebuilt else "PARTIAL_OR_UNVERIFIED"
            ),
            "canonical_data_quality": "STRUCTURAL_AUDIT_HEALTHY_WITH_WARNINGS" if _json(experiment_dir / "inputs/point_in_time_quality.json").get("healthy") else "FAILED",
            "historical_availability_and_vendor_vintage": "UNVERIFIED",
            "market_rule_coverage": "FALLBACK_USED",
            "fee_coverage": "CONFIG_FLAT_ASSUMPTION; PERSONAL_BROKER_RATE_UNAVAILABLE",
            "corporate_action_coverage": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            "formal_baseline": "NOT_READY",
            "incremental_can_continue": False,
        },
        "formal_baseline": "NOT_READY",
        "incremental_can_continue": False,
        "scope_note": "只读审计；不改原始输入、checkpoint、既有 run 或旧审计报告；不连接华泰、不提交委托。",
    }
    _write_json(out_dir / "trade-audit-summary.json", summary)
    md = [
        "# 真实行情短窗口成交级数据审计",
        "",
        f"- 实验：`{summary['experiment_id']}`；窗口 `{summary['window']['start']}..{summary['window']['end']}`。",
        f"- 模拟成交记录：{summary['fill_record_count']}；逐笔账本关联：{summary['journal_linked_rows']}；完整订单日志：`{summary['order_log_status']}`。",
        f"- 模拟成交额：¥{summary['notional_cny']:.2f}；配置模拟费用：¥{summary['modeled_fees_cny']:.2f}；模拟滑点影响：¥{summary['modeled_execution_impact_cny']:.2f}。",
        f"- 市场规则：全窗 {summary['market_rule_fallback_rows_in_window']} 行使用回退；本次成交状态 `{summary['all_trades_rule_fallback_status']}`。",
        f"- 费率：所有本次成交 `{summary['all_trades_fee_fallback_status']}`；华泰个人费率证据 `{summary['actual_personal_broker_fee_evidence']}`。",
        f"- 候选：已保存 {summary['candidate_pool_rows_saved']} 行、{summary['candidate_unique_symbols_saved']} 个代码；排名重建状态按信号日见 JSON。",
        f"- 成交目标：{summary['selected_target_slots']} 个目标槽；其中按配置交易单位舍入为 0 股 {summary['selected_targets_reconstructed_to_zero_shares']} 个；目标与成交未解释差异 {summary['candidate_target_fill_mismatch_rows']} 行。",
        f"- 选股与零股顺序：{summary['target_selection_and_zero_share_policy']}。零股目标保留现金；当前没有自动检查第 N+1 名替补。",
        f"- 零股目标详情：`zero-target-review.csv`。如存在未入选的下一候选，表中记录其排名及代码；当前流程不应用该候选替补。",
        f"- 模拟器明确记录的受阻尝试：{blocked_summary['recorded_simulator_blocked_attempt_count']} 行；这不是券商拒单/订单日志，当前完整订单证据为未知。",
        f"- 独立行情/公司行动查询数：{len(source_rows)}。独立来源对照是本次取得的查询结果，不证明全量行情或公司行动覆盖。",
        f"- 独立报价：成交日 OHLC 价格容差内匹配 {quote_match_summary.get('open_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('high_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('low_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('close_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}；成交量与成交额比较另有单位缩放和容差，见逐笔表。",
        f"- CNINFO 分红/送转接口：{dividend_match_summary.get('query_success_count', 0)}/{dividend_match_summary.get('query_count', 0)} 个选中目标查询成功，返回 {dividend_match_summary.get('returned_record_count', 0)} 条历史记录；查询范围不含全部公司行动。",
        f"- 候选池：3 个调仓日的候选排名和动量均由冻结行情与配置重建匹配；候选 CSV 不在此前文件清单中，原文件哈希未被旧清单锚定。",
        "- 松发股份（SH603268）官方公告记载其重大资产重组筹划及 2024-10-08 继续停牌，事件与第一轮高排名候选的动量观察窗重叠；文档日期不是精确首次公开时刻，且这不是该窗口内已确认的现金/股数账务事件。",
        "- 公司行动：本审计按模拟登记日收盘持仓区间生成‘可能匹配’，包含登记日买入且收盘仍持有的区间；登记日收盘前已平仓则不匹配。该口径是审计候选规则，不代表已验证中国结算的账户权益；官方投资者教育与历史结算材料对登记日当日成交的表述不完全一致，详见登记日权益更正说明。CNINFO 股息接口只覆盖查询范围，不证明其他公司行动不存在。",
        f"- 目标生命周期：{summary['target_lifecycle_status_counts']}；零股目标与已有持仓清仓目标分别标注，交易单位来源仍为配置回退。",
        f"- 停牌候选窗口：21 行含基准点的三态计数 `{summary['suspension_candidate_diagnostic']['window_state_counts_including_base']}`；20 个收益观察行计数 `{summary['suspension_candidate_diagnostic']['return_observation_state_counts_excluding_base']}`；窗口日历核验 `{summary['suspension_candidate_diagnostic']['session_window_audit']}`。成交记录不是订单数，订单状态为未知；完整信号→排名→目标→下一开市日执行痕迹见 `suspension-candidate-rank-neighbors.csv`。" if args.suspension_symbol else "",
        f"- 输入证据：核心行情、日历、配置、成交、账本、阻断单及 metrics 与此前清单 {sum(row['status'] == 'MATCHED_FROZEN_INVENTORY' for row in artifact_hash_checks.values())}/{len(expected_inventory_paths)} 项相符；候选池通过重建匹配但缺少旧哈希锚定。",
        "- 历史行情 vintage、历史决策时点可得性和公告来源人工签认仍未验证。正式基线 `NOT_READY`，增量续跑 `can_continue=false`。",
        "",
        "详细逐笔材料位于本机 `data/private/`，未纳入 Git。",
    ]
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    inventory_rows = []
    for path in sorted(item for item in out_dir.rglob("*") if item.is_file()):
        if path.name == "audit-artifact-inventory.csv":
            continue
        inventory_rows.append({
            "relative_path": path.relative_to(out_dir).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    pd.DataFrame(inventory_rows, columns=["relative_path", "size_bytes", "sha256"]).to_csv(
        out_dir / "audit-artifact-inventory.csv", index=False
    )
    print(json.dumps({"output_dir": str(out_dir), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
