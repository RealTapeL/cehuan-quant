from __future__ import annotations

import runpy
from pathlib import Path

import pandas as pd

_match_action_to_holding_windows = runpy.run_path(
    Path(__file__).parents[1] / "scripts/audit_real_trade_trace.py"
)["_match_action_to_holding_windows"]


def _holding(*, start: str, through: str, exit_day: str | None = None, closure: str = "OPEN_AT_WINDOW_END") -> pd.DataFrame:
    return pd.DataFrame([{
        "symbol": "SZ000001",
        "holding_start_trade_date_at_open": start,
        "held_at_end_of_day_through": through,
        "holding_end_trade_date_before_open": exit_day,
        "closure": closure,
    }])


def test_action_audit_keeps_record_date_entitlement_when_sold_at_record_date_open():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04", "2024-12-05"],
        _holding(start="2024-11-06", through="2024-12-03", exit_day="2024-12-04", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_RECORD_DATE_ENTITLEMENT"


def test_action_audit_does_not_grant_record_date_rights_for_same_day_purchase():
    result = _match_action_to_holding_windows(
        "2024-12-04",
        ["2024-12-04"],
        _holding(start="2024-12-04", through="2024-12-04"),
    )
    assert result["record_date_entitlement_match"] is False
    assert result["phase_overlap_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_EVENT_PHASE_OVERLAP"


def test_action_phase_after_record_date_remains_linked_to_eligible_holding():
    result = _match_action_to_holding_windows(
        "2024-12-03",
        ["2024-12-03", "2024-12-04", "2024-12-05"],
        _holding(start="2024-11-06", through="2024-12-03", exit_day="2024-12-04", closure="SOLD_AT_OPEN"),
    )
    assert result["record_date_entitlement_match"] is True
    assert result["holding_match_status"] == "POSSIBLE_RECORD_DATE_ENTITLEMENT"
