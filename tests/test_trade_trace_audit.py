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
