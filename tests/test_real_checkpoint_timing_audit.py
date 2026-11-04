from __future__ import annotations

from datetime import date
from pathlib import Path
import runpy

import pandas as pd
import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_real_checkpoint_timing.py"
_AUDIT = runpy.run_path(str(_SCRIPT), run_name="real_checkpoint_timing_test")
_position_intervals = _AUDIT["_position_intervals"]
_next_open_after = _AUDIT["_next_open_after"]


def test_sold_at_open_is_not_held_through_record_date_close() -> None:
    trades = pd.DataFrame([
        {"symbol": "SH600001", "trade_date": "2025-01-03", "side": "BUY", "quantity": 100, "_row_order": 0},
        {"symbol": "SH600001", "trade_date": "2025-01-06", "side": "SELL", "quantity": 100, "_row_order": 1},
    ])

    intervals = _position_intervals(trades, [date(2025, 1, 3), date(2025, 1, 6)], date(2025, 1, 6))

    assert intervals == [{
        "symbol": "SH600001",
        "holding_start_trade_date_at_open": "2025-01-03",
        "holding_end_trade_date_before_open": "2025-01-06",
        "held_at_end_of_day_through": "2025-01-03",
        "closure": "SOLD_AT_OPEN",
        "remaining_shares": 0,
    }]


def test_partial_sale_keeps_remaining_position_open() -> None:
    trades = pd.DataFrame([
        {"symbol": "SH600001", "trade_date": "2025-01-03", "side": "BUY", "quantity": 300, "_row_order": 0},
        {"symbol": "SH600001", "trade_date": "2025-01-06", "side": "SELL", "quantity": 100, "_row_order": 1},
    ])

    intervals = _position_intervals(trades, [date(2025, 1, 3), date(2025, 1, 6)], date(2025, 1, 6))

    assert intervals == [{
        "symbol": "SH600001",
        "holding_start_trade_date_at_open": "2025-01-03",
        "holding_end_trade_date_before_open": None,
        "held_at_end_of_day_through": "2025-01-06",
        "closure": "OPEN_AT_WINDOW_END",
        "remaining_shares": 200,
    }]


def test_next_open_session_is_exact_not_lexical() -> None:
    calendar = pd.DataFrame({
        "trade_date": ["2025-01-10", "2025-01-11", "2025-01-12", "2025-01-13", "2025-01-14"],
        "is_open": ["true", "false", "false", "true", "true"],
    })

    assert _next_open_after(calendar, date(2025, 1, 10)) == date(2025, 1, 13)
    assert _next_open_after(calendar, date(2025, 1, 13)) == date(2025, 1, 14)


def test_next_open_session_requires_explicit_calendar_state() -> None:
    calendar = pd.DataFrame({"trade_date": ["2025-01-13"], "is_open": [""]})

    with pytest.raises(ValueError, match="is_open 必须明确"):
        _next_open_after(calendar, date(2025, 1, 10))


def test_next_open_session_rejects_duplicate_dates() -> None:
    calendar = pd.DataFrame({
        "trade_date": ["2025-01-13", "2025-01-13"],
        "is_open": ["true", "false"],
    })

    with pytest.raises(ValueError, match="重复日期"):
        _next_open_after(calendar, date(2025, 1, 10))
