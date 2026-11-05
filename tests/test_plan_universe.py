from __future__ import annotations

import pandas as pd
import pytest

from cehuan.config import load_config
from cehuan.research.synthetic_cycle import synthetic_cycle_bars
from cehuan.signals.plan import _confirmed_stock_members, generate_plan


def _universe(signal_date: str, rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame([
        {"trade_date": signal_date, "symbol": symbol, "pool_status": status}
        for symbol, status in rows
    ])


def test_new_buy_candidates_are_limited_to_same_day_stock_members_and_outside_holdings_remain_reviewable():
    bars = synthetic_cycle_bars()
    signal_date = str(bars.trade_date.max())
    universe = _universe(signal_date, [("SZ000002", "STOCK_MEMBER")])
    config = load_config()
    config["strategy"].update({
        "lookback_sessions": 5,
        "max_positions": 1,
        "min_momentum": -1.0,
        "max_position_weight": 0.2,
        "max_total_exposure": 0.2,
    })
    positions = pd.DataFrame([{
        "symbol": "SZ000001", "shares": 1000, "available_to_sell": 1000,
        "average_cost": 10.0,
    }])

    plan = generate_plan(
        bars, 100_000.0, config, positions=positions, universe=universe,
        purpose="SYNTHETIC_DIAGNOSTIC",
    )

    actions = plan.set_index("symbol")["action"]
    assert actions["SZ000002"] == "BUY"
    assert actions["SZ000001"] == "SELL"
    assert plan.loc[plan.symbol.eq("SZ000001"), "current_shares"].iloc[0] == 1000
    audit = plan.attrs["readiness_report"]["new_buy_candidate_scope"]
    assert audit["confirmed_member_count"] == 1
    assert audit["membership_semantics"].startswith("only same-day STOCK_MEMBER")


def test_unknown_member_is_excluded_and_code_is_canonicalized():
    members, audit = _confirmed_stock_members(
        _universe("2026-11-04", [("000002", "stock_member"), ("SZ000001", "UNKNOWN_TYPE")]),
        "2026-11-04",
        {"SZ000002", "SZ000001"},
    )
    assert members == {"SZ000002"}
    assert audit["unknown_or_excluded_members"] == 1


@pytest.mark.parametrize(
    ("rows", "signal_date", "current_symbols", "message"),
    [
        ([ ("000002", "STOCK_MEMBER"), ("SZ000002", "STOCK_MEMBER") ], "2026-11-04", {"SZ000002"}, "重复或状态冲突"),
        ([ ("SZ000002", "STOCK_MEMBER") ], "2026-11-05", {"SZ000002"}, "没有成员记录"),
        ([ ("SZ000003", "STOCK_MEMBER") ], "2026-11-04", {"SZ000002"}, "缺少最新日线"),
    ],
)
def test_ambiguous_wrong_date_or_missing_bar_universe_is_rejected(rows, signal_date, current_symbols, message):
    with pytest.raises(ValueError, match=message):
        _confirmed_stock_members(_universe("2026-11-04", rows), signal_date, current_symbols)
