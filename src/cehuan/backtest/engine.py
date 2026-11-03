from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
import time
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from cehuan import __version__
from cehuan.accounting import FeeItem, PortfolioLedger, PriceMark, TradeFill
from cehuan.backtest.accounting_shadow import run_accounting_shadow
from cehuan.backtest.event_scheduler import AccountingEventSchedule, SupportedAccountingEvent
from cehuan.config import load_config
from cehuan.data.canonical import BAR_COLUMNS, normalize_bar_frame, read_bars
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
    if accounting_mode != "ledger_v2" and (accounting_events or dividend_tax_cny_by_event):
        raise ValueError("显式账务事件与分红税费仅支持 ledger_v2 的非正式研究诊断")
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
    data = add_momentum(data, lookback, normalized=True)
    data["previous_close"] = data.groupby("symbol", sort=False, observed=True)["close"].shift(1)
    trade_dates = data["trade_date"].to_numpy()
    boundaries = np.flatnonzero(trade_dates[1:] != trade_dates[:-1]) + 1
    day_starts = np.concatenate(([0], boundaries))
    day_ends = np.concatenate((boundaries, [len(data)]))
    dates = [data.iloc[start]["trade_date"] for start in day_starts]
    scheduled_fees_by_date = (
        map_fee_schedule_to_sessions(normalized_fee_schedule, dates)
        if normalized_fee_schedule is not None else {}
    )
    date_session_index = {
        pd.Timestamp(value).strftime("%Y-%m-%d"): index for index, value in enumerate(dates)
    }
    if len(dates) <= lookback + 1:
        raise ValueError(f"至少需要 {lookback + 2} 个交易日，当前只有 {len(dates)} 天")

    accounting_event_list = tuple(accounting_events or ())
    event_schedule = AccountingEventSchedule.build(
        accounting_event_list,
        (pd.Timestamp(value).date() for value in dates),
        dividend_tax_cny_by_event,
    )

    cash = float(initial_cash_cny)
    holdings: dict[str, int] = {}
    cost_basis_cny: dict[str, float] = {}
    holding_since: dict[str, str] = {}
    last_prices: dict[str, float] = {}
    last_price_dates: dict[str, str] = {}
    last_price_sources: dict[str, str] = {}
    execution_ledger = (
        PortfolioLedger(Decimal(str(initial_cash_cny)), portfolio_id="backtest-ledger-v2")
        if accounting_mode == "ledger_v2" else None
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

    pending_targets: dict[str, float] | None = None
    transition_signal_ready_index: dict[str, int] = {}
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
            if any(
                pd.isna(new_row.get(column))
                or not math.isfinite(float(new_row[column]))
                or float(new_row[column]) <= 0
                for column in ("open", "high", "low", "close")
            ):
                raise ValueError(f"{day_date} {new_symbol} 生效日 OHLC 无效，不能迁移持仓")
            if pd.isna(new_row.get("is_suspended")) or bool(new_row["is_suspended"]):
                raise ValueError(f"{day_date} {new_symbol} 生效日停牌状态未知或为停牌，不能迁移持仓")
            if pd.isna(new_row.get("is_listed")) or not bool(new_row["is_listed"]):
                raise ValueError(f"{day_date} {new_symbol} 生效日上市状态未知或未上市，不能迁移持仓")
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
                transition_signal_ready_index[new_symbol] = index + lookback
            last_prices.pop(old_symbol, None)
            last_price_dates.pop(old_symbol, None)
            last_price_sources.pop(old_symbol, None)
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
            if any(
                pd.isna(new_row.get(column))
                or not math.isfinite(float(new_row[column]))
                or float(new_row[column]) <= 0
                for column in ("open", "high", "low", "close")
            ):
                raise ValueError(f"{day_date} {new_symbol} 生效日 OHLC 无效，不能迁移持仓")
            if pd.isna(new_row.get("is_suspended")) or bool(new_row["is_suspended"]):
                raise ValueError(f"{day_date} {new_symbol} 生效日停牌状态未知或为停牌，不能迁移持仓")
            if pd.isna(new_row.get("is_listed")) or not bool(new_row["is_listed"]):
                raise ValueError(f"{day_date} {new_symbol} 生效日上市状态未知或未上市，不能迁移持仓")
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
                transition_signal_ready_index[new_symbol] = index + lookback
            last_prices.pop(old_symbol, None)
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
                if symbol in holdings and index <= ready_index
            }
            for symbol in protected_symbols:
                target_shares[symbol] = holdings[symbol]

            # Sells first, so their proceeds can fund the target buys.
            for symbol in list(holdings):
                current = holdings.get(symbol, 0)
                desired = target_shares.get(symbol, 0)
                quantity = max(0, current - desired)
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
                        fill_id=f"bt-{day_date}-{symbol}-SELL-{len(trades):08d}",
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
                    execution_ledger.apply_fill(TradeFill(
                        fill_id=f"bt-{day_date}-{symbol}-BUY-{len(trades):08d}",
                        trade_date=day_date,
                        symbol=symbol,
                        side="BUY",
                        quantity=quantity,
                        price_cny=Decimal(str(fill_price)),
                        fees=tuple(
                            FeeItem(name, Decimal(str(amount)))
                            for name, amount in fee_components.items()
                        ),
                        sellable_from=dates[index + 1],
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
        # date. An unknown dividend tax leaves NAV unvalued and stops the run.
        if execution_ledger is not None:
            action_day = pd.Timestamp(date).date()
            for event in event_schedule.dividend_records.get(action_day, ()):
                execution_ledger.record_cash_dividend(event, as_of=event.record_date)
            for event in event_schedule.dividend_accruals.get(action_day, ()):
                execution_ledger.accrue_cash_dividend(
                    event,
                    as_of=event.ex_date,
                    tax_cny=event_schedule.dividend_tax_cny.get(event.event_id),
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
                    age_sessions=max(0, index - date_session_index[quote_date]),
                )
            nav = execution_ledger.mark_to_market(
                day_date,
                prices,
                max_stale_age_sessions=max_stale_price_age,
            )
            if nav.status != "VALUED" or nav.nav_cny is None:
                raise ValueError(
                    f"ledger_v2 在 {day_date} 无法计算可信 NAV：" + "；".join(nav.reasons)
                )
            equity = float(nav.nav_cny)
            sync_projection_from_ledger()
        equity_rows.append({"trade_date": day_date, "equity_cny": equity, "cash_cny": cash, "holdings_count": len(holdings)})

        if index >= lookback and (index - lookback) % rebalance_sessions == 0 and index + 1 < len(dates):
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
    close_returns = data.groupby("symbol", sort=False, observed=True)["close"].pct_change()
    data["raw_close_return"] = close_returns
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
    else:
        benchmark_values = peer_returns
        peer_curve = pd.Series(
            [float(benchmark_values.get(day, 0.0)) for day in curve["trade_date"]],
            index=curve.index,
            dtype=float,
        )
    benchmark = pd.Series(initial_cash_cny * (1.0 + peer_curve.fillna(0.0)).cumprod().to_numpy())
    equity_series = curve["equity_cny"]

    compact_config = {name: config[name] for name in ("data", "strategy", "market", "costs", "backtest", "benchmark") if name in config}
    config_json = json.dumps(compact_config, ensure_ascii=False, sort_keys=True, default=str)
    data_digest = source_data_sha256 or hashlib.sha256(
        pd.util.hash_pandas_object(data[BAR_COLUMNS], index=False).values.tobytes()
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
    metrics = _metrics(equity_series, curve["return"], total_turnover / max(float(equity_series.mean()), 1.0), benchmark)
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
    metrics.update({
        "experiment_id": f"chq-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "strategy_id": str(strategy["name"]),
        "config_sha256": hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
        "data_version_sha256": data_digest,
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
        "accounting_event_scheduler_status": "supported_subset_applied" if accounting_event_list else "no_typed_events",
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
        result["accounting_snapshot"] = execution_ledger.snapshot()
        result["metrics"]["accounting_mode"] = "ledger_v2"
        result["metrics"]["accounting_result_type"] = "ledger_state_source_research_diagnostic_not_formal"
        result["metrics"]["valuation_max_stale_age_sessions"] = max_stale_price_age
    return result
