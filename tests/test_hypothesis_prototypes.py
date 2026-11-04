from __future__ import annotations

import pandas as pd
import pytest

from cehuan.research.hypothesis_prototypes import (
    build_h2_open_based_replacement_plan,
    compare_h1_candidate_pool,
    evaluate_h1_window,
)


def _h1_inputs() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    dates = pd.bdate_range("2025-01-01", periods=21).strftime("%Y-%m-%d").tolist()
    calendar = pd.DataFrame({"trade_date": dates, "is_open": True})
    rows = []
    symbols = ["CLEAR", "HALTED", "UNKNOWN", "MISSING", "DUPLICATE"]
    for symbol in symbols:
        for index, day in enumerate(dates):
            if symbol == "MISSING" and index == 5:
                continue
            suspended: object = False
            if symbol == "HALTED" and index == 8:
                suspended = True
            if symbol == "UNKNOWN" and index == 8:
                suspended = ""
            rows.append({"symbol": symbol, "trade_date": day, "is_suspended": suspended})
            if symbol == "DUPLICATE" and index == 8:
                rows.append({"symbol": symbol, "trade_date": day, "is_suspended": False})
    return pd.DataFrame(rows), calendar, dates


def test_h1_requires_exact_complete_21_session_known_non_suspended_window() -> None:
    bars, calendar, dates = _h1_inputs()
    signal_date = dates[-1]
    expected = {
        "CLEAR": ("ELIGIBLE", []),
        "HALTED": ("EXCLUDED", ["SUSPENDED_IN_WINDOW"]),
        "UNKNOWN": ("EXCLUDED", ["SUSPENSION_UNKNOWN"]),
        "MISSING": ("EXCLUDED", ["MISSING_SESSION"]),
        "DUPLICATE": ("EXCLUDED", ["DUPLICATE_SESSION"]),
    }
    for symbol, (status, reasons) in expected.items():
        result = evaluate_h1_window(bars, calendar, symbol=symbol, signal_date=signal_date)
        assert result["required_open_session_count"] == 21
        assert result["status"] == status
        assert result["reasons"] == reasons


def test_h1_insufficient_history_and_unknown_calendar_are_explicit() -> None:
    bars, calendar, dates = _h1_inputs()
    short_calendar = calendar.iloc[-20:].copy()
    short_bars = bars.loc[(bars.symbol == "CLEAR") & bars.trade_date.isin(short_calendar.trade_date)]
    result = evaluate_h1_window(short_bars, short_calendar, symbol="CLEAR", signal_date=dates[-1])
    assert "INSUFFICIENT_HISTORY" in result["reasons"]

    unknown_calendar = calendar.copy().astype({"is_open": "object"})
    unknown_calendar.loc[unknown_calendar.trade_date.eq(dates[3]), "is_open"] = None
    result = evaluate_h1_window(bars, unknown_calendar, symbol="CLEAR", signal_date=dates[-1])
    assert "CALENDAR_STATUS_UNKNOWN" in result["reasons"]

    leading_gap_bars = bars.loc[~((bars.symbol == "CLEAR") & (bars.trade_date == dates[0]))]
    result = evaluate_h1_window(leading_gap_bars, calendar, symbol="CLEAR", signal_date=dates[-1])
    assert result["reasons"] == ["INSUFFICIENT_HISTORY", "MISSING_SESSION"]

    duplicate_calendar = pd.concat([
        calendar,
        pd.DataFrame([{"trade_date": dates[-2], "is_open": False}]),
    ], ignore_index=True)
    result = evaluate_h1_window(bars, duplicate_calendar, symbol="CLEAR", signal_date=dates[-1])
    assert "DUPLICATE_CALENDAR_SESSION" in result["reasons"]


def test_h1_future_data_cannot_change_prior_signal_and_reranks_only_eligible() -> None:
    bars, calendar, dates = _h1_inputs()
    candidates = pd.DataFrame([
        {"symbol": "HALTED", "signal_date": dates[-1], "momentum": 0.3, "rank": 1},
        {"symbol": "CLEAR", "signal_date": dates[-1], "momentum": 0.2, "rank": 2},
    ])
    before = compare_h1_candidate_pool(candidates, bars, calendar)
    future = pd.DataFrame([{"symbol": "CLEAR", "trade_date": "2025-02-01", "is_suspended": True}])
    after = compare_h1_candidate_pool(candidates, pd.concat([bars, future]), calendar)
    pd.testing.assert_frame_equal(before, after)
    assert before.loc[before.symbol.eq("HALTED"), "h1_rank"].isna().all()
    assert before.loc[before.symbol.eq("CLEAR"), "h1_rank"].iloc[0] == 1


def _h2(
    *,
    rank2_price: float = 4.0,
    rank2_suspended: object = False,
    rank2_can_buy: object = True,
    execution_price: float = 4.5,
    execution_suspended: object = False,
    execution_can_buy: object = True,
    ranking_available_at_utc: str = "2025-01-03T07:10:00Z",
    fallback_can_buy: object = True,
    cash: float = 500.0,
    risk: float = 1000.0,
    fee_rate: float = 0.0,
    minimum_fee: float = 5.0,
    existing_positions: dict[str, int] | None = None,
) -> dict:
    ranked = pd.DataFrame([
        {"symbol": "PRIMARY", "rank": 1, "momentum": 0.5},
        {"symbol": "REPLACEMENT", "rank": 2, "momentum": 0.4},
        {"symbol": "FALLBACK", "rank": 3, "momentum": 0.3},
    ])
    decision = pd.DataFrame([
        {"symbol": "PRIMARY", "trade_date": "2025-01-06", "open_price_cny": 10.0, "available_at_utc": "2025-01-06T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
        {"symbol": "REPLACEMENT", "trade_date": "2025-01-06", "open_price_cny": rank2_price, "available_at_utc": "2025-01-06T01:30:00Z", "is_suspended": rank2_suspended, "is_listed": True, "can_buy": rank2_can_buy},
        {"symbol": "FALLBACK", "trade_date": "2025-01-06", "open_price_cny": 3.0, "available_at_utc": "2025-01-06T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": fallback_can_buy},
    ])
    execution = pd.DataFrame([
        {"symbol": "REPLACEMENT", "trade_date": "2025-01-07", "open_price_cny": execution_price, "available_at_utc": "2025-01-07T01:30:00Z", "is_suspended": execution_suspended, "is_listed": True, "can_buy": execution_can_buy},
        {"symbol": "FALLBACK", "trade_date": "2025-01-07", "open_price_cny": 3.5, "available_at_utc": "2025-01-07T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
    ])
    return build_h2_open_based_replacement_plan(
        ranked, decision, execution,
        signal_date="2025-01-03",
        ranking_available_at_utc=ranking_available_at_utc,
        decision_session_date="2025-01-06",
        execution_session_date="2025-01-07",
        decision_at_utc="2025-01-06T01:31:00Z",
        execution_at_utc="2025-01-07T01:31:00Z",
        max_positions=1,
        target_amount_per_slot_cny=500,
        portfolio_risk_budget_cny=risk,
        reserved_risk_cny=0,
        available_cash_cny=cash,
        buy_min_shares=100,
        buy_step_shares=100,
        commission_rate=fee_rate,
        minimum_commission_cny=minimum_fee,
        existing_positions=existing_positions,
    )


def test_h2_zero_target_is_decided_after_open_and_executes_next_session() -> None:
    result = _h2()
    order = result["replacement_orders"][0]
    fill = result["execution_results"][0]
    assert result["result_type"] == "SYNTHETIC_RESEARCH_PROTOTYPE_NOT_BACKTEST"
    assert result["control_policy"] == "SELECT_TOP_N_THEN_ROUND_KEEP_CASH"
    assert order["symbol"] == "REPLACEMENT"
    assert order["planned_shares"] == 100
    assert order["same_decision_session_open_fill_allowed"] is False
    assert fill["status"] == "SIMULATED_FILL_NEXT_SESSION_OPEN"
    assert fill["filled_shares"] == 100


def test_h2_skips_zero_or_unavailable_candidates_and_retains_cash_if_none() -> None:
    zero = _h2(rank2_price=8.0)
    assert zero["replacement_orders"][0]["symbol"] == "FALLBACK"
    assert any(item["status"] == "SKIPPED_ZERO_AFTER_LOT_AT_DECISION_OPEN" for item in zero["candidate_evaluations"])

    blocked = _h2(rank2_suspended=True)
    assert blocked["replacement_orders"][0]["symbol"] == "FALLBACK"
    unknown = _h2(rank2_suspended="")
    assert unknown["replacement_orders"][0]["symbol"] == "FALLBACK"
    assert any(item.get("reason") == "SUSPENSION_STATUS_UNKNOWN" for item in unknown["candidate_evaluations"])


def test_h2_keeps_cash_when_all_replacements_are_zero_or_not_buyable() -> None:
    result = _h2(rank2_price=8.0, rank2_can_buy=False, fallback_can_buy=False)
    assert result["replacement_orders"][0]["order_status"] == "NO_REPLACEMENT_CANDIDATE_REMAINS_CASH"
    assert result["execution_results"] == []


def test_h2_execution_price_never_reselects_and_budget_or_cash_can_block() -> None:
    risk_block = _h2(execution_price=6.0, risk=550)
    assert risk_block["replacement_orders"][0]["symbol"] == "REPLACEMENT"
    assert risk_block["replacement_orders"][0]["planned_shares"] == 100
    assert risk_block["execution_results"][0]["reason"] == "PORTFOLIO_RISK_BUDGET_EXCEEDED_AT_EXECUTION"
    assert risk_block["execution_reselection_after_price_change"] is False

    cash_block = _h2(execution_price=4.5, cash=454, fee_rate=0.01, minimum_fee=5)
    assert cash_block["execution_results"][0]["reason"] == "CASH_INSUFFICIENT_AFTER_FEES_AT_EXECUTION"

    suspended_at_execution = _h2(execution_suspended=True)
    assert suspended_at_execution["execution_results"][0]["reason"] == "SUSPENDED_AT_EXECUTION"
    unknown_at_execution = _h2(execution_suspended="")
    assert unknown_at_execution["execution_results"][0]["reason"] == "EXECUTION_SUSPENSION_STATUS_UNKNOWN"


def test_h2_rejects_rankings_that_were_not_available_by_decision() -> None:
    with pytest.raises(ValueError, match="signal_date"):
        _h2(ranking_available_at_utc="2025-01-06T02:00:00Z")


def test_h2_does_not_treat_zero_target_existing_holding_as_replacement_slot() -> None:
    result = _h2(existing_positions={"PRIMARY": 100})
    assert result["replacement_orders"] == []
    review = result["primary_target_reviews"][0]
    assert review["status"] == "EXISTING_POSITION_ZERO_TARGET_REBALANCE_OUT_OF_SCOPE"


def test_h2_rejects_same_session_or_invalid_time_ordering() -> None:
    ranked = pd.DataFrame([{"symbol": "A", "rank": 1, "momentum": 0.1}])
    decision = pd.DataFrame(columns=["symbol", "trade_date", "open_price_cny", "available_at_utc", "is_suspended", "is_listed", "can_buy"])
    with pytest.raises(ValueError, match="信号日 < 决策开市日 < 替补执行开市日"):
        build_h2_open_based_replacement_plan(
            ranked, decision, decision,
            signal_date="2025-01-06", decision_session_date="2025-01-06", execution_session_date="2025-01-07",
            ranking_available_at_utc="2025-01-06T00:30:00Z",
            decision_at_utc="2025-01-06T01:31:00Z", execution_at_utc="2025-01-07T01:31:00Z",
            max_positions=1, target_amount_per_slot_cny=500, portfolio_risk_budget_cny=500,
            reserved_risk_cny=0, available_cash_cny=500, buy_min_shares=100, buy_step_shares=100,
            commission_rate=0, minimum_commission_cny=5,
        )

