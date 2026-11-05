from __future__ import annotations

import pandas as pd

from cehuan.data.calendar import audit_security_windows


def test_security_window_audit_does_not_hide_one_symbol_gap_with_other_symbol_rows() -> None:
    days = pd.bdate_range("2025-01-06", periods=6).strftime("%Y-%m-%d").tolist()
    calendar = pd.DataFrame({"trade_date": days, "is_open": True, "source": "synthetic-calendar"})
    rows = []
    for symbol in ("SH600001", "SZ000001", "SH600002"):
        for index, day in enumerate(days):
            if symbol == "SH600001" and index == 2:
                continue
            rows.append({
                "trade_date": day, "symbol": symbol,
                "open": 10.0 + index, "high": 10.0 + index,
                "low": 10.0 + index, "close": 10.0 + index,
                "adjusted_close": 10.0 + index, "volume_shares": 1000,
                "is_listed": True, "is_suspended": False if symbol != "SZ000001" else index == 3,
                "is_st": False,
            })
    bars = pd.DataFrame(rows)
    bars["is_suspended"] = bars["is_suspended"].astype("boolean")
    bars.loc[(bars.symbol == "SH600002") & (bars.trade_date == days[3]), "is_suspended"] = pd.NA

    report = audit_security_windows(
        bars, calendar, ["SH600001", "SZ000001", "SH600002"],
        signal_date=days[5], lookback_sessions=3,
    ).set_index("symbol")
    assert report.loc["SH600001", "window_status"] == "MISSING_BAR"
    assert days[2] in report.loc["SH600001", "missing_sessions"]
    assert days[2] in report.loc["SH600001", "unknown_status_sessions"]
    assert report.loc["SZ000001", "window_status"] == "COMPLETE_WITH_SUSPENSION"
    assert report.loc["SH600002", "window_status"] == "UNKNOWN_STATUS"
