from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.data.canonical import normalize_bar_frame
from cehuan.data.universe_snapshot import filter_bars_to_point_in_time_universe


def run_baseline_sensitivity(
    bars: pd.DataFrame,
    membership: pd.DataFrame,
    benchmark: pd.DataFrame,
    initial_cash_cny: float,
    frozen_config: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Any]:
    """Run the frozen baseline and transparent one-assumption-at-a-time checks."""
    point_in_time_bars, universe_coverage, discrepancies = filter_bars_to_point_in_time_universe(
        bars, membership,
    )
    reconciliation = universe_coverage["reconciliation_quality"]
    if not reconciliation["healthy"]:
        raise ValueError(
            "点时股票池对账未通过，不能把该结果作为正式基线："
            + "; ".join(reconciliation["errors"])
            + "；请先运行 universe-audit 并核对逐条差异"
        )
    if point_in_time_bars.empty:
        raise ValueError("每日股票池与行情没有交集")
    base_config = copy.deepcopy(frozen_config)
    scenarios: list[tuple[str, str, pd.DataFrame, dict[str, Any]]] = [
        ("frozen_baseline", "冻结配置；后复权收盘信号、当前费用和规则回退", point_in_time_bars, base_config),
    ]

    legacy_symbols = {"SH600519", "SZ000001", "SH600000", "SZ000002"}
    available_legacy_symbols = set(point_in_time_bars["symbol"].astype(str)) & legacy_symbols
    if available_legacy_symbols == legacy_symbols:
        legacy_bars = point_in_time_bars.loc[
            point_in_time_bars["symbol"].astype(str).isin(legacy_symbols)
        ].copy()
        scenarios.append((
            "legacy_four_stock_survivor_reference",
            "与原四只当前仍有行情样本相同的固定股票子集；仅作股票池差异参照，含幸存者偏差",
            legacy_bars,
            copy.deepcopy(base_config),
        ))

    zero_cost = copy.deepcopy(base_config)
    zero_cost["costs"].update({
        "commission_rate": 0.0,
        "minimum_commission_cny": 0.0,
        "stamp_duty_sell_rate": 0.0,
        "transfer_fee_rate": 0.0,
        "slippage_bps": 0.0,
    })
    scenarios.append(("zero_cost_sensitivity", "费用和滑点全部设零，仅作费用影响对照", point_in_time_bars, zero_cost))

    no_price_limit = copy.deepcopy(base_config)
    no_price_limit["market"]["assume_price_limit_applies_when_unknown"] = False
    scenarios.append(("no_price_limit_fallback", "未知涨跌停规则不阻止订单，单独衡量价格限制回退影响", point_in_time_bars, no_price_limit))

    one_share_lot = copy.deepcopy(base_config)
    one_share_lot["market"]["buy_lot_shares"] = 1
    one_share_lot["market"]["sell_lot_shares"] = 1
    scenarios.append(("one_share_lot_sensitivity", "缺少历史交易单位时按 1 股取整，单独衡量 100 股回退影响", point_in_time_bars, one_share_lot))

    raw_signal = point_in_time_bars.copy()
    raw_signal["adjusted_close"] = raw_signal["close"]
    scenarios.append(("raw_close_signal", "把信号价改为未复权收盘；仅比较信号序列，不模拟分红送转现金或持股变化", normalize_bar_frame(raw_signal), copy.deepcopy(base_config)))

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    result_rows: list[dict[str, Any]] = []
    all_metrics: dict[str, Any] = {}
    for name, interpretation, scenario_bars, scenario_config in scenarios:
        result = run_backtest(
            scenario_bars,
            initial_cash_cny,
            scenario_config,
            benchmark_frame=benchmark,
        )
        metrics = result["metrics"]
        trades = result["trades"]
        metrics["scenario_name"] = name
        metrics["scenario_interpretation"] = interpretation
        metrics["point_in_time_universe_audit"] = universe_coverage
        metrics["scenario_universe_symbol_count"] = int(scenario_bars["symbol"].nunique())
        metrics["scenario_universe_symbols"] = sorted(scenario_bars["symbol"].astype(str).unique().tolist())
        folder = root / name
        folder.mkdir()
        (folder / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        (folder / "config.json").write_text(json.dumps(scenario_config, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        result["equity_curve"].to_csv(folder / "equity_curve.csv", index=False)
        trades.to_csv(folder / "trades.csv", index=False)
        result["blocked_orders"].to_csv(folder / "blocked_orders.csv", index=False)
        fee_total = float(trades["fees_cny"].sum()) if not trades.empty else 0.0
        result_rows.append({
            "scenario": name,
            "interpretation": interpretation,
            "universe_symbol_count": metrics["scenario_universe_symbol_count"],
            "total_return": metrics.get("total_return"),
            "annualized_return": metrics.get("annualized_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "annualized_volatility": metrics.get("annualized_volatility"),
            "daily_win_rate": metrics.get("daily_win_rate"),
            "turnover_over_average_equity": metrics.get("turnover_over_average_equity"),
            "trade_count": metrics.get("trade_count"),
            "fees_cny": fee_total,
            "blocked_order_count": metrics.get("blocked_order_count"),
            "market_rule_fallback_row_count": metrics.get("market_rule_fallback_row_count"),
            "benchmark_return": metrics.get("benchmark_return"),
            "excess_return_vs_benchmark": metrics.get("excess_return_vs_benchmark"),
            "experiment_id": metrics.get("experiment_id"),
            "config_sha256": metrics.get("config_sha256"),
            "data_version_sha256": metrics.get("data_version_sha256"),
        })
        all_metrics[name] = metrics

    summary = pd.DataFrame(result_rows)
    summary.to_csv(root / "scenario_summary.csv", index=False)
    (root / "universe_price_discrepancies.csv").write_bytes(discrepancies.to_csv(index=False).encode("utf-8"))
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evaluation_type": "short historical diagnostic on a point-in-time universe; not an independent holdout test",
        "strategy_parameters_changed": False,
        "data_start": str(point_in_time_bars["trade_date"].min()),
        "data_end": str(point_in_time_bars["trade_date"].max()),
        "point_in_time_universe_audit": universe_coverage,
        "scenarios": {name: values for name, values in all_metrics.items()},
        "scenario_summary_file": "scenario_summary.csv",
        "universe_discrepancies_file": "universe_price_discrepancies.csv",
        "limitations": [
            "The short interval was selected after prior historical results were inspected and is not an independent test set.",
            "Price-limit and order-size scenarios vary fallback assumptions; they do not establish actual historical exchange rules.",
            "Adjusted-close versus raw-close is a signal-price sensitivity only; cash dividends, stock distributions, rights issues, and delisting proceeds are not booked.",
            "The benchmark is a price index and excludes constituent dividends.",
            "All outputs remain research backtests; no brokerage order is sent.",
        ],
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return {"output_dir": str(root), "scenario_summary": result_rows, "point_in_time_universe_audit": universe_coverage}
