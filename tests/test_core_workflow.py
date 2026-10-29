from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.adjustment import select_signal_prices
from cehuan.data.akshare_adapter import normalize_akshare_history, normalize_tencent_history
from cehuan.data.calendar import next_open_session, validate_bars_against_calendar
from cehuan.data.canonical import canonical_symbol, read_bars
from cehuan.data.pool import fetch_pool_daily
from cehuan.data.quality import check_data_quality
from cehuan.data.security_identity import read_security_identity_events
from cehuan.review.reconcile import (
    compare_plan_and_fills,
    compare_positions_to_plan,
    read_fills,
    read_positions,
)
from cehuan.research.baseline_diagnostics import run_baseline_sensitivity
from cehuan.research.qlib_bridge import calculate_qlib_momentum
from cehuan.signals.plan import generate_plan
from cehuan.signals.strategy import add_momentum


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


def test_backtest_uses_independent_configured_index_benchmark():
    config = load_config()
    config["benchmark"]["symbol"] = "SH000300"
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    stock_data = bars(16)
    index_data = stock_data.drop_duplicates("trade_date").copy().sort_values("trade_date")
    index_data["symbol"] = "SH000300"
    index_data["name"] = "CSI 300"
    index_data["open"] = 100.0
    index_data["high"] = [100 + i for i in range(len(index_data))]
    index_data["low"] = 100.0
    index_data["close"] = [100 + i for i in range(len(index_data))]
    index_data["adjusted_close"] = index_data["close"]
    index_data["is_listed"] = False
    index_data["is_suspended"] = False
    index_data["is_st"] = False
    index_data["has_price_limit"] = False
    index_data["data_source"] = "synthetic_index_test"

    result = run_backtest(stock_data, 100_000, config, benchmark_frame=index_data)

    expected_return = float(index_data["close"].iloc[-1] / index_data["close"].iloc[0] - 1)
    assert result["metrics"]["benchmark_method"] == "configured_index_close_return:SH000300"
    assert abs(result["metrics"]["peer_benchmark_return"] - expected_return) < 1e-12
    assert abs(result["metrics"]["benchmark_return"] - expected_return) < 1e-12
    assert "SH000300" not in result["metrics"]["universe_symbols"]
    assert result["metrics"]["benchmark_data_version_sha256"]
    assert any("价格指数" in item for item in result["metrics"]["limitations"])
    assert not any("输入股票池等权" in item for item in result["metrics"]["limitations"])


@pytest.mark.parametrize("drop_position", [5, -1], ids=["middle-session", "last-session"])
def test_backtest_rejects_missing_configured_benchmark_session(drop_position):
    config = load_config()
    config["benchmark"]["symbol"] = "SH000300"
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    stock_data = bars(16)
    index_data = stock_data.drop_duplicates("trade_date").copy().sort_values("trade_date")
    missing_date = index_data.iloc[drop_position]["trade_date"]
    index_data = index_data.loc[index_data["trade_date"] != missing_date].copy()
    index_data["symbol"] = "SH000300"
    index_data["name"] = "CSI 300"
    index_data["is_listed"] = False
    index_data["is_st"] = False
    index_data["has_price_limit"] = False
    index_data["data_source"] = "synthetic_index_test"

    with pytest.raises(ValueError, match=f"基准数据缺少评价区间交易日：{missing_date}"):
        run_backtest(stock_data, 100_000, config, benchmark_frame=index_data)


def test_existing_st_position_can_be_exited_but_is_not_a_new_buy_candidate():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    data = bars(16)
    data["is_st"] = False
    last_day = data["trade_date"].max()
    data.loc[(data["symbol"] == "SIM001") & (data["trade_date"] == last_day), "is_st"] = True
    data["buy_min_shares"] = pd.NA
    data["buy_step_shares"] = pd.NA
    positions = pd.DataFrame([{
        "symbol": "SIM001", "shares": 500, "available_to_sell": 500, "average_cost": 10.0,
    }])

    plan = generate_plan(data, 100_000, config, positions=positions)

    held = plan.loc[plan["symbol"] == "SIM001"].iloc[0]
    assert held["action"] == "SELL"
    assert held["planned_shares"] == 500
    assert "ST 等状态可按卖出规则退出" in held["major_risks"]


def test_backtest_sells_existing_holding_after_it_becomes_st():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    data = bars(16)
    data["is_st"] = False
    st_start_date = data.loc[data["trade_date"] == data["trade_date"].drop_duplicates().iloc[6], "trade_date"].iloc[0]
    data.loc[(data["symbol"] == "SIM001") & (data["trade_date"] >= st_start_date), "is_st"] = True

    result = run_backtest(data, 100_000, config)

    sim001_trades = result["trades"].loc[result["trades"]["symbol"] == "SIM001"]
    assert "BUY" in sim001_trades["side"].tolist()
    assert "SELL" in sim001_trades["side"].tolist()
    assert sim001_trades.loc[sim001_trades["side"] == "SELL", "trade_date"].min() > st_start_date


def _code_change_bars(days: int = 20, event_index: int = 6) -> pd.DataFrame:
    data = bars(days)
    rows = data.loc[data["symbol"] == "SIM001"].copy().sort_values("trade_date")
    dates = rows["trade_date"].tolist()
    rows.loc[rows.index[:event_index], "symbol"] = "SZ300114"
    rows.loc[rows.index[event_index:], "symbol"] = "SZ302132"
    # The successor starts with its own price history and turns down, so the
    # first fresh signal can later request an exit without splicing old prices.
    successor_indices = rows.index[event_index:]
    start_price = float(rows.loc[successor_indices[0], "close"])
    for offset, row_index in enumerate(successor_indices):
        close = round(start_price - 0.25 * offset, 2)
        open_price = close if offset == 0 else round(start_price - 0.25 * (offset - 1), 2)
        rows.loc[row_index, ["open", "high", "low", "close", "adjusted_close"]] = [
            open_price, max(open_price, close) + 0.05, min(open_price, close) - 0.05, close, close,
        ]
    data = data.loc[data["symbol"] != "SIM001"].copy()
    return pd.concat([data, rows], ignore_index=True).sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def test_backtest_migrates_holding_without_trade_or_fee_and_sells_successor_after_fresh_signal():
    config = load_config()
    config["strategy"].update({
        "lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1,
        "max_position_weight": 0.8, "max_total_exposure": 0.8,
    })
    data = _code_change_bars()
    signalled = add_momentum(data, 3)
    successor_signal = signalled.loc[signalled["symbol"] == "SZ302132", "momentum"]
    assert successor_signal.iloc[:3].isna().all()
    assert pd.notna(successor_signal.iloc[3])
    events = read_security_identity_events()
    events["effective_date"] = "2025-01-07"
    result = run_backtest(data, 100_000, config, security_identity_events=events)

    migration = result["security_events"].loc[
        result["security_events"]["event_type"] == "MIGRATE_POSITION"
    ].iloc[0]
    event_day = migration["trade_date"]
    assert migration["old_symbol"] == "SZ300114"
    assert migration["new_symbol"] == "SZ302132"
    assert migration["old_shares"] == migration["new_shares"]
    assert migration["cost_basis_before_cny"] == migration["cost_basis_after_cny"]
    assert migration["cash_before_cny"] == migration["cash_after_cny"]
    assert migration["trade_count_before"] == migration["trade_count_after"]
    assert not result["trades"].loc[result["trades"]["trade_date"] == event_day, "symbol"].eq("SZ302132").any()
    assert not (
        result["trades"]["symbol"].eq("SZ300114")
        & result["trades"]["trade_date"].ge(event_day)
    ).any()

    successor_trades = result["trades"].loc[result["trades"]["symbol"] == "SZ302132"]
    assert successor_trades["side"].tolist() == ["SELL"]
    assert successor_trades.iloc[0]["quantity"] == migration["new_shares"]
    assert successor_trades.iloc[0]["allocated_cost_basis_cny"] == migration["cost_basis_after_cny"]
    dates = sorted(data["trade_date"].unique().tolist())
    assert dates.index(successor_trades.iloc[0]["trade_date"]) > dates.index(event_day) + config["strategy"]["lookback_sessions"]
    assert successor_trades.iloc[0]["holding_since"] < event_day
    assert pd.notna(successor_trades.iloc[0]["realized_pnl_cny"])


def test_backtest_records_no_position_for_registered_event_without_fake_order():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 20, "max_positions": 1})
    events = pd.DataFrame([{
        "event_id": "TEST-1", "old_symbol": "SZ300114", "new_symbol": "SZ302132",
        "effective_date": "2025-01-07", "share_ratio": "1.0",
        "ratio_basis": "old_shares_to_new_shares", "security_identity": "test issuer",
        "event_status": "CONFIRMED", "evidence_type": "test_fixture",
        "evidence_url": "https://example.invalid/test", "evidence_summary": "test event",
    }])
    data = _code_change_bars()
    # Start the backtest after the old-code history so there is no old holding.
    data = data.loc[data["trade_date"] >= "2025-01-07"].copy()
    data.loc[data["symbol"] == "SZ300114", "symbol"] = "SZ302132"
    result = run_backtest(data, 100_000, config, security_identity_events=events)
    event = result["security_events"].iloc[0]
    assert event["event_type"] == "NO_POSITION"
    assert event["old_shares"] == event["new_shares"] == 0
    assert not ((result["trades"]["trade_date"] == "2025-01-07") & result["trades"]["symbol"].isin(["SZ300114", "SZ302132"])).any()


def test_backtest_reports_fixed_rule_rolling_window_diagnostics() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    config["backtest"].update({"rolling_window_sessions": 10, "rolling_step_sessions": 5})
    result = run_backtest(bars(40), 100_000, config)
    rolling = result["metrics"]["chronological_evaluation"]["rolling_fixed_rule_diagnostics"]
    assert rolling["available"] is True
    assert rolling["method"].startswith("fixed-rule continuous")
    assert len(rolling["windows"]) == 7
    assert rolling["windows"][0]["start"] < rolling["windows"][0]["end"]
    assert rolling["windows"][0]["end"] > rolling["windows"][1]["start"]
    assert "不属于" in rolling["interpretation"]


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


def test_tencent_history_normalizes_share_volume_and_distinct_column_names() -> None:
    raw = pd.DataFrame([{
        "date": "2026-10-23", "open": 1311.84, "high": 1343.88,
        "low": 1310.0, "close": 1335.0, "volume": 5_768_600,
        "turnover": 0.0046, "amount": 7_677_216_283.0,
    }])
    adjusted = pd.DataFrame([{"date": "2026-10-23", "close": 9316.38}])
    result = normalize_tencent_history(
        raw, adjusted, "sh600519", "贵州茅台", "2026-10-27T02:00:00+00:00"
    )
    row = result.iloc[0]
    assert row["symbol"] == "SH600519"
    assert row["volume_shares"] == 5_768_600
    assert row["amount_cny"] == 7_677_216_283.0
    assert row["adjusted_close"] == 9316.38
    assert row["data_source"] == "akshare.stock_zh_a_hist_tx"


def test_partial_adjusted_prices_do_not_mix_with_raw_prices_for_signals() -> None:
    data = bars(16)
    data.loc[(data["symbol"] == "SIM001") & (data["trade_date"] == "2025-01-08"), "adjusted_close"] = pd.NA
    selected, modes = select_signal_prices(data)
    assert modes["SIM001"] == "unusable_mixed_or_nonpositive_adjustment"
    assert selected.loc[data["symbol"] == "SIM001"].isna().all()
    signalled = add_momentum(data, 3)
    assert signalled.loc[signalled["symbol"] == "SIM001", "momentum"].isna().all()
    assert signalled.loc[signalled["symbol"] == "SIM002", "momentum"].notna().any()


def test_data_quality_rejects_mixed_price_sources_for_one_security() -> None:
    data = bars(4)
    data.loc[data["symbol"] == "SIM001", "data_source"] = "synthetic_source_a"
    data.loc[(data["symbol"] == "SIM001") & (data["trade_date"] == "2025-01-04"), "data_source"] = "synthetic_source_b"
    from cehuan.data.quality import check_data_quality

    quality = check_data_quality(data)
    assert quality["healthy"] is False
    assert any("混用了多个行情来源" in message for message in quality["errors"])


def test_calendar_validates_bar_dates_and_resolves_next_open_session() -> None:
    data = bars(3)
    calendar = pd.DataFrame({
        "cal_date": [20250101, 20250102, 20250103, 20250104, 20250106],
        "is_open": [1, 1, 1, 0, 1],
        "pretrade_date": [pd.NA, 20250101, 20250102, 20250103, 20250103],
        "source": ["synthetic_calendar"] * 5,
    })
    result = validate_bars_against_calendar(data, calendar)
    assert result["healthy"] is True
    assert result["bar_date_count"] == 3
    assert next_open_session(calendar, "2025-01-03") == ("2025-01-06", "synthetic_calendar")


def test_plan_records_next_open_day_and_calendar_source() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    data = bars(16)
    dates = pd.date_range("2025-01-01", periods=17, freq="D")
    calendar = pd.DataFrame({
        "trade_date": dates.strftime("%Y-%m-%d"),
        "is_open": True,
        "source": "synthetic_calendar",
    })
    plan = generate_plan(data, 100_000, config, calendar=calendar)
    assert not plan.empty
    assert plan["effective_time"].str.contains("2025-01-17").all()
    assert plan["calendar_source"].eq("synthetic_calendar").all()


def test_plan_rejects_snapshot_fetched_before_close_even_if_plan_runs_after_close() -> None:
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "max_positions": 1})
    data = bars()
    data["available_at"] = data["trade_date"].map(lambda value: f"{value}T15:00:00+08:00")
    data["retrieved_at_utc"] = "2025-01-16T06:00:00+00:00"
    after_close = datetime(2025, 1, 16, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    try:
        generate_plan(data, 100_000, config, now=after_close)
    except ValueError as exc:
        assert "收盘可用时间前获取" in str(exc)
    else:
        raise AssertionError("a pre-close snapshot must not be used after the close without refetching")


def test_a_share_code_forms_are_normalized() -> None:
    assert canonical_symbol("600519") == "SH600519"
    assert canonical_symbol("000001.SZ") == "SZ000001"
    assert canonical_symbol("830799.BJ") == "BJ830799"
    assert canonical_symbol("SH000300") == "SH000300"
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


def test_compact_prevalidated_backtest_matches_standard_run(tmp_path) -> None:
    frame = bars(days=50)
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    config = load_config()
    standard = run_backtest(frame, 100_000, config)
    compact = read_bars(path, compact=True)
    compact_quality = check_data_quality(frame)
    compact_result = run_backtest(
        compact,
        100_000,
        config,
        prevalidated_quality=compact_quality,
        source_data_sha256="test-file-hash",
        mutate_input=True,
    )

    assert compact["symbol"].dtype.name == "category"
    assert compact["trade_date"].dtype.name == "category"
    assert compact_result["metrics"]["total_return"] == standard["metrics"]["total_return"]
    assert compact_result["metrics"]["max_drawdown"] == standard["metrics"]["max_drawdown"]
    pd.testing.assert_frame_equal(compact_result["trades"], standard["trades"])


def test_prevalidated_point_in_time_data_cannot_run_backtest_before_baseline_gates():
    config = load_config()
    quality = {
        "healthy": True,
        "result_type": "prevalidated_source_snapshot_after_streaming_point_in_time_audit",
        "baseline_ready": False,
        "baseline_blockers": ["CORPORATE_ACTIONS_INCOMPLETE"],
    }
    with pytest.raises(ValueError, match="正式基线未就绪.*CORPORATE_ACTIONS_INCOMPLETE"):
        run_backtest(bars(16), 100_000, config, prevalidated_quality=quality)


def test_baseline_diagnostics_function_has_its_own_disabled_gate(tmp_path):
    config = load_config()
    with pytest.raises(ValueError, match="正式点时基线尚未启用"):
        run_baseline_sensitivity(
            bars(16), pd.DataFrame(), bars(16), 100_000, config, tmp_path / "baseline",
        )
