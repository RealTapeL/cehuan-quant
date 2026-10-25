from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.akshare_adapter import normalize_akshare_history
from cehuan.data.canonical import canonical_symbol
from cehuan.data.pool import fetch_pool_daily
from cehuan.review.reconcile import (
    compare_plan_and_fills,
    compare_positions_to_plan,
    read_fills,
    read_positions,
)
from cehuan.research.qlib_bridge import calculate_qlib_momentum
from cehuan.signals.plan import generate_plan


def bars(days: int = 16, locked_buy_day: int | None = None, st_unknown: bool = False) -> pd.DataFrame:
    rows = []
    start = date(2025, 1, 1)
    for symbol, drift, initial in [("SIM001", 0.25, 10.0), ("SIM002", -0.05, 30.0)]:
        closes = [initial + drift * index for index in range(days)]
        for index, close in enumerate(closes):
            previous = closes[max(0, index - 1)]
            open_price = previous
            high = max(open_price, close) + 0.05
            low = min(open_price, close) - 0.05
            if symbol == "SIM001" and locked_buy_day == index:
                open_price = previous * 1.10
                close = open_price
                high = open_price
                low = open_price
            rows.append({
                "trade_date": (start + timedelta(days=index)).isoformat(),
                "symbol": symbol,
                "name": symbol,
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(low, 2),
                "close": round(close, 2),
                "adjusted_close": round(close, 4),
                "volume_shares": 100000,
                "amount_cny": close * 100000,
                "is_listed": True,
                "is_suspended": False,
                "is_st": pd.NA if st_unknown else False,
                "has_price_limit": True,
                "price_limit_pct": 0.10,
                "buy_min_shares": 100,
                "buy_step_shares": 100,
                "sell_min_shares": 100,
                "sell_step_shares": 100,
                "board": "synthetic_main_board_assumption",
                "market_rule_source": "synthetic_test_assumption",
                "market_rule_valid_from": "2025-01-01",
                "data_source": "synthetic_demo_generator",
            })
    return pd.DataFrame(rows)


def test_signal_is_executed_no_earlier_than_next_session_open() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    result = run_backtest(bars(), 100_000, config)
    trade_dates = result["trades"]["trade_date"].tolist()
    expected_first_order = (date(2025, 1, 1) + timedelta(days=4)).isoformat()
    assert trade_dates[0] == expected_first_order
    assert (result["trades"].loc[result["trades"]["side"] == "BUY", "quantity"] % 100 == 0).all()


def test_backtest_reports_ordered_development_validation_test_periods() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    result = run_backtest(bars(40), 100_000, config)
    evaluation = result["metrics"]["chronological_evaluation"]
    periods = evaluation["periods"]
    assert evaluation["available"] is True
    assert periods["development"]["end"] < periods["validation"]["start"]
    assert periods["validation"]["end"] < periods["test"]["start"]
    assert periods["test"]["trade_count"] >= 0


def test_locked_limit_up_open_blocks_buy() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 20, "max_positions": 1})
    result = run_backtest(bars(locked_buy_day=4), 100_000, config)
    assert result["trades"].empty
    assert not result["blocked_orders"].empty
    assert "涨停阈值" in result["blocked_orders"].iloc[0]["reason"]


def test_unknown_st_status_requires_manual_verification() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    plan = generate_plan(bars(st_unknown=True), 100_000, config)
    assert not plan.empty
    assert set(plan["action"]) == {"VERIFY"}
    assert (plan["planned_shares"] == 0).all()


def test_unknown_market_order_rules_require_manual_verification() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    data = bars()
    data["buy_step_shares"] = pd.NA
    plan = generate_plan(data, 100_000, config)
    assert not plan.empty
    assert set(plan["action"]) == {"VERIFY"}
    assert plan["planned_shares"].eq(0).all()
    assert plan["major_risks"].str.contains("买入数量增量未知").all()


def test_plan_cannot_disable_point_in_time_rule_checks() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    config["market"]["require_point_in_time_status"] = False
    try:
        generate_plan(bars(), 100_000, config)
    except ValueError as exc:
        assert "不能关闭点时" in str(exc)
    else:
        raise AssertionError("personal plan generation must retain strict point-in-time checks")


def test_plan_respects_board_specific_minimum_and_increment() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    data = bars()
    data["board"] = "STAR"
    data["buy_min_shares"] = 200
    data["buy_step_shares"] = 1
    data["sell_min_shares"] = 200
    data["sell_step_shares"] = 1
    plan = generate_plan(data, 100_000, config)
    buys = plan.loc[plan["action"] == "BUY"]
    assert not buys.empty
    assert (buys["planned_shares"] >= 200).all()


def test_qlib_momentum_feature_uses_instrument_history_and_date_rank() -> None:
    dates = pd.date_range("2025-01-01", periods=3)
    index = pd.MultiIndex.from_product(
        [dates, ["SH600001", "SZ000001"]], names=["datetime", "instrument"]
    )
    loaded = pd.DataFrame({"close": [10, 20, 11, 18, 12, 22]}, index=index)
    result = calculate_qlib_momentum(loaded, 1)
    third_day = result.xs(dates[2], level="datetime")
    assert third_day.loc["SH600001", "momentum"] == 12 / 11 - 1
    assert third_day.loc["SZ000001", "rank"] == 1


def test_fill_reconciliation_reports_unfilled_and_vwap_gap() -> None:
    plan = pd.DataFrame([{
        "symbol": "SIM001", "action": "BUY", "planned_shares": 200, "reference_price": 10.0,
    }])
    fills = pd.DataFrame([{
        "symbol": "SIM001", "side": "BUY", "quantity": 100, "price": 10.1,
        "fees_cny": 5.01, "cost_basis_per_share": pd.NA,
    }])
    report = compare_plan_and_fills(plan, fills)
    row = report.iloc[0]
    assert row["unfilled_quantity"] == 100
    assert row["reconciliation_status"] == "PARTIAL_FILL"
    assert abs(float(row["price_difference_pct"]) - 0.01) < 1e-10


def test_akshare_volume_units_and_unknown_history_flags_are_preserved() -> None:
    raw = pd.DataFrame([{
        "日期": "2026-09-01", "股票代码": "600519", "开盘": 1295.0,
        "收盘": 1299.56, "最高": 1305.0, "最低": 1290.0,
        "成交量": 32664, "成交额": 4242440861.0,
    }])
    adjusted = pd.DataFrame([{"日期": "2026-09-01", "收盘": 8135.66}])
    result = normalize_akshare_history(
        raw, adjusted, "600519", "贵州茅台", "2026-10-25T12:00:00+00:00"
    )
    row = result.iloc[0]
    assert row["volume_shares"] == 3_266_400
    assert row["adjusted_close"] == 8135.66
    assert pd.isna(row["is_st"])
    assert pd.isna(row["is_suspended"])
    assert "15:00:00" in row["available_at"]
    assert row["symbol"] == "SH600519"


def test_a_share_code_forms_are_normalized() -> None:
    assert canonical_symbol("600519") == "SH600519"
    assert canonical_symbol("000001.SZ") == "SZ000001"
    assert canonical_symbol("830799.BJ") == "BJ830799"
    assert canonical_symbol("900001") == "900001"  # Shanghai B share: do not mislabel as Beijing A share.
    assert canonical_symbol("200001.SZ") == "200001"  # Shenzhen B share: outside this A-share adapter.


def test_huatai_column_alias_mapping_reads_fill_and_position_templates(tmp_path) -> None:
    fills_path = tmp_path / "fills.csv"
    fills_path.write_text(
        "成交日期,成交编号,证券代码,买卖方向,成交数量,成交价格,手续费,成本价\n"
        "2025-01-02,ORD-1,600519,买入,100,10.5,5.01,\n",
        encoding="utf-8",
    )
    fill = read_fills(fills_path).iloc[0]
    assert fill["symbol"] == "SH600519"
    assert fill["side"] == "BUY"
    assert fill["quantity"] == 100

    positions_path = tmp_path / "positions.csv"
    positions_path.write_text(
        "证券代码,持仓数量,可用数量,成本价\n600519,300,200,9.5\n",
        encoding="utf-8",
    )
    position = read_positions(positions_path).iloc[0]
    assert position["symbol"] == "SH600519"
    assert position["shares"] == 300
    assert position["available_to_sell"] == 200


def test_position_snapshot_reconciles_target_and_open_pnl() -> None:
    plan = pd.DataFrame([{
        "symbol": "SH600519", "action": "BUY", "target_shares": 200,
    }])
    positions = pd.DataFrame([{
        "symbol": "600519", "shares": 100, "available_to_sell": 0,
        "average_cost": 10.0, "last_price": 11.0,
    }])
    result = compare_positions_to_plan(plan, positions).iloc[0]
    assert result["position_status"] == "BELOW_TARGET"
    assert result["shares_deviation"] == -100
    assert result["unrealized_pnl_cny"] == 100


def test_pool_fetch_does_not_return_partial_data() -> None:
    class Provider:
        def fetch_daily(self, symbol, start, end, name=None):
            if symbol == "000001":
                raise RuntimeError("source unavailable")
            return pd.DataFrame([{"trade_date": "2025-01-02", "symbol": f"SH{symbol}"}])

    universe = pd.DataFrame([{"symbol": "600519"}, {"symbol": "000001"}])
    try:
        fetch_pool_daily(Provider(), universe, "20250101", "20250103", delay_seconds=0)
    except RuntimeError as exc:
        assert "未生成新数据文件" in str(exc)
        assert "SZ000001" in str(exc)
    else:
        raise AssertionError("pool fetch must fail when any requested symbol fails")


def test_plan_does_not_sell_more_than_t_plus_one_available_quantity() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    positions = pd.DataFrame([{
        "symbol": "SIM002", "shares": 100, "available_to_sell": 0, "average_cost": 30.0,
    }])
    plan = generate_plan(bars(), 100_000, config, positions)
    held = plan.loc[plan["symbol"] == "SIM002"].iloc[0]
    assert held["action"] == "HOLD"
    assert held["planned_shares"] == 0
    assert "可卖数量" in held["major_risks"]


def test_plan_refuses_daily_bar_before_its_available_time() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    data = bars()
    data["available_at"] = data["trade_date"].map(lambda value: f"{value}T15:00:00+08:00")
    before_close = datetime(2025, 1, 16, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
    try:
        generate_plan(data, 100_000, config, now=before_close)
    except ValueError as exc:
        assert "尚不能生成计划" in str(exc)
    else:
        raise AssertionError("plan should reject a daily bar before its availability time")
