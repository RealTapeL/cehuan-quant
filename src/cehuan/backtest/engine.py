from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from cehuan import __version__
from cehuan.accounting import (
    CashDividendEvent,
    CodeChangeEvent,
    FeeItem,
    LedgerSnapshot,
    PortfolioLedger,
    PriceMark,
    ShareDistributionEvent,
    TradeFill,
)
from cehuan.backtest.checkpoint import BacktestCheckpoint, CheckpointPrice, SignalHistoryBar
from cehuan.backtest.accounting_shadow import run_accounting_shadow
from cehuan.backtest.event_scheduler import AccountingEventSchedule, SupportedAccountingEvent
from cehuan.config import load_config
from cehuan.data.canonical import BAR_COLUMNS, normalize_bar_frame, read_bars
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.corporate_actions import normalize_corporate_actions, read_corporate_actions
from cehuan.data.fee_schedule import map_fee_schedule_to_sessions, normalize_fee_schedule, scheduled_fee_components
from cehuan.data.market_rules import attach_market_rule_schedule, normalize_market_rule_schedule
from cehuan.data.quality import check_data_quality
from cehuan.data.security_identity import (
    normalize_security_identity_events,
    read_security_identity_events,
)
from cehuan.research.baseline_readiness import (
    READINESS_CONTEXT_KEYS,
    build_baseline_readiness,
    readiness_input_hashes as compute_readiness_input_hashes,
    require_baseline_ready,
    require_report_matches_recomputed,
)
from cehuan.signals.strategy import (
    add_momentum,
    order_constraints,
    rank_precomputed_day,
    round_order_quantity,
    status_is_buy_eligible,
    status_is_sell_eligible,
)
from cehuan.data.adjustment import select_signal_prices


def _git_metadata() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    commit: str | None = None
    dirty: bool | None = None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        pass
    try:
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=root,
            check=True, capture_output=True, text=True,
        ).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"git_commit": commit, "git_dirty": dirty}


def _normalized_event_table_digest(frame: pd.DataFrame) -> str:
    """Hash a normalized event table independent of CSV formatting and dtypes."""
    canonical = frame.copy()
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_table_digest(frame: pd.DataFrame) -> str:
    canonical = frame.copy()
    canonical = canonical.sort_values(list(canonical.columns), kind="stable", na_position="first")
    for column in canonical.columns:
        canonical[column] = canonical[column].astype("string").fillna("")
    payload = canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fees(notional: float, side: str, costs: dict[str, Any]) -> float:
    return sum(_fee_components(notional, side, costs).values())


def _fee_components(notional: float, side: str, costs: dict[str, Any]) -> dict[str, float]:
    commission = max(
        float(costs.get("minimum_commission_cny", 0.0)),
        notional * float(costs.get("commission_rate", 0.0)),
    )
    stamp = notional * float(costs.get("stamp_duty_sell_rate", 0.0)) if side == "SELL" else 0.0
    transfer = notional * float(costs.get("transfer_fee_rate", 0.0))
    return {
        "commission_cny": commission,
        "stamp_duty_cny": stamp,
        "transfer_fee_cny": transfer,
    }


def _price_limit_block(
    row: pd.Series,
    previous_close: float | None,
    side: str,
    market: dict[str, Any],
    *,
    require_quote_bounds: bool = False,
) -> str | None:
    if previous_close is None or pd.isna(previous_close) or not math.isfinite(float(previous_close)) or float(previous_close) <= 0:
        if require_quote_bounds:
            return "计算价格约束缺少有效前收盘，按保守规则阻断"
        return None
    previous_close = float(previous_close)
    has_limit = row.get("has_price_limit", pd.NA)
    if pd.isna(has_limit):
        has_limit = bool(market.get("assume_price_limit_applies_when_unknown", True))
    if not bool(has_limit):
        if require_quote_bounds:
            return "当日无涨跌幅限制，但缺少适用的开盘报价边界数据，按保守规则阻断"
        return None
    pct_value = row.get("price_limit_pct", pd.NA)
    limit_pct = float(pct_value) if not pd.isna(pct_value) else float(market["default_price_limit_pct"])
    tick = float(market.get("price_tick_cny", 0.01))
    if side == "BUY" and float(row["open"]) >= previous_close * (1.0 + limit_pct) - tick / 2:
        return "开盘价触及配置涨停阈值，按保守规则不模拟买入"
    if side == "SELL" and float(row["open"]) <= previous_close * (1.0 - limit_pct) + tick / 2:
        return "开盘价触及配置跌停阈值，按保守规则不模拟卖出"
    return None


def _require_code_change_open_eligibility(row: pd.Series, day_date: str, symbol: str) -> None:
    """Check only facts needed at the simulated open; full OHLC is audited separately."""
    open_value = row.get("open")
    if pd.isna(open_value) or not math.isfinite(float(open_value)) or float(open_value) <= 0:
        raise ValueError(f"{day_date} {symbol} 生效日开盘价无效，不能迁移持仓")
    if pd.isna(row.get("is_suspended")) or bool(row["is_suspended"]):
        raise ValueError(f"{day_date} {symbol} 生效日停牌状态未知或为停牌，不能迁移持仓")
    if pd.isna(row.get("is_listed")) or not bool(row["is_listed"]):
        raise ValueError(f"{day_date} {symbol} 生效日上市状态未知或未上市，不能迁移持仓")


def _metrics(equity: pd.Series, returns: pd.Series, turnover: float, benchmark: pd.Series) -> dict[str, Any]:
    if equity.empty:
        return {}
    valid_returns = returns.dropna()
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0) if equity.iloc[0] else 0.0
    periods = max(1, len(equity) - 1)
    annual_return = float((equity.iloc[-1] / equity.iloc[0]) ** (252 / periods) - 1.0) if equity.iloc[0] else 0.0
    annual_vol = float(valid_returns.std(ddof=1) * np.sqrt(252)) if len(valid_returns) > 1 else 0.0
    sharpe = float(valid_returns.mean() / valid_returns.std(ddof=1) * np.sqrt(252)) if len(valid_returns) > 1 and valid_returns.std(ddof=1) else None
    drawdown = equity.div(equity.cummax()).sub(1.0)
    benchmark_return = float(benchmark.iloc[-1] / benchmark.iloc[0] - 1.0) if not benchmark.empty and benchmark.iloc[0] else None
    return {
        "total_return": total_return,
        "annualized_return": annual_return,
        "annualized_volatility": annual_vol,
        "max_drawdown": float(drawdown.min()),
        "daily_win_rate": float((valid_returns > 0).mean()) if len(valid_returns) else None,
        "turnover_over_average_equity": turnover,
        "sharpe_zero_risk_free": sharpe,
        "benchmark_return": benchmark_return,
        "excess_return_vs_benchmark": total_return - benchmark_return if benchmark_return is not None else None,
        # Legacy aliases retained for existing experiment consumers.
        "peer_benchmark_return": benchmark_return,
        "excess_return_vs_peer_benchmark": total_return - benchmark_return if benchmark_return is not None else None,
        "return_period_count": periods,
    }


def _chronological_evaluation(
    curve: pd.DataFrame,
    benchmark: pd.Series,
    trades: pd.DataFrame,
    development_fraction: float,
    validation_fraction: float,
    rolling_window_sessions: int,
    rolling_step_sessions: int,
) -> dict[str, Any]:
    count = len(curve)
    if not 0 < development_fraction < 1 or not 0 < validation_fraction < 1:
        raise ValueError("回测 development_fraction 与 validation_fraction 必须在 0 和 1 之间")
    if development_fraction + validation_fraction >= 1:
        raise ValueError("development_fraction 与 validation_fraction 之和必须小于 1")
    if rolling_window_sessions < 2:
        raise ValueError("rolling_window_sessions 必须至少为 2")
    if rolling_step_sessions < 1:
        raise ValueError("rolling_step_sessions 必须大于 0")
    if count < 5:
        return {"available": False, "reason": "少于 5 个日期点，无法形成三个非空时间区间"}

    development_end = min(count - 2, max(1, int(count * development_fraction)))
    validation_end = min(count - 1, max(development_end + 1, int(count * (development_fraction + validation_fraction))))
    boundaries = [
        ("development", 0, development_end, 0, development_end),
        ("validation", development_end, validation_end, development_end - 1, validation_end),
        ("test", validation_end, count, validation_end - 1, count),
    ]
    periods: dict[str, Any] = {}
    for label, trade_start, trade_end, equity_start, equity_end in boundaries:
        equity = curve["equity_cny"].iloc[equity_start:equity_end].reset_index(drop=True)
        returns = curve["return"].iloc[trade_start:trade_end].reset_index(drop=True)
        peer = benchmark.iloc[equity_start:equity_end].reset_index(drop=True)
        start_date = str(curve.iloc[trade_start]["trade_date"])
        end_date = str(curve.iloc[trade_end - 1]["trade_date"])
        period_trades = trades.loc[
            trades["trade_date"].astype(str).between(start_date, end_date, inclusive="both")
        ] if not trades.empty else trades
        turnover = float(period_trades["notional_cny"].sum()) / max(float(equity.mean()), 1.0) if not period_trades.empty else 0.0
        result = _metrics(equity, returns, turnover, peer)
        result.update({"start": start_date, "end": end_date, "trade_count": int(len(period_trades))})
        periods[label] = result
    rolling: dict[str, Any] = {
        "available": False,
        "method": "fixed-rule continuous portfolio diagnostics; no parameter fitting or fold reset",
        "window_sessions": rolling_window_sessions,
        "step_sessions": rolling_step_sessions,
        "windows": [],
        "reason": (
            f"当前只有 {count} 个日期点，少于滚动窗口所需的 {rolling_window_sessions} 个日期点"
            if count < rolling_window_sessions else None
        ),
        "interpretation": "用于观察固定规则组合在连续时间窗中的表现稳定性；不属于模型训练、参数滚动选择或滚动样本外验证。",
    }
    if count >= rolling_window_sessions:
        rolling_windows: list[dict[str, Any]] = []
        trade_dates = trades["trade_date"].astype(str) if not trades.empty else pd.Series(dtype=str)
        for window_number, start in enumerate(
            range(0, count - rolling_window_sessions + 1, rolling_step_sessions),
            start=1,
        ):
            end = start + rolling_window_sessions
            window_curve = curve["equity_cny"].iloc[start:end].reset_index(drop=True)
            # The first point establishes the window's capital base. Subsequent
            # returns are aligned to the remaining equity observations.
            window_returns = curve["return"].iloc[start + 1:end].reset_index(drop=True)
            window_benchmark = benchmark.iloc[start:end].reset_index(drop=True)
            start_date = str(curve.iloc[start]["trade_date"])
            end_date = str(curve.iloc[end - 1]["trade_date"])
            window_trades = (
                trades.loc[trade_dates.gt(start_date) & trade_dates.le(end_date)]
                if not trades.empty else trades
            )
            turnover = (
                float(window_trades["notional_cny"].sum()) / max(float(window_curve.mean()), 1.0)
                if not window_trades.empty else 0.0
            )
            result = _metrics(window_curve, window_returns, turnover, window_benchmark)
            result.update({
                "window": window_number,
                "start": start_date,
                "end": end_date,
                "start_session_index": start,
                "end_session_index_exclusive": end,
                "trade_count": int(len(window_trades)),
            })
            rolling_windows.append(result)
        rolling.update({"available": bool(rolling_windows), "windows": rolling_windows, "reason": None})
    return {
        "available": True,
        "method": "chronological development/validation/test slices of one fixed-rule simulation",
        "development_fraction": development_fraction,
        "validation_fraction": validation_fraction,
        "test_fraction": 1.0 - development_fraction - validation_fraction,
        "periods": periods,
        "rolling_fixed_rule_diagnostics": rolling,
        "interpretation": "固定规则基线无模型训练；时间留出指标不代表参数未被研究者事后选择，也不是滚动样本外验证。",
    }


def run_backtest(
    frame: pd.DataFrame,
    initial_cash_cny: float,
    config: dict[str, Any],
    benchmark_frame: pd.DataFrame | None = None,
    *,
    prevalidated_quality: dict[str, Any] | None = None,
    source_data_sha256: str | None = None,
    security_identity_events: pd.DataFrame | None = None,
    corporate_actions: pd.DataFrame | None = None,
    mutate_input: bool = False,
    formal: bool = False,
    readiness_report: dict[str, Any] | None = None,
    readiness_input_hashes: dict[str, str] | None = None,
    evaluation_start: str | None = None,
    evaluation_end: str | None = None,
    readiness_context: dict[str, str | Path] | None = None,
    accounting_mode: str = "legacy",
    market_rule_schedule: pd.DataFrame | None = None,
    fee_schedule: pd.DataFrame | None = None,
    accounting_events: Sequence[SupportedAccountingEvent] | None = None,
    dividend_tax_cny_by_event: Mapping[str, Decimal | int | str | None] | None = None,
    dividend_tax_treatment_by_event: Mapping[str, str] | None = None,
    dividend_payment_withholding_per_share_cny_by_event: Mapping[str, Decimal | int | str | None] | None = None,
    opening_accounting_snapshot: LedgerSnapshot | None = None,
    backtest_checkpoint: BacktestCheckpoint | None = None,
    trading_calendar: pd.DataFrame | None = None,
    source_data_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run a small long-only daily baseline with next-session-open execution.

    It uses unadjusted OHLC for execution/valuation, adjusted close for signal
    continuity, point-in-time share rules when supplied (otherwise configured
    research fallbacks), fees, and conservative price-limit checks.
    """
    started = time.perf_counter()
    accounting_mode = str(accounting_mode).strip().lower()
    if accounting_mode not in {"legacy", "ledger_shadow", "ledger_v2"}:
        raise ValueError("accounting_mode 只能是 legacy、ledger_shadow 或 ledger_v2")
    if formal and accounting_mode != "legacy":
        raise ValueError("ledger 诊断模式尚未通过正式门控，不能用于 formal=True 的正式基线")
    if accounting_mode in {"ledger_shadow", "ledger_v2"} and not isinstance(corporate_actions, pd.DataFrame):
        raise ValueError("ledger 诊断模式仅接受显式空 identity_events 与 corporate_actions 输入")
    if accounting_mode in {"ledger_shadow", "ledger_v2"} and not isinstance(security_identity_events, pd.DataFrame):
        raise ValueError("ledger 诊断模式仅接受显式空 identity_events 与 corporate_actions 输入")
    if accounting_mode != "ledger_v2" and (
        accounting_events or dividend_tax_cny_by_event or dividend_tax_treatment_by_event
        or dividend_payment_withholding_per_share_cny_by_event
    ):
        raise ValueError("显式账务事件与分红税费仅支持 ledger_v2 的非正式研究诊断")
    if opening_accounting_snapshot is not None and accounting_mode != "ledger_v2":
        raise ValueError("期初账本快照仅支持 --diagnostic --accounting-mode ledger_v2")
    if backtest_checkpoint is not None and accounting_mode != "ledger_v2":
        raise ValueError("完整策略 checkpoint 仅支持 --diagnostic --accounting-mode ledger_v2")
    if backtest_checkpoint is not None and opening_accounting_snapshot is not None:
        raise ValueError("完整策略 checkpoint 已包含账本快照，不能再单独传入期初账本快照")
    if (backtest_checkpoint is not None or trading_calendar is not None) and formal:
        raise ValueError("策略 checkpoint 与交易日历续跑仅用于非正式研究诊断")
    if trading_calendar is not None and accounting_mode != "ledger_v2":
        raise ValueError("完整策略 checkpoint/交易日历状态目前仅支持 ledger_v2 研究诊断")
    if backtest_checkpoint is not None and trading_calendar is None:
        raise ValueError("完整策略续跑必须传入交易日历，校验交易日连续性")
    if (market_rule_schedule is None) != (fee_schedule is None):
        raise ValueError("有效日期规则和历史费率必须成对提供")
    use_effective_schedules = market_rule_schedule is not None
    if use_effective_schedules and formal:
        raise ValueError("研究诊断用有效日期规则/费率不能替代正式 readiness 输入")
    if use_effective_schedules and accounting_mode != "ledger_v2":
        raise ValueError("当前有效日期规则/费率接入仅支持 accounting_mode='ledger_v2' 的非正式研究诊断")
    if initial_cash_cny <= 0:
        raise ValueError("回测资金必须大于 0；请显式传入演示或研究资金")
    data = normalize_bar_frame(
        frame,
        copy_frame=not mutate_input,
        compact=mutate_input,
        assume_sorted=mutate_input,
    )
    current_window_bars_sha256 = hashlib.sha256(
        pd.util.hash_pandas_object(data[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
    ).hexdigest()
    current_window_data_sha256 = source_data_sha256 or hashlib.sha256(
        pd.util.hash_pandas_object(data[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
    ).hexdigest()
    source_path = Path(source_data_path).expanduser().resolve() if source_data_path else None
    if source_path is not None:
        if not source_path.is_file():
            raise FileNotFoundError(f"当前窗口行情文件不存在：{source_path}")
        observed_current_data_sha256 = _sha256_path(source_path)
        if source_data_sha256 is not None and observed_current_data_sha256 != source_data_sha256:
            raise ValueError("当前窗口行情文件 SHA-256 与调用方绑定散列不一致")
        current_window_data_sha256 = observed_current_data_sha256
    mark_columns = ("trade_date", "symbol", "close", "is_suspended", "data_source")
    mark_inputs = pd.DataFrame({
        column: (
            data[column].astype("string").fillna("")
            if column in data.columns else pd.Series("", index=data.index, dtype="string")
        )
        for column in mark_columns
    }).sort_values(["trade_date", "symbol"], kind="stable")
    mark_digest = hashlib.sha256()
    mark_digest.update((source_data_sha256 or "").encode("ascii", errors="ignore"))
    mark_digest.update(mark_inputs.to_csv(index=False, lineterminator="\n").encode("utf-8"))
    accounting_valuation_inputs_sha256 = mark_digest.hexdigest()
    normalized_market_rules = None
    normalized_fee_schedule = None
    if use_effective_schedules:
        normalized_market_rules = normalize_market_rule_schedule(market_rule_schedule)
        normalized_fee_schedule = normalize_fee_schedule(fee_schedule)
        data = attach_market_rule_schedule(data, normalized_market_rules)
    if formal:
        if (
            readiness_report is None or readiness_input_hashes is None or not evaluation_start
            or not evaluation_end or readiness_context is None
        ):
            raise ValueError("formal=True 必须提供就绪报告、全部审计输入路径、当前输入散列和评价窗口")
        missing_context = set(READINESS_CONTEXT_KEYS).difference(readiness_context)
        if missing_context:
            raise ValueError("正式基线缺少 readiness 输入路径：" + ", ".join(sorted(missing_context)))
        current_hashes = compute_readiness_input_hashes(**{
            key: readiness_context[key]
            for key in READINESS_CONTEXT_KEYS
        })
        if current_hashes != readiness_input_hashes:
            raise ValueError("正式基线输入文件哈希已变化，请先重新运行 baseline-readiness")
        bound_identity_events = read_security_identity_events(readiness_context["identity_events"])
        if security_identity_events is not None:
            supplied_identity_events = normalize_security_identity_events(security_identity_events)
            if _normalized_event_table_digest(supplied_identity_events) != _normalized_event_table_digest(
                bound_identity_events
            ):
                raise ValueError("正式基线内存证券身份事件与 readiness 绑定文件内容不一致")
        security_identity_events = bound_identity_events

        bound_corporate_actions = read_corporate_actions(readiness_context["corporate_actions"])
        if corporate_actions is not None:
            supplied_corporate_actions = normalize_corporate_actions(corporate_actions)
            if _normalized_event_table_digest(supplied_corporate_actions) != _normalized_event_table_digest(
                bound_corporate_actions
            ):
                raise ValueError("正式基线内存公司行动与 readiness 绑定文件内容不一致")
        corporate_actions = bound_corporate_actions

        file_data = normalize_bar_frame(read_bars(readiness_context["data"]))
        supplied_frame_digest = hashlib.sha256(
            pd.util.hash_pandas_object(data[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
        ).hexdigest()
        file_frame_digest = hashlib.sha256(
            pd.util.hash_pandas_object(file_data[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
        ).hexdigest()
        if supplied_frame_digest != file_frame_digest:
            raise ValueError("正式基线内存行情与散列绑定的 --data 文件内容不同")
        configured_values = load_config(readiness_context["config"])
        if configured_values != config:
            raise ValueError("正式基线运行配置与 readiness 审计所绑定的配置文件不同")
        if benchmark_frame is None:
            raise ValueError("正式基线必须加载 readiness 绑定的独立基准数据")
        file_benchmark = normalize_bar_frame(read_bars(readiness_context["benchmark"]))
        supplied_benchmark = normalize_bar_frame(benchmark_frame)
        supplied_benchmark_digest = hashlib.sha256(
            pd.util.hash_pandas_object(supplied_benchmark[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
        ).hexdigest()
        file_benchmark_digest = hashlib.sha256(
            pd.util.hash_pandas_object(file_benchmark[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
        ).hexdigest()
        if supplied_benchmark_digest != file_benchmark_digest:
            raise ValueError("正式基线内存基准与 readiness 绑定的基准文件内容不同")
        recomputed_report = build_baseline_readiness(
            data=file_data,
            input_hashes=current_hashes,
            universe=readiness_context["universe"],
            identity_events_path=readiness_context["identity_events"],
            out_date_evidence_path=readiness_context["out_date_evidence"],
            unknown_type_review_path=readiness_context["unknown_type_review"],
            unknown_type_summary_path=readiness_context["unknown_type_summary"],
            unknown_type_bars_path=readiness_context["unknown_type_bars"],
            point_in_time_quality_path=readiness_context["point_in_time_quality"],
            corporate_actions_path=readiness_context["corporate_actions"],
            corporate_action_coverage_path=readiness_context["corporate_action_coverage"],
            market_rules_path=readiness_context["market_rules"],
            fees_path=readiness_context["fees"],
            benchmark=file_benchmark,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
        )
        require_report_matches_recomputed(readiness_report, recomputed_report)
        current_commit = _git_metadata().get("git_commit")
        require_baseline_ready(
            readiness_report,
            readiness_input_hashes,
            evaluation_start,
            evaluation_end,
            current_git_commit=current_commit,
        )
    quality = prevalidated_quality or check_data_quality(
        data,
        large_change_warning=float(config.get("data", {}).get("large_daily_price_change_warning", 0.50)),
    )
    if not quality.get("healthy", False):
        raise ValueError("数据质量检查未通过：" + "; ".join(quality.get("errors", [])))
    strategy = config["strategy"]
    market = config["market"]
    costs = config["costs"]
    lookback = int(strategy["lookback_sessions"])
    rebalance_sessions = int(strategy["rebalance_sessions"])
    if rebalance_sessions < 1:
        raise ValueError("rebalance_sessions 必须大于 0")
    max_stale_price_age = config.get("backtest", {}).get("max_stale_price_age_sessions", 0)
    if isinstance(max_stale_price_age, bool) or not isinstance(max_stale_price_age, int) or max_stale_price_age < 0:
        raise ValueError("backtest.max_stale_price_age_sessions 必须是非负整数")

    identity_events = normalize_security_identity_events(security_identity_events)
    action_events = normalize_corporate_actions(
        read_corporate_actions() if corporate_actions is None else corporate_actions
    )
    if accounting_mode in {"ledger_shadow", "ledger_v2"} and (not identity_events.empty or not action_events.empty):
        raise ValueError("当前 ledger 诊断模式仅支持无身份变更、无公司行动区间")
    symbols_in_data = set(data["symbol"].astype(str).unique())
    if not identity_events.empty:
        identity_events = identity_events.loc[
            identity_events["old_symbol"].isin(symbols_in_data)
            | identity_events["new_symbol"].isin(symbols_in_data)
        ].reset_index(drop=True)
    compact_config = {name: config[name] for name in ("data", "strategy", "market", "costs", "backtest", "benchmark") if name in config}
    config_sha256 = hashlib.sha256(
        json.dumps(compact_config, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    identity_events_sha256 = _normalized_event_table_digest(identity_events)
    corporate_actions_sha256 = _normalized_event_table_digest(action_events)
    accounting_events_sha256 = hashlib.sha256(json.dumps(
        {
            "events": [asdict(event) for event in sorted(accounting_events or (), key=lambda event: event.event_id)],
            "dividend_tax_cny_by_event": {
                event_id: None if amount is None else str(amount)
                for event_id, amount in sorted((dividend_tax_cny_by_event or {}).items())
            },
            "dividend_tax_treatment_by_event": dict(sorted((dividend_tax_treatment_by_event or {}).items())),
            "dividend_payment_withholding_per_share_cny_by_event": {
                event_id: None if amount is None else str(amount)
                for event_id, amount in sorted((dividend_payment_withholding_per_share_cny_by_event or {}).items())
            },
        },
        ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    normalized_calendar = normalize_trading_calendar(trading_calendar) if trading_calendar is not None else None
    normalized_calendar_sha256 = (
        _normalized_table_digest(normalized_calendar) if normalized_calendar is not None else None
    )

    data["signal_price"], current_signal_modes = select_signal_prices(data)
    current_modes = {str(symbol): mode for symbol, mode in current_signal_modes.items()}
    if backtest_checkpoint is None:
        signal_modes = current_modes
        feature_data = add_momentum(data, lookback, normalized=True)
        feature_data["previous_close"] = feature_data.groupby("symbol", sort=False, observed=True)["close"].shift(1)
        signal_history_source = feature_data[["trade_date", "symbol", "close", "signal_price"]].copy()
    else:
        if backtest_checkpoint.strategy_id != str(strategy["name"]):
            raise ValueError("checkpoint 策略名称变化；请从受影响日期重新计算")
        if backtest_checkpoint.config_sha256 != config_sha256:
            raise ValueError("checkpoint 绑定的策略配置已变化；请从受影响日期重新计算")
        if backtest_checkpoint.market_rules_sha256 != (
            _normalized_table_digest(normalized_market_rules) if normalized_market_rules is not None else None
        ):
            raise ValueError("checkpoint 绑定的交易规则版本已变化；请从受影响日期重新计算")
        if backtest_checkpoint.fee_schedule_sha256 != (
            _normalized_table_digest(normalized_fee_schedule) if normalized_fee_schedule is not None else None
        ):
            raise ValueError("checkpoint 绑定的费率版本已变化；请从受影响日期重新计算")
        if backtest_checkpoint.identity_events_sha256 != identity_events_sha256:
            raise ValueError("checkpoint 绑定的证券身份事件已变化；请从受影响日期重新计算")
        if backtest_checkpoint.corporate_actions_sha256 != corporate_actions_sha256:
            raise ValueError("checkpoint 绑定的退市/公司行动输入已变化；请从受影响日期重新计算")
        if backtest_checkpoint.source_data_path:
            prior_path = Path(backtest_checkpoint.source_data_path)
            if not prior_path.is_file():
                raise FileNotFoundError(f"checkpoint 绑定的历史行情文件不存在：{prior_path}")
            observed_prior = _sha256_path(prior_path)
            if observed_prior != backtest_checkpoint.last_window_data_sha256:
                raise ValueError("checkpoint 绑定的历史行情文件已修改；请从受影响日期重新计算")
        if normalized_calendar is None:
            raise ValueError("完整策略续跑必须传入交易日历")
        if backtest_checkpoint.calendar_sha256 != normalized_calendar_sha256:
            raise ValueError("checkpoint 绑定的完整交易日历版本已变化")
        first_current_date = pd.Timestamp(data["trade_date"].min()).date()
        last_current_date = pd.Timestamp(data["trade_date"].max()).date()
        if first_current_date != backtest_checkpoint.next_expected_session:
            raise ValueError(
                f"续跑首日 {first_current_date} 不等于 checkpoint 记录的下一交易日 "
                f"{backtest_checkpoint.next_expected_session}"
            )
        prior_calendar_rows = normalized_calendar.loc[
            normalized_calendar["trade_date"] <= backtest_checkpoint.as_of_date
        ].copy()
        prior_calendar_sha256 = _normalized_table_digest(prior_calendar_rows)
        if prior_calendar_sha256 != backtest_checkpoint.calendar_prefix_sha256:
            raise ValueError("checkpoint 绑定的历史交易日历前缀已变化")
        checkpoint_calendar_open = set(
            normalized_calendar.loc[normalized_calendar["is_open"].fillna(False), "trade_date"]
        )
        if first_current_date not in checkpoint_calendar_open:
            raise ValueError(f"续跑首日 {first_current_date} 不在交易日历开市日中")
        prior_open = normalized_calendar.loc[
            normalized_calendar["is_open"].fillna(False)
            & (normalized_calendar["trade_date"] <= backtest_checkpoint.as_of_date),
            "trade_date",
        ]
        expected_next = normalized_calendar.loc[
            normalized_calendar["is_open"].fillna(False)
            & (normalized_calendar["trade_date"] > backtest_checkpoint.as_of_date),
            "trade_date",
        ]
        if prior_open.empty or expected_next.empty or expected_next.iloc[0] != first_current_date:
            raise ValueError("续跑交易日历在 checkpoint 与当前窗口之间存在缺口或缺少下一交易日")

        checkpoint_modes = dict(backtest_checkpoint.signal_price_modes)
        signal_modes = dict(checkpoint_modes)
        for symbol, current_mode in current_modes.items():
            prior_mode = checkpoint_modes.get(symbol)
            if prior_mode is None:
                signal_modes[symbol] = current_mode
            elif prior_mode == "adjusted_close" and current_mode != "adjusted_close":
                raise ValueError(f"{symbol} 续跑后复权价格缺失/变化；不能沿用 checkpoint 信号历史")
            elif prior_mode == "raw_close_no_adjustment" and current_mode != "raw_close_no_adjustment":
                raise ValueError(f"{symbol} 续跑后复权字段可用性发生变化；请重新计算信号历史")

        current_signal_values: list[float] = []
        for row_index, row in data.iterrows():
            symbol = str(row["symbol"])
            mode = signal_modes[symbol]
            if mode == "adjusted_close":
                value = pd.to_numeric(pd.Series([row.get("adjusted_close")]), errors="coerce").iloc[0]
            elif mode == "raw_close_no_adjustment":
                value = row["close"]
            else:
                value = np.nan
            if mode != "unusable_mixed_or_nonpositive_adjustment" and (
                pd.isna(value) or not np.isfinite(float(value)) or float(value) <= 0
            ):
                raise ValueError(f"{symbol} 续跑信号价格无效；请重新生成完整历史特征")
            current_signal_values.append(float(value) if not pd.isna(value) else np.nan)
        data["signal_price"] = current_signal_values
        history_frame = pd.DataFrame([
            {
                "trade_date": item.trade_date,
                "symbol": item.symbol,
                "close": float(item.close_cny),
                "signal_price": np.nan if item.signal_price_cny is None else float(item.signal_price_cny),
            }
            for item in backtest_checkpoint.signal_history
        ])
        current_feature_rows = data[["trade_date", "symbol", "close", "signal_price"]].copy()
        signal_history_source = pd.concat([history_frame, current_feature_rows], ignore_index=True)
        signal_history_source = signal_history_source.sort_values(["trade_date", "symbol"], kind="stable")
        signal_history_source["momentum"] = signal_history_source.groupby(
            "symbol", sort=False, observed=True
        )["signal_price"].transform(lambda values: values.div(values.shift(lookback)).sub(1.0))
        signal_history_source["previous_close"] = signal_history_source.groupby(
            "symbol", sort=False, observed=True
        )["close"].shift(1)
        features = signal_history_source.loc[
            signal_history_source["trade_date"].isin(data["trade_date"]),
            ["trade_date", "symbol", "momentum", "previous_close"],
        ]
        data = data.merge(features, on=["trade_date", "symbol"], how="left", validate="one_to_one")
    if backtest_checkpoint is None:
        data = feature_data
    if normalized_calendar is not None:
        bar_sessions = sorted(pd.to_datetime(data["trade_date"]).dt.date.unique())
        calendar_open_sessions = sorted(
            normalized_calendar.loc[normalized_calendar["is_open"].fillna(False), "trade_date"].tolist()
        )
        expected_in_window = [day for day in calendar_open_sessions if bar_sessions[0] <= day <= bar_sessions[-1]]
        if bar_sessions != expected_in_window:
            missing_sessions = sorted(set(expected_in_window).difference(bar_sessions))
            extra_sessions = sorted(set(bar_sessions).difference(calendar_open_sessions))
            raise ValueError(
                "回测行情日期与交易日历不连续："
                f"missing={','.join(day.isoformat() for day in missing_sessions[:10])}; "
                f"closed_or_unknown={','.join(day.isoformat() for day in extra_sessions[:10])}"
            )
    trade_dates = data["trade_date"].to_numpy()
    boundaries = np.flatnonzero(trade_dates[1:] != trade_dates[:-1]) + 1
    day_starts = np.concatenate(([0], boundaries))
    day_ends = np.concatenate((boundaries, [len(data)]))
    dates = [data.iloc[start]["trade_date"] for start in day_starts]
    if normalized_calendar is not None:
        last_session_date = pd.Timestamp(dates[-1]).date()
        future_open_dates = normalized_calendar.loc[
            normalized_calendar["is_open"].fillna(False)
            & (normalized_calendar["trade_date"] > last_session_date),
            "trade_date",
        ]
        if future_open_dates.empty:
            raise ValueError("完整策略 checkpoint 需要包含回测期末之后的下一开市日历日期")
    scheduled_fees_by_date = (
        map_fee_schedule_to_sessions(normalized_fee_schedule, dates)
        if normalized_fee_schedule is not None else {}
    )
    if not dates:
        raise ValueError("回测输入至少需要一个交易日")
    if backtest_checkpoint is None and len(dates) <= lookback + 1:
        raise ValueError(f"至少需要 {lookback + 2} 个交易日，当前只有 {len(dates)} 天")
    global_session_start = (
        backtest_checkpoint.global_session_index + 1 if backtest_checkpoint is not None else 0
    )
    trade_sequence_start = backtest_checkpoint.trade_count if backtest_checkpoint is not None else 0
    next_open_session_by_date: dict[str, date] = {}
    if normalized_calendar is not None:
        calendar_open_dates = normalized_calendar.loc[
            normalized_calendar["is_open"].fillna(False), "trade_date"
        ].tolist()
        for current, following in zip(calendar_open_dates, calendar_open_dates[1:]):
            next_open_session_by_date[current.isoformat()] = following
    for index, value in enumerate(dates[:-1]):
        next_open_session_by_date.setdefault(
            pd.Timestamp(value).strftime("%Y-%m-%d"), pd.Timestamp(dates[index + 1]).date()
        )

    accounting_event_list = tuple(accounting_events or ())
    event_schedule = AccountingEventSchedule.build(
        accounting_event_list,
        (pd.Timestamp(value).date() for value in dates),
        dividend_tax_cny_by_event,
        dividend_tax_treatment_by_event,
        dividend_payment_withholding_per_share_cny_by_event,
    )
    accounting_events_sha256 = hashlib.sha256(json.dumps(
        {
            "events": [asdict(event) for event in sorted(accounting_event_list, key=lambda item: item.event_id)],
            "dividend_tax_cny_by_event": {
                event_id: None if amount is None else str(amount)
                for event_id, amount in sorted(event_schedule.dividend_tax_cny.items())
            },
            "dividend_tax_treatment_by_event": dict(sorted(event_schedule.dividend_tax_treatment.items())),
            "dividend_payment_withholding_per_share_cny_by_event": {
                event_id: None if amount is None else str(amount)
                for event_id, amount in sorted(event_schedule.dividend_payment_withholding_per_share_cny.items())
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if backtest_checkpoint is not None and backtest_checkpoint.accounting_events_sha256 != accounting_events_sha256:
        raise ValueError("checkpoint 绑定的公司行动事件或分红税务处理已变化；请从受影响日期重新计算")

    if backtest_checkpoint is not None:
        opening_accounting_snapshot = backtest_checkpoint.ledger_snapshot
    cash = float(initial_cash_cny)
    holdings: dict[str, int] = {}
    cost_basis_cny: dict[str, float] = {}
    holding_since: dict[str, str] = {}
    last_prices: dict[str, float] = {}
    last_price_dates: dict[str, str] = {}
    last_price_sources: dict[str, str] = {}
    last_price_global_indices: dict[str, int] = {}
    if opening_accounting_snapshot is not None:
        if opening_accounting_snapshot.as_of_date is None:
            raise ValueError("期初账本快照缺少 as_of_date，无法验证跨窗口时间边界")
        first_session = pd.Timestamp(dates[0]).date()
        if opening_accounting_snapshot.as_of_date >= first_session:
            raise ValueError(
                f"期初账本快照日期 {opening_accounting_snapshot.as_of_date} 必须早于首个输入交易日 {first_session}"
            )
        cash_argument = Decimal(str(initial_cash_cny))
        if abs(opening_accounting_snapshot.cash_cny - cash_argument) > Decimal("0.01"):
            raise ValueError(
                "--cash 与期初账本快照 cash_cny 相差超过一分钱；请传入快照当前现金，避免现金口径混淆"
            )
        if any(entry.booking_date > opening_accounting_snapshot.as_of_date for entry in opening_accounting_snapshot.journal):
            raise ValueError("期初账本快照包含晚于 as_of_date 的账务流水")
    execution_ledger = None
    if accounting_mode == "ledger_v2":
        execution_ledger = (
            PortfolioLedger.from_snapshot(opening_accounting_snapshot)
            if opening_accounting_snapshot is not None
            else PortfolioLedger(Decimal(str(initial_cash_cny)), portfolio_id="backtest-ledger-v2")
        )

    event_by_id = {event.event_id: event for event in accounting_event_list}
    start_day = pd.Timestamp(dates[0]).date()
    if opening_accounting_snapshot is None:
        for event in accounting_event_list:
            if isinstance(event, ShareDistributionEvent) and event.record_date < start_day and any(
                phase >= start_day for phase in (event.effective_date, event.registration_date, event.tradable_date)
            ):
                raise ValueError(
                    f"送转 {event.event_id} ({event.symbol}) 缺少覆盖登记日 {event.record_date} 的期初权益快照；"
                    "请提供连续预热账本或经过校验的 opening snapshot，不能按零权利继续"
                )
            if isinstance(event, CashDividendEvent) and event.record_date < start_day and (
                event.ex_date >= start_day or event.payable_date >= start_day
            ):
                raise ValueError(
                    f"现金分红 {event.event_id} ({event.symbol}) 缺少覆盖登记日 {event.record_date} 的期初权益快照"
                )
    if opening_accounting_snapshot is not None:
        entitlement_by_id = {item.event_id: item for item in opening_accounting_snapshot.entitlements}
        claim_by_id = {item.event_id: item for item in opening_accounting_snapshot.claims}
        receivable_by_id = {item.event_id: item for item in opening_accounting_snapshot.receivables}
        for event in accounting_event_list:
            if isinstance(event, ShareDistributionEvent) and event.record_date < start_day:
                has_window_phase = any(phase >= start_day for phase in (
                    event.effective_date, event.registration_date, event.tradable_date,
                ))
                if has_window_phase:
                    entitlement = entitlement_by_id.get(event.event_id)
                    claim = claim_by_id.get(event.event_id)
                    if entitlement is None or entitlement.captured_on != event.record_date:
                        raise ValueError(
                            f"送转 {event.event_id} ({event.symbol}) 缺少覆盖登记日 {event.record_date} 的期初权益快照"
                        )
                    if claim is None:
                        raise ValueError(
                            f"送转 {event.event_id} ({event.symbol}) 缺少期初权利账目；不能按零权利继续"
                        )
                    if entitlement.symbol != event.symbol or claim.exact_entitlement != (
                        Decimal(entitlement.total_shares) * event.shares_per_old_share
                    ):
                        raise ValueError(f"送转 {event.event_id} 的期初权益证券或精确股数与事件不匹配")
                    if claim.source_id != event.source.source_id or claim.source_sha256 != event.source.sha256:
                        raise ValueError(f"送转 {event.event_id} 的期初权利来源哈希与本次事件不匹配")
                    snapshot_day = opening_accounting_snapshot.as_of_date
                    if event.effective_date > snapshot_day and claim.status not in {
                        "DISTRIBUTION_NOT_YET_EFFECTIVE", "CANCELLED",
                    }:
                        raise ValueError(f"送转 {event.event_id} 期初状态提前反映尚未发生的生效日 {event.effective_date}")
                    if event.effective_date <= snapshot_day and event.registration_date > snapshot_day and claim.status not in {
                        "DISTRIBUTION_PENDING_REGISTRATION", "CANCELLED",
                    }:
                        raise ValueError(f"送转 {event.event_id} 期初状态未反映已发生的生效日 {event.effective_date}")
                    if event.registration_date <= snapshot_day and claim.status not in {"SETTLED", "CANCELLED"}:
                        raise ValueError(f"送转 {event.event_id} 期初状态未反映已发生的登记日 {event.registration_date}")
                    expected_next_phase = (
                        None if claim.status == "CANCELLED"
                        else event.effective_date if event.effective_date > snapshot_day
                        else event.registration_date if event.registration_date > snapshot_day
                        else event.tradable_date if event.tradable_date > snapshot_day and claim.status == "SETTLED"
                        else None
                    )
                    if claim.next_phase_date != expected_next_phase:
                        raise ValueError(
                            f"送转 {event.event_id} 期初下一阶段日期不匹配："
                            f"expected={expected_next_phase}, actual={claim.next_phase_date}"
                        )
            elif isinstance(event, CashDividendEvent) and event.record_date < start_day:
                has_window_phase = event.ex_date >= start_day or event.payable_date >= start_day
                if has_window_phase:
                    entitlement = entitlement_by_id.get(event.event_id)
                    if entitlement is None or entitlement.captured_on != event.record_date or entitlement.symbol != event.symbol:
                        raise ValueError(
                            f"现金分红 {event.event_id} ({event.symbol}) 缺少覆盖登记日 {event.record_date} 的期初权益快照"
                        )
                    if event.ex_date < start_day <= event.payable_date:
                        receivable = receivable_by_id.get(event.event_id)
                        if receivable is None:
                            raise ValueError(
                                f"现金分红 {event.event_id} 已在窗口前除息，但缺少期初应收款"
                            )
        if opening_accounting_snapshot is not None:
            for event in accounting_event_list:
                if isinstance(event, CodeChangeEvent) and event.effective_date < start_day:
                    old_lots = [lot for lot in opening_accounting_snapshot.positions if lot.symbol == event.old_symbol]
                    if old_lots and not any(
                        entry.event_id == event.event_id and entry.entry_type == "CODE_CHANGE_1_TO_1"
                        for entry in opening_accounting_snapshot.journal
                    ):
                        raise ValueError(
                            f"期初账本仍持有已于 {event.effective_date} 变更代码的 {event.old_symbol}，"
                            "且流水未证明该 1:1 迁移"
                        )
        known_event_ids = set(event_by_id)
        for claim in opening_accounting_snapshot.claims:
            if (
                claim.status in {"DISTRIBUTION_NOT_YET_EFFECTIVE", "DISTRIBUTION_PENDING_REGISTRATION"}
                or claim.next_phase_date is not None
            ) and claim.event_id not in known_event_ids:
                raise ValueError(
                    f"期初账本含未完成送转 {claim.event_id}，但本次事件输入中没有对应公告事件"
                )
        for receivable in opening_accounting_snapshot.receivables:
            if receivable.status == "PENDING" and receivable.payable_on and receivable.payable_on >= start_day and receivable.event_id not in known_event_ids:
                raise ValueError(
                    f"期初账本含待支付分红 {receivable.event_id}，但本次事件输入中没有对应公告事件"
                )

    def mark_distribution_claim_at_close(
        event: ShareDistributionEvent,
        action_day: date,
        day_frame: pd.DataFrame,
    ) -> None:
        if execution_ledger is None:
            raise ValueError("送转权利估值仅支持 ledger_v2")
        row = day_frame.loc[event.symbol] if event.symbol in day_frame.index else None
        if row is None or pd.isna(row.get("close")) or float(row["close"]) <= 0:
            raise ValueError(
                f"送转权利 {event.event_id} 在 {action_day} 缺少有效底层收盘价，不能估值待登记权利"
            )
        suspended = row.get("is_suspended")
        if pd.isna(suspended) or bool(suspended):
            raise ValueError(
                f"送转权利 {event.event_id} 在 {action_day} 的停牌状态未知或已停牌，"
                "不能用陈旧价格生成 NAV"
            )
        execution_ledger.mark_share_distribution_claim(
            event,
            as_of=action_day,
            underlying_close_cny=Decimal(str(float(row["close"]))),
            price_source=str(row.get("data_source", "normalized_input_bars")),
            price_source_sha256=accounting_valuation_inputs_sha256,
            price_date=action_day,
            is_suspended=False,
        )

    def sync_projection_from_ledger() -> None:
        nonlocal cash
        if execution_ledger is None:
            return
        cash = float(execution_ledger.cash_cny)
        holdings.clear()
        cost_basis_cny.clear()
        holding_since.clear()
        for lot in execution_ledger.positions:
            holdings[lot.symbol] = holdings.get(lot.symbol, 0) + lot.total_shares
            cost_basis_cny[lot.symbol] = cost_basis_cny.get(lot.symbol, 0.0) + float(lot.cost_basis_cny)
            acquired = lot.acquired_on.isoformat()
            previous = holding_since.get(lot.symbol)
            holding_since[lot.symbol] = min(previous, acquired) if previous else acquired

    if execution_ledger is not None:
        sync_projection_from_ledger()
    if backtest_checkpoint is not None:
        for mark in backtest_checkpoint.last_prices:
            last_prices[mark.symbol] = float(mark.price_cny)
            last_price_dates[mark.symbol] = mark.price_date.isoformat()
            last_price_sources[mark.symbol] = mark.source
            last_price_global_indices[mark.symbol] = mark.global_session_index

    active_distribution_claims: dict[str, ShareDistributionEvent] = {}
    if execution_ledger is not None:
        events_by_id = {
            event.event_id: event for event in accounting_event_list if isinstance(event, ShareDistributionEvent)
        }
        for claim in execution_ledger.claims:
            if claim.status == "DISTRIBUTION_PENDING_REGISTRATION":
                event = events_by_id.get(claim.event_id)
                if event is None:
                    raise ValueError(f"期初待登记权利 {claim.event_id} 缺少对应送转事件")
                active_distribution_claims[event.event_id] = event

    pending_targets: dict[str, float] | None = (
        dict(backtest_checkpoint.pending_targets) if backtest_checkpoint is not None else None
    )
    transition_signal_ready_index: dict[str, int] = (
        dict(backtest_checkpoint.transition_signal_ready_index) if backtest_checkpoint is not None else {}
    )
    equity_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    security_events: list[dict[str, Any]] = []
    blocked_orders: list[dict[str, Any]] = []
    total_turnover = 0.0
    slippage = float(costs.get("slippage_bps", 0.0)) / 10000.0
    identity_events_by_date = {
        str(day): group.to_dict(orient="records")
        for day, group in identity_events.groupby("effective_date", sort=False)
    } if not identity_events.empty else {}

    for index, (date, start, end) in enumerate(zip(dates, day_starts, day_ends)):
        global_index = global_session_start + index
        day = data.iloc[start:end].set_index("symbol", drop=False)
        day_date = pd.Timestamp(date).strftime("%Y-%m-%d")

        # Typed 1:1 code changes run at the new code's first effective
        # session. They are ledger events, never fills or charged orders.
        for event in event_schedule.code_changes.get(pd.Timestamp(date).date(), ()):
            if execution_ledger is None:
                raise ValueError("代码变更账务事件仅支持 ledger_v2")
            old_symbol, new_symbol = event.old_symbol, event.new_symbol
            new_row = day.loc[new_symbol] if new_symbol in day.index else None
            if isinstance(new_row, pd.DataFrame):
                raise ValueError(f"{day_date} {new_symbol} 的日线不唯一，不能执行身份迁移")
            if new_row is None:
                raise ValueError(f"{day_date} {new_symbol} 缺少代码变更生效日行情，不能验证身份迁移")
            _require_code_change_open_eligibility(new_row, day_date, new_symbol)
            if old_symbol in day.index:
                raise ValueError(f"{day_date} 旧代码 {old_symbol} 与新代码行情同时存在，身份事件需人工核验")
            cash_before = cash
            shares_before = holdings.get(old_symbol, 0)
            basis_before = cost_basis_cny.get(old_symbol, 0.0)
            trade_count_before = len(trades)
            execution_ledger.apply_code_change(event, as_of=event.effective_date)
            sync_projection_from_ledger()
            if pending_targets is not None:
                pending_targets.pop(old_symbol, None)
                pending_targets.pop(new_symbol, None)
            if shares_before:
                transition_signal_ready_index[new_symbol] = global_index + lookback
            last_prices.pop(old_symbol, None)
            last_price_dates.pop(old_symbol, None)
            last_price_sources.pop(old_symbol, None)
            last_price_global_indices.pop(old_symbol, None)
            security_events.append({
                "trade_date": day_date,
                "event_id": event.event_id,
                "event_type": "MIGRATE_POSITION" if shares_before else "NO_POSITION",
                "old_symbol": old_symbol,
                "new_symbol": new_symbol,
                "share_ratio": "1:1",
                "old_shares": shares_before,
                "new_shares": shares_before,
                "cost_basis_before_cny": basis_before,
                "cost_basis_after_cny": basis_before,
                "cash_before_cny": cash_before,
                "cash_after_cny": cash,
                "trade_count_before": trade_count_before,
                "trade_count_after": len(trades),
                "signal_policy": f"new code starts a fresh {lookback}-session momentum window",
                "accounting_source": "typed_accounting_event_ledger_v2",
            })

        for event in event_schedule.distribution_effective.get(pd.Timestamp(date).date(), ()):
            if execution_ledger is None:
                raise ValueError("送转账务事件仅支持 ledger_v2")
            execution_ledger.apply_share_distribution(event, as_of=event.effective_date)
            sync_projection_from_ledger()
            updated_claim = next(
                (item for item in execution_ledger.claims if item.claim_id == f"distribution:{event.event_id}"),
                None,
            )
            if updated_claim is not None and updated_claim.status == "DISTRIBUTION_PENDING_REGISTRATION":
                active_distribution_claims[event.event_id] = event
            security_events.append({
                "trade_date": day_date,
                "event_id": event.event_id,
                "event_type": "SHARE_DISTRIBUTION_EFFECTIVE",
                "symbol": event.symbol,
                "registration_date": event.registration_date.isoformat(),
                "tradable_date": event.tradable_date.isoformat(),
                "cash_delta_cny": 0,
                "trade_or_commission_created": False,
                "accounting_source": "typed_accounting_event_ledger_v2",
            })

        for event in event_schedule.distribution_registrations.get(pd.Timestamp(date).date(), ()):
            if event.registration_date == event.effective_date:
                continue
            if execution_ledger is None:
                raise ValueError("送转账务事件仅支持 ledger_v2")
            claim = next((item for item in execution_ledger.claims if item.claim_id == f"distribution:{event.event_id}"), None)
            if claim is None or claim.status == "CANCELLED":
                continue
            execution_ledger.register_share_distribution(event, as_of=event.registration_date)
            active_distribution_claims.pop(event.event_id, None)
            sync_projection_from_ledger()
            security_events.append({
                "trade_date": day_date,
                "event_id": event.event_id,
                "event_type": "SHARE_DISTRIBUTION_REGISTRATION",
                "symbol": event.symbol,
                "registration_date": event.registration_date.isoformat(),
                "tradable_date": event.tradable_date.isoformat(),
                "cash_delta_cny": 0,
                "trade_or_commission_created": False,
                "accounting_source": "typed_accounting_event_ledger_v2",
            })

        for event in event_schedule.distribution_tradabilities.get(pd.Timestamp(date).date(), ()):
            if execution_ledger is None:
                raise ValueError("送转可流通阶段仅支持 ledger_v2")
            claim = next(
                (item for item in execution_ledger.claims if item.claim_id == f"distribution:{event.event_id}"),
                None,
            )
            if claim is None or claim.status in {"CANCELLED", "DISTRIBUTION_NOT_YET_EFFECTIVE"}:
                continue
            execution_ledger.mark_share_distribution_tradable(event, as_of=event.tradable_date)
            security_events.append({
                "trade_date": day_date,
                "event_id": event.event_id,
                "event_type": "SHARE_DISTRIBUTION_TRADABLE",
                "symbol": event.symbol,
                "tradable_date": event.tradable_date.isoformat(),
                "accounting_source": "typed_accounting_event_ledger_v2",
            })

        # Older receivables are settled before the day's simulated orders.
        for event in event_schedule.dividend_payments.get(pd.Timestamp(date).date(), ()):
            receivable_id = f"dividend:{event.event_id}"
            if execution_ledger is not None and event.payable_date != event.ex_date:
                if any(item.receivable_id == receivable_id for item in execution_ledger.snapshot().receivables):
                    execution_ledger.settle_receivable(receivable_id, settled_on=event.payable_date)
                    sync_projection_from_ledger()

        for action in action_events.to_dict(orient="records"):
            old_symbol = str(action["old_symbol"])
            if day_date > str(action["old_last_trade_date"]) and holdings.get(old_symbol, 0) > 0:
                raise ValueError(
                    f"正式/研究净值中止：{old_symbol} 在 {day_date} 仍有 {holdings[old_symbol]} 股，"
                    f"涉及未完成公司行动 {action['event_id']}；个人最终获配数与可估值权益未核实，"
                    "不得沿用旧股收盘价或按现金补尾继续计算"
                )

        # A confirmed code replacement transfers the position as a ledger event.
        # It is deliberately excluded from `trades`: no cash, fee, or realized P&L.
        for event in identity_events_by_date.get(day_date, []):
            old_symbol = str(event["old_symbol"])
            new_symbol = str(event["new_symbol"])
            cash_before = cash
            trade_count_before = len(trades)
            new_row = day.loc[new_symbol] if new_symbol in day.index else None
            if isinstance(new_row, pd.DataFrame):
                raise ValueError(f"{day_date} {new_symbol} 的日线不唯一，不能执行身份迁移")
            if new_row is None:
                raise ValueError(f"{day_date} {new_symbol} 缺少代码变更生效日行情，不能验证身份迁移")
            _require_code_change_open_eligibility(new_row, day_date, new_symbol)
            if old_symbol in day.index:
                raise ValueError(f"{day_date} 旧代码 {old_symbol} 与新代码行情同时存在，身份事件需人工核验")

            old_shares = int(holdings.pop(old_symbol, 0))
            old_basis = float(cost_basis_cny.pop(old_symbol, 0.0))
            old_holding_since = holding_since.pop(old_symbol, "")
            ratio = float(event["share_ratio"])
            raw_new_shares = old_shares * ratio
            new_shares = int(round(raw_new_shares))
            if not math.isclose(raw_new_shares, new_shares, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError(
                    f"{day_date} {old_symbol}->{new_symbol} 转换后产生非整数股；需先实现公司行动尾差记账"
                )
            if new_shares:
                holdings[new_symbol] = holdings.get(new_symbol, 0) + new_shares
                cost_basis_cny[new_symbol] = cost_basis_cny.get(new_symbol, 0.0) + old_basis
                existing_since = holding_since.get(new_symbol, "")
                candidates = [value for value in (existing_since, old_holding_since) if value]
                holding_since[new_symbol] = min(candidates) if candidates else day_date
                transition_signal_ready_index[new_symbol] = global_index + lookback
            last_prices.pop(old_symbol, None)
            last_price_dates.pop(old_symbol, None)
            last_price_sources.pop(old_symbol, None)
            last_price_global_indices.pop(old_symbol, None)
            security_events.append({
                "trade_date": day_date,
                "event_id": str(event["event_id"]),
                "event_type": "MIGRATE_POSITION" if old_shares else "NO_POSITION",
                "old_symbol": old_symbol,
                "new_symbol": new_symbol,
                "share_ratio": ratio,
                "old_shares": old_shares,
                "new_shares": new_shares,
                "cost_basis_before_cny": old_basis,
                "cost_basis_after_cny": cost_basis_cny.get(new_symbol, 0.0) if new_shares else 0.0,
                "holding_since": holding_since.get(new_symbol, "") if new_shares else "",
                "cash_before_cny": cash_before,
                "cash_after_cny": cash,
                "trade_count_before": trade_count_before,
                "trade_count_after": len(trades),
                "signal_policy": f"new code starts a fresh {lookback}-session momentum window",
            })

        if pending_targets is not None:
            estimate_equity = cash
            for symbol, shares in holdings.items():
                row = day.loc[symbol] if symbol in day.index else None
                mark = float(row["open"]) if row is not None and pd.notna(row["open"]) else last_prices.get(symbol, 0.0)
                estimate_equity += shares * mark
            target_shares: dict[str, int] = {}
            for symbol, weight in pending_targets.items():
                row = day.loc[symbol] if symbol in day.index else None
                if row is None or pd.isna(row["open"]) or float(row["open"]) <= 0:
                    continue
                target_amount = estimate_equity * weight
                minimum, step = order_constraints(row, "BUY", market, allow_fallback=not use_effective_schedules)
                raw_target = int(target_amount / float(row["open"]))
                target_shares[symbol] = round_order_quantity(raw_target, minimum, step)

            # Keep migrated shares unchanged while the successor builds its own
            # lookback. A signal from the previous code is never carried over.
            protected_symbols = {
                symbol for symbol, ready_index in transition_signal_ready_index.items()
                if symbol in holdings and global_index <= ready_index
            }
            for symbol in protected_symbols:
                target_shares[symbol] = holdings[symbol]

            # Sells first, so their proceeds can fund the target buys.
            for symbol in list(holdings):
                current = holdings.get(symbol, 0)
                desired = target_shares.get(symbol, 0)
                quantity = max(0, current - desired)
                if quantity and execution_ledger is not None:
                    trade_day = pd.Timestamp(date).date()
                    available = sum(
                        lot.sellable_on(trade_day)
                        for lot in execution_ledger.positions
                        if lot.symbol == symbol
                    )
                    unavailable = max(0, quantity - available)
                    if unavailable:
                        blocked_orders.append({
                            "trade_date": day_date,
                            "symbol": symbol,
                            "side": "SELL",
                            "quantity": unavailable,
                            "reason": "送转或新买入股份尚未到可卖日期；按 T+1/可流通日约束保留持仓",
                        })
                    quantity = min(quantity, available)
                row = day.loc[symbol] if symbol in day.index else None
                if row is None:
                    if quantity:
                        blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": "當日無行情行，交易狀態無法確認"})
                    continue
                minimum, step = order_constraints(row, "SELL", market, allow_fallback=not use_effective_schedules)
                quantity = round_order_quantity(
                    quantity, minimum, step,
                    allow_full_liquidation=True, full_position_shares=current,
                )
                if quantity <= 0:
                    continue
                eligible, reason = status_is_sell_eligible(row, market, require_order_rules=use_effective_schedules)
                if not eligible:
                    blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": reason})
                    continue
                limit_reason = _price_limit_block(
                    row, row.get("previous_close"), "SELL", market,
                    require_quote_bounds=use_effective_schedules,
                )
                if limit_reason:
                    blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": limit_reason})
                    continue
                reference = float(row["open"])
                fill_price = reference * (1.0 - slippage)
                notional = quantity * fill_price
                fee_row = scheduled_fees_by_date.get(day_date)
                fee_components = (
                    scheduled_fee_components(notional, "SELL", fee_row)
                    if fee_row is not None else _fee_components(notional, "SELL", costs)
                )
                fees = sum(fee_components.values())
                acquisition_date = holding_since.get(symbol, "")
                if execution_ledger is None:
                    allocated_basis = cost_basis_cny.get(symbol, 0.0) * quantity / current if current else 0.0
                    realized_pnl = notional - fees - allocated_basis
                    cash += notional - fees
                    holdings[symbol] = current - quantity
                    cost_basis_cny[symbol] = max(0.0, cost_basis_cny.get(symbol, 0.0) - allocated_basis)
                else:
                    (entry,) = execution_ledger.apply_fill(TradeFill(
                        fill_id=f"bt-{day_date}-{symbol}-SELL-{trade_sequence_start + len(trades):08d}",
                        trade_date=day_date,
                        symbol=symbol,
                        side="SELL",
                        quantity=quantity,
                        price_cny=Decimal(str(fill_price)),
                        fees=tuple(
                            FeeItem(name, Decimal(str(amount)))
                            for name, amount in fee_components.items()
                        ),
                    ))
                    entry_details = dict(entry.details)
                    allocated_basis = float(Decimal(entry_details["allocated_cost_basis_cny"]))
                    realized_pnl = float(Decimal(entry_details["realized_pnl_cny"]))
                    sync_projection_from_ledger()
                total_turnover += notional
                execution_impact = quantity * (reference - fill_price)
                trades.append({
                    "trade_date": day_date, "symbol": symbol, "side": "SELL", "quantity": quantity,
                    "reference_price": reference, "estimated_fill_price": fill_price,
                    "notional_cny": notional, "fees_cny": fees,
                    "commission_cny": fee_components["commission_cny"],
                    "stamp_duty_cny": fee_components["stamp_duty_cny"],
                    "transfer_fee_cny": fee_components["transfer_fee_cny"],
                    "execution_impact_vs_reference_cny": execution_impact,
                    "allocated_cost_basis_cny": allocated_basis, "realized_pnl_cny": realized_pnl,
                    "holding_since": acquisition_date, "reason": "目标排名/权重调整；次交易日开盘模拟",
                })
                if fee_row is not None:
                    trades[-1].update({"fee_source": fee_row["source"], "fee_valid_from": fee_row["valid_from"].date().isoformat()})
                if use_effective_schedules:
                    trades[-1].update({
                        "market_rule_source": row["market_rule_source"],
                        "market_rule_source_sha256": row["market_rule_source_sha256"],
                        "market_rule_valid_from": pd.Timestamp(row["market_rule_valid_from"]).date().isoformat(),
                    })
                if holdings.get(symbol, 0) <= 0:
                    holdings.pop(symbol, None)
                    cost_basis_cny.pop(symbol, None)
                    holding_since.pop(symbol, None)

            for symbol, desired in target_shares.items():
                current = holdings.get(symbol, 0)
                quantity = desired - current
                row = day.loc[symbol] if symbol in day.index else None
                if row is None:
                    if quantity > 0:
                        blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": "當日無行情行，交易狀態無法確認"})
                    continue
                minimum, step = order_constraints(row, "BUY", market, allow_fallback=not use_effective_schedules)
                quantity = round_order_quantity(quantity, minimum, step)
                if quantity < minimum:
                    continue
                eligible, reason = status_is_buy_eligible(row, market, require_order_rules=use_effective_schedules)
                if not eligible:
                    blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": reason})
                    continue
                limit_reason = _price_limit_block(
                    row, row.get("previous_close"), "BUY", market,
                    require_quote_bounds=use_effective_schedules,
                )
                if limit_reason:
                    blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": limit_reason})
                    continue
                reference = float(row["open"])
                fill_price = reference * (1.0 + slippage)
                while quantity >= minimum:
                    notional = quantity * fill_price
                    fee_row = scheduled_fees_by_date.get(day_date)
                    fee_components = (
                        scheduled_fee_components(notional, "BUY", fee_row)
                        if fee_row is not None else _fee_components(notional, "BUY", costs)
                    )
                    fees = sum(fee_components.values())
                    if execution_ledger is None:
                        cash_sufficient = notional + fees <= cash
                    else:
                        cash_sufficient = (
                            Decimal(str(fill_price)) * quantity + Decimal(str(fees))
                            <= execution_ledger.cash_cny
                        )
                    if cash_sufficient:
                        break
                    quantity -= step
                if quantity < minimum:
                    blocked_orders.append({"trade_date": day_date, "symbol": symbol, "side": "BUY", "quantity": desired - current, "reason": "可用現金不足，按買入單位縮減後仍無法成交"})
                    continue
                notional = quantity * fill_price
                fee_row = scheduled_fees_by_date.get(day_date)
                fee_components = (
                    scheduled_fee_components(notional, "BUY", fee_row)
                    if fee_row is not None else _fee_components(notional, "BUY", costs)
                )
                fees = sum(fee_components.values())
                if execution_ledger is None:
                    cash -= notional + fees
                    holdings[symbol] = current + quantity
                    cost_basis_cny[symbol] = cost_basis_cny.get(symbol, 0.0) + notional + fees
                    holding_since.setdefault(symbol, day_date)
                else:
                    sellable_from = next_open_session_by_date.get(day_date)
                    if sellable_from is None:
                        raise ValueError(
                            f"{day_date} 买入成交缺少下一交易日历，无法确定 T+1 可卖日期"
                        )
                    execution_ledger.apply_fill(TradeFill(
                        fill_id=f"bt-{day_date}-{symbol}-BUY-{trade_sequence_start + len(trades):08d}",
                        trade_date=day_date,
                        symbol=symbol,
                        side="BUY",
                        quantity=quantity,
                        price_cny=Decimal(str(fill_price)),
                        fees=tuple(
                            FeeItem(name, Decimal(str(amount)))
                            for name, amount in fee_components.items()
                        ),
                        sellable_from=sellable_from,
                    ))
                    sync_projection_from_ledger()
                total_turnover += notional
                execution_impact = quantity * (fill_price - reference)
                trades.append({
                    "trade_date": day_date, "symbol": symbol, "side": "BUY", "quantity": quantity,
                    "reference_price": reference, "estimated_fill_price": fill_price,
                    "notional_cny": notional, "fees_cny": fees,
                    "commission_cny": fee_components["commission_cny"],
                    "stamp_duty_cny": fee_components["stamp_duty_cny"],
                    "transfer_fee_cny": fee_components["transfer_fee_cny"],
                    "execution_impact_vs_reference_cny": execution_impact,
                    "allocated_cost_basis_cny": 0.0, "realized_pnl_cny": pd.NA,
                    "holding_since": holding_since[symbol], "reason": "收盘排名入选；次交易日开盘模拟",
                })
                if fee_row is not None:
                    trades[-1].update({"fee_source": fee_row["source"], "fee_valid_from": fee_row["valid_from"].date().isoformat()})
                if use_effective_schedules:
                    trades[-1].update({
                        "market_rule_source": row["market_rule_source"],
                        "market_rule_source_sha256": row["market_rule_source_sha256"],
                        "market_rule_valid_from": pd.Timestamp(row["market_rule_valid_from"]).date().isoformat(),
                    })
            pending_targets = None

        # Company-action accounting is applied after the simulated open fills:
        # record-date entitlement reflects end-of-session holdings, accrual is
        # recognized on ex-date, and payable cash is settled on its scheduled
        # date. Unknown receivable tax blocks NAV; unresolved future tax after
        # deferred-at-payment treatment is reported with an explicit gross scope.
        if execution_ledger is not None:
            action_day = pd.Timestamp(date).date()
            for event in event_schedule.distribution_records.get(action_day, ()):
                execution_ledger.record_share_distribution(event, as_of=event.record_date)
            for event in event_schedule.dividend_records.get(action_day, ()):
                execution_ledger.record_cash_dividend(event, as_of=event.record_date)
            for pending_event in tuple(active_distribution_claims.values()):
                if not pending_event.effective_date <= action_day < pending_event.registration_date:
                    continue
                claim = next(
                    (item for item in execution_ledger.claims if item.claim_id == f"distribution:{pending_event.event_id}"),
                    None,
                )
                if claim is None or claim.status != "DISTRIBUTION_PENDING_REGISTRATION":
                    active_distribution_claims.pop(pending_event.event_id, None)
                    continue
                mark_distribution_claim_at_close(pending_event, action_day, day)
            for event in event_schedule.dividend_accruals.get(action_day, ()):
                execution_ledger.accrue_cash_dividend(
                    event,
                    as_of=event.ex_date,
                    tax_cny=event_schedule.dividend_tax_cny.get(event.event_id),
                    tax_treatment=event_schedule.dividend_tax_treatment.get(event.event_id),
                    payment_withholding_per_share_cny=(
                        event_schedule.dividend_payment_withholding_per_share_cny.get(event.event_id)
                    ),
                )
            for event in event_schedule.dividend_payments.get(action_day, ()):
                if event.payable_date == event.ex_date:
                    execution_ledger.settle_receivable(
                        f"dividend:{event.event_id}", settled_on=event.payable_date,
                    )
            sync_projection_from_ledger()

        for symbol in holdings:
            row = day.loc[symbol] if symbol in day.index else None
            if row is not None and pd.notna(row["close"]) and float(row["close"]) > 0:
                last_prices[symbol] = float(row["close"])
                last_price_dates[symbol] = day_date
                last_price_sources[symbol] = str(row.get("data_source", "normalized_input_bars"))
                last_price_global_indices[symbol] = global_index
        if execution_ledger is None:
            equity = cash + sum(shares * last_prices.get(symbol, 0.0) for symbol, shares in holdings.items())
        else:
            prices: dict[str, PriceMark] = {}
            for symbol in {lot.symbol for lot in execution_ledger.positions}:
                if symbol not in last_prices or symbol not in last_price_dates:
                    continue
                quote_date = last_price_dates[symbol]
                prices[symbol] = PriceMark(
                    price_cny=Decimal(str(last_prices[symbol])),
                    price_date=quote_date,
                    source=last_price_sources.get(symbol, "normalized_input_bars"),
                    age_sessions=max(0, global_index - last_price_global_indices.get(symbol, global_index)),
                )
            nav = execution_ledger.mark_to_market(
                day_date,
                prices,
                max_stale_age_sessions=max_stale_price_age,
            )
            if nav.status == "UNVALUED" or nav.nav_cny is None:
                raise ValueError(
                    f"ledger_v2 在 {day_date} 无法计算可信 NAV：" + "；".join(nav.reasons)
                )
            equity = float(nav.nav_cny)
            sync_projection_from_ledger()
            tax_review_status = nav.status
            tax_scope = nav.valuation_tax_scope
            unresolved_dividend_tax_count = nav.unresolved_dividend_tax_count
        if execution_ledger is None:
            tax_review_status = "NOT_APPLICABLE"
            tax_scope = "NOT_APPLICABLE"
            unresolved_dividend_tax_count = 0
        equity_rows.append({
            "trade_date": day_date,
            "equity_cny": equity,
            "cash_cny": cash,
            "holdings_count": len(holdings),
            "tax_review_status": tax_review_status,
            "valuation_tax_scope": tax_scope,
            "unresolved_dividend_tax_count": unresolved_dividend_tax_count,
        })

        create_final_pending_signal = backtest_checkpoint is not None or normalized_calendar is not None
        if (
            global_index >= lookback
            and (global_index - lookback) % rebalance_sessions == 0
            and (index + 1 < len(dates) or create_final_pending_signal)
        ):
            ranked = rank_precomputed_day(day, strategy, market, require_order_rules=use_effective_schedules)
            selected = ranked.head(int(strategy["max_positions"]))
            if not selected.empty:
                target_weight = min(
                    float(strategy["max_position_weight"]),
                    float(strategy["max_total_exposure"]) / len(selected),
                )
                pending_targets = {str(row["symbol"]): target_weight for _, row in selected.iterrows()}
            else:
                pending_targets = {}

    curve = pd.DataFrame(equity_rows)
    curve["return"] = curve["equity_cny"].pct_change().fillna(0.0)
    if backtest_checkpoint is not None:
        curve.loc[curve.index[0], "return"] = (
            float(curve.loc[curve.index[0], "equity_cny"])
            / float(backtest_checkpoint.previous_equity_cny) - 1.0
        )
    data["raw_close_return"] = data["close"].div(data["previous_close"]).sub(1.0)
    benchmark_symbol = str(config.get("benchmark", {}).get("symbol", ""))
    benchmark_data: pd.DataFrame | None = None
    if benchmark_frame is not None:
        benchmark_data = normalize_bar_frame(benchmark_frame)
        benchmark_quality = check_data_quality(
            benchmark_data,
            large_change_warning=float(config.get("data", {}).get("large_daily_price_change_warning", 0.50)),
        )
        if not benchmark_quality["healthy"]:
            raise ValueError("基准数据质量检查未通过：" + "；".join(benchmark_quality["errors"]))
        benchmark_rows = benchmark_data.loc[benchmark_data["symbol"] == benchmark_symbol]
        if benchmark_rows.empty:
            raise ValueError(f"独立基准数据不包含配置标的 {benchmark_symbol}")
        benchmark_rows = benchmark_rows.sort_values("trade_date").copy()
        benchmark_method = f"configured_index_close_return:{benchmark_symbol}"
    else:
        benchmark_rows = data.loc[data["symbol"] == benchmark_symbol] if benchmark_symbol else pd.DataFrame()
    if benchmark_frame is None and not benchmark_rows.empty:
        benchmark_rows = benchmark_rows.sort_values("trade_date").copy()
        benchmark_method = f"configured_index_close_return:{benchmark_symbol}"
    elif benchmark_frame is None:
        allowed = data.apply(lambda row: status_is_buy_eligible(row, market, require_order_rules=False)[0], axis=1)
        data["peer_return_eligible"] = allowed & data["raw_close_return"].notna()
        peer_returns = data.loc[data["peer_return_eligible"]].assign(
            trade_date_key=lambda table: pd.to_datetime(table["trade_date"]).dt.strftime("%Y-%m-%d")
        ).groupby("trade_date_key")["raw_close_return"].mean()
        benchmark_method = "equal-weight daily returns of status-known eligible input symbols; not an index benchmark"
    if benchmark_method.startswith("configured_index_close_return:"):
        benchmark_rows["trade_date_key"] = pd.to_datetime(benchmark_rows["trade_date"]).dt.strftime("%Y-%m-%d")
        benchmark_close = benchmark_rows.set_index("trade_date_key")["close"]
        evaluation_dates = curve["trade_date"].astype(str).tolist()
        missing_benchmark_dates = sorted(set(evaluation_dates) - set(benchmark_close.index.astype(str)))
        if missing_benchmark_dates:
            raise ValueError(
                "基准数据缺少评价区间交易日：" + ", ".join(missing_benchmark_dates)
            )
        aligned_close = benchmark_close.reindex(evaluation_dates)
        invalid_closes = aligned_close.isna() | ~np.isfinite(aligned_close.astype(float)) | aligned_close.astype(float).le(0)
        if invalid_closes.any():
            invalid_dates = curve.loc[invalid_closes.to_numpy(), "trade_date"].astype(str).tolist()
            raise ValueError("基准数据评价区间收盘价无效：" + ", ".join(invalid_dates))
        peer_curve = aligned_close.astype(float).pct_change(fill_method=None).fillna(0.0).reset_index(drop=True)
        if backtest_checkpoint is not None:
            if backtest_checkpoint.benchmark_symbol != benchmark_symbol:
                raise ValueError("checkpoint 基准标的与本次运行配置不同")
            if backtest_checkpoint.benchmark_previous_close_cny is None:
                raise ValueError("checkpoint 缺少上一窗口基准收盘价，无法衔接基准日收益")
            peer_curve.iloc[0] = (
                float(aligned_close.iloc[0]) / float(backtest_checkpoint.benchmark_previous_close_cny) - 1.0
            )
    else:
        benchmark_values = peer_returns
        peer_curve = pd.Series(
            [float(benchmark_values.get(day, 0.0)) for day in curve["trade_date"]],
            index=curve.index,
            dtype=float,
        )
    benchmark_base_equity = (
        float(backtest_checkpoint.previous_equity_cny)
        if backtest_checkpoint is not None else float(curve["equity_cny"].iloc[0])
    )
    benchmark_values = pd.Series(
        benchmark_base_equity * (1.0 + peer_curve.fillna(0.0)).cumprod().to_numpy()
    )
    benchmark = (
        pd.concat([pd.Series([benchmark_base_equity]), benchmark_values], ignore_index=True)
        if backtest_checkpoint is not None else benchmark_values
    )
    equity_series = (
        pd.concat(
            [pd.Series([float(backtest_checkpoint.previous_equity_cny)]), curve["equity_cny"]],
            ignore_index=True,
        )
        if backtest_checkpoint is not None else curve["equity_cny"]
    )

    compact_config = {name: config[name] for name in ("data", "strategy", "market", "costs", "backtest", "benchmark") if name in config}
    config_json = json.dumps(compact_config, ensure_ascii=False, sort_keys=True, default=str)
    data_digest = source_data_sha256 or hashlib.sha256(
        pd.util.hash_pandas_object(data[BAR_COLUMNS], index=False).values.tobytes()
    ).hexdigest()
    data_digest = hashlib.sha256(
        f"{data_digest}:{current_window_bars_sha256}".encode("utf-8")
    ).hexdigest()
    opening_snapshot_sha256 = None
    if opening_accounting_snapshot is not None:
        snapshot_payload = json.dumps(
            asdict(opening_accounting_snapshot), ensure_ascii=False, sort_keys=True,
            default=str, separators=(",", ":"),
        ).encode("utf-8")
        opening_snapshot_sha256 = hashlib.sha256(snapshot_payload).hexdigest()
    data_digest = hashlib.sha256(
        f"{data_digest}:{accounting_valuation_inputs_sha256}:{opening_snapshot_sha256 or ''}".encode("utf-8")
    ).hexdigest()
    market_rule_schedule_digest = (
        _normalized_table_digest(normalized_market_rules) if normalized_market_rules is not None else None
    )
    fee_schedule_digest = (
        _normalized_table_digest(normalized_fee_schedule) if normalized_fee_schedule is not None else None
    )
    if market_rule_schedule_digest is not None and fee_schedule_digest is not None:
        data_digest = hashlib.sha256(
            f"{data_digest}:{market_rule_schedule_digest}:{fee_schedule_digest}".encode("utf-8")
        ).hexdigest()
    identity_event_data_sha256 = None
    if not identity_events.empty:
        identity_event_data_sha256 = hashlib.sha256(
            identity_events.to_csv(index=False, lineterminator="\n").encode("utf-8")
        ).hexdigest()
        data_digest = hashlib.sha256(
            f"{data_digest}:{identity_event_data_sha256}".encode("utf-8")
        ).hexdigest()
    corporate_action_data_sha256 = None
    if not action_events.empty:
        corporate_action_data_sha256 = hashlib.sha256(
            action_events.to_csv(index=False, lineterminator="\n").encode("utf-8")
        ).hexdigest()
        data_digest = hashlib.sha256(
            f"{data_digest}:{corporate_action_data_sha256}".encode("utf-8")
        ).hexdigest()
    accounting_event_data_sha256 = None
    if accounting_event_list:
        event_payload = json.dumps(
            {
                "events": [asdict(event) for event in sorted(accounting_event_list, key=lambda item: item.event_id)],
                "dividend_tax_cny_by_event": {
                    event_id: None if amount is None else str(amount)
                    for event_id, amount in sorted(event_schedule.dividend_tax_cny.items())
                },
                "dividend_tax_treatment_by_event": dict(sorted(event_schedule.dividend_tax_treatment.items())),
                "dividend_payment_withholding_per_share_cny_by_event": {
                    event_id: None if amount is None else str(amount)
                    for event_id, amount in sorted(event_schedule.dividend_payment_withholding_per_share_cny.items())
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        accounting_event_data_sha256 = hashlib.sha256(event_payload).hexdigest()
        data_digest = hashlib.sha256(
            f"{data_digest}:{accounting_event_data_sha256}".encode("utf-8")
        ).hexdigest()
    benchmark_data_digest = None
    if benchmark_data is not None:
        benchmark_data_digest = hashlib.sha256(
            pd.util.hash_pandas_object(benchmark_data[BAR_COLUMNS], index=False).values.tobytes()
        ).hexdigest()
        data_digest = hashlib.sha256(
            f"{data_digest}:{benchmark_data_digest}".encode("utf-8")
        ).hexdigest()
    source_frames = [data] + ([benchmark_data] if benchmark_data is not None else [])
    data_sources = sorted({
        str(value)
        for source_frame in source_frames
        for value in source_frame["data_source"].dropna().unique()
    })
    retrieved_values: list[str] = []
    for source_frame in source_frames:
        values = source_frame["retrieved_at_utc"]
        candidates = values.cat.categories if isinstance(values.dtype, pd.CategoricalDtype) else values.dropna().unique()
        parsed_candidates = [str(value) for value in candidates if not pd.isna(value)]
        if parsed_candidates:
            retrieved_values.append(max(parsed_candidates))
    missing_units = data[[
        "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
    ]].isna().any(axis=1)
    has_limit = data["has_price_limit"]
    missing_limit = (
        (has_limit.isna() & bool(market.get("assume_price_limit_applies_when_unknown", True)))
        | (has_limit.fillna(False) & data["price_limit_pct"].isna())
    )
    rule_fallback_rows = int((missing_units | missing_limit).sum())
    chronological = _chronological_evaluation(
        curve,
        benchmark,
        pd.DataFrame(trades, columns=["trade_date", "notional_cny"]),
        float(config.get("backtest", {}).get("development_fraction", 0.60)),
        float(config.get("backtest", {}).get("validation_fraction", 0.20)),
        int(config.get("backtest", {}).get("rolling_window_sessions", 60)),
        int(config.get("backtest", {}).get("rolling_step_sessions", 20)),
    )
    trade_frame = pd.DataFrame(trades)
    metric_returns = equity_series.pct_change().fillna(0.0)
    metrics = _metrics(
        equity_series,
        metric_returns,
        total_turnover / max(float(equity_series.mean()), 1.0),
        benchmark,
    )
    limitations = [
        "基线以收盘 t 的信号在下一交易日开盘模拟；不代表可成交价格。",
        "日线 OHLC 无法重建开盘队列、盘口流动性、真实停牌日与部分涨跌停成交。",
        "账务事件只覆盖本次显式登记并支持的类型；未登记或未实现的送转、配股、退市、其他合并及历史成分股处理仍可能影响结果。",
    ]
    if normalized_fee_schedule is None:
        limitations.append("配置中的费用费率统一应用于整段历史，不代表历史逐年费率或个人账户实际佣金。")
    else:
        limitations.append("使用输入费率表按有效日期计算；其证据登记/人工复核状态由维护者提供，不代表华泰账户实际佣金或费用取整明细。")
    if normalized_market_rules is None:
        limitations.append("缺失的涨跌幅阈值、是否限价状态或交易单位使用配置回退假设；成交结果仅用于研究，计划生成要求完整点时证券规则。")
    else:
        limitations.append("使用输入交易规则表逐证券、逐有效日期匹配；本研究模式仍不替代正式点时审计或公告人工复核。")
    if benchmark_data is not None:
        limitations.append("沪深300基准采用价格指数收盘价，不含成分股分红的总回报。")
    else:
        limitations.append("没有独立指数行情时，使用输入股票池等权收益作参照；该参照不是市场指数，也不修正幸存者偏差。")
    strategy_checkpoint: BacktestCheckpoint | None = None
    if normalized_calendar is not None:
        if execution_ledger is None:
            raise ValueError("完整策略 checkpoint 需要 ledger_v2 账本")
        last_date = pd.Timestamp(dates[-1]).date()
        future_open_dates = normalized_calendar.loc[
            normalized_calendar["is_open"].fillna(False)
            & (normalized_calendar["trade_date"] > last_date),
            "trade_date",
        ]
        if future_open_dates.empty:
            raise ValueError("checkpoint 缺少期末后的下一开市日")
        history_cutoff = pd.Timestamp(last_date)
        history = signal_history_source.loc[
            pd.to_datetime(signal_history_source["trade_date"]) <= history_cutoff
        ].copy()
        history_rows: list[SignalHistoryBar] = []
        for symbol, group in history.groupby("symbol", sort=False, observed=True):
            for row in group.sort_values("trade_date", kind="stable").tail(lookback).itertuples(index=False):
                signal_value = None if pd.isna(row.signal_price) else Decimal(str(float(row.signal_price)))
                history_rows.append(SignalHistoryBar(
                    symbol=str(symbol),
                    trade_date=pd.Timestamp(row.trade_date).date(),
                    close_cny=Decimal(str(float(row.close))),
                    signal_price_cny=signal_value,
                ))
        checkpoint_prices = tuple(
            CheckpointPrice(
                symbol=symbol,
                price_cny=Decimal(str(price)),
                price_date=last_price_dates[symbol],
                global_session_index=last_price_global_indices.get(
                    symbol, global_session_start + len(dates) - 1
                ),
                source=last_price_sources.get(symbol, "normalized_input_bars"),
            )
            for symbol, price in sorted(last_prices.items())
            if symbol in holdings and symbol in last_price_dates
        )
        benchmark_previous_close: Decimal | None = None
        checkpoint_benchmark_symbol: str | None = None
        if benchmark_method.startswith("configured_index_close_return:"):
            checkpoint_benchmark_symbol = benchmark_symbol
            benchmark_as_of = benchmark_rows.loc[
                pd.to_datetime(benchmark_rows["trade_date"]).dt.date == last_date
            ]
            if benchmark_as_of.empty:
                raise ValueError(f"checkpoint 缺少期末基准收盘价：{last_date} {benchmark_symbol}")
            benchmark_previous_close = Decimal(str(float(benchmark_as_of.iloc[-1]["close"])))
        ledger_snapshot = execution_ledger.snapshot(as_of_date=last_date)
        calendar_prefix = normalized_calendar.loc[
            normalized_calendar["trade_date"] <= last_date
        ].copy()
        chain_previous = backtest_checkpoint.data_chain_sha256 if backtest_checkpoint is not None else ""
        current_chain_part = (
            f"{current_window_data_sha256}:{current_window_bars_sha256}:"
            f"{benchmark_data_digest or 'peer_from_input'}"
        )
        data_chain_sha256 = hashlib.sha256(
            f"{chain_previous}:{current_chain_part}".encode("utf-8")
        ).hexdigest()
        strategy_checkpoint = BacktestCheckpoint(
            schema_version=1,
            strategy_id=str(strategy["name"]),
            as_of_date=last_date,
            next_expected_session=future_open_dates.iloc[0],
            global_session_index=global_session_start + len(dates) - 1,
            trade_count=trade_sequence_start + len(trades),
            pending_targets=tuple(sorted((pending_targets or {}).items())),
            transition_signal_ready_index=tuple(sorted(transition_signal_ready_index.items())),
            signal_history=tuple(history_rows),
            signal_price_modes=tuple(sorted(signal_modes.items())),
            last_prices=checkpoint_prices,
            previous_equity_cny=Decimal(str(float(curve["equity_cny"].iloc[-1]))),
            ledger_snapshot=ledger_snapshot,
            config_sha256=config_sha256,
            market_rules_sha256=(
                _normalized_table_digest(normalized_market_rules) if normalized_market_rules is not None else None
            ),
            fee_schedule_sha256=(
                _normalized_table_digest(normalized_fee_schedule) if normalized_fee_schedule is not None else None
            ),
            accounting_events_sha256=accounting_event_data_sha256 or accounting_events_sha256,
            identity_events_sha256=identity_events_sha256,
            corporate_actions_sha256=corporate_actions_sha256,
            calendar_sha256=normalized_calendar_sha256,
            calendar_prefix_sha256=_normalized_table_digest(calendar_prefix),
            last_window_data_sha256=current_window_data_sha256,
            last_window_bars_sha256=current_window_bars_sha256,
            data_chain_sha256=data_chain_sha256,
            source_data_path=str(source_path) if source_path is not None else None,
            benchmark_symbol=checkpoint_benchmark_symbol,
            benchmark_previous_close_cny=benchmark_previous_close,
        ).sealed()
    metrics.update({
        "experiment_id": f"chq-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "strategy_id": str(strategy["name"]),
        "config_sha256": hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
        "data_version_sha256": data_digest,
        "normalized_window_bars_sha256": current_window_bars_sha256,
        "security_identity_events_sha256": identity_event_data_sha256,
        "security_identity_event_ids": identity_events["event_id"].astype(str).tolist() if not identity_events.empty else [],
        "corporate_actions_sha256": corporate_action_data_sha256,
        "corporate_action_event_ids": action_events["event_id"].astype(str).tolist() if not action_events.empty else [],
        "accounting_events_sha256": accounting_event_data_sha256,
        "accounting_event_ids": [event.event_id for event in accounting_event_list],
        "accounting_event_types": [type(event).__name__ for event in accounting_event_list],
        "dividend_tax_known_event_ids": sorted(
            event_id for event_id, amount in event_schedule.dividend_tax_cny.items() if amount is not None
        ),
        "dividend_tax_treatment_by_event": dict(sorted(event_schedule.dividend_tax_treatment.items())),
        "dividend_payment_withholding_per_share_cny_by_event": {
            event_id: None if amount is None else str(amount)
            for event_id, amount in sorted(event_schedule.dividend_payment_withholding_per_share_cny.items())
        },
        "dividend_tax_valuation_scope": (
            str(curve["valuation_tax_scope"].iloc[-1]) if "valuation_tax_scope" in curve else "NOT_APPLICABLE"
        ),
        "unresolved_dividend_tax_count_at_end": (
            int(curve["unresolved_dividend_tax_count"].iloc[-1])
            if "unresolved_dividend_tax_count" in curve else 0
        ),
        "unresolved_dividend_tax_count_max": (
            int(curve["unresolved_dividend_tax_count"].max())
            if "unresolved_dividend_tax_count" in curve else 0
        ),
        "accounting_event_scheduler_status": "supported_subset_applied" if accounting_event_list else "no_typed_events",
        "accounting_valuation_inputs_sha256": accounting_valuation_inputs_sha256,
        "accounting_opening_snapshot_sha256": opening_snapshot_sha256,
        "accounting_opening_snapshot_as_of": (
            opening_accounting_snapshot.as_of_date.isoformat()
            if opening_accounting_snapshot is not None and opening_accounting_snapshot.as_of_date else None
        ),
        "backtest_checkpoint_sha256": strategy_checkpoint.state_sha256 if strategy_checkpoint else None,
        "backtest_checkpoint_as_of": strategy_checkpoint.as_of_date.isoformat() if strategy_checkpoint else None,
        "backtest_checkpoint_next_expected_session": (
            strategy_checkpoint.next_expected_session.isoformat() if strategy_checkpoint else None
        ),
        "backtest_continuation_scope": (
            "full_rule_based_strategy_checkpoint_v1" if strategy_checkpoint
            else "ledger_snapshot_only" if opening_accounting_snapshot is not None
            else "single_window_no_continuation"
        ),
        "market_rule_schedule_sha256": market_rule_schedule_digest,
        "market_rule_schedule_rows": int(len(normalized_market_rules)) if normalized_market_rules is not None else 0,
        "fee_schedule_sha256": fee_schedule_digest,
        "fee_schedule_rows": int(len(normalized_fee_schedule)) if normalized_fee_schedule is not None else 0,
        "market_rule_mode": "effective_dated_schedule" if normalized_market_rules is not None else "config_fallback",
        "fee_mode": "effective_dated_schedule" if normalized_fee_schedule is not None else "config_flat_assumption",
        "benchmark_data_version_sha256": benchmark_data_digest,
        "code_version": __version__,
        **_git_metadata(),
        "data_sources": data_sources,
        "data_retrieved_at_utc": max(retrieved_values) if retrieved_values else None,
        "universe_symbols": sorted(str(value) for value in data["symbol"].unique()),
        "universe_filter": "status-known, listed and not suspended; ST excluded when configured; momentum threshold from config",
        "features": [f"{lookback}-session adjusted-close momentum"],
        "security_identity_events": int(len(identity_events)),
        "security_identity_signal_policy": "security-code changes never splice momentum history; successor begins a fresh configured lookback",
        "label": "none; rule-based baseline",
        "model": "none; rank-based technical baseline",
        "training_interval": "not applicable",
        "sample_evaluation": "full-period summary, chronological development/validation/test slices, and rolling fixed-rule stability diagnostics; no fitted model and no rolling out-of-sample claim",
        "chronological_evaluation": chronological,
        "runtime_seconds": round(time.perf_counter() - started, 6),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_start": str(dates[0]),
        "data_end": str(dates[-1]),
        "benchmark_data_start": str(benchmark_rows["trade_date"].min()) if not benchmark_rows.empty else None,
        "benchmark_data_end": str(benchmark_rows["trade_date"].max()) if not benchmark_rows.empty else None,
        "initial_cash_cny": initial_cash_cny,
        "evaluation_start_equity_cny": benchmark_base_equity,
        "trade_count": len(trades),
        "blocked_order_count": len(blocked_orders),
        "total_commission_cny": float(trade_frame.get("commission_cny", pd.Series(dtype=float)).fillna(0).sum()),
        "total_stamp_duty_cny": float(trade_frame.get("stamp_duty_cny", pd.Series(dtype=float)).fillna(0).sum()),
        "total_transfer_fee_cny": float(trade_frame.get("transfer_fee_cny", pd.Series(dtype=float)).fillna(0).sum()),
        "total_execution_impact_vs_reference_cny": float(
            trade_frame.get("execution_impact_vs_reference_cny", pd.Series(dtype=float)).fillna(0).sum()
        ),
        "market_rule_fallback_row_count": rule_fallback_rows,
        "market_rule_fallback_assumptions": {
            "price_limit_pct": market.get("default_price_limit_pct"),
            "assume_price_limit_applies_when_unknown": market.get("assume_price_limit_applies_when_unknown", True),
            "buy_min_and_step_shares": market.get("buy_lot_shares"),
            "sell_min_and_step_shares": market.get("sell_lot_shares"),
        },
        "benchmark_method": benchmark_method,
        "result_type": (
            "formal_historical_baseline_research_not_simulated_or_real_account"
            if formal else "research_backtest_not_simulated_or_real_account"
        ),
        "quality_warnings": quality["warnings"],
        "limitations": limitations,
    })
    result = {
        "metrics": metrics,
        "equity_curve": curve,
        "trades": trade_frame.reindex(columns=[
            "trade_date", "symbol", "side", "quantity", "reference_price", "estimated_fill_price",
            "notional_cny", "fees_cny", "commission_cny", "stamp_duty_cny", "transfer_fee_cny",
            "execution_impact_vs_reference_cny", "fee_source", "fee_valid_from", "market_rule_source",
            "market_rule_source_sha256", "market_rule_valid_from", "allocated_cost_basis_cny",
            "realized_pnl_cny", "holding_since", "reason",
        ]),
        "blocked_orders": pd.DataFrame(blocked_orders, columns=["trade_date", "symbol", "side", "quantity", "reason"]),
        "security_events": pd.DataFrame(security_events),
        "open_positions": pd.DataFrame([
            {
                "symbol": symbol,
                "shares": shares,
                "cost_basis_cny": cost_basis_cny.get(symbol, 0.0),
                "average_cost_cny": cost_basis_cny.get(symbol, 0.0) / shares if shares else 0.0,
                "holding_since": holding_since.get(symbol, ""),
            }
            for symbol, shares in sorted(holdings.items())
        ], columns=["symbol", "shares", "cost_basis_cny", "average_cost_cny", "holding_since"]),
    }
    if accounting_mode == "ledger_shadow":
        shadow = run_accounting_shadow(
            initial_cash_cny=initial_cash_cny,
            trades=result["trades"],
            bars=data,
            equity_curve=curve,
            open_positions=result["open_positions"],
        )
        result["accounting_daily_comparison"] = shadow["daily_comparison"]
        result["accounting_journal"] = shadow["journal"]
        result["accounting_snapshot"] = shadow["snapshot"]
        result["accounting_shadow"] = {
            key: value for key, value in shadow.items()
            if key not in {"daily_comparison", "journal", "snapshot"}
        }
        result["metrics"]["accounting_mode"] = "ledger_shadow"
        result["metrics"]["accounting_shadow_status"] = shadow["status"]
        result["metrics"]["accounting_shadow_result_type"] = shadow["result_type"]
    elif accounting_mode == "ledger_v2":
        assert execution_ledger is not None
        execution_ledger.assert_invariants()
        result["accounting_journal"] = pd.DataFrame([
            {
                "entry_id": entry.entry_id,
                "operation_key": entry.operation_key,
                "event_id": entry.event_id,
                "phase": entry.phase,
                "booking_date": entry.booking_date.isoformat(),
                "entry_type": entry.entry_type,
                "cash_delta_cny": entry.cash_delta_cny,
                "position_deltas": entry.position_deltas,
                "details": dict(entry.details),
            }
            for entry in execution_ledger.journal
        ])
        result["accounting_snapshot"] = (
            strategy_checkpoint.ledger_snapshot if strategy_checkpoint is not None
            else execution_ledger.snapshot(as_of_date=pd.Timestamp(dates[-1]).date())
        )
        if strategy_checkpoint is not None:
            result["backtest_checkpoint"] = strategy_checkpoint
        result["metrics"]["accounting_mode"] = "ledger_v2"
        result["metrics"]["accounting_result_type"] = "ledger_state_source_research_diagnostic_not_formal"
        result["metrics"]["valuation_max_stale_age_sessions"] = max_stale_price_age
    return result
