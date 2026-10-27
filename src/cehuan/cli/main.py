from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.akshare_adapter import make_akshare_provider
from cehuan.data.baostock_adapter import BaoStockSession, create_baostock_snapshot
from cehuan.data.canonical import read_bars, write_bars
from cehuan.data.quality import check_data_quality
from cehuan.data.pool import fetch_pool_daily
from cehuan.data.security_master import apply_security_master
from cehuan.research.qlib_bridge import qlib_momentum_signals, qlib_static_loader_check
from cehuan.review.reconcile import (
    compare_plan_and_fills,
    compare_positions_to_plan,
    read_fills,
    read_positions,
    review_markdown,
)
from cehuan.signals.plan import generate_plan, plan_markdown


REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_json(value: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _console_metrics_summary(metrics: dict[str, object]) -> dict[str, object]:
    """Keep the saved experiment complete without dumping hundreds of rolling windows."""
    summary = dict(metrics)
    evaluation = summary.get("chronological_evaluation")
    if isinstance(evaluation, dict):
        evaluation_summary = dict(evaluation)
        rolling = evaluation_summary.get("rolling_fixed_rule_diagnostics")
        if isinstance(rolling, dict):
            rolling_summary = dict(rolling)
            windows = rolling_summary.pop("windows", [])
            rolling_summary["window_count"] = len(windows)
            evaluation_summary["rolling_fixed_rule_diagnostics"] = rolling_summary
        summary["chronological_evaluation"] = evaluation_summary
    return summary


def _provider_for(config: dict[str, object], source: str | None):
    configured = str(config.get("data", {}).get("provider", "eastmoney"))
    return make_akshare_provider(source or configured)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cehuan", description="策环：本地量化研究与人工审核工具")
    subs = parser.add_subparsers(dest="command", required=True)

    fetch = subs.add_parser("fetch", help="通过 AKShare 获取单只 A 股日线并检查质量")
    fetch.add_argument("--symbol", required=True)
    fetch.add_argument("--start", required=True, help="YYYYMMDD")
    fetch.add_argument("--end", required=True, help="YYYYMMDD")
    fetch.add_argument("--name")
    fetch.add_argument("--source", choices=("eastmoney", "tencent"), help="行情来源；默认读取配置")
    fetch.add_argument("--max-age-days", type=int, help="可选的最大日历日滞后；未提供交易日历时仅作粗略检查")
    fetch.add_argument("--calendar", help="可选交易日历 CSV，用于检查行情日期覆盖")
    fetch.add_argument("--output")
    fetch.add_argument("--security-master", help="带有效日期的证券状态 CSV")

    fetch_pool = subs.add_parser("fetch-pool", help="按有限股票池 CSV 顺序获取完整日线；失败时不保存部分结果")
    fetch_pool.add_argument("--universe", required=True, help="包含 symbol/name 列的 CSV")
    fetch_pool.add_argument("--start", required=True, help="YYYYMMDD")
    fetch_pool.add_argument("--end", required=True, help="YYYYMMDD")
    fetch_pool.add_argument("--security-master")
    fetch_pool.add_argument("--source", choices=("eastmoney", "tencent"), help="行情来源；默认读取配置")
    fetch_pool.add_argument("--max-age-days", type=int, help="可选的最大日历日滞后；未提供交易日历时仅作粗略检查")
    fetch_pool.add_argument("--calendar", help="可选交易日历 CSV，用于检查行情日期覆盖")
    fetch_pool.add_argument("--output", default="data/private/raw/pool_daily.csv")

    snapshot = subs.add_parser("snapshot", help="获取带原始响应和哈希清单的 BaoStock 私有数据快照")
    snapshot.add_argument("--symbols", required=True, help="逗号分隔的沪深代码，例如 SH600519,SZ000001")
    snapshot.add_argument("--start", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    snapshot.add_argument("--end", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    snapshot.add_argument("--snapshot-id", help="可选的快照版本名；已存在时拒绝覆盖")
    snapshot.add_argument("--benchmark-symbol", default="SH000300", help="指数基准代码；独立保存，不进入股票池")
    snapshot.add_argument("--output", default="data/private/snapshots", help="私有快照根目录")

    quality = subs.add_parser("quality", help="检查规范化日线 CSV")
    quality.add_argument("--data", required=True)
    quality.add_argument("--max-age-days", type=int, help="可选的最大日历日滞后；未提供交易日历时仅作粗略检查")
    quality.add_argument("--calendar", help="交易日历 CSV，用于检查行情日期覆盖")

    backtest = subs.add_parser("backtest", help="运行技术基线回测")
    backtest.add_argument("--data", required=True)
    backtest.add_argument("--cash", type=float, required=True)
    backtest.add_argument("--config")
    backtest.add_argument("--security-master")
    backtest.add_argument("--benchmark-data", help="可选独立基准规范日线 CSV")

    plan = subs.add_parser("plan", help="生成独立的人工审核交易计划")
    plan.add_argument("--data", required=True)
    plan.add_argument("--cash", type=float, required=True)
    plan.add_argument("--positions")
    plan.add_argument("--config")
    plan.add_argument("--security-master")
    plan.add_argument("--calendar", help="可选交易日历 CSV；提供后标记下一个开市日")
    plan.add_argument("--mapping")

    review = subs.add_parser("review", help="将成交 CSV 与计划对账")
    review.add_argument("--plan", required=True)
    review.add_argument("--fills", required=True)
    review.add_argument("--positions", help="可选的成交后持仓快照 CSV")
    review.add_argument("--mapping")

    qlib = subs.add_parser("qlib-check", help="将规范数据交给 Qlib StaticDataLoader 读取")
    qlib.add_argument("--data", required=True)
    qlib.add_argument("--output", default="data/processed/qlib_snapshot.parquet")

    qlib_signals = subs.add_parser("qlib-signals", help="从 Qlib 静态数据计算动量研究特征")
    qlib_signals.add_argument("--data", required=True)
    qlib_signals.add_argument("--lookback", type=int)
    qlib_signals.add_argument("--data-output", default="data/processed/qlib_snapshot.parquet")
    qlib_signals.add_argument("--output", default="outputs/qlib_signals.csv")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            config = load_config()
            bars = _provider_for(config, args.source).fetch_daily(args.symbol, args.start, args.end, name=args.name)
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            quality = check_data_quality(
                bars,
                max_age_days=args.max_age_days,
                large_change_warning=float(config["data"]["large_daily_price_change_warning"]),
                calendar=pd.read_csv(args.calendar, dtype=str) if args.calendar else None,
            )
            output = Path(args.output) if args.output else REPO_ROOT / "data" / "private" / "raw" / f"{args.symbol}_daily.csv"
            write_bars(bars, output)
            print(json.dumps({"saved_to": str(output), "quality": quality}, ensure_ascii=False, indent=2))
            return 0 if quality["healthy"] else 2

        if args.command == "fetch-pool":
            config = load_config()
            universe = pd.read_csv(args.universe, dtype={"symbol": str})
            bars = fetch_pool_daily(
                _provider_for(config, args.source), universe, args.start, args.end,
                max_symbols=int(config["data"].get("max_symbols_per_fetch", 50)),
                delay_seconds=float(config["data"].get("request_delay_seconds", 0.25)),
            )
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            quality = check_data_quality(
                bars,
                max_age_days=args.max_age_days,
                large_change_warning=float(config["data"]["large_daily_price_change_warning"]),
                calendar=pd.read_csv(args.calendar, dtype=str) if args.calendar else None,
            )
            if not quality["healthy"]:
                raise ValueError("股票池数据质量检查未通过：" + "；".join(quality["errors"]))
            output = Path(args.output)
            if not output.is_absolute():
                output = REPO_ROOT / output
            write_bars(bars, output)
            print(json.dumps({"saved_to": str(output), "quality": quality}, ensure_ascii=False, indent=2))
            return 0

        if args.command == "snapshot":
            symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
            output = Path(args.output)
            if not output.is_absolute():
                output = REPO_ROOT / output
            with BaoStockSession() as session:
                result = create_baostock_snapshot(
                    session,
                    symbols=symbols,
                    start_date=args.start,
                    end_date=args.end,
                    root=output,
                    snapshot_id=args.snapshot_id,
                    benchmark_symbol=args.benchmark_symbol,
                )
            print(json.dumps({
                "snapshot_path": result["snapshot_path"],
                "snapshot_id": result["snapshot_id"],
                "date_range": result["date_range"],
                "universe": result["universe"],
                "benchmark": result["benchmark"],
                "coverage": result["coverage"],
                "normalized_bars_sha256": result["normalized_bars_sha256"],
                "manifest": str(Path(result["snapshot_path"]) / "manifest.json"),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "quality":
            config = load_config()
            quality = check_data_quality(
                read_bars(args.data),
                max_age_days=args.max_age_days,
                large_change_warning=float(config["data"]["large_daily_price_change_warning"]),
                calendar=pd.read_csv(args.calendar, dtype=str) if args.calendar else None,
            )
            print(json.dumps(quality, ensure_ascii=False, indent=2))
            return 0 if quality["healthy"] else 2

        if args.command == "backtest":
            config = load_config(args.config)
            bars = read_bars(args.data)
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            benchmark = read_bars(args.benchmark_data) if args.benchmark_data else None
            result = run_backtest(bars, args.cash, config, benchmark_frame=benchmark)
            experiment = str(result["metrics"]["experiment_id"])
            folder = REPO_ROOT / "outputs" / "experiments" / experiment
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(result["metrics"], folder / "metrics.json")
            result["equity_curve"].to_csv(folder / "equity_curve.csv", index=False)
            result["trades"].to_csv(folder / "trades.csv", index=False)
            result["blocked_orders"].to_csv(folder / "blocked_orders.csv", index=False)
            (folder / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _write_json(result["metrics"], REPO_ROOT / "outputs" / "backtest_metrics.json")
            shutil.copyfile(folder / "trades.csv", REPO_ROOT / "outputs" / "trades.csv")
            shutil.copyfile(folder / "equity_curve.csv", REPO_ROOT / "outputs" / "equity_curve.csv")
            print(json.dumps({
                "experiment_dir": str(folder),
                "metrics": _console_metrics_summary(result["metrics"]),
            }, ensure_ascii=False, indent=2))
            return 0

        if args.command == "plan":
            config = load_config(args.config)
            bars = read_bars(args.data)
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            positions = read_positions(args.positions, args.mapping) if args.positions else None
            calendar = pd.read_csv(args.calendar, dtype=str) if args.calendar else None
            plan_frame = generate_plan(bars, args.cash, config, positions, calendar=calendar)
            plan_id = str(plan_frame["plan_id"].iloc[0]) if not plan_frame.empty else "empty-plan"
            folder = REPO_ROOT / "outputs" / "plans" / plan_id
            folder.mkdir(parents=True, exist_ok=True)
            plan_path = folder / "plan.csv"
            plan_frame.to_csv(plan_path, index=False)
            markdown = plan_markdown(plan_frame)
            (folder / "plan.md").write_text(markdown, encoding="utf-8")
            (REPO_ROOT / "outputs").mkdir(parents=True, exist_ok=True)
            plan_frame.to_csv(REPO_ROOT / "outputs" / "latest_plan.csv", index=False)
            (REPO_ROOT / "outputs" / "latest_plan.md").write_text(markdown, encoding="utf-8")
            print(f"计划已保存：{plan_path}\n人工审核必需；本命令不会下单。")
            if not plan_frame.empty:
                print(plan_frame[["symbol", "action", "planned_shares", "reference_price", "major_risks"]].to_string(index=False))
            return 0

        if args.command == "review":
            plan_frame = pd.read_csv(args.plan)
            fills = read_fills(args.fills, args.mapping)
            report = compare_plan_and_fills(plan_frame, fills)
            position_report = None
            if args.positions:
                positions = read_positions(args.positions, args.mapping)
                position_report = compare_positions_to_plan(plan_frame, positions)
            root = REPO_ROOT / "outputs"
            root.mkdir(parents=True, exist_ok=True)
            report.to_csv(root / "review.csv", index=False)
            if position_report is not None:
                position_report.to_csv(root / "review_positions.csv", index=False)
            markdown = review_markdown(report, position_report)
            (root / "review.md").write_text(markdown, encoding="utf-8")
            print(markdown)
            print(f"复盘文件：{root / 'review.md'}")
            if position_report is not None:
                print(f"持仓对照：{root / 'review_positions.csv'}")
            return 0

        if args.command == "qlib-check":
            result = qlib_static_loader_check(read_bars(args.data), REPO_ROOT / args.output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "qlib-signals":
            config = load_config()
            result = qlib_momentum_signals(
                read_bars(args.data),
                REPO_ROOT / args.data_output,
                REPO_ROOT / args.output,
                int(args.lookback or config["strategy"]["lookback_sessions"]),
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
