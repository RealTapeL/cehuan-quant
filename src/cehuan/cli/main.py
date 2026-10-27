from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import shlex
import sys
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.akshare_adapter import make_akshare_provider
from cehuan.data.baostock_adapter import BaoStockSession, create_baostock_snapshot
from cehuan.data.canonical import read_bars, write_bars
from cehuan.data.quality import check_data_quality
from cehuan.data.pool import fetch_pool_daily
from cehuan.data.security_master import apply_security_master
from cehuan.data.universe_snapshot import (
    audit_baostock_universe_prices,
    create_baostock_universe_snapshot,
    filter_bars_to_point_in_time_universe,
)
from cehuan.research.qlib_bridge import qlib_momentum_signals, qlib_static_loader_check
from cehuan.research.baseline_diagnostics import run_baseline_sensitivity
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
    symbol_source = snapshot.add_mutually_exclusive_group(required=True)
    symbol_source.add_argument("--symbols", help="逗号分隔的沪深代码，例如 SH600519,SZ000001")
    symbol_source.add_argument("--symbols-file", help="含 symbol 列的 CSV；适用于从历史股票池生成的大任务")
    snapshot.add_argument("--start", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    snapshot.add_argument("--end", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    snapshot.add_argument("--snapshot-id", help="可选的快照版本名；已存在时拒绝覆盖")
    snapshot.add_argument("--benchmark-symbol", default="SH000300", help="指数基准代码；独立保存，不进入股票池")
    snapshot.add_argument("--batch-size", type=int, default=30, help="每批证券数，1–30；大任务可续跑")
    snapshot.add_argument("--max-batches", type=int, help="本次最多执行的批次数，便于分段下载")
    snapshot.add_argument("--resume", action="store_true", help="续跑同一 snapshot-id 的未完成批次")
    snapshot.add_argument("--output", default="data/private/snapshots", help="私有快照根目录")

    universe_snapshot = subs.add_parser(
        "universe-snapshot", help="按历史交易日抓取 BaoStock 证券列表，保存股票池、排除项和差异；可分批续跑",
    )
    universe_snapshot.add_argument("--start", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    universe_snapshot.add_argument("--end", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    universe_snapshot.add_argument("--snapshot-id", required=True, help="用于续跑的固定任务名")
    universe_snapshot.add_argument("--batch-size", type=int, help="本次最多抓取的交易日数量；省略时完成全部剩余日期")
    universe_snapshot.add_argument("--resume", action="store_true", help="从 data/private 中的 partial 任务续跑")
    universe_snapshot.add_argument("--bars", help="可选的规范 bars.csv，用于核对行情覆盖与退市样本覆盖")
    universe_snapshot.add_argument("--output", default="data/private/universes", help="私有股票池快照根目录")

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
    backtest.add_argument("--universe", help="可选的每日股票池 CSV 或股票池快照目录；提供后仅回测日期名单中 type=1 股票")
    backtest.add_argument("--prevalidated-quality", help="universe-audit 生成且与 --data 哈希匹配的 point_in_time_quality.json；用于大型 CSV")

    universe_audit = subs.add_parser("universe-audit", help="核对每日股票池、行情覆盖、停牌状态差异和退市样本覆盖")
    universe_audit.add_argument("--universe", required=True, help="包含 manifest.json 与 securities_by_date.csv 的股票池快照目录")
    universe_audit.add_argument("--bars", required=True, help="规范 bars.csv")
    universe_audit.add_argument("--output", default="outputs/universe-audit", help="私有审计结果目录")

    diagnostics = subs.add_parser("baseline-diagnostics", help="按日期股票池运行冻结基线及单假设敏感性对照")
    diagnostics.add_argument("--data", required=True, help="规范 bars.csv")
    diagnostics.add_argument("--universe", required=True, help="股票池快照目录或 securities_by_date.csv")
    diagnostics.add_argument("--benchmark-data", required=True, help="独立基准规范日线 CSV")
    diagnostics.add_argument("--cash", type=float, required=True, help="研究演示资金，不是账户资金")
    diagnostics.add_argument("--config", help="策略配置；默认使用冻结的 configs/default.toml")
    diagnostics.add_argument("--output", help="私有结果目录；默认创建带时间戳的 outputs 子目录")

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
            if args.symbols:
                symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
            else:
                symbol_file = Path(args.symbols_file)
                if not symbol_file.is_absolute():
                    symbol_file = REPO_ROOT / symbol_file
                frame = pd.read_csv(symbol_file, dtype=str)
                if "symbol" not in frame.columns:
                    raise ValueError("--symbols-file CSV 必须包含 symbol 列")
                symbols = frame["symbol"].dropna().astype(str).tolist()
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
                    batch_size=args.batch_size,
                    max_batches=args.max_batches,
                    resume=args.resume,
                )
            if result.get("state") == "partial":
                if args.symbols_file:
                    source_args = f"--symbols-file {shlex.quote(args.symbols_file)}"
                else:
                    source_args = f"--symbols {shlex.quote(args.symbols)}"
                result["resume_command"] = (
                    "uv run --extra baostock cehuan snapshot "
                    f"{source_args} --start {shlex.quote(args.start)} --end {shlex.quote(args.end)} "
                    f"--snapshot-id {shlex.quote(result['snapshot_id'])} --batch-size {args.batch_size} "
                    f"--benchmark-symbol {shlex.quote(args.benchmark_symbol)} --resume "
                    f"--output {shlex.quote(args.output)}"
                )
                summary = result
            else:
                summary = {
                    "snapshot_path": result["snapshot_path"],
                    "snapshot_id": result["snapshot_id"],
                    "date_range": result["date_range"],
                    "universe": result["universe"],
                    "benchmark": result["benchmark"],
                    "coverage": result["coverage"],
                    "normalized_bars_sha256": result["normalized_bars_sha256"],
                    "manifest": str(Path(result["snapshot_path"]) / "manifest.json"),
                }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        if args.command == "universe-snapshot":
            output = Path(args.output)
            if not output.is_absolute():
                output = REPO_ROOT / output
            bars = Path(args.bars) if args.bars else None
            if bars is not None and not bars.is_absolute():
                bars = REPO_ROOT / bars
            with BaoStockSession() as session:
                result = create_baostock_universe_snapshot(
                    session,
                    start_date=args.start,
                    end_date=args.end,
                    root=output,
                    snapshot_id=args.snapshot_id,
                    batch_size=args.batch_size,
                    resume=args.resume,
                    bars_path=bars,
                )
            if result.get("state") == "partial":
                resume_command = (
                    "uv run --extra baostock cehuan universe-snapshot "
                    f"--start {shlex.quote(args.start)} --end {shlex.quote(args.end)} "
                    f"--snapshot-id {shlex.quote(args.snapshot_id)} --resume --output {shlex.quote(args.output)}"
                )
                if args.bars:
                    resume_command += f" --bars {shlex.quote(args.bars)}"
                result["resume_command"] = resume_command
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "universe-audit":
            universe = Path(args.universe)
            bars = Path(args.bars)
            output = Path(args.output)
            if not universe.is_absolute():
                universe = REPO_ROOT / universe
            if not bars.is_absolute():
                bars = REPO_ROOT / bars
            if not output.is_absolute():
                output = REPO_ROOT / output
            result = audit_baostock_universe_prices(universe, bars, output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "baseline-diagnostics":
            data_path = Path(args.data)
            universe_path = Path(args.universe)
            benchmark_path = Path(args.benchmark_data)
            output = Path(args.output) if args.output else Path(
                "outputs"
            ) / "baseline-diagnostics" / f"chq-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
            if not data_path.is_absolute():
                data_path = REPO_ROOT / data_path
            if not universe_path.is_absolute():
                universe_path = REPO_ROOT / universe_path
            if universe_path.is_dir():
                universe_path = universe_path / "securities_by_date.csv"
            if not benchmark_path.is_absolute():
                benchmark_path = REPO_ROOT / benchmark_path
            if not output.is_absolute():
                output = REPO_ROOT / output
            config = load_config(args.config)
            result = run_baseline_sensitivity(
                bars=read_bars(data_path),
                membership=pd.read_csv(universe_path, dtype=str, keep_default_na=False),
                benchmark=read_bars(benchmark_path),
                initial_cash_cny=args.cash,
                frozen_config=config,
                output_dir=output,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
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
            prevalidated_quality = None
            source_data_sha256 = None
            mutate_input = False
            if args.prevalidated_quality:
                if args.universe or args.security_master:
                    raise ValueError("--prevalidated-quality 仅用于已完成点时股票池过滤与证券主数据校验的规范 CSV")
                quality_path = Path(args.prevalidated_quality)
                if not quality_path.is_absolute():
                    quality_path = REPO_ROOT / quality_path
                prevalidated_quality = json.loads(quality_path.read_text(encoding="utf-8"))
                if not prevalidated_quality.get("healthy"):
                    raise ValueError("提供的预验证质量报告未通过")
                bars_path = Path(args.data)
                if not bars_path.is_absolute():
                    bars_path = REPO_ROOT / bars_path
                bars = read_bars(bars_path, compact=True)
                digest = hashlib.sha256()
                with bars_path.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                source_data_sha256 = digest.hexdigest()
                if prevalidated_quality.get("data_sha256") != source_data_sha256:
                    raise ValueError("预验证报告与 --data 文件 SHA-256 不一致；请对该点时行情重新运行 universe-audit")
                mutate_input = True
            else:
                bars = read_bars(args.data)
            universe_summary = None
            universe_discrepancies = None
            if args.universe:
                universe_path = Path(args.universe)
                if not universe_path.is_absolute():
                    universe_path = REPO_ROOT / universe_path
                if universe_path.is_dir():
                    universe_path = universe_path / "securities_by_date.csv"
                membership = pd.read_csv(universe_path, dtype=str, keep_default_na=False)
                bars, universe_summary, universe_discrepancies = filter_bars_to_point_in_time_universe(bars, membership)
                if bars.empty:
                    raise ValueError("每日股票池与行情没有交集，无法回测")
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            benchmark = read_bars(args.benchmark_data) if args.benchmark_data else None
            result = run_backtest(
                bars,
                args.cash,
                config,
                benchmark_frame=benchmark,
                prevalidated_quality=prevalidated_quality,
                source_data_sha256=source_data_sha256,
                mutate_input=mutate_input,
            )
            if universe_summary is not None:
                result["metrics"]["point_in_time_universe_audit"] = universe_summary
            experiment = str(result["metrics"]["experiment_id"])
            folder = REPO_ROOT / "outputs" / "experiments" / experiment
            folder.mkdir(parents=True, exist_ok=True)
            _write_json(result["metrics"], folder / "metrics.json")
            result["equity_curve"].to_csv(folder / "equity_curve.csv", index=False)
            result["trades"].to_csv(folder / "trades.csv", index=False)
            result["blocked_orders"].to_csv(folder / "blocked_orders.csv", index=False)
            if universe_summary is not None:
                _write_json(universe_summary, folder / "point_in_time_universe_audit.json")
                universe_discrepancies.to_csv(folder / "point_in_time_universe_discrepancies.csv", index=False)
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
