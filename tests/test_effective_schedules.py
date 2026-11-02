from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.fee_schedule import calculate_scheduled_fees, map_fee_schedule_to_sessions
from cehuan.data.market_rules import attach_market_rule_schedule, normalize_market_rule_schedule


def _bars(days: int = 16) -> pd.DataFrame:
    rows = []
    start = date(2025, 1, 1)
    for symbol, drift, initial in (("SZ000001", 0.25, 10.0), ("SZ000002", -0.05, 30.0)):
        for index in range(days):
            close = initial + drift * index
            previous = initial + drift * max(0, index - 1)
            rows.append({
                "trade_date": (start + timedelta(days=index)).isoformat(),
                "symbol": symbol,
                "name": symbol,
                "open": round(previous, 2),
                "high": round(max(previous, close) + 0.05, 2),
                "low": round(min(previous, close) - 0.05, 2),
                "close": round(close, 2),
                "adjusted_close": round(close, 4),
                "volume_shares": 100_000,
                "amount_cny": close * 100_000,
                # The point-in-time schedule must replace these provider/demo values.
                "is_listed": True,
                "is_suspended": True,
                "is_st": True,
                "has_price_limit": True,
                "price_limit_pct": 0.20,
                "buy_min_shares": 1,
                "buy_step_shares": 1,
                "sell_min_shares": 1,
                "sell_step_shares": 1,
                "board": "bar_value_that_must_be_overwritten",
                "market_rule_source": "bar_value_that_must_be_overwritten",
                "data_source": "synthetic_effective_schedule_test",
            })
    return pd.DataFrame(rows)


def _rule_row(symbol: str, valid_from: str, valid_to: str, *, suspended: bool = False, step: int = 100) -> dict[str, object]:
    return {
        "symbol": symbol,
        "board": "MAIN",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "is_listed": "true",
        "is_suspended": str(suspended).lower(),
        "is_st": "false",
        "has_price_limit": "true",
        "price_limit_pct": "0.10",
        "buy_min_shares": str(step),
        "buy_step_shares": str(step),
        "sell_min_shares": str(step),
        "sell_step_shares": str(step),
        "source": "synthetic_test_fixture_not_market_evidence",
        "source_sha256": "a" * 64,
        "review_status": "REVIEWED",
        "reviewed_by": "test-fixture",
        "reviewed_at": "2026-11-02",
    }


def _rules(*, step: int = 100, suspended_on: str | None = None) -> pd.DataFrame:
    rows = []
    for symbol in ("SZ000001", "SZ000002"):
        rows.append(_rule_row(symbol, "2025-01-01", "2025-01-16", step=step))
    if suspended_on:
        rows.append(_rule_row("SZ000001", suspended_on, suspended_on, suspended=True, step=step))
        # Split the original interval around the one-day suspension.
        rows = [row for row in rows if not (row["symbol"] == "SZ000001" and row["valid_from"] == "2025-01-01")]
        rows.extend([
            _rule_row("SZ000001", "2025-01-01", (pd.Timestamp(suspended_on) - pd.Timedelta(days=1)).date().isoformat(), step=step),
            _rule_row("SZ000001", (pd.Timestamp(suspended_on) + pd.Timedelta(days=1)).date().isoformat(), "2025-01-16", step=step),
        ])
    return pd.DataFrame(rows)


def _fee_row(valid_from: str, valid_to: str, source: str, commission_rate: str) -> dict[str, str]:
    return {
        "valid_from": valid_from,
        "valid_to": valid_to,
        "market": "A_SHARE",
        "commission_rate": commission_rate,
        "minimum_commission_cny": "0",
        "stamp_duty_sell_rate": "0.0005",
        "transfer_fee_rate": "0.00001",
        "source": source,
        "source_sha256": "b" * 64,
        "review_status": "REVIEWED",
        "reviewed_by": "test-fixture",
        "reviewed_at": "2026-11-02",
    }


def _fees() -> pd.DataFrame:
    return pd.DataFrame([
        _fee_row("2025-01-01", "2025-01-05", "synthetic_low_fee", "0.000001"),
        _fee_row("2025-01-06", "2025-01-16", "synthetic_high_fee", "0.0001"),
    ])


def test_rule_schedule_asof_join_overwrites_bar_status_and_respects_effective_boundary():
    rules = pd.DataFrame([
        _rule_row("SZ000001", "2025-01-01", "2025-01-07", step=100),
        _rule_row("SZ000001", "2025-01-08", "2025-01-16", step=200),
        _rule_row("SZ000002", "2025-01-01", "2025-01-16", step=100),
    ])
    joined = attach_market_rule_schedule(_bars(), rules)
    first = joined.loc[(joined["symbol"] == "SZ000001") & (joined["trade_date"] == "2025-01-07")].iloc[0]
    next_day = joined.loc[(joined["symbol"] == "SZ000001") & (joined["trade_date"] == "2025-01-08")].iloc[0]
    assert bool(first["is_suspended"]) is False
    assert bool(first["is_st"]) is False
    assert first["buy_step_shares"] == 100
    assert first["board"] == "MAIN"
    assert first["market_rule_valid_from"] == pd.Timestamp("2025-01-01")
    assert next_day["buy_step_shares"] == 200
    assert next_day["market_rule_valid_from"] == pd.Timestamp("2025-01-08")


def test_rule_schedule_rejects_uncovered_dates_and_overlapping_intervals():
    incomplete = _rules().loc[lambda table: table["symbol"] == "SZ000001"]
    with pytest.raises(ValueError, match="未覆盖全部行情证券日"):
        attach_market_rule_schedule(_bars(), incomplete)

    overlap = pd.concat([
        pd.DataFrame([_rule_row("SZ000001", "2025-01-01", "2025-01-10")]),
        pd.DataFrame([_rule_row("SZ000001", "2025-01-10", "2025-01-16")]),
    ], ignore_index=True)
    with pytest.raises(ValueError, match="区间重叠"):
        normalize_market_rule_schedule(overlap)


def test_fee_schedule_uses_inclusive_effective_dates_and_directional_stamp_duty():
    mapped = map_fee_schedule_to_sessions(_fees(), ["2025-01-05", "2025-01-06"])
    assert mapped["2025-01-05"]["source"] == "synthetic_low_fee"
    assert mapped["2025-01-06"]["source"] == "synthetic_high_fee"
    buy = calculate_scheduled_fees(10_000, "BUY", mapped["2025-01-06"])
    sell = calculate_scheduled_fees(10_000, "SELL", mapped["2025-01-06"])
    assert sell - buy == pytest.approx(5.0)
    with pytest.raises(ValueError, match="没有唯一有效"):
        map_fee_schedule_to_sessions(_fees(), ["2025-01-17"])


def test_ledger_v2_execution_consumes_reviewed_rule_and_fee_schedule():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    config["costs"].update({"commission_rate": 0.0, "minimum_commission_cny": 0.0, "stamp_duty_sell_rate": 0.0, "transfer_fee_rate": 0.0})
    effective_rules = pd.DataFrame([
        _rule_row(symbol, "2025-01-01", "2025-01-04", step=100)
        for symbol in ("SZ000001", "SZ000002")
    ] + [
        _rule_row(symbol, "2025-01-05", "2025-01-16", step=200)
        for symbol in ("SZ000001", "SZ000002")
    ])
    result = run_backtest(
        _bars(),
        100_000,
        config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
        market_rule_schedule=effective_rules,
        fee_schedule=_fees(),
    )
    assert result["metrics"]["result_type"] == "research_backtest_not_simulated_or_real_account"
    assert result["metrics"]["market_rule_mode"] == "effective_dated_schedule"
    assert result["metrics"]["fee_mode"] == "effective_dated_schedule"
    assert result["metrics"]["market_rule_fallback_row_count"] == 0
    assert set(result["trades"]["fee_source"].dropna()) == {"synthetic_low_fee", "synthetic_high_fee"}
    assert result["trades"].loc[result["trades"]["trade_date"] >= "2025-01-06", "fee_valid_from"].eq("2025-01-06").all()
    buys = result["trades"].loc[result["trades"]["side"] == "BUY"]
    assert not buys.empty
    assert (buys["quantity"] % 200 == 0).all()
    assert buys["market_rule_valid_from"].eq("2025-01-05").all()
    assert (result["trades"]["fees_cny"] > 0).all()
    fee_parts = result["trades"]["commission_cny"] + result["trades"]["stamp_duty_cny"] + result["trades"]["transfer_fee_cny"]
    assert (fee_parts - result["trades"]["fees_cny"]).abs().max() < 1e-8
    assert result["accounting_journal"]["details"].astype(str).str.contains("fee:commission_cny").any()


def test_ledger_v2_with_rule_schedule_does_not_trade_when_rule_marks_security_suspended():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    suspended_date = "2025-01-05"
    rules = _rules(suspended_on=suspended_date)
    result = run_backtest(
        _bars(),
        100_000,
        config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
        market_rule_schedule=rules,
        fee_schedule=_fees(),
    )
    assert not result["trades"].loc[
        (result["trades"]["trade_date"] == suspended_date) & (result["trades"]["symbol"] == "SZ000001")
    ].shape[0]
    assert result["blocked_orders"]["reason"].astype(str).str.contains("停牌").any()


def test_effective_schedule_blocks_no_limit_day_without_quote_boundary_data():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    rules = _rules().loc[lambda table: table["symbol"] != "SZ000001"].copy()
    no_limit = _rule_row("SZ000001", "2025-01-05", "2025-01-05")
    no_limit["has_price_limit"] = "false"
    no_limit["price_limit_pct"] = ""
    rules = pd.concat([
        rules,
        pd.DataFrame([
            _rule_row("SZ000001", "2025-01-01", "2025-01-04"),
            no_limit,
            _rule_row("SZ000001", "2025-01-06", "2025-01-16"),
        ]),
    ], ignore_index=True)
    result = run_backtest(
        _bars(), 100_000, config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
        market_rule_schedule=rules,
        fee_schedule=_fees(),
    )
    rejected = result["blocked_orders"].loc[result["blocked_orders"]["trade_date"] == "2025-01-05"]
    assert rejected["reason"].astype(str).str.contains("无涨跌幅限制").any()


def test_scheduled_rules_and_fees_are_research_only_and_must_be_paired():
    config = load_config()
    config["strategy"].update({"lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1})
    with pytest.raises(ValueError, match="必须成对提供"):
        run_backtest(
            _bars(), 100_000, config,
            security_identity_events=pd.DataFrame(),
            corporate_actions=pd.DataFrame(),
            accounting_mode="ledger_v2",
            market_rule_schedule=_rules(),
        )
    with pytest.raises(ValueError, match="仅支持 accounting_mode='ledger_v2'"):
        run_backtest(
            _bars(), 100_000, config,
            security_identity_events=pd.DataFrame(),
            corporate_actions=pd.DataFrame(),
            market_rule_schedule=_rules(),
            fee_schedule=_fees(),
        )


def test_cli_scheduled_diagnostic_saves_bound_tables_and_ledger_outputs(tmp_path, monkeypatch, capsys):
    from cehuan.cli import main as cli_main

    data_path = tmp_path / "bars.csv"
    rules_path = tmp_path / "rules.csv"
    fees_path = tmp_path / "fees.csv"
    _bars().to_csv(data_path, index=False)
    _rules().to_csv(rules_path, index=False)
    _fees().to_csv(fees_path, index=False)
    monkeypatch.setattr(cli_main, "REPO_ROOT", tmp_path)
    config_path = tmp_path / "config.toml"
    # Keep this CLI test hermetic by writing a compact valid TOML configuration.
    config_path.write_text(
        "[data]\nprovider = 'eastmoney'\nlarge_daily_price_change_warning = 0.5\n"
        "[strategy]\nname = 'cli-schedule-test'\nlookback_sessions = 3\nrebalance_sessions = 3\n"
        "max_positions = 1\nmax_position_weight = 0.5\nmax_total_exposure = 0.5\nmin_momentum = 0.0\n"
        "[market]\nexclude_st = true\nrequire_point_in_time_status = true\n"
        "buy_lot_shares = 100\nsell_lot_shares = 100\ndefault_price_limit_pct = 0.1\n"
        "assume_price_limit_applies_when_unknown = true\nprice_tick_cny = 0.01\n"
        "[costs]\ncommission_rate = 0.0\nminimum_commission_cny = 0.0\n"
        "stamp_duty_sell_rate = 0.0\ntransfer_fee_rate = 0.0\nslippage_bps = 0.0\n"
        "[backtest]\nmax_stale_price_age_sessions = 0\ndevelopment_fraction = 0.6\n"
        "validation_fraction = 0.2\nrolling_window_sessions = 6\nrolling_step_sessions = 3\n"
        "[benchmark]\nsymbol = ''\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    args = [
        "backtest", "--data", str(data_path), "--cash", "100000", "--config", str(config_path),
        "--identity-events", str(repo_root / "examples/empty_security_identity_events.csv"),
        "--corporate-actions", str(repo_root / "examples/empty_corporate_actions.csv"),
        "--market-rules", str(rules_path), "--fees", str(fees_path),
        "--diagnostic", "--accounting-mode", "ledger_v2",
    ]
    assert cli_main.main(args) == 0
    captured = capsys.readouterr()
    assert "ledger_v2" in captured.out
    experiment_dirs = list((tmp_path / "outputs/experiments").iterdir())
    assert len(experiment_dirs) == 1
    experiment_dir = experiment_dirs[0]
    assert (experiment_dir / "market_rule_schedule.csv").is_file()
    assert (experiment_dir / "fee_schedule.csv").is_file()
    assert (experiment_dir / "accounting_journal.csv").is_file()
    assert (experiment_dir / "accounting_snapshot.json").is_file()
