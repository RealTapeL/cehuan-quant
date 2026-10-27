from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from cehuan import __version__
from cehuan.data.canonical import BAR_COLUMNS, normalize_bar_frame
from cehuan.data.quality import check_data_quality
from cehuan.signals.strategy import (
    add_momentum,
    order_constraints,
    rank_at_close,
    round_order_quantity,
    status_is_eligible,
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


def _fees(notional: float, side: str, costs: dict[str, Any]) -> float:
    commission = max(
        float(costs.get("minimum_commission_cny", 0.0)),
        notional * float(costs.get("commission_rate", 0.0)),
    )
    stamp = notional * float(costs.get("stamp_duty_sell_rate", 0.0)) if side == "SELL" else 0.0
    transfer = notional * float(costs.get("transfer_fee_rate", 0.0))
    return commission + stamp + transfer


def _price_limit_block(
    row: pd.Series,
    previous_close: float | None,
    side: str,
    market: dict[str, Any],
) -> str | None:
    if previous_close is None or pd.isna(previous_close) or not math.isfinite(float(previous_close)) or float(previous_close) <= 0:
        return None
    previous_close = float(previous_close)
    has_limit = row.get("has_price_limit", pd.NA)
    if pd.isna(has_limit):
        has_limit = bool(market.get("assume_price_limit_applies_when_unknown", True))
    if not bool(has_limit):
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
) -> dict[str, Any]:
    """Run a small long-only daily baseline with next-session-open execution.

    It uses unadjusted OHLC for execution/valuation, adjusted close for signal
    continuity, point-in-time share rules when supplied (otherwise configured
    research fallbacks), fees, and conservative price-limit checks.
    """
    started = time.perf_counter()
    if initial_cash_cny <= 0:
        raise ValueError("回测资金必须大于 0；请显式传入演示或研究资金")
    data = normalize_bar_frame(frame)
    quality = check_data_quality(data, large_change_warning=float(config.get("data", {}).get("large_daily_price_change_warning", 0.50)))
    if not quality["healthy"]:
        raise ValueError("数据质量检查未通过：" + "；".join(quality["errors"]))
    strategy = config["strategy"]
    market = config["market"]
    costs = config["costs"]
    lookback = int(strategy["lookback_sessions"])
    rebalance_sessions = int(strategy["rebalance_sessions"])
    if rebalance_sessions < 1:
        raise ValueError("rebalance_sessions 必须大于 0")

    data = add_momentum(data, lookback)
    data["previous_close"] = data.groupby("symbol", sort=False)["close"].shift(1)
    dates = sorted(data["trade_date"].dropna().unique())
    if len(dates) <= lookback + 1:
        raise ValueError(f"至少需要 {lookback + 2} 个交易日，当前只有 {len(dates)} 天")

    cash = float(initial_cash_cny)
    holdings: dict[str, int] = {}
    last_prices: dict[str, float] = {}
    pending_targets: dict[str, float] | None = None
    equity_rows: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    blocked_orders: list[dict[str, Any]] = []
    total_turnover = 0.0
    slippage = float(costs.get("slippage_bps", 0.0)) / 10000.0

    for index, date in enumerate(dates):
        day = data.loc[data["trade_date"] == date].set_index("symbol", drop=False)
        day_map = {symbol: row for symbol, row in day.iterrows()}

        if pending_targets is not None:
            estimate_equity = cash
            for symbol, shares in holdings.items():
                row = day_map.get(symbol)
                mark = float(row["open"]) if row is not None and pd.notna(row["open"]) else last_prices.get(symbol, 0.0)
                estimate_equity += shares * mark
            target_shares: dict[str, int] = {}
            for symbol, weight in pending_targets.items():
                row = day_map.get(symbol)
                if row is None or pd.isna(row["open"]) or float(row["open"]) <= 0:
                    continue
                target_amount = estimate_equity * weight
                minimum, step = order_constraints(row, "BUY", market, allow_fallback=True)
                raw_target = int(target_amount / float(row["open"]))
                target_shares[symbol] = round_order_quantity(raw_target, minimum, step)

            # Sells first, so their proceeds can fund the target buys.
            for symbol in list(holdings):
                current = holdings.get(symbol, 0)
                desired = target_shares.get(symbol, 0)
                quantity = max(0, current - desired)
                row = day_map.get(symbol)
                if row is None:
                    if quantity:
                        blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": "當日無行情行，交易狀態無法確認"})
                    continue
                minimum, step = order_constraints(row, "SELL", market, allow_fallback=True)
                quantity = round_order_quantity(
                    quantity, minimum, step,
                    allow_full_liquidation=True, full_position_shares=current,
                )
                if quantity <= 0:
                    continue
                eligible, reason = status_is_eligible(row, market, require_order_rules=False)
                if not eligible:
                    blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": reason})
                    continue
                limit_reason = _price_limit_block(row, row.get("previous_close"), "SELL", market)
                if limit_reason:
                    blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "SELL", "quantity": quantity, "reason": limit_reason})
                    continue
                reference = float(row["open"])
                fill_price = reference * (1.0 - slippage)
                notional = quantity * fill_price
                fees = _fees(notional, "SELL", costs)
                cash += notional - fees
                holdings[symbol] = current - quantity
                total_turnover += notional
                trades.append({"trade_date": str(date), "symbol": symbol, "side": "SELL", "quantity": quantity, "reference_price": reference, "estimated_fill_price": fill_price, "notional_cny": notional, "fees_cny": fees, "reason": "目標排名/權重調整；次交易日開盤模擬"})
                if holdings[symbol] <= 0:
                    holdings.pop(symbol, None)

            for symbol, desired in target_shares.items():
                current = holdings.get(symbol, 0)
                quantity = desired - current
                row = day_map.get(symbol)
                if row is None:
                    if quantity > 0:
                        blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": "當日無行情行，交易狀態無法確認"})
                    continue
                minimum, step = order_constraints(row, "BUY", market, allow_fallback=True)
                quantity = round_order_quantity(quantity, minimum, step)
                if quantity < minimum:
                    continue
                eligible, reason = status_is_eligible(row, market, require_order_rules=False)
                if not eligible:
                    blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": reason})
                    continue
                limit_reason = _price_limit_block(row, row.get("previous_close"), "BUY", market)
                if limit_reason:
                    blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "BUY", "quantity": quantity, "reason": limit_reason})
                    continue
                reference = float(row["open"])
                fill_price = reference * (1.0 + slippage)
                while quantity >= minimum:
                    notional = quantity * fill_price
                    fees = _fees(notional, "BUY", costs)
                    if notional + fees <= cash:
                        break
                    quantity -= step
                if quantity < minimum:
                    blocked_orders.append({"trade_date": str(date), "symbol": symbol, "side": "BUY", "quantity": desired - current, "reason": "可用現金不足，按買入單位縮減後仍無法成交"})
                    continue
                notional = quantity * fill_price
                fees = _fees(notional, "BUY", costs)
                cash -= notional + fees
                holdings[symbol] = current + quantity
                total_turnover += notional
                trades.append({"trade_date": str(date), "symbol": symbol, "side": "BUY", "quantity": quantity, "reference_price": reference, "estimated_fill_price": fill_price, "notional_cny": notional, "fees_cny": fees, "reason": "收盤排名入選；次交易日開盤模擬"})
            pending_targets = None

        for symbol, row in day_map.items():
            if pd.notna(row["close"]) and float(row["close"]) > 0:
                last_prices[symbol] = float(row["close"])
        equity = cash + sum(shares * last_prices.get(symbol, 0.0) for symbol, shares in holdings.items())
        equity_rows.append({"trade_date": str(date), "equity_cny": equity, "cash_cny": cash, "holdings_count": len(holdings)})

        if index >= lookback and (index - lookback) % rebalance_sessions == 0 and index + 1 < len(dates):
            ranked = rank_at_close(data, date, strategy, market, require_order_rules=False)
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
    close_returns = data.sort_values(["symbol", "trade_date"]).groupby("symbol")["close"].pct_change()
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
        benchmark_rows = benchmark_rows.sort_values("trade_date")
        benchmark_values = benchmark_rows.set_index(benchmark_rows["trade_date"].astype(str))["close"].pct_change()
        benchmark_method = f"configured_index_close_return:{benchmark_symbol}"
    else:
        benchmark_rows = data.loc[data["symbol"] == benchmark_symbol] if benchmark_symbol else pd.DataFrame()
    if benchmark_frame is None and not benchmark_rows.empty:
        benchmark_values = benchmark_rows.set_index(benchmark_rows["trade_date"].astype(str))["raw_close_return"]
        benchmark_method = f"configured_index_close_return:{benchmark_symbol}"
    elif benchmark_frame is None:
        allowed = data.apply(lambda row: status_is_eligible(row, market, require_order_rules=False)[0], axis=1)
        data["peer_return_eligible"] = allowed & data["raw_close_return"].notna()
        benchmark_values = data.loc[data["peer_return_eligible"]].assign(
            trade_date_key=lambda table: table["trade_date"].astype(str)
        ).groupby("trade_date_key")["raw_close_return"].mean()
        benchmark_method = "equal-weight daily returns of status-known eligible input symbols; not an index benchmark"
    peer_curve = pd.Series(
        [float(benchmark_values.get(day, 0.0)) for day in curve["trade_date"]],
        index=curve.index,
        dtype=float,
    )
    benchmark = pd.Series(initial_cash_cny * (1.0 + peer_curve.fillna(0.0)).cumprod().to_numpy())
    equity_series = curve["equity_cny"]

    compact_config = {name: config[name] for name in ("data", "strategy", "market", "costs", "backtest", "benchmark") if name in config}
    config_json = json.dumps(compact_config, ensure_ascii=False, sort_keys=True, default=str)
    data_digest = hashlib.sha256(
        pd.util.hash_pandas_object(data[BAR_COLUMNS], index=False).values.tobytes()
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
    retrieved_values = pd.concat([source_frame["retrieved_at_utc"] for source_frame in source_frames]).dropna()
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
    metrics = _metrics(equity_series, curve["return"], total_turnover / max(float(equity_series.mean()), 1.0), benchmark)
    limitations = [
        "基线以收盘 t 的信号在下一交易日开盘模拟；不代表可成交价格。",
        "日线 OHLC 无法重建开盘队列、盘口流动性、真实停牌日与部分涨跌停成交。",
        "使用原始价结算，没有现金分红、送转、配股、退市与历史成分股处理；结果仅为技术验证。",
        "配置中的费用费率统一应用于整段历史，不代表历史逐年费率或个人账户实际佣金。",
        "缺失的涨跌幅阈值、是否限价状态或交易单位使用配置回退假设；成交结果仅用于研究，计划生成要求完整点时证券规则。",
    ]
    if benchmark_data is not None:
        limitations.append("沪深300基准采用价格指数收盘价，不含成分股分红的总回报。")
    else:
        limitations.append("没有独立指数行情时，使用输入股票池等权收益作参照；该参照不是市场指数，也不修正幸存者偏差。")
    metrics.update({
        "experiment_id": f"chq-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        "strategy_id": str(strategy["name"]),
        "config_sha256": hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
        "data_version_sha256": data_digest,
        "benchmark_data_version_sha256": benchmark_data_digest,
        "code_version": __version__,
        **_git_metadata(),
        "data_sources": data_sources,
        "data_retrieved_at_utc": str(retrieved_values.max()) if not retrieved_values.empty else None,
        "universe_symbols": sorted(str(value) for value in data["symbol"].unique()),
        "universe_filter": "status-known, listed and not suspended; ST excluded when configured; momentum threshold from config",
        "features": [f"{lookback}-session adjusted-close momentum"],
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
        "market_rule_fallback_row_count": rule_fallback_rows,
        "market_rule_fallback_assumptions": {
            "price_limit_pct": market.get("default_price_limit_pct"),
            "assume_price_limit_applies_when_unknown": market.get("assume_price_limit_applies_when_unknown", True),
            "buy_min_and_step_shares": market.get("buy_lot_shares"),
            "sell_min_and_step_shares": market.get("sell_lot_shares"),
        },
        "benchmark_method": benchmark_method,
        "result_type": "research_backtest_not_simulated_or_real_account",
        "quality_warnings": quality["warnings"],
        "limitations": limitations,
    })
    return {
        "metrics": metrics,
        "equity_curve": curve,
        "trades": pd.DataFrame(trades, columns=["trade_date", "symbol", "side", "quantity", "reference_price", "estimated_fill_price", "notional_cny", "fees_cny", "reason"]),
        "blocked_orders": pd.DataFrame(blocked_orders, columns=["trade_date", "symbol", "side", "quantity", "reason"]),
    }
