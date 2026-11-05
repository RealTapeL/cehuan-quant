from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import hashlib
import json
import os
import shutil
import shlex
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.backtest.checkpoint import read_backtest_checkpoint
from cehuan.accounting.snapshot_io import read_ledger_snapshot
from cehuan.config import load_config
from cehuan.data.akshare_adapter import make_akshare_provider
from cehuan.data.baostock_adapter import BaoStockSession, create_baostock_snapshot
from cehuan.data.canonical import read_bars, write_bars
from cehuan.data.accounting_event_csv import read_accounting_event_csv
from cehuan.data.corporate_actions import DEFAULT_CORPORATE_ACTIONS, read_corporate_actions
from cehuan.data.quality import check_data_quality
from cehuan.data.security_identity import read_security_identity_events, sha256_file
from cehuan.data.pool import fetch_pool_daily
from cehuan.data.security_master import apply_security_master
from cehuan.data.universe_snapshot import (
    audit_baostock_universe_prices,
    create_baostock_universe_snapshot,
    export_baostock_universe_symbol_union,
    filter_bars_to_point_in_time_universe,
    record_baostock_universe_failure,
)
from cehuan.data.universe_import import import_historical_universe_csv
from cehuan.data.unknown_types import audit_unknown_security_types
from cehuan.research.qlib_bridge import qlib_momentum_signals, qlib_static_loader_check
from cehuan.research.baseline_diagnostics import run_baseline_sensitivity
from cehuan.research.baseline_readiness import build_baseline_readiness, readiness_input_hashes
from cehuan.review.reconcile import (
    compare_plan_revisions,
    compare_plan_and_fills,
    compare_positions_to_plan,
    read_fills,
    read_order_statuses,
    read_positions,
    review_markdown,
)
from cehuan.review.import_registry import find_fill_import_record, register_fill_import
from cehuan.review.fill_dedup import audit_cross_file_fills
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


def _configured_path(config: dict[str, object], key: str, argument: str | None) -> Path | None:
    value = argument or config.get("data", {}).get(key)
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


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
    snapshot.add_argument("--socket-timeout-seconds", type=float, default=20.0)
    snapshot.add_argument("--request-interval-seconds", type=float, default=1.0, help="BaoStock 单连接最小请求间隔")
    snapshot.add_argument("--max-retries", type=int, default=1, help="仅对连接错误执行的有限重试次数")
    snapshot.add_argument("--retry-base-seconds", type=float, default=2.0, help="指数退避初始等待秒数")
    snapshot.add_argument("--daily-request-budget", type=int, default=49000, help="低于 BaoStock 官方 50,000 次日限额的本地停止阈值")

    universe_snapshot = subs.add_parser(
        "universe-snapshot", help="按历史交易日抓取 BaoStock 证券列表，保存股票池、排除项和差异；可分批续跑",
    )
    universe_snapshot.add_argument("--start", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    universe_snapshot.add_argument("--end", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    universe_snapshot.add_argument("--snapshot-id", required=True, help="用于续跑的固定任务名")
    universe_snapshot.add_argument("--batch-size", type=int, help="本次最多抓取的交易日数量；省略时完成全部剩余日期")
    universe_snapshot.add_argument("--resume", action="store_true", help="从 data/private 中的 partial 任务续跑")
    universe_snapshot.add_argument("--defer-finalize", action="store_true", help="完成逐日列表后仍保留 partial，等待附加行情进行 ST/覆盖核对")
    universe_snapshot.add_argument("--bars", help="可选的规范 bars.csv，用于核对行情覆盖与退市样本覆盖")
    universe_snapshot.add_argument("--output", default="data/private/universes", help="私有股票池快照根目录")
    universe_snapshot.add_argument("--socket-timeout-seconds", type=float, default=20.0)
    universe_snapshot.add_argument("--request-interval-seconds", type=float, default=1.0, help="BaoStock 单连接最小请求间隔")
    universe_snapshot.add_argument("--max-retries", type=int, default=1, help="仅对连接错误执行的有限重试次数")
    universe_snapshot.add_argument("--retry-base-seconds", type=float, default=2.0, help="指数退避初始等待秒数")
    universe_snapshot.add_argument("--daily-request-budget", type=int, default=49000, help="低于 BaoStock 官方 50,000 次日限额的本地停止阈值")

    universe_import = subs.add_parser(
        "universe-import", help="导入带逐日来源日期的外部历史股票池 CSV，不使用当前名单回填历史",
    )
    universe_import.add_argument("--input", required=True, help="历史成员 CSV；字段见 docs/data-contract.md")
    universe_import.add_argument("--snapshot-id", required=True)
    universe_import.add_argument("--output", default="data/private/universes")

    universe_symbols = subs.add_parser(
        "universe-symbols", help="从完成验证的历史股票池导出实际出现过的 type=1 证券并集",
    )
    universe_symbols.add_argument("--universe", required=True, help="已封存快照或全部日期均已验证的 partial 路径")
    universe_symbols.add_argument("--output", required=True, help="输出包含 symbol 列的私有 CSV")

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
    backtest.add_argument("--universe", help="仅供 --diagnostic 使用的每日股票池 CSV 或快照目录")
    backtest.add_argument("--prevalidated-quality", help="universe-audit 生成且与已过滤 --data 哈希匹配的 point_in_time_quality.json")
    backtest.add_argument("--identity-events", help="证券身份变更事件 CSV；默认读取配置")
    backtest.add_argument("--out-date-evidence", help="正式模式下绑定的逐条退市边界证据登记表")
    backtest.add_argument("--unknown-type-review", help="正式模式下绑定的未知类型逐代码分类表")
    backtest.add_argument("--unknown-type-summary", help="未知类型审计生成的 summary.json")
    backtest.add_argument("--unknown-type-bars", help="未知类型行情覆盖检查所用的原始规范 bars.csv")
    backtest.add_argument("--formal", action="store_true", help="正式历史基线；必须提供通过审计且与本次输入完全匹配的 readiness 报告")
    backtest.add_argument("--diagnostic", action="store_true", help="显式运行非正式研究诊断，可使用配置回退规则")
    backtest.add_argument(
        "--accounting-mode", choices=("legacy", "ledger_shadow", "ledger_v2"), default="legacy",
        help="研究账务路径；ledger_v2 是非正式诊断，不能用于 formal",
    )
    backtest.add_argument("--readiness", help="由 baseline-readiness 生成的就绪报告")
    backtest.add_argument("--readiness-universe", help="正式模式下绑定 readiness 的点时股票池快照目录")
    backtest.add_argument("--readiness-quality", help="正式模式下绑定 readiness 的点时质量 JSON")
    backtest.add_argument("--corporate-actions", help="正式模式下使用的公司行动表")
    backtest.add_argument("--accounting-events", help="带来源原件与人工复核字段的事件 CSV；仅限 ledger_v2 研究诊断")
    backtest.add_argument("--accounting-opening-snapshot", help="ledger_v2 分窗口续跑使用的前一期 accounting_snapshot.json")
    backtest.add_argument("--backtest-checkpoint", help="完整 rule-based ledger_v2 研究策略续跑 checkpoint")
    backtest.add_argument("--trading-calendar", help="checkpoint 续跑所用交易日历 CSV，至少覆盖期末后的下一开市日")
    backtest.add_argument("--corporate-action-coverage", help="逐证券公司行动覆盖与来源复核表")
    backtest.add_argument("--market-rules", help="有效日期交易规则 CSV")
    backtest.add_argument("--fees", help="有效日期历史费率 CSV")
    backtest.add_argument("--evaluation-start", help="正式模式评价区间开始日期 YYYY-MM-DD")
    backtest.add_argument("--evaluation-end", help="正式模式评价区间结束日期 YYYY-MM-DD")

    universe_audit = subs.add_parser("universe-audit", help="核对每日股票池、行情覆盖、停牌状态差异和退市样本覆盖")
    universe_audit.add_argument("--universe", required=True, help="包含 manifest.json 与 securities_by_date.csv 的股票池快照目录")
    universe_audit.add_argument("--bars", required=True, help="规范 bars.csv")
    universe_audit.add_argument("--identity-events", help="证券身份变更事件 CSV；默认读取配置")
    universe_audit.add_argument("--out-date-evidence", help="逐条退市边界独立证据 CSV；默认读取配置")
    universe_audit.add_argument("--output", default="outputs/universe-audit", help="私有审计结果目录")

    unknown_types = subs.add_parser("universe-unknown-types", help="按证券代码汇总 UNKNOWN_TYPE 成员与行情窗口；不自动推断证券类型")
    unknown_types.add_argument("--universe", required=True, help="股票池快照目录或 securities_by_date.csv")
    unknown_types.add_argument("--bars", required=True, help="规范日线 CSV，仅用于报告行情行覆盖观察")
    unknown_types.add_argument("--output", required=True, help="CSV 复核登记表；复跑会保留已有人工分类")

    readiness = subs.add_parser("baseline-readiness", help="审计正式历史基线的输入完整性并生成散列绑定报告；不运行回测")
    readiness.add_argument("--data", required=True, help="经过点时过滤的规范 bars.csv")
    readiness.add_argument("--universe", required=True, help="包含 manifest.json 和 securities_by_date.csv 的快照目录")
    readiness.add_argument("--point-in-time-quality", required=True)
    readiness.add_argument("--identity-events", required=True)
    readiness.add_argument("--out-date-evidence", required=True)
    readiness.add_argument("--unknown-type-review", required=True)
    readiness.add_argument("--unknown-type-summary", help="默认读取 --unknown-type-review.csv.summary.json")
    readiness.add_argument("--unknown-type-bars", required=True)
    readiness.add_argument("--corporate-actions", required=True)
    readiness.add_argument("--corporate-action-coverage", required=True)
    readiness.add_argument("--market-rules", required=True)
    readiness.add_argument("--fees", required=True)
    readiness.add_argument("--benchmark-data", required=True)
    readiness.add_argument("--config")
    readiness.add_argument("--start", required=True, help="评价窗口开始日期 YYYY-MM-DD")
    readiness.add_argument("--end", required=True, help="评价窗口结束日期 YYYY-MM-DD")
    readiness.add_argument("--output", required=True, help="私有 baseline_readiness.json 输出路径")

    diagnostics = subs.add_parser("baseline-diagnostics", help="按日期股票池运行冻结基线及单假设敏感性对照")
    diagnostics.add_argument("--data", required=True, help="规范 bars.csv")
    diagnostics.add_argument("--universe", required=True, help="股票池快照目录或 securities_by_date.csv")
    diagnostics.add_argument("--benchmark-data", required=True, help="独立基准规范日线 CSV")
    diagnostics.add_argument("--cash", type=float, required=True, help="研究演示资金，不是账户资金")
    diagnostics.add_argument("--config", help="策略配置；默认使用冻结的 configs/default.toml")
    diagnostics.add_argument("--output", help="私有结果目录；默认创建带时间戳的 outputs 子目录")
    diagnostics.add_argument("--diagnostic", action="store_true", help="确认本命令只运行非正式敏感性诊断")

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
    review.add_argument("--original-plan", help="用户改动计划时，提供发出时原计划以生成逐字段修订差异")
    review.add_argument("--fills", required=True)
    review.add_argument("--positions", help="可选的成交后持仓快照 CSV")
    review.add_argument("--order-statuses", help="可选的带来源散列与时间戳的委托状态证据 CSV")
    review.add_argument("--import-registry", help="可选的本地成交导入登记簿；重复导入同一文件会拒绝")
    review.add_argument("--output-dir", help="复盘产物目录；必须不存在，默认创建唯一私有目录")
    review.add_argument("--mapping")

    fills_dedup = subs.add_parser(
        "fills-dedup", help="对规范成交 CSV 做保守的跨文件重叠分类；不会合并或删除记录",
    )
    fills_dedup.add_argument("--prior", action="append", required=True, help="先前成交 CSV；可重复指定")
    fills_dedup.add_argument("--current", required=True, help="本次待检查 CSV")
    fills_dedup.add_argument("--output", required=True, help="分类结果 CSV")
    fills_dedup.add_argument("--execution-id-column", help="经核实后显式指定的稳定成交标识列；默认不假设有此字段")
    fills_dedup.add_argument("--correction-link-column", help="显式指向原成交标识的更正/冲正关联列")
    fills_dedup.add_argument("--status-column", help="可选记录状态列，仅作来源字段保留，不单独推断冲正")

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
            with BaoStockSession(
                socket_timeout_seconds=args.socket_timeout_seconds,
                minimum_interval_seconds=args.request_interval_seconds,
                max_retries=args.max_retries,
                retry_base_seconds=args.retry_base_seconds,
                daily_request_budget=args.daily_request_budget,
                lock_path=REPO_ROOT / "data" / "private" / ".baostock-session.lock",
            ) as session:
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
            session = BaoStockSession(
                socket_timeout_seconds=args.socket_timeout_seconds,
                minimum_interval_seconds=args.request_interval_seconds,
                max_retries=args.max_retries,
                retry_base_seconds=args.retry_base_seconds,
                daily_request_budget=args.daily_request_budget,
                lock_path=REPO_ROOT / "data" / "private" / ".baostock-session.lock",
            )

            def report_universe_progress(item: dict[str, object]) -> None:
                completed_dates = int(item["completed_dates"])
                total_dates = int(item["total_dates"])
                if completed_dates == 1 or completed_dates % 25 == 0 or completed_dates == total_dates:
                    print(json.dumps({"event": "universe_checkpoint", **item}, ensure_ascii=False), flush=True)

            try:
                with session:
                    result = create_baostock_universe_snapshot(
                        session,
                        start_date=args.start,
                        end_date=args.end,
                        root=output,
                        snapshot_id=args.snapshot_id,
                        batch_size=args.batch_size,
                        resume=args.resume,
                        bars_path=bars,
                        defer_finalize=args.defer_finalize,
                        progress_callback=report_universe_progress,
                    )
            except KeyboardInterrupt:
                interrupted = RuntimeError("BaoStock 采集被操作员中断；最近已验证日期和请求日志已保留")
                interrupted.category = "operator_interrupted"
                record_baostock_universe_failure(
                    output, args.snapshot_id, args.start, args.end, session, interrupted,
                )
                print(str(interrupted), file=sys.stderr)
                return 130
            except Exception as exc:
                if getattr(exc, "category", None) != "local_concurrency_stop":
                    record_baostock_universe_failure(
                        output, args.snapshot_id, args.start, args.end, session, exc,
                    )
                raise
            if result.get("state") == "partial":
                resume_command = (
                    "uv run --extra baostock cehuan universe-snapshot "
                    f"--start {shlex.quote(args.start)} --end {shlex.quote(args.end)} "
                    f"--snapshot-id {shlex.quote(args.snapshot_id)} --resume --output {shlex.quote(args.output)} "
                    f"--socket-timeout-seconds {args.socket_timeout_seconds} "
                    f"--request-interval-seconds {args.request_interval_seconds} "
                    f"--max-retries {args.max_retries} --retry-base-seconds {args.retry_base_seconds} "
                    f"--daily-request-budget {args.daily_request_budget}"
                )
                if args.bars:
                    resume_command += f" --bars {shlex.quote(args.bars)}"
                result["resume_command"] = resume_command
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "universe-import":
            source = Path(args.input)
            output = Path(args.output)
            if not source.is_absolute():
                source = REPO_ROOT / source
            if not output.is_absolute():
                output = REPO_ROOT / output
            result = import_historical_universe_csv(source, output, args.snapshot_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "universe-symbols":
            universe = Path(args.universe)
            output = Path(args.output)
            if not universe.is_absolute():
                universe = REPO_ROOT / universe
            if not output.is_absolute():
                output = REPO_ROOT / output
            result = export_baostock_universe_symbol_union(universe, output)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "universe-audit":
            config = load_config()
            universe = Path(args.universe)
            bars = Path(args.bars)
            output = Path(args.output)
            if not universe.is_absolute():
                universe = REPO_ROOT / universe
            if not bars.is_absolute():
                bars = REPO_ROOT / bars
            if not output.is_absolute():
                output = REPO_ROOT / output
            identity_path = _configured_path(config, "security_identity_events_file", args.identity_events)
            out_date_path = _configured_path(config, "out_date_evidence_file", args.out_date_evidence)
            identity_events = read_security_identity_events(identity_path) if identity_path else None
            result = audit_baostock_universe_prices(
                universe,
                bars,
                output,
                security_identity_events=identity_events,
                security_identity_events_sha256=sha256_file(identity_path) if identity_path else None,
                out_date_evidence_path=out_date_path,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["healthy"] else 2

        if args.command == "universe-unknown-types":
            universe_path = Path(args.universe).expanduser()
            bars_path = Path(args.bars).expanduser()
            output_path = Path(args.output).expanduser()
            if not universe_path.is_absolute():
                universe_path = REPO_ROOT / universe_path
            if not bars_path.is_absolute():
                bars_path = REPO_ROOT / bars_path
            if not output_path.is_absolute():
                output_path = REPO_ROOT / output_path
            result = audit_unknown_security_types(universe_path, bars_path, output_path)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["classification_partition_balances"] else 2

        if args.command == "baseline-readiness":
            def repo_path(value: str) -> Path:
                path = Path(value).expanduser()
                return path if path.is_absolute() else REPO_ROOT / path

            data_path = repo_path(args.data)
            universe_path = repo_path(args.universe)
            quality_path = repo_path(args.point_in_time_quality)
            identity_path = repo_path(args.identity_events)
            out_date_path = repo_path(args.out_date_evidence)
            unknown_review_path = repo_path(args.unknown_type_review)
            unknown_summary_path = (
                repo_path(args.unknown_type_summary)
                if args.unknown_type_summary
                else unknown_review_path.with_suffix(unknown_review_path.suffix + ".summary.json")
            )
            unknown_bars_path = repo_path(args.unknown_type_bars)
            actions_path = repo_path(args.corporate_actions)
            action_coverage_path = repo_path(args.corporate_action_coverage)
            rules_path = repo_path(args.market_rules)
            fees_path = repo_path(args.fees)
            benchmark_path = repo_path(args.benchmark_data)
            config_path = repo_path(args.config) if args.config else REPO_ROOT / "configs" / "default.toml"
            output_path = repo_path(args.output)
            paths = (
                data_path, universe_path, quality_path, identity_path, out_date_path,
                unknown_review_path, unknown_summary_path, unknown_bars_path, actions_path,
                action_coverage_path, rules_path, fees_path, benchmark_path, config_path,
            )
            absent = [str(path) for path in paths if not path.exists()]
            if absent:
                raise FileNotFoundError("正式基线审计输入不存在：" + ", ".join(absent))
            hashes = readiness_input_hashes(
                data=data_path,
                point_in_time_quality=quality_path,
                universe=universe_path,
                identity_events=identity_path,
                out_date_evidence=out_date_path,
                unknown_type_review=unknown_review_path,
                unknown_type_summary=unknown_summary_path,
                unknown_type_bars=unknown_bars_path,
                corporate_actions=actions_path,
                corporate_action_coverage=action_coverage_path,
                market_rules=rules_path,
                fees=fees_path,
                benchmark=benchmark_path,
                config=config_path,
            )
            report = build_baseline_readiness(
                data=read_bars(data_path),
                input_hashes=hashes,
                universe=universe_path,
                identity_events_path=identity_path,
                out_date_evidence_path=out_date_path,
                unknown_type_review_path=unknown_review_path,
                unknown_type_summary_path=unknown_summary_path,
                unknown_type_bars_path=unknown_bars_path,
                point_in_time_quality_path=quality_path,
                corporate_actions_path=actions_path,
                corporate_action_coverage_path=action_coverage_path,
                market_rules_path=rules_path,
                fees_path=fees_path,
                benchmark=read_bars(benchmark_path),
                evaluation_start=args.start,
                evaluation_end=args.end,
            )
            _write_json(report, output_path)
            print(json.dumps({
                "readiness_report": str(output_path),
                "decision": report["decision"],
                "blockers": report["blockers"],
                "report_sha256": report["report_sha256"],
            }, ensure_ascii=False, indent=2))
            return 0 if report["ready"] else 2

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
            if not args.diagnostic:
                raise ValueError(
                    "baseline-diagnostics 只运行含规则回退的研究敏感性场景；请显式附加 --diagnostic，"
                    "不得将其结果称为正式基线"
                )
            identity_path = _configured_path(config, "security_identity_events_file", None)
            identity_events = read_security_identity_events(identity_path) if identity_path else None
            result = run_baseline_sensitivity(
                bars=read_bars(data_path),
                membership=pd.read_csv(universe_path, dtype=str, keep_default_na=False),
                benchmark=read_bars(benchmark_path),
                initial_cash_cny=args.cash,
                frozen_config=config,
                output_dir=output,
                security_identity_events=identity_events,
                diagnostic=True,
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
            if args.formal and args.diagnostic:
                raise ValueError("--formal 与 --diagnostic 不能同时使用")
            if args.formal and args.universe:
                raise ValueError(
                    "正式模式不接受 --universe；请先运行 universe-audit，并将其过滤后的 point_in_time_bars.csv "
                    "与对应 --prevalidated-quality 一起传入"
                )
            if (args.universe or args.prevalidated_quality) and not (args.formal or args.diagnostic):
                raise ValueError("点时股票池/预验证行情必须显式选择 --formal 或 --diagnostic")
            if args.accounting_mode != "legacy" and not args.diagnostic:
                raise ValueError("ledger 账务模式仅供非正式研究，请显式附加 --diagnostic")
            if args.accounting_events and (args.formal or not args.diagnostic or args.accounting_mode != "ledger_v2"):
                raise ValueError("--accounting-events 仅支持 --diagnostic --accounting-mode ledger_v2，不能用于正式基线")
            if args.accounting_opening_snapshot and (
                args.formal or not args.diagnostic or args.accounting_mode != "ledger_v2"
            ):
                raise ValueError(
                    "--accounting-opening-snapshot 仅支持 --diagnostic --accounting-mode ledger_v2，不能用于正式基线"
                )
            if args.backtest_checkpoint and (
                args.formal or not args.diagnostic or args.accounting_mode != "ledger_v2"
            ):
                raise ValueError("--backtest-checkpoint 仅支持 --diagnostic --accounting-mode ledger_v2")
            if args.backtest_checkpoint and args.accounting_opening_snapshot:
                raise ValueError("完整策略 checkpoint 已包含账本快照，不能同时传 --accounting-opening-snapshot")
            if args.trading_calendar and (args.formal or not args.diagnostic or args.accounting_mode != "ledger_v2"):
                raise ValueError("--trading-calendar checkpoint 模式仅支持 --diagnostic --accounting-mode ledger_v2")
            if args.backtest_checkpoint and not args.trading_calendar:
                raise ValueError("--backtest-checkpoint 必须同时提供 --trading-calendar")
            if not args.formal and (bool(args.market_rules) != bool(args.fees)):
                raise ValueError("研究诊断的 --market-rules 与 --fees 必须成对提供")
            if not args.formal and (args.market_rules or args.fees) and not args.diagnostic:
                raise ValueError("有效日期规则/费率只允许用于显式 --diagnostic 研究")
            if not args.formal and (args.market_rules or args.fees) and args.accounting_mode != "ledger_v2":
                raise ValueError("研究诊断使用有效日期规则/费率时必须设置 --accounting-mode ledger_v2")
            if not args.formal and args.readiness:
                raise ValueError("--readiness 只能与 --formal 一起使用")
            identity_path = _configured_path(config, "security_identity_events_file", args.identity_events)
            identity_events = read_security_identity_events(identity_path) if identity_path else None
            corporate_actions_path = Path(args.corporate_actions).expanduser() if args.corporate_actions else DEFAULT_CORPORATE_ACTIONS
            if not corporate_actions_path.is_absolute():
                corporate_actions_path = REPO_ROOT / corporate_actions_path
            corporate_actions = read_corporate_actions(corporate_actions_path)
            accounting_event_bundle = None
            accounting_event_path = None
            if args.accounting_events:
                accounting_event_path = Path(args.accounting_events).expanduser()
                if not accounting_event_path.is_absolute():
                    accounting_event_path = REPO_ROOT / accounting_event_path
                accounting_event_path = accounting_event_path.resolve()
                accounting_event_bundle = read_accounting_event_csv(accounting_event_path)
            opening_snapshot = None
            opening_snapshot_path = None
            opening_snapshot_sha256 = None
            if args.accounting_opening_snapshot:
                opening_snapshot_path = Path(args.accounting_opening_snapshot).expanduser()
                if not opening_snapshot_path.is_absolute():
                    opening_snapshot_path = REPO_ROOT / opening_snapshot_path
                opening_snapshot_path = opening_snapshot_path.resolve()
                opening_snapshot_sha256 = sha256_file(opening_snapshot_path)
                opening_snapshot = read_ledger_snapshot(opening_snapshot_path)
            backtest_checkpoint = None
            backtest_checkpoint_path = None
            backtest_checkpoint_file_sha256 = None
            if args.backtest_checkpoint:
                backtest_checkpoint_path = Path(args.backtest_checkpoint).expanduser()
                if not backtest_checkpoint_path.is_absolute():
                    backtest_checkpoint_path = REPO_ROOT / backtest_checkpoint_path
                backtest_checkpoint_path = backtest_checkpoint_path.resolve()
                backtest_checkpoint_file_sha256 = sha256_file(backtest_checkpoint_path)
                backtest_checkpoint = read_backtest_checkpoint(backtest_checkpoint_path)
            trading_calendar_path = None
            trading_calendar_sha256 = None
            trading_calendar = None
            if args.trading_calendar:
                trading_calendar_path = Path(args.trading_calendar).expanduser()
                if not trading_calendar_path.is_absolute():
                    trading_calendar_path = REPO_ROOT / trading_calendar_path
                trading_calendar_path = trading_calendar_path.resolve()
                trading_calendar_sha256 = sha256_file(trading_calendar_path)
                trading_calendar = pd.read_csv(trading_calendar_path, dtype=str, keep_default_na=False)
            readiness_report = None
            expected_readiness_hashes = None
            readiness_context = None
            evaluation_start = args.evaluation_start
            evaluation_end = args.evaluation_end
            if args.formal:
                required_formal = {
                    "--readiness": args.readiness,
                    "--readiness-universe": args.readiness_universe,
                    "--out-date-evidence": args.out_date_evidence,
                    "--unknown-type-review": args.unknown_type_review,
                    "--unknown-type-bars": args.unknown_type_bars,
                    "--corporate-actions": args.corporate_actions,
                    "--corporate-action-coverage": args.corporate_action_coverage,
                    "--market-rules": args.market_rules,
                    "--fees": args.fees,
                    "--benchmark-data": args.benchmark_data,
                    "--evaluation-start": evaluation_start,
                    "--evaluation-end": evaluation_end,
                }
                missing_formal = [name for name, value in required_formal.items() if not value]
                if missing_formal:
                    raise ValueError("正式基线缺少参数：" + ", ".join(missing_formal))
                if not args.prevalidated_quality:
                    raise ValueError(
                        "正式基线必须提供 --prevalidated-quality，且 --data 必须是同次 universe-audit 生成的过滤行情"
                    )
                quality_for_hash_value = args.prevalidated_quality
                if not identity_path:
                    raise ValueError("正式基线必须提供证券身份事件表")
                def formal_path(value: str) -> Path:
                    path = Path(value).expanduser()
                    return path if path.is_absolute() else REPO_ROOT / path
                quality_for_hash = formal_path(quality_for_hash_value)
                if args.readiness_quality:
                    readiness_quality_path = formal_path(args.readiness_quality)
                    if sha256_file(quality_for_hash) != sha256_file(readiness_quality_path):
                        raise ValueError("--readiness-quality 必须与 --prevalidated-quality 指向内容相同的点时质量报告")
                config_for_hash = formal_path(args.config) if args.config else REPO_ROOT / "configs" / "default.toml"
                data_for_hash = formal_path(args.data)
                readiness_universe = formal_path(args.readiness_universe)
                readiness_path = formal_path(args.readiness)
                actions_for_hash = formal_path(args.corporate_actions)
                action_coverage_for_hash = formal_path(args.corporate_action_coverage)
                rules_for_hash = formal_path(args.market_rules)
                fees_for_hash = formal_path(args.fees)
                benchmark_for_hash = formal_path(args.benchmark_data)
                unknown_review_for_hash = formal_path(args.unknown_type_review)
                unknown_summary_for_hash = (
                    formal_path(args.unknown_type_summary)
                    if args.unknown_type_summary
                    else unknown_review_for_hash.with_suffix(unknown_review_for_hash.suffix + ".summary.json")
                )
                unknown_bars_for_hash = formal_path(args.unknown_type_bars)
                if not readiness_universe.is_dir():
                    raise ValueError("--readiness-universe 必须是完整股票池快照目录")
                expected_readiness_hashes = readiness_input_hashes(
                    data=data_for_hash,
                    point_in_time_quality=quality_for_hash,
                    universe=readiness_universe,
                    identity_events=identity_path,
                    out_date_evidence=formal_path(args.out_date_evidence),
                    unknown_type_review=unknown_review_for_hash,
                    unknown_type_summary=unknown_summary_for_hash,
                    unknown_type_bars=unknown_bars_for_hash,
                    corporate_actions=actions_for_hash,
                    corporate_action_coverage=action_coverage_for_hash,
                    market_rules=rules_for_hash,
                    fees=fees_for_hash,
                    benchmark=benchmark_for_hash,
                    config=config_for_hash,
                )
                readiness_context = {
                    "data": data_for_hash,
                    "point_in_time_quality": quality_for_hash,
                    "universe": readiness_universe,
                    "identity_events": identity_path,
                    "out_date_evidence": formal_path(args.out_date_evidence),
                    "unknown_type_review": unknown_review_for_hash,
                    "unknown_type_summary": unknown_summary_for_hash,
                    "unknown_type_bars": unknown_bars_for_hash,
                    "corporate_actions": actions_for_hash,
                    "corporate_action_coverage": action_coverage_for_hash,
                    "market_rules": rules_for_hash,
                    "fees": fees_for_hash,
                    "benchmark": benchmark_for_hash,
                    "config": config_for_hash,
                }
                readiness_report = json.loads(readiness_path.read_text(encoding="utf-8"))
            prevalidated_quality = None
            source_data_sha256 = None
            mutate_input = False
            quality_argument = args.prevalidated_quality or (args.readiness_quality if args.formal else None)
            if quality_argument:
                if args.prevalidated_quality and (args.universe or args.security_master):
                    raise ValueError("--prevalidated-quality 仅用于已完成点时股票池过滤与证券主数据校验的规范 CSV")
                quality_path = Path(quality_argument)
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
                bars_path = Path(args.data).expanduser()
                if not bars_path.is_absolute():
                    bars_path = REPO_ROOT / bars_path
                bars_path = bars_path.resolve()
                bars = read_bars(bars_path)
                source_data_sha256 = sha256_file(bars_path)
            if quality_argument:
                bars_path = Path(args.data).expanduser()
                if not bars_path.is_absolute():
                    bars_path = REPO_ROOT / bars_path
                bars_path = bars_path.resolve()
            universe_summary = None
            universe_discrepancies = None
            if args.universe:
                universe_path = Path(args.universe)
                if not universe_path.is_absolute():
                    universe_path = REPO_ROOT / universe_path
                if universe_path.is_dir():
                    universe_path = universe_path / "securities_by_date.csv"
                membership = pd.read_csv(universe_path, dtype=str, keep_default_na=False)
                bars, universe_summary, universe_discrepancies = filter_bars_to_point_in_time_universe(
                    bars, membership, security_identity_events=identity_events,
                )
                reconciliation = universe_summary["reconciliation_quality"]
                if not reconciliation["healthy"]:
                    raise ValueError(
                        "点时股票池对账未通过，不能把该结果作为正式基线："
                        + "; ".join(reconciliation["errors"])
                        + "；请先运行 universe-audit 并核对逐条差异"
                    )
                if bars.empty:
                    raise ValueError("每日股票池与行情没有交集，无法回测")
            if args.security_master:
                bars = apply_security_master(bars, pd.read_csv(args.security_master))
            benchmark = read_bars(args.benchmark_data) if args.benchmark_data else None
            research_market_rules = None
            research_fee_schedule = None
            research_market_rules_path = None
            research_fee_schedule_path = None
            research_market_rules_sha256 = None
            research_fee_schedule_sha256 = None
            if not args.formal and args.market_rules and args.fees:
                rules_path = Path(args.market_rules).expanduser()
                fees_path = Path(args.fees).expanduser()
                if not rules_path.is_absolute():
                    rules_path = REPO_ROOT / rules_path
                if not fees_path.is_absolute():
                    fees_path = REPO_ROOT / fees_path
                research_market_rules_path = rules_path.resolve()
                research_fee_schedule_path = fees_path.resolve()
                research_market_rules_sha256 = sha256_file(research_market_rules_path)
                research_fee_schedule_sha256 = sha256_file(research_fee_schedule_path)
                research_market_rules = pd.read_csv(rules_path, dtype=str, keep_default_na=False)
                research_fee_schedule = pd.read_csv(fees_path, dtype=str, keep_default_na=False)
            run_started_at_utc = datetime.now(timezone.utc).isoformat()
            result = run_backtest(
                bars,
                args.cash,
                config,
                benchmark_frame=benchmark,
                prevalidated_quality=prevalidated_quality,
                source_data_sha256=source_data_sha256,
                security_identity_events=identity_events,
                corporate_actions=corporate_actions,
                mutate_input=mutate_input,
                formal=args.formal,
                readiness_report=readiness_report,
                readiness_input_hashes=expected_readiness_hashes,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                readiness_context=readiness_context,
                accounting_mode=args.accounting_mode,
                market_rule_schedule=research_market_rules,
                fee_schedule=research_fee_schedule,
                accounting_events=accounting_event_bundle.events if accounting_event_bundle else None,
                dividend_tax_cny_by_event=(
                    accounting_event_bundle.dividend_tax_cny_by_event if accounting_event_bundle else None
                ),
                dividend_tax_treatment_by_event=(
                    accounting_event_bundle.dividend_tax_treatment_by_event if accounting_event_bundle else None
                ),
                dividend_payment_withholding_per_share_cny_by_event=(
                    accounting_event_bundle.dividend_payment_withholding_per_share_cny_by_event
                    if accounting_event_bundle else None
                ),
                rights_offering_decisions_by_event=(
                    accounting_event_bundle.rights_offering_decisions_by_event
                    if accounting_event_bundle else None
                ),
                opening_accounting_snapshot=opening_snapshot,
                backtest_checkpoint=backtest_checkpoint,
                trading_calendar=trading_calendar,
                source_data_path=bars_path,
            )
            run_completed_at_utc = datetime.now(timezone.utc).isoformat()
            if sha256_file(bars_path) != source_data_sha256:
                raise ValueError("回测期间当前窗口行情文件发生变化；本次实验结果不予保存")
            if opening_snapshot_path is not None:
                if sha256_file(opening_snapshot_path) != opening_snapshot_sha256:
                    raise ValueError("回测期间期初账本快照文件发生变化；本次实验结果不予保存")
                result["metrics"]["accounting_opening_snapshot_file_sha256"] = opening_snapshot_sha256
            if backtest_checkpoint_path is not None:
                if sha256_file(backtest_checkpoint_path) != backtest_checkpoint_file_sha256:
                    raise ValueError("回测期间 backtest checkpoint 文件发生变化；本次实验结果不予保存")
                if (
                    backtest_checkpoint is not None
                    and backtest_checkpoint.source_data_path
                    and sha256_file(backtest_checkpoint.source_data_path)
                    != backtest_checkpoint.last_window_data_sha256
                ):
                    raise ValueError("checkpoint 绑定的历史行情文件在回测期间发生变化；本次实验结果不予保存")
                result["metrics"]["backtest_checkpoint_input_file_sha256"] = backtest_checkpoint_file_sha256
            if trading_calendar_path is not None:
                if sha256_file(trading_calendar_path) != trading_calendar_sha256:
                    raise ValueError("回测期间交易日历文件发生变化；本次实验结果不予保存")
                result["metrics"]["trading_calendar_input_sha256"] = trading_calendar_sha256
            if accounting_event_bundle is not None:
                if sha256_file(accounting_event_path) != accounting_event_bundle.source_csv_sha256:
                    raise ValueError("回测期间公司行动事件 CSV 发生变化；本次实验结果不予保存")
                for _, source in accounting_event_bundle.source_audit.iterrows():
                    if sha256_file(source["source_archive_file"]) != source["observed_sha256"]:
                        raise ValueError(f"回测期间来源原件发生变化：{source['source_id']}；本次实验结果不予保存")
                result["metrics"]["accounting_event_csv_sha256"] = accounting_event_bundle.source_csv_sha256
                result["metrics"]["accounting_event_count"] = len(accounting_event_bundle.events)
            if universe_summary is not None:
                result["metrics"]["point_in_time_universe_audit"] = universe_summary
            if research_market_rules_path is not None and research_fee_schedule_path is not None:
                if sha256_file(research_market_rules_path) != research_market_rules_sha256:
                    raise ValueError("回测期间市场规则文件发生变化；本次实验结果不予保存")
                if sha256_file(research_fee_schedule_path) != research_fee_schedule_sha256:
                    raise ValueError("回测期间费率文件发生变化；本次实验结果不予保存")
                result["metrics"]["market_rule_schedule_input_sha256"] = research_market_rules_sha256
                result["metrics"]["fee_schedule_input_sha256"] = research_fee_schedule_sha256
            experiment = str(result["metrics"]["experiment_id"])
            folder = REPO_ROOT / "outputs" / "experiments" / experiment
            folder.mkdir(parents=True, exist_ok=True)
            if accounting_event_bundle is not None and accounting_event_path is not None:
                shutil.copyfile(accounting_event_path, folder / "accounting_events.csv")
                accounting_event_bundle.source_audit.to_csv(folder / "accounting_event_source_audit.csv", index=False)
            if opening_snapshot_path is not None:
                shutil.copyfile(opening_snapshot_path, folder / "accounting_opening_snapshot.json")
            if backtest_checkpoint_path is not None:
                shutil.copyfile(backtest_checkpoint_path, folder / "backtest_opening_checkpoint.json")
            if trading_calendar_path is not None:
                shutil.copyfile(trading_calendar_path, folder / "trading_calendar.csv")
            if research_market_rules_path is not None and research_fee_schedule_path is not None:
                shutil.copyfile(research_market_rules_path, folder / "market_rule_schedule.csv")
                shutil.copyfile(research_fee_schedule_path, folder / "fee_schedule.csv")
            _write_json(result["metrics"], folder / "metrics.json")
            result["equity_curve"].to_csv(folder / "equity_curve.csv", index=False)
            candidate_pool_path = folder / "candidate-pool-by-signal.csv"
            result["candidate_pool"].to_csv(candidate_pool_path, index=False)
            candidate_pool_sha256 = sha256_file(candidate_pool_path)
            result["metrics"]["candidate_pool_file_sha256"] = candidate_pool_sha256
            _write_json(result["metrics"], folder / "metrics.json")
            result["trades"].to_csv(folder / "trades.csv", index=False)
            result["blocked_orders"].to_csv(folder / "blocked_orders.csv", index=False)
            result["security_events"].to_csv(folder / "security_events.csv", index=False)
            result["open_positions"].to_csv(folder / "open_positions.csv", index=False)
            if "accounting_journal" in result:
                result["accounting_journal"].to_csv(folder / "accounting_journal.csv", index=False)
            if "accounting_snapshot" in result:
                snapshot = result["accounting_snapshot"]
                _write_json(asdict(snapshot) if is_dataclass(snapshot) else snapshot, folder / "accounting_snapshot.json")
            if "backtest_checkpoint" in result:
                checkpoint_path = folder / "backtest_checkpoint.json"
                _write_json(result["backtest_checkpoint"].to_dict(), checkpoint_path)
                result["metrics"]["backtest_checkpoint_file_sha256"] = sha256_file(checkpoint_path)
                _write_json(result["metrics"], folder / "metrics.json")
            if universe_summary is not None:
                _write_json(universe_summary, folder / "point_in_time_universe_audit.json")
                universe_discrepancies.to_csv(folder / "point_in_time_universe_discrepancies.csv", index=False)
            (folder / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _write_json({
                "schema_version": 1,
                "manifest_type": "BACKTEST_RUNTIME_ARTIFACT_MANIFEST",
                "experiment_id": experiment,
                "run_started_at_utc": run_started_at_utc,
                "run_completed_at_utc": run_completed_at_utc,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "runtime_artifacts": {
                    "candidate-pool-by-signal.csv": {
                        "path": "candidate-pool-by-signal.csv",
                        "sha256": candidate_pool_sha256,
                        "row_count": int(len(result["candidate_pool"])),
                        "signal_dates": sorted(result["candidate_pool"]["signal_date"].astype(str).unique().tolist()),
                        "generation_source": "candidate rows captured from rank_precomputed_day inside run_backtest and sealed by this CLI run output manifest",
                        "generation_timestamp_precision": "within_run_interval_only; per-row historical publication time is not proved",
                    }
                },
                "input_bindings": {
                    "source_input_file_sha256": result["metrics"].get("current_window_input_file_sha256"),
                    "normalized_data_version_sha256": result["metrics"].get("data_version_sha256"),
                    "config_sha256": result["metrics"].get("config_sha256"),
                    "code_commit": result["metrics"].get("git_commit"),
                    "code_worktree_dirty": result["metrics"].get("git_dirty"),
                },
                "authenticity": {
                    "manifest_signature": "UNSIGNED",
                    "trusted_external_timestamp": "UNVERIFIED",
                    "meaning": "The manifest binds the candidate artifact produced by this CLI run; it is not tamper-proof proof of wall-clock creation time or vendor availability.",
                },
                "result_type": result["metrics"].get("result_type"),
                "broker_connected": False,
                "orders_generated": False,
            }, folder / "experiment-manifest.json")
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
            def input_path(value: str) -> Path:
                path = Path(value).expanduser()
                return (path if path.is_absolute() else REPO_ROOT / path).resolve()

            plan_path = input_path(args.plan)
            fills_path = input_path(args.fills)
            original_plan_path = input_path(args.original_plan) if args.original_plan else None
            positions_path = input_path(args.positions) if args.positions else None
            statuses_path = input_path(args.order_statuses) if args.order_statuses else None
            mapping_path = input_path(args.mapping) if args.mapping else REPO_ROOT / "configs" / "huatai_columns.toml"
            tracked_inputs = {
                "plan": plan_path,
                "fills": fills_path,
                "mapping": mapping_path,
            }
            if original_plan_path:
                tracked_inputs["original_plan"] = original_plan_path
            if positions_path:
                tracked_inputs["positions"] = positions_path
            if statuses_path:
                tracked_inputs["order_statuses"] = statuses_path
            input_hashes_before = {name: sha256_file(path) for name, path in tracked_inputs.items()}

            plan_frame = pd.read_csv(plan_path)
            fills = read_fills(fills_path, mapping_path)
            order_statuses = read_order_statuses(statuses_path) if statuses_path else None
            if order_statuses is not None:
                for _, status_row in order_statuses.iterrows():
                    evidence_key = f"order_status_evidence:{status_row['order_id']}"
                    evidence_path = Path(status_row["evidence_file"]).resolve()
                    evidence_hash = sha256_file(evidence_path)
                    if evidence_hash != status_row["evidence_sha256"]:
                        raise ValueError(f"委托状态来源原件在解析后发生变化：{status_row['order_id']}")
                    tracked_inputs[evidence_key] = evidence_path
                    input_hashes_before[evidence_key] = evidence_hash
            report = compare_plan_and_fills(plan_frame, fills, order_statuses=order_statuses)
            position_report = None
            if positions_path:
                positions = read_positions(positions_path, mapping_path)
                position_report = compare_positions_to_plan(plan_frame, positions)
            plan_revision_report = None
            if original_plan_path:
                original_plan_frame = pd.read_csv(original_plan_path)
                plan_revision_report = compare_plan_revisions(original_plan_frame, plan_frame)
                if not plan_revision_report.empty:
                    plan_revision_report.insert(0, "change_source", "reviewer_supplied_revision_file")
                    plan_revision_report.insert(0, "evidence", "field-level diff; user identity is not independently verified")
            input_hashes_after = {name: sha256_file(path) for name, path in tracked_inputs.items()}
            if input_hashes_before != input_hashes_after:
                raise ValueError("复盘期间输入文件发生变化；拒绝保存不一致结果")

            run_id = f"review-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
            root = Path(args.output_dir).expanduser() if args.output_dir else REPO_ROOT / "outputs" / "reviews" / run_id
            if not root.is_absolute():
                root = REPO_ROOT / root
            root = root.resolve()
            registry_path = input_path(args.import_registry) if args.import_registry else None
            fills_sha256 = input_hashes_before["fills"]
            plan_ids = plan_frame["plan_id"].dropna().astype(str).unique().tolist() if "plan_id" in plan_frame else []
            cycle_run_id = plan_ids[0] if len(plan_ids) == 1 else None
            if root.exists():
                manifest_path = root / "manifest.json"
                if registry_path is not None and manifest_path.is_file():
                    existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    existing_import_reference = existing_manifest.get("fill_import_reference") or {}
                    if (
                        existing_manifest.get("result_type") == "manual_account_reconciliation_csv_not_broker_connected"
                        and existing_manifest.get("input_sha256") == input_hashes_before
                        and existing_manifest.get("cycle_run_id") == cycle_run_id
                        and existing_import_reference.get("source_sha256") == fills_sha256
                    ):
                        existing_record = find_fill_import_record(fills_path, registry_path)
                        existing_manifest_hash = sha256_file(manifest_path)
                        if existing_record is not None:
                            same_run = (
                                existing_record.get("output_manifest_path") == str(manifest_path)
                                and existing_record.get("output_manifest_sha256") == existing_manifest_hash
                            )
                            if same_run:
                                print(f"复盘和成交导入已完成；按原运行恢复成功：{manifest_path}")
                                return 0
                            raise ValueError("该成交文件已登记到另一份复盘产物；本次不会重复计入")
                        receipt = register_fill_import(
                            fills_path,
                            registry_path,
                            row_count=len(fills),
                            cycle_run_id=cycle_run_id,
                            output_manifest_path=manifest_path,
                        )
                        print(f"恢复已发布的复盘并完成成交登记：{manifest_path} ({receipt['import_id']})")
                        return 0
                raise FileExistsError(f"复盘输出目录已经存在，拒绝覆盖：{root}")
            fill_import_reference = None
            if registry_path is not None:
                fill_import_reference = {
                    "import_id": f"fill-import-{fills_sha256[:16]}",
                    "source_sha256": fills_sha256,
                    "registry_path": str(registry_path),
                }
            staging = root.with_name(f".{root.name}.staging-{uuid.uuid4().hex[:8]}")
            root.parent.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=False, exist_ok=False)
            published = False
            try:
                report.to_csv(staging / "review.csv", index=False)
                if position_report is not None:
                    position_report.to_csv(staging / "review_positions.csv", index=False)
                if order_statuses is not None:
                    order_statuses.to_csv(staging / "review_order_statuses.csv", index=False)
                if plan_revision_report is not None:
                    plan_revision_report.to_csv(staging / "plan_revision_diff.csv", index=False)
                markdown = review_markdown(report, position_report)
                if plan_revision_report is not None:
                    markdown += "\n## 计划修订留痕\n\n"
                    markdown += f"修改字段的计划行数：{len(plan_revision_report)}。证据类型：用户提供的原计划与修改后计划 CSV 差异；系统不独立验证修改人身份。\n"
                    if plan_revision_report.empty:
                        markdown += "两份计划字段相同。\n"
                (staging / "review.md").write_text(markdown, encoding="utf-8")
                manifest = {
                    "schema_version": 1,
                    "run_id": run_id,
                    "cycle_run_id": cycle_run_id,
                    "result_type": "manual_account_reconciliation_csv_not_broker_connected",
                    "input_sha256": input_hashes_before,
                    "input_paths": {name: str(path) for name, path in tracked_inputs.items()},
                    "position_snapshot_status": "PROVIDED_CSV" if positions_path else "NOT_PROVIDED",
                    "order_status_evidence_status": "PROVIDED" if statuses_path else "NOT_PROVIDED",
                    "plan_revision_diff_status": "PROVIDED" if original_plan_path else "NOT_PROVIDED",
                    "fill_import_reference": fill_import_reference,
                    "broker_connection_or_order": False,
                }
                _write_json(manifest, staging / "manifest.json")
                input_hashes_at_publish = {name: sha256_file(path) for name, path in tracked_inputs.items()}
                if input_hashes_before != input_hashes_at_publish:
                    raise ValueError("复盘产物写入期间输入文件发生变化；拒绝登记成交")
                os.replace(staging, root)
                published = True
                import_receipt = None
                if registry_path is not None:
                    import_receipt = register_fill_import(
                        fills_path,
                        registry_path,
                        row_count=len(fills),
                        cycle_run_id=cycle_run_id,
                        output_manifest_path=root / "manifest.json",
                    )
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                if published:
                    shutil.rmtree(root, ignore_errors=True)
                raise
            print(markdown)
            print(f"复盘文件：{root / 'review.md'}\n运行清单：{root / 'manifest.json'}")
            if import_receipt is not None:
                print(f"成交导入登记：{registry_path} ({import_receipt['import_id']})")
            return 0

        if args.command == "fills-dedup":
            def input_path(value: str) -> Path:
                path = Path(value).expanduser()
                return (path if path.is_absolute() else REPO_ROOT / path).resolve()

            report = audit_cross_file_fills(
                [input_path(value) for value in args.prior],
                input_path(args.current),
                execution_id_column=args.execution_id_column,
                correction_link_column=args.correction_link_column,
                status_column=args.status_column,
            )
            output = Path(args.output).expanduser()
            if not output.is_absolute():
                output = REPO_ROOT / output
            output = output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="") as handle:
                report.to_csv(handle, index=False)
            counts = report["classification"].value_counts().to_dict() if not report.empty else {}
            summary = {
                "result_type": "synthetic_or_user_normalized_fill_overlap_audit_not_broker_validated",
                "rows": len(report),
                "classification_counts": counts,
                "execution_id_column_assumed": args.execution_id_column,
                "automatic_rows_removed": 0,
                "output": str(output),
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
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
