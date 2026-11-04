from __future__ import annotations

import runpy
from pathlib import Path

import pandas as pd

_match_action_to_holding_windows = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_match_action_to_holding_windows"]
_reconstruct_targets = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_reconstruct_targets"]
_suspension_candidate_audit = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_suspension_candidate_audit"]
_suspension_state_counts = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_suspension_state_counts"]
_session_window_audit = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_session_window_audit"]


def _holding(*, start: str, through: str, exit_day: str | None = None, closure: str = "OPEN_AT_WINDOW_END") -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": "SZ000001",
        "holding_start_trade_date_at_open": start,
        "held_at_end_of_day_through": through,
        "holding_end_trade_date_before_open": exit_day,
        "closure": closure,
    }])


def test_action_audit_does_not_keep_record_date_entitlement_after_record_date_sale():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04", "2024-12-05"],
        _holding(start="2024-11-06", through="2024-12-03", exit_day="2024-12-04", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is False
    assert result["phase_overlap_match"] is False
    assert result["holding_match_status"] == "NO_MATCH_IN_RECORDED_HOLDING_INTERVALS"


def test_action_audit_grants_record_date_entitlement_for_same_day_purchase():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04"],
        _holding(start="2024-12-04", through="2024-12-04"),
    )
    assert result["record_date_entitlement_match"] is True
    assert result["phase_overlap_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_RECORD_DATE_ENTITLEMENT"


def test_action_audit_uses_end_of_day_interval_for_partial_sale_and_opening_holding():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04", "2024-12-05"],
        _holding(start="2024-11-06", through="2024-12-04", exit_day="2024-12-05", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_RECORD_DATE_ENTITLEMENT"


def test_action_audit_does_not_match_position_fully_sold_before_record_date():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04"],
        _holding(start="2024-11-06", through="2024-12-03", exit_day="2024-12-04", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is False


def test_action_audit_supports_multiple_lots_or_fills_around_record_date():
    windows = pd.concat([
        _holding(start="2024-11-06", through="2024-12-04"),
        _holding(start="2024-12-04", through="2024-12-04"),
    ], ignore_index=True)
    result = _match_action_to_holding_windows("2024-12-04", [], windows)
    assert result["record_date_entitlement_match"] is True
    assert len(result["matching_holding_intervals"]) == 2


def test_action_phase_after_record_date_remains_linked_to_eligible_holding():
    result = _match_action_to_holding_windows(
        "2024-12-03",
        ["2024-12-03", "2024-12-04", "2024-12-05"],
        _holding(start="2024-11-06", through="2024-12-03", exit_day="2024-12-04", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_RECORD_DATE_ENTITLEMENT"


def test_target_audit_distinguishes_zero_share_noop_from_zero_target_liquidation():
    bars = pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SZ000001", "open": 10.0},
        {"trade_date": "2025-01-03", "symbol": "SZ000001", "open": 10.0},
        {"trade_date": "2025-01-04", "symbol": "SZ000001", "open": 20.0},
    ])
    calendar = pd.DataFrame({"trade_date": ["2025-01-02", "2025-01-03", "2025-01-04"], "is_open": [True] * 3})
    candidates = pd.DataFrame([
        {"signal_date": "2025-01-02", "symbol": "SZ000001", "rank": 1, "momentum": 1.0, "selected_target": True},
        {"signal_date": "2025-01-03", "symbol": "SZ000001", "rank": 1, "momentum": 1.0, "selected_target": True},
    ])
    trades = pd.DataFrame([
        {"trade_date": "2025-01-03", "symbol": "SZ000001", "side": "BUY", "quantity": 100, "notional_cny": 1000.0, "fees_cny": 0.0},
        {"trade_date": "2025-01-04", "symbol": "SZ000001", "side": "SELL", "quantity": 100, "notional_cny": 2000.0, "fees_cny": 0.0},
    ])
    target_rows = _reconstruct_targets(
        trades, candidates, bars, calendar,
        {"max_positions": 1, "max_position_weight": 0.1, "max_total_exposure": 0.1},
        {"buy_lot_shares": 100, "sell_lot_shares": 100},
        {"slippage_bps": 0},
        10_000.0,
    )
    exit_row = target_rows.loc[target_rows.execution_date.eq("2025-01-04")].iloc[0]
    assert exit_row.raw_target_shares_before_rounding == 55
    assert exit_row.target_shares_after_100_share_fallback == 0
    assert exit_row.expected_order_side == "SELL"
    assert exit_row.expected_order_quantity_after_fallback == 100
    assert exit_row.target_fill_status == "ZERO_TARGET_EXIT_MATCHED"
    assert exit_row.target_lifecycle_status == "ZERO_TARGET_REQUIRES_EXIT"


def test_suspension_counts_keep_unknown_separate_and_reconcile():
    counts = _suspension_state_counts(pd.Series([True, False, pd.NA, "unknown", "是", "否"]))
    assert counts == {
        "total_rows": 6,
        "suspended_rows": 2,
        "not_suspended_rows": 2,
        "unknown_rows": 2,
    }
    assert counts["suspended_rows"] + counts["not_suspended_rows"] + counts["unknown_rows"] == counts["total_rows"]


def test_session_window_audit_detects_missing_duplicate_and_off_calendar_rows():
    history = pd.DataFrame({
        "trade_date": ["2024-01-01", "2024-01-02", "2024-01-02", "2024-01-04"],
        "data_source": ["BaoStock"] * 4,
    })
    window = history.iloc[[0, 2, 3]].reset_index(drop=True)
    result = _session_window_audit(
        window,
        ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        "2024-01-04",
        2,
        history,
    )
    assert result["expected_open_session_dates"] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert result["missing_open_session_dates"] == ["2024-01-03"]
    assert result["duplicate_symbol_date_rows"] == 1
    assert result["window_alignment_status"] == "MISMATCH_ROW_WINDOW_VS_OPEN_SESSIONS"
    assert result["provider_source_values"] == ["BaoStock"]
    assert result["project_forward_fill_detected"] is False


def test_suspension_candidate_reports_fills_and_blocked_attempts_without_claiming_order_count():
    dates = pd.date_range("2024-01-01", periods=22, freq="D").strftime("%Y-%m-%d").tolist()
    bars = pd.DataFrame([{
        "trade_date": day,
        "symbol": "SH603268",
        "adjusted_close": 10.0 + index,
        "open": 20.0,
        "is_suspended": pd.NA if index == 2 else index in {0, 1, 3},
        "volume_shares": 0 if index in {0, 1, 3} else 100,
        "data_source": "BaoStock",
    } for index, day in enumerate(dates)])
    ranked = pd.DataFrame([{
        "signal_date": dates[20], "symbol": "SH603268", "rank": 1, "momentum": 2.0,
    }])
    fills = pd.DataFrame([
        {"trade_date": dates[21], "symbol": "SH603268", "side": "BUY", "quantity": 100},
        {"trade_date": dates[21], "symbol": "SH603268", "side": "BUY", "quantity": 100},
    ])
    blocked = pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity", "reason"])
    target = pd.DataFrame()
    calendar = pd.DataFrame({"trade_date": dates, "is_open": [True] * len(dates)})
    window, neighbors = _suspension_candidate_audit(
        bars, ranked, fills, blocked, target, calendar,
        {"lookback_sessions": 20, "max_positions": 5}, "SH603268",
    )
    assert len(window) == 21
    assert _suspension_state_counts(window.is_suspended) == {
        "total_rows": 21, "suspended_rows": 3, "not_suspended_rows": 17, "unknown_rows": 1,
    }
    candidate = neighbors.iloc[0]
    assert candidate.execution_fill_record_count == 2
    assert candidate.execution_fill_sides == "BUY,BUY"
    assert candidate.execution_blocked_simulation_attempt_count == 0
    assert candidate.order_evidence_status == "UNKNOWN_NO_COMPLETE_ORDER_LOG"


def test_suspension_candidate_with_no_fill_and_blocked_attempt_keeps_order_status_unknown():
    dates = pd.date_range("2024-01-01", periods=22, freq="D").strftime("%Y-%m-%d").tolist()
    bars = pd.DataFrame([{
        "trade_date": day, "symbol": "SH603268", "adjusted_close": 10 + index,
        "open": 20.0, "is_suspended": False, "volume_shares": 100, "data_source": "BaoStock",
    } for index, day in enumerate(dates)])
    ranked = pd.DataFrame([{"signal_date": dates[20], "symbol": "SH603268", "rank": 1, "momentum": 2.0}])
    blocked = pd.DataFrame([{
        "trade_date": dates[21], "symbol": "SH603268", "side": "BUY", "quantity": 0, "reason": "below lot",
    }])
    _, neighbors = _suspension_candidate_audit(
        bars, ranked, pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity"]), blocked,
        pd.DataFrame(), pd.DataFrame({"trade_date": dates, "is_open": [True] * len(dates)}),
        {"lookback_sessions": 20, "max_positions": 5}, "SH603268",
    )
    candidate = neighbors.iloc[0]
    assert candidate.execution_fill_record_count == 0
    assert candidate.execution_blocked_simulation_attempt_count == 1
    assert candidate.execution_blocked_simulation_attempt_reasons == "below lot"
    assert candidate.order_evidence_status == "UNKNOWN_NO_COMPLETE_ORDER_LOG"


def test_future_rows_do_not_change_past_momentum_or_ranking():
    from cehuan.signals.strategy import add_momentum, rank_precomputed_day

    signal_dates = pd.date_range("2024-01-01", periods=21, freq="D").strftime("%Y-%m-%d").tolist()
    rows = []
    for symbol, multiplier in (("SH600001", 2.0), ("SZ000001", 1.5)):
        for index, day in enumerate(signal_dates):
            rows.append({
                "trade_date": day,
                "symbol": symbol,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "adjusted_close": 10.0 * (multiplier if index == 20 else 1.0),
                "volume_shares": 1000,
                "is_listed": True,
                "is_suspended": False,
                "is_st": False,
            })
    original = pd.DataFrame(rows)
    future = pd.DataFrame([
        {**row, "trade_date": "2024-01-22", "adjusted_close": 99999.0, "close": 99999.0}
        for row in rows[-2:]
    ])
    changed_future = future.copy()
    changed_future.loc[:, "adjusted_close"] = [0.01, 500000.0]
    changed_future.loc[:, "close"] = changed_future["adjusted_close"]
    strategy = {"min_momentum": 0.0}
    market = {"require_point_in_time_status": True, "exclude_st": True}

    def ranked_at_signal(frame: pd.DataFrame) -> pd.DataFrame:
        calculated = add_momentum(frame, 20)
        day = calculated.loc[calculated.trade_date.astype(str).eq(signal_dates[-1])]
        return rank_precomputed_day(day, strategy, market, require_order_rules=False)[["symbol", "momentum", "rank"]]

    baseline = ranked_at_signal(original)
    with_future = ranked_at_signal(pd.concat([original, future], ignore_index=True))
    with_changed_future = ranked_at_signal(pd.concat([original, changed_future], ignore_index=True))
    pd.testing.assert_frame_equal(baseline.reset_index(drop=True), with_future.reset_index(drop=True))
    pd.testing.assert_frame_equal(baseline.reset_index(drop=True), with_changed_future.reset_index(drop=True))
