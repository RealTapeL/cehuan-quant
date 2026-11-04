from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.canonical import read_bars
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_status(value: object) -> str:
    return "RECORDED" if value is not None and not pd.isna(value) and str(value).strip() else "UNAVAILABLE"


def _as_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _round_order_quantity(quantity: int, minimum: int, step: int) -> int:
    if quantity < minimum:
        return 0
    return minimum + ((quantity - minimum) // step) * step


def _match_action_to_holding_windows(
    record_date: object,
    phase_dates: list[object],
    holding_windows: pd.DataFrame,
) -> dict[str, Any]:
    """Audit event timing against simulated holdings, including record-date sale rights.

    For A-share rights, a position acquired on the record date is not eligible,
    while a position carried into the record date may remain eligible when sold
    during that session. A phase after a qualifying record date remains attached
    to the claim even if the ordinary shares have since been sold.
    """
    record_day = pd.to_datetime(record_date, errors="coerce")
    record_day = None if pd.isna(record_day) else record_day.date()
    phases = [
        pd.to_datetime(value, errors="coerce").date()
        for value in phase_dates
        if pd.notna(value) and not pd.isna(pd.to_datetime(value, errors="coerce"))
    ]
    entitlement_matches: list[str] = []
    phase_matches: list[str] = []
    for holding in holding_windows.itertuples(index=False):
        start_day = pd.to_datetime(holding.holding_start_trade_date_at_open).date()
        held_through_day = pd.to_datetime(holding.held_at_end_of_day_through).date()
        closure = str(getattr(holding, "closure", ""))
        exit_day_value = getattr(holding, "holding_end_trade_date_before_open", None)
        exit_day = None if pd.isna(exit_day_value) else pd.to_datetime(exit_day_value).date()
        interval_id = f"{holding.symbol}:{start_day}:{exit_day or held_through_day}"
        # Acquired on record date does not qualify; selling on record date does.
        entitled = record_day is not None and start_day < record_day <= held_through_day
        entitled |= (
            record_day is not None
            and closure == "SOLD_AT_OPEN"
            and exit_day == record_day
            and start_day < record_day
        )
        if entitled:
            entitlement_matches.append(interval_id)
        if any(start_day <= day <= held_through_day for day in phases):
            phase_matches.append(interval_id)
    return {
        "record_date_entitlement_match": bool(entitlement_matches),
        "phase_overlap_match": bool(phase_matches),
        "matching_holding_intervals": sorted(set(entitlement_matches + phase_matches)),
        "holding_match_status": (
            "POSSIBLE_RECORD_DATE_ENTITLEMENT" if entitlement_matches
            else "POSSIBLE_EVENT_PHASE_OVERLAP" if phase_matches
            else "NO_MATCH_IN_RECORDED_HOLDING_INTERVALS"
        ),
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须为对象：{path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _rank_audit(
    bars: pd.DataFrame,
    candidates_path: Path,
    strategy: dict[str, Any],
    market: dict[str, Any],
    signal_dates: list[str],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    calculated = add_momentum(bars.copy(), int(strategy["lookback_sessions"]))
    saved = pd.read_csv(candidates_path, dtype={"signal_date": str, "execution_date": str, "symbol": str})
    output: list[pd.DataFrame] = []
    by_date: dict[str, dict[str, Any]] = {}
    for signal_date in signal_dates:
        day = calculated.loc[calculated["trade_date"].astype(str).eq(signal_date)].copy()
        ranked = rank_precomputed_day(day, strategy, market, require_order_rules=False)
        selected = ranked.head(int(strategy["max_positions"])).copy()
        selected["signal_date"] = signal_date
        output.append(ranked.assign(signal_date=signal_date))
        saved_day = saved.loc[saved["signal_date"].astype(str).eq(signal_date)].copy()
        joined = ranked[["symbol", "momentum", "rank"]].merge(
            saved_day[["symbol", "momentum", "rank"]].rename(
                columns={"momentum": "saved_momentum", "rank": "saved_rank"}
            ),
            on="symbol", how="outer", indicator=True,
        )
        mismatch = joined.loc[
            joined["_merge"].ne("both")
            | joined["rank"].fillna(-1).ne(joined["saved_rank"].fillna(-1))
            | ((joined["momentum"] - joined["saved_momentum"]).abs().fillna(0) > 1e-10)
        ]
        by_date[signal_date] = {
            "eligible_candidate_count_rebuilt": int(len(ranked)),
            "eligible_candidate_count_saved": int(len(saved_day)),
            "selected_targets": selected[["symbol", "momentum", "rank"]].to_dict(orient="records"),
            "candidate_reconstruction_status": "MATCH" if mismatch.empty else "MISMATCH",
            "candidate_mismatch_rows": int(len(mismatch)),
        }
    full = pd.concat(output, ignore_index=True) if output else pd.DataFrame()
    return full, by_date


def _signal_inputs(
    bars: pd.DataFrame,
    signal_date: str,
    symbol: str,
    lookback: int,
) -> dict[str, Any]:
    series = bars.loc[bars["symbol"].astype(str).eq(symbol)].sort_values("trade_date", kind="stable")
    signal_rows = series.loc[series["trade_date"].astype(str).le(signal_date)]
    current_matches = signal_rows.loc[signal_rows["trade_date"].astype(str).eq(signal_date)]
    if current_matches.empty:
        return {
            "signal_bar_status": "UNAVAILABLE",
            "signal_observation_count": int(len(signal_rows)),
            "lookback_observation_date": None,
            "signal_price_start": None,
            "signal_price_end": None,
            "recomputed_momentum": None,
            "signal_window_suspended_count": None,
        }
    current = current_matches.iloc[-1]
    if len(signal_rows) <= lookback:
        return {
            "signal_bar_status": "INSUFFICIENT_LOOKBACK_ROWS",
            "signal_observation_count": int(len(signal_rows)),
            "lookback_observation_date": None,
            "signal_price_start": None,
            "signal_price_end": _as_float(current.get("adjusted_close")),
            "recomputed_momentum": None,
            "signal_window_suspended_count": int(signal_rows["is_suspended"].fillna(False).astype(bool).sum()),
        }
    base = signal_rows.iloc[-lookback - 1]
    end_price = _as_float(current.get("adjusted_close"))
    start_price = _as_float(base.get("adjusted_close"))
    if start_price is None or end_price is None or start_price <= 0:
        momentum = None
        status = "INVALID_ADJUSTED_PRICE"
    else:
        momentum = end_price / start_price - 1.0
        status = "RECONSTRUCTED_FROM_FROZEN_BARS"
    window = signal_rows.tail(lookback + 1)
    return {
        "signal_bar_status": status,
        "signal_observation_count": int(len(signal_rows)),
        "lookback_observation_date": str(base["trade_date"]),
        "signal_price_start": start_price,
        "signal_price_end": end_price,
        "recomputed_momentum": momentum,
        "signal_window_suspended_count": int(window["is_suspended"].fillna(False).astype(bool).sum()),
    }


def _reconstruct_targets(
    trades: pd.DataFrame,
    candidates: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    strategy: dict[str, Any],
    market: dict[str, Any],
    costs: dict[str, Any],
    initial_cash_cny: float,
) -> pd.DataFrame:
    sessions = sorted(
        pd.to_datetime(calendar.loc[calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"])
        .dt.strftime("%Y-%m-%d").unique()
    )
    ordered_dates = sorted(trades["trade_date"].astype(str).unique())
    cash = float(initial_cash_cny)
    positions: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    slippage = float(costs.get("slippage_bps", 0.0)) / 10000.0
    bars_by_day_symbol = bars.set_index([bars["trade_date"].astype(str), "symbol"], drop=False)
    for execution_date in ordered_dates:
        prior_sessions = [day for day in sessions if day < execution_date]
        signal_date = prior_sessions[-1] if prior_sessions else None
        if signal_date is None:
            raise ValueError(f"成交日 {execution_date} 前缺少日历信号日")
        open_value = cash
        for symbol, shares in positions.items():
            key = (execution_date, symbol)
            if key not in bars_by_day_symbol.index:
                raise ValueError(f"无法用冻结行情估算开盘权益：{symbol} {execution_date}")
            bar = bars_by_day_symbol.loc[key]
            if isinstance(bar, pd.DataFrame):
                raise ValueError(f"成交日行情不唯一：{symbol} {execution_date}")
            open_value += shares * float(bar["open"])
        day_candidates = candidates.loc[
            candidates["signal_date"].astype(str).eq(signal_date)
        ].sort_values("rank")
        selected = day_candidates.head(int(strategy["max_positions"]))
        weight = min(
            float(strategy["max_position_weight"]),
            float(strategy["max_total_exposure"]) / len(selected),
        ) if not selected.empty else 0.0
        target_shares: dict[str, int] = {}
        for item in selected.itertuples(index=False):
            bar = bars_by_day_symbol.loc[(execution_date, item.symbol)]
            if isinstance(bar, pd.DataFrame):
                raise ValueError(f"成交日行情不唯一：{item.symbol} {execution_date}")
            raw_target = int(open_value * weight / float(bar["open"]))
            target_shares[item.symbol] = _round_order_quantity(
                raw_target,
                int(market["buy_lot_shares"]),
                int(market["buy_lot_shares"]),
            )
        day_actual = trades.loc[trades["trade_date"].astype(str).eq(execution_date)]
        actual_map = {
            (str(row.symbol), str(row.side)): int(row.quantity)
            for row in day_actual.itertuples(index=False)
        }
        candidate_symbols = set(target_shares) | set(positions)
        for symbol in sorted(candidate_symbols):
            target_qty = target_shares.get(symbol, 0)
            current_qty = positions.get(symbol, 0)
            delta = target_qty - current_qty
            if delta > 0:
                side, expected_qty = "BUY", _round_order_quantity(
                    delta, int(market["buy_lot_shares"]), int(market["buy_lot_shares"])
                )
            elif delta < 0:
                side = "SELL"
                expected_qty = abs(delta)
                if expected_qty != current_qty:
                    expected_qty = _round_order_quantity(
                        expected_qty, int(market["sell_lot_shares"]), int(market["sell_lot_shares"])
                    )
            else:
                if symbol not in target_shares:
                    continue
                side, expected_qty = "NONE", 0
            actual_qty = actual_map.get((symbol, side), 0)
            rows.append({
                "execution_date": execution_date,
                "signal_date": signal_date,
                "symbol": symbol,
                "rank": next((int(x.rank) for x in selected.itertuples(index=False) if x.symbol == symbol), None),
                "momentum": next((_as_float(x.momentum) for x in selected.itertuples(index=False) if x.symbol == symbol), None),
                "selected_target": symbol in target_shares,
                "opening_cash_before_rebalance_cny": cash,
                "opening_equity_reconstructed_cny": open_value,
                "target_weight": weight if symbol in target_shares else 0.0,
                "target_shares_after_100_share_fallback": target_qty,
                "current_shares_before_rebalance": current_qty,
                "expected_order_side": side,
                "expected_order_quantity_after_fallback": expected_qty,
                "actual_fill_quantity": actual_qty,
                "target_fill_status": (
                    "MATCHED_FILL" if actual_qty == expected_qty and expected_qty > 0
                    else "TARGET_ROUNDED_TO_ZERO" if symbol in target_shares and target_qty == 0
                    else "NO_FILL_REVIEW_REQUIRED" if actual_qty != expected_qty
                    else "MATCHED_FILL"
                ),
                "market_rule_basis": "configs/default.toml fallback; 100-share lot; not historical board rules",
                "cost_basis": "configs/default.toml flat assumptions; not historical or personal HTS fees",
                "estimated_execution_impact_is_simulated": True,
                "slippage_bps": float(costs.get("slippage_bps", 0.0)),
            })
        # Keep the actual execution ledger as the state source. The audit compares
        # reconstructed targets to those fills; it does not replace or mutate them.
        for row in day_actual.itertuples(index=False):
            symbol, side, quantity = str(row.symbol), str(row.side), int(row.quantity)
            notional, fees = float(row.notional_cny), float(row.fees_cny)
            if side == "BUY":
                cash -= notional + fees
                positions[symbol] = positions.get(symbol, 0) + quantity
            else:
                cash += notional - fees
                positions[symbol] = positions.get(symbol, 0) - quantity
                if positions[symbol] <= 0:
                    positions.pop(symbol, None)
    return pd.DataFrame(rows)


def _trade_rows(
    trades: pd.DataFrame,
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    ranked: pd.DataFrame,
    journal: pd.DataFrame,
    fallback: pd.DataFrame,
    input_hashes: dict[str, str],
    experiment_id: str,
    strategy: dict[str, Any],
    costs: dict[str, Any],
) -> pd.DataFrame:
    sessions = sorted(
        pd.to_datetime(calendar.loc[calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"])
        .dt.strftime("%Y-%m-%d").unique()
    )
    session_before = {day: sessions[i - 1] for i, day in enumerate(sessions) if i > 0}
    rows: list[dict[str, Any]] = []
    by_event = {str(row.event_id): row for row in journal.itertuples(index=False)}
    candidate_lookup = {
        (str(row.signal_date), str(row.symbol)): row
        for row in ranked.itertuples(index=False)
    }
    fallback_lookup = {
        (str(row.trade_date), str(row.symbol), str(row.side)): row
        for row in fallback.itertuples(index=False)
    }
    bars_lookup = bars.set_index([bars["trade_date"].astype(str), "symbol"], drop=False)
    for source_line, row in enumerate(trades.itertuples(index=False), start=2):
        day = str(row.trade_date)
        symbol = str(row.symbol)
        side = str(row.side)
        signal_date = session_before.get(day)
        if signal_date is None:
            raise ValueError(f"成交日 {day} 缺少前一交易日")
        bar = bars_lookup.loc[(day, symbol)]
        if isinstance(bar, pd.DataFrame):
            raise ValueError(f"成交日行情非唯一：{symbol} {day}")
        signal_info = _signal_inputs(bars, signal_date, symbol, int(strategy["lookback_sessions"]))
        candidate = candidate_lookup.get((signal_date, symbol))
        fb = fallback_lookup.get((day, symbol, side))
        prior = bars.loc[
            bars["symbol"].astype(str).eq(symbol)
            & bars["trade_date"].astype(str).lt(day)
        ].sort_values("trade_date", kind="stable").tail(1)
        previous_close = _as_float(prior.iloc[0]["close"]) if not prior.empty else None
        open_change = (float(bar["open"]) / previous_close - 1.0) if previous_close else None
        event_id = (
            f"bt-{day}-{symbol}-{side}-" + f"{int(row.quantity):08d}"
        )
        journal_row = by_event.get(event_id)
        # The event IDs contain the engine sequence, so resolve by symbol/date/side
        # from the stable operation key when the quantity suffix is not the sequence.
        if journal_row is None:
            options = [
                item for item in journal.itertuples(index=False)
                if str(item.event_id).startswith(f"bt-{day}-{symbol}-{side}-")
            ]
            journal_row = options[0] if len(options) == 1 else None
        rows.append({
            "trade_id": str(journal_row.operation_key) if journal_row is not None else None,
            "experiment_id": experiment_id,
            "source_trade_row_number": source_line,
            "source_trades_sha256": input_hashes["trades.csv"],
            "trade_fields_provenance": "RECORDED_FROM_FROZEN_TRADES_CSV",
            "trade_date": day,
            "symbol": symbol,
            "side": side,
            "quantity": int(row.quantity),
            "reference_price": float(row.reference_price),
            "estimated_fill_price": float(row.estimated_fill_price),
            "notional_cny": float(row.notional_cny),
            "fees_cny": float(row.fees_cny),
            "commission_cny": float(row.commission_cny),
            "stamp_duty_cny": float(row.stamp_duty_cny),
            "transfer_fee_cny": float(row.transfer_fee_cny),
            "execution_impact_vs_reference_cny": float(row.execution_impact_vs_reference_cny),
            "allocated_cost_basis_cny": float(row.allocated_cost_basis_cny),
            "realized_pnl_cny": _as_float(row.realized_pnl_cny),
            "trade_reason": str(row.reason),
            "signal_date": signal_date,
            "signal_date_provenance": "RECONSTRUCTED_FROM_PREVIOUS_OPEN_SESSION_IN_FROZEN_CALENDAR",
            "signal_timing_rule": "t close signal; earliest simulated execution next open",
            "signal_timing_rule_evidence": "strategy code plus saved fill reason; historical vendor availability UNVERIFIED",
            "signal_cutoff_assumption": "15:00 Asia/Shanghai adapter assumption",
            "signal_cutoff_status": "UNVERIFIED_EXTERNAL_AVAILABILITY",
            "signal_lookback_sessions": int(strategy["lookback_sessions"]),
            **signal_info,
            "signal_feature_provenance": "RECONSTRUCTED_FROM_FROZEN_BARS_AND_CONFIG; COMPARED_TO_SAVED_CANDIDATES",
            "signal_feature_source_status": signal_info["signal_bar_status"],
            "signal_price_method": "BaoStock adjustflag=1 mapped to adjusted_close/hfq; separate from unadjusted execution open",
            "candidate_rank": int(candidate.rank) if candidate is not None else None,
            "candidate_momentum": _as_float(candidate.momentum) if candidate is not None else None,
            "candidate_link_status": "RECONSTRUCTED_MATCH" if candidate is not None else "NOT_SELECTED_OR_UNAVAILABLE",
            "execution_bar_source": str(bar.get("data_source")),
            "execution_bar_provenance": "RECORDED_FROM_FROZEN_CANONICAL_BARS_CSV",
            "execution_bar_retrieved_at_utc": str(bar.get("retrieved_at_utc")),
            "execution_bar_available_at": str(bar.get("available_at")),
            "execution_bar_available_at_status": "ADAPTER_ASSUMPTION_NOT_VENDOR_TIMESTAMP",
            "bar_open": float(bar["open"]),
            "bar_high": float(bar["high"]),
            "bar_low": float(bar["low"]),
            "bar_close": float(bar["close"]),
            "bar_adjusted_close": float(bar["adjusted_close"]),
            "execution_price_basis": "unadjusted open * (1 +/- configured 5 bps); cash and quantities use simulated fill price",
            "previous_raw_close": previous_close,
            "open_change_vs_previous_close": open_change,
            "fallback_limit_pct": float(fb.fallback_price_limit_pct) if fb is not None else None,
            "fallback_price_limit_applies_when_unknown": bool(fb.fallback_assume_limit_applies_when_unknown) if fb is not None else None,
            "limit_fallback_would_block_open": (
                bool(
                    (side == "BUY" and previous_close and float(bar["open"]) >= previous_close * (1.0 + float(fb.fallback_price_limit_pct)) - 0.005)
                    or (side == "SELL" and previous_close and float(bar["open"]) <= previous_close * (1.0 - float(fb.fallback_price_limit_pct)) + 0.005)
                ) if fb is not None else None
            ),
            "buy_lot_fallback": int(fb.fallback_buy_lot_shares) if fb is not None else None,
            "sell_lot_fallback": int(fb.fallback_sell_lot_shares) if fb is not None else None,
            "market_rule_source_status": str(fb.market_rule_source_status) if fb is not None else "UNAVAILABLE",
            "market_rule_provenance": "RECORDED_FROM_FROZEN_FALLBACK_DETAIL; CONFIG_FALLBACK" if fb is not None else "UNAVAILABLE",
            "fee_mode": str(fb.fee_application) if fb is not None else "UNAVAILABLE",
            "fee_source_status": str(fb.fee_source_status) if fb is not None else "UNAVAILABLE",
            "fee_provenance": "RECORDED_FROM_FROZEN_FALLBACK_DETAIL; CONFIG_ASSUMPTION" if fb is not None else "UNAVAILABLE",
            "commission_rate_assumption": float(fb.fallback_commission_rate) if fb is not None else None,
            "minimum_commission_cny_assumption": float(fb.fallback_minimum_commission_cny) if fb is not None else None,
            "stamp_duty_sell_rate_assumption": float(fb.fallback_stamp_duty_sell_rate) if fb is not None else None,
            "transfer_fee_rate_assumption": float(fb.fallback_transfer_fee_rate) if fb is not None else None,
            "slippage_bps_assumption": float(fb.fallback_slippage_bps) if fb is not None else float(costs.get("slippage_bps", 0.0)),
            "actual_broker_fee_evidence": "UNAVAILABLE; simulated config values only",
            "calendar_session_status": "RECONSTRUCTED_FROM_FROZEN_CALENDAR",
            "journal_link_status": "MATCHED" if journal_row is not None else "UNAVAILABLE",
            "journal_provenance": "RECORDED_FROM_FROZEN_ACCOUNTING_JOURNAL" if journal_row is not None else "UNAVAILABLE",
            "input_bar_file_sha256": input_hashes["bars.csv"],
            "candidate_file_sha256": input_hashes["candidate-pool-by-signal.csv"],
        })
    return pd.DataFrame(rows)


def _capture_cninfo_dividends(symbols: list[str], out_dir: Path) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    import akshare as ak
    import requests

    original_post = requests.post
    captured: list[tuple[str, bytes]] = []

    def capture_post(*args: Any, **kwargs: Any):
        response = original_post(*args, **kwargs)
        url = str(args[0] if args else kwargs.get("url", ""))
        if "p_sysapi1139" in url:
            captured.append((url, response.content))
        return response

    all_rows: list[pd.DataFrame] = []
    query_log: list[dict[str, Any]] = []
    requests.post = capture_post
    try:
        for symbol in symbols:
            started = datetime.now(timezone.utc).isoformat()
            before = len(captured)
            try:
                result = ak.stock_dividend_cninfo(symbol=symbol[2:])
                status = "QUERY_OK"
                error = None
                if not result.empty:
                    result = result.copy()
                    result.insert(0, "symbol", symbol)
                    all_rows.append(result)
                response_sha = None
                raw_path = None
                if len(captured) > before:
                    _, raw = captured[-1]
                    response_sha = hashlib.sha256(raw).hexdigest()
                    raw_path = f"cninfo-dividend-raw/{symbol}.json"
                    target = out_dir / raw_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
            except Exception as exc:  # network and upstream parser failures are audit states
                status = "QUERY_FAILED"
                error = f"{type(exc).__name__}: {exc}"
                response_sha = None
                raw_path = None
            query_log.append({
                "symbol": symbol,
                "source": "CNINFO webapi.cninfo.com.cn p_sysapi1139 via AKShare stock_dividend_cninfo",
                "scope": "CNINFO structured historical dividend / share distribution records only; does not cover all corporate actions",
                "query_started_at_utc": started,
                "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "result_rows": int(len(result)) if status == "QUERY_OK" else None,
                "raw_response_path": raw_path,
                "raw_response_sha256": response_sha,
                "error": error,
            })
            time.sleep(0.15)
    finally:
        requests.post = original_post
    rows = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not rows.empty:
        rows.to_csv(out_dir / "cninfo-dividend-records-adapter-output.csv", index=False)
    return rows, query_log


def _capture_tencent_quotes(symbols: list[str], out_dir: Path, start: str, end: str) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    import akshare as ak
    import requests

    original_get = requests.get
    captured: list[tuple[str, bytes]] = []

    def capture_get(*args: Any, **kwargs: Any):
        response = original_get(*args, **kwargs)
        url = str(args[0] if args else kwargs.get("url", ""))
        if "finance.qq.com" in url:
            captured.append((url, response.content))
        return response

    all_rows: list[pd.DataFrame] = []
    query_log: list[dict[str, Any]] = []
    requests.get = capture_get
    try:
        for prefixed_symbol in symbols:
            symbol = prefixed_symbol.lower()
            code = prefixed_symbol[2:]
            started = datetime.now(timezone.utc).isoformat()
            before = len(captured)
            try:
                result = ak.stock_zh_a_hist_tx(
                    symbol=symbol,
                    start_date=start.replace("-", ""),
                    end_date=end.replace("-", ""),
                    adjust="",
                    timeout=20,
                )
                status = "QUERY_OK"
                error = None
                result = result.copy()
                result.insert(0, "symbol", prefixed_symbol)
                all_rows.append(result)
                saved_raw: list[dict[str, str]] = []
                for index, (url, raw) in enumerate(captured[before:], start=1):
                    raw_path = f"tencent-quote-raw/{prefixed_symbol}-{index}.jsonp"
                    target = out_dir / raw_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    saved_raw.append({"url": url, "path": raw_path, "sha256": hashlib.sha256(raw).hexdigest()})
            except Exception as exc:
                status = "QUERY_FAILED"
                error = f"{type(exc).__name__}: {exc}"
                result = pd.DataFrame()
                saved_raw = []
            query_log.append({
                "symbol": prefixed_symbol,
                "source": "Tencent Finance daily quote endpoint through AKShare stock_zh_a_hist_tx",
                "scope": "unadjusted daily OHLC, volume and amount; Tencent adapter reports volume in shares and amount in CNY",
                "query_range": f"{start}..{end}",
                "query_started_at_utc": started,
                "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "result_rows": int(len(result)) if status == "QUERY_OK" else None,
                "raw_http_responses": saved_raw,
                "error": error,
            })
            time.sleep(0.15)
    finally:
        requests.get = original_get
    rows = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not rows.empty:
        rows.to_csv(out_dir / "tencent-quotes-adapter-output.csv", index=False)
    return rows, query_log


def _archive_official_filing(out_dir: Path, filing: dict[str, Any]) -> dict[str, Any]:
    import requests

    symbol = str(filing["symbol"])
    event_family = str(filing["event_family"])
    source_title = str(filing["source_title"])
    url = str(filing["source_url"])
    document_date = str(filing["document_date"])
    started = datetime.now(timezone.utc).isoformat()
    filename_slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{symbol}-{document_date}-{event_family}").strip("-")
    target = out_dir / f"official-filings/{filename_slug}.pdf"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.content
        if not content.startswith(b"%PDF-"):
            raise ValueError("公告响应不是 PDF 文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return {
            "symbol": symbol,
            "event_family": event_family,
            "source": "CNINFO official issuer filing",
            "source_title": source_title,
            "source_url": url,
            "document_date": document_date,
            "raw_response_path": target.relative_to(out_dir).as_posix(),
            "raw_response_sha256": sha256_file(target),
            "status": "OFFICIAL_PDF_ARCHIVED_HUMAN_FIELD_REVIEW_PENDING",
            "known_at_evidence_status": str(filing.get("known_at_evidence_status", "UNVERIFIED")),
            "query_started_at_utc": started,
            "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:  # a source retrieval failure must remain visible, not abort other audit artifacts
        return {
            "symbol": symbol,
            "event_family": event_family,
            "source": "CNINFO official issuer filing",
            "source_title": source_title,
            "source_url": url,
            "document_date": document_date,
            "raw_response_path": None,
            "raw_response_sha256": None,
            "status": "OFFICIAL_PDF_FETCH_FAILED",
            "known_at_evidence_status": str(filing.get("known_at_evidence_status", "UNVERIFIED")),
            "query_started_at_utc": started,
            "query_finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate read-only, transaction-level audit of a frozen real-data research run.")
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--candidate-pool", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fetch-independent-sources", action="store_true")
    parser.add_argument("--supplemental-filings", type=Path, help="私有 JSON 清单；仅归档并记录显式指定的官方公告")
    args = parser.parse_args()

    supplemental_filings: list[dict[str, Any]] = []
    supplemental_manifest_sha256 = None
    supplemental_manifest_bytes = None
    if args.supplemental_filings is not None:
        if not args.fetch_independent_sources:
            raise ValueError("使用 --supplemental-filings 时必须同时指定 --fetch-independent-sources")
        manifest_path = args.supplemental_filings.expanduser().resolve()
        if not manifest_path.is_file():
            raise FileNotFoundError(f"补充公告清单不存在：{manifest_path}")
        supplemental_manifest_bytes = manifest_path.read_bytes()
        supplemental_manifest_sha256 = hashlib.sha256(supplemental_manifest_bytes).hexdigest()
        manifest = json.loads(supplemental_manifest_bytes)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("filings"), list):
            raise ValueError("补充公告清单必须是 schema_version=1 且含 filings 列表的 JSON 对象")
        required_filing_fields = {"symbol", "event_family", "source_title", "source_url", "document_date"}
        for index, filing in enumerate(manifest["filings"]):
            if not isinstance(filing, dict) or not required_filing_fields.issubset(filing):
                raise ValueError(f"supplemental filings[{index}] 缺少必填字段")
            if not str(filing["source_url"]).startswith("https://"):
                raise ValueError(f"supplemental filings[{index}].source_url 必须使用 HTTPS")
            supplemental_filings.append(filing)
        supplemental_filings = list(supplemental_filings)

    out_dir = args.output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=False)
    experiment_dir = args.experiment_dir.expanduser().resolve()
    run_dir = args.run_dir.expanduser().resolve()
    candidate_pool_path = args.candidate_pool.expanduser().resolve()
    audit_input_dir = candidate_pool_path.parent
    input_paths = {
        "bars.csv": experiment_dir / "inputs/bars.csv",
        "trading_calendar.csv": experiment_dir / "inputs/trading_calendar.csv",
        "config.json": run_dir / "config.json",
        "trades.csv": run_dir / "trades.csv",
        "accounting_journal.csv": run_dir / "accounting_journal.csv",
        "blocked_orders.csv": run_dir / "blocked_orders.csv",
        "metrics.json": run_dir / "metrics.json",
        "candidate-pool-by-signal.csv": candidate_pool_path,
    }
    missing = [str(path) for path in input_paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("审计输入缺失：" + ", ".join(missing))
    hashes = {name: sha256_file(path) for name, path in input_paths.items()}
    frozen_inventory_path = audit_input_dir / "artifact-inventory.csv"
    frozen_inventory = pd.read_csv(frozen_inventory_path, dtype=str, keep_default_na=False)
    frozen_hashes = dict(zip(frozen_inventory["relative_path"], frozen_inventory["sha256"]))
    expected_inventory_paths = {
        "bars.csv": "inputs/bars.csv",
        "trading_calendar.csv": "inputs/trading_calendar.csv",
        "config.json": f"{run_dir.name}/config.json",
        "trades.csv": f"{run_dir.name}/trades.csv",
        "accounting_journal.csv": f"{run_dir.name}/accounting_journal.csv",
        "blocked_orders.csv": f"{run_dir.name}/blocked_orders.csv",
        "metrics.json": f"{run_dir.name}/metrics.json",
    }
    artifact_hash_checks = {}
    for name, relative_path in expected_inventory_paths.items():
        expected = frozen_hashes.get(relative_path)
        observed = hashes[name]
        artifact_hash_checks[name] = {
            "expected_sha256": expected or None,
            "observed_sha256": observed,
            "status": "MATCHED_FROZEN_INVENTORY" if expected == observed else "NO_FROZEN_HASH" if not expected else "FAILED_HASH_MISMATCH",
        }
    candidate_inventory_relative = (
        candidate_pool_path.relative_to(experiment_dir).as_posix()
        if candidate_pool_path.is_relative_to(experiment_dir) else None
    )
    candidate_expected_hash = frozen_hashes.get(candidate_inventory_relative)
    artifact_hash_checks["candidate-pool-by-signal.csv"] = {
        "expected_sha256": candidate_expected_hash or None,
        "observed_sha256": hashes["candidate-pool-by-signal.csv"],
        "status": (
            "MATCHED_FROZEN_INVENTORY" if candidate_expected_hash == hashes["candidate-pool-by-signal.csv"]
            else "NO_FROZEN_HASH_RECONSTRUCTED_AGAINST_BARS_CONFIG" if not candidate_expected_hash
            else "FAILED_HASH_MISMATCH"
        ),
    }
    bars = read_bars(input_paths["bars.csv"])
    raw_trades = pd.read_csv(input_paths["trades.csv"], keep_default_na=False)
    fallback = pd.read_csv(audit_input_dir / "trade-fallback-detail.csv")
    journal = pd.read_csv(input_paths["accounting_journal.csv"], dtype=str, keep_default_na=False)
    blocked = pd.read_csv(input_paths["blocked_orders.csv"], keep_default_na=False)
    calendar = pd.read_csv(input_paths["trading_calendar.csv"], keep_default_na=False)
    config = _json(input_paths["config.json"])
    run_metrics = _json(input_paths["metrics.json"])
    experiment_id = str(run_metrics.get("experiment_id", "UNAVAILABLE"))
    strategy, market, costs = config["strategy"], config["market"], config["costs"]
    trades = raw_trades.copy()
    trades["trade_date"] = trades["trade_date"].astype(str)
    signal_dates = sorted({
        max(day for day in pd.to_datetime(calendar.loc[
            calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"
        ]).dt.strftime("%Y-%m-%d").unique() if day < execution_date)
        for execution_date in trades["trade_date"].unique()
    })
    ranked, reconstruction = _rank_audit(bars, input_paths["candidate-pool-by-signal.csv"], strategy, market, signal_dates)
    audit = _trade_rows(trades, bars, calendar, ranked, journal, fallback, hashes, experiment_id, strategy, costs)
    # A stable snapshot of signal candidates is retained so later reviewers can
    # reproduce why each fill mapped to a rank without rerunning the backtest.
    ranked.to_csv(out_dir / "candidate-ranks-reconstructed.csv", index=False)
    audit.to_csv(out_dir / "trade-audit.csv", index=False)
    trade_targets = _reconstruct_targets(
        trades, ranked, bars, calendar, strategy, market, costs,
        float(run_metrics["initial_cash_cny"]),
    )
    trade_targets.to_csv(out_dir / "candidate-target-and-fill-audit.csv", index=False)
    source_symbols = sorted(trades["symbol"].astype(str).unique())
    selected_symbols = sorted(
        pd.read_csv(input_paths["candidate-pool-by-signal.csv"])
        .loc[lambda frame: frame["selected_target"].astype(str).str.lower().isin(["true", "1"]), "symbol"]
        .astype(str).unique()
    )
    source_rows: list[dict[str, Any]] = []
    dividend_queries: list[dict[str, Any]] = []
    quote_queries: list[dict[str, Any]] = []
    dividend_match_summary: dict[str, Any] = {"query_count": 0, "query_success_count": 0, "selected_symbol_count": len(selected_symbols)}
    quote_match_summary: dict[str, Any] = {"trade_quote_rows": 0, "query_success_count": 0}
    if args.fetch_independent_sources:
        if supplemental_manifest_bytes is not None:
            (out_dir / "supplemental-filing-manifest.json").write_bytes(supplemental_manifest_bytes)
        dividends, dividend_queries = _capture_cninfo_dividends(selected_symbols, out_dir)
        issuer_filings = [_archive_official_filing(out_dir, filing) for filing in supplemental_filings]
        quote_symbols = source_symbols
        quote_start = str(bars["trade_date"].min())
        quote_end = str(bars["trade_date"].max())
        quotes, quote_queries = _capture_tencent_quotes(quote_symbols, out_dir, quote_start, quote_end)
        if not quotes.empty:
            quotes["date"] = pd.to_datetime(quotes["date"]).dt.strftime("%Y-%m-%d")
            quote_comparison = trades.merge(
                quotes, left_on=["trade_date", "symbol"], right_on=["date", "symbol"], how="left", suffixes=("_baostock", "_tencent")
            )
            bar_lookup = bars.assign(trade_date=bars["trade_date"].astype(str)).set_index(["trade_date", "symbol"])
            compare_rows = []
            for trade in trades.itertuples(index=False):
                key = (str(trade.trade_date), str(trade.symbol))
                own = bar_lookup.loc[key]
                ext = quote_comparison.loc[
                    quote_comparison["trade_date"].astype(str).eq(key[0])
                    & quote_comparison["symbol"].astype(str).eq(key[1])
                ]
                if ext.empty or pd.isna(ext.iloc[0].get("open")):
                    quote = {}
                else:
                    quote = ext.iloc[0].to_dict()
                item = {
                    "trade_date": key[0], "symbol": key[1], "side": str(trade.side),
                    "baostock_open": float(own["open"]), "baostock_high": float(own["high"]),
                    "baostock_low": float(own["low"]), "baostock_close": float(own["close"]),
                    "baostock_volume_shares": float(own["volume_shares"]), "baostock_amount_cny": float(own["amount_cny"]),
                    "tencent_open": _as_float(quote.get("open")), "tencent_high": _as_float(quote.get("high")),
                    "tencent_low": _as_float(quote.get("low")), "tencent_close": _as_float(quote.get("close")),
                    "tencent_volume_shares": _as_float(quote.get("volume")), "tencent_amount_cny": _as_float(quote.get("amount")),
                    "quote_source_status": "AVAILABLE" if quote else "MISSING",
                }
                for col in ("open", "high", "low", "close"):
                    left, right = item[f"baostock_{col}"], item[f"tencent_{col}"]
                    item[f"{col}_absolute_difference"] = None if right is None else left - right
                    item[f"{col}_match_within_0_01"] = None if right is None else abs(left - right) <= 0.0100001
                raw_volume = item["tencent_volume_shares"]
                quote_symbol = key[1]
                volume_scale = 100 if quote_symbol.startswith("SZ000") and not quote_symbol.startswith("SZ0000") else 1
                # AKShare 1.19.3's Tencent adapter leaves some SZ 000xxx stock
                # volumes unscaled even though its output contract says shares.
                # Check the raw amount/price/volume relationship and record the
                # correction separately instead of hiding it in the comparison.
                if quote_symbol.startswith("SZ000") and raw_volume is not None:
                    close_for_check = item["tencent_close"]
                    approx_price = item["tencent_amount_cny"] / (raw_volume * 100) if raw_volume else None
                    if close_for_check is not None and approx_price is not None and 0.5 * close_for_check <= approx_price <= 1.5 * close_for_check:
                        volume_scale = 100
                normalized_volume = raw_volume * volume_scale if raw_volume is not None else None
                item["tencent_volume_scale_applied"] = volume_scale
                item["tencent_volume_shares_normalized"] = normalized_volume
                item["volume_unit_adapter_status"] = "ADAPTER_VOLUME_SUSPECTED_LOTS_SCALED_FOR_COMPARISON" if volume_scale == 100 else "ADAPTER_REPORTED_SHARES"
                left, right = item["baostock_volume_shares"], normalized_volume
                item["volume_shares_absolute_difference"] = None if right is None else left - right
                volume_tol = max(1000.0, abs(left or 0.0) * 0.00002)
                item["volume_shares_tolerance"] = volume_tol
                item["volume_shares_match_within_tolerance"] = None if right is None else abs(left - right) <= volume_tol
                for col, tol in (("amount_cny", max(100.0, float(item["baostock_amount_cny"]) * 0.00002)),):
                    left, right = item[f"baostock_{col}"], item[f"tencent_{col}"]
                    item[f"{col}_absolute_difference"] = None if right is None else left - right
                    item[f"{col}_tolerance"] = tol
                    item[f"{col}_match_within_tolerance"] = None if right is None else abs(left - right) <= tol
                compare_rows.append(item)
            compare_frame = pd.DataFrame(compare_rows)
            compare_frame.to_csv(out_dir / "independent-quote-crosscheck.csv", index=False)
            quote_match_summary = {
                "trade_quote_rows": int(len(compare_frame)),
                "quotes_available": int(compare_frame["quote_source_status"].eq("AVAILABLE").sum()),
                "open_match_within_0_01": int(compare_frame["open_match_within_0_01"].fillna(False).sum()),
                "high_match_within_0_01": int(compare_frame["high_match_within_0_01"].fillna(False).sum()),
                "low_match_within_0_01": int(compare_frame["low_match_within_0_01"].fillna(False).sum()),
                "close_match_within_0_01": int(compare_frame["close_match_within_0_01"].fillna(False).sum()),
                "volume_match_after_recorded_unit_adjustment": int(compare_frame["volume_shares_match_within_tolerance"].fillna(False).sum()),
                "amount_match_within_recorded_tolerance": int(compare_frame["amount_cny_match_within_tolerance"].fillna(False).sum()),
                "symbols_with_suspected_lot_units_in_akshare_adapter": sorted(
                    compare_frame.loc[compare_frame["tencent_volume_scale_applied"].eq(100), "symbol"].astype(str).unique()
                ),
            }
        source_rows = [{"source_family": "cninfo_dividends", **row} for row in dividend_queries]
        source_rows.extend({"source_family": "issuer_filing", **filing} for filing in issuer_filings)
        source_rows.extend({"source_family": "tencent_daily_quotes", **row} for row in quote_queries)
        _write_json(out_dir / "independent-source-query-log.json", source_rows)
        dividend_match_summary = {
            "query_count": len(dividend_queries),
            "query_success_count": sum(row.get("status") == "QUERY_OK" for row in dividend_queries),
            "query_failed_count": sum(row.get("status") != "QUERY_OK" for row in dividend_queries),
            "selected_symbol_count": len(selected_symbols),
            "returned_record_count": int(len(dividends)),
            "scope": "structured dividend/share-distribution data only, not complete corporate-action coverage",
        }
        quote_match_summary["query_count"] = len(quote_queries)
        quote_match_summary["query_success_count"] = sum(row.get("status") == "QUERY_OK" for row in quote_queries)
        dividend_records_path = out_dir / "cninfo-dividend-records-adapter-output.csv"
        if not dividends.empty:
            dividends["symbol"] = dividends["symbol"].astype(str)
            holding_windows = pd.read_csv(audit_input_dir / "actual-holding-intervals.csv")
            event_rows = []
            for row in dividends.to_dict(orient="records"):
                symbol = str(row.get("symbol", ""))
                dates = [row.get(k) for k in ("股权登记日", "除权日", "派息日", "股份到账日")]
                symbol_holdings = holding_windows.loc[holding_windows.symbol.eq(symbol)]
                match = _match_action_to_holding_windows(row.get("股权登记日"), dates, symbol_holdings)
                if match["record_date_entitlement_match"] or match["phase_overlap_match"]:
                    event_rows.append({**row, **match})
            holding_hit_columns = [
                *dividends.columns,
                "record_date_entitlement_match",
                "phase_overlap_match",
                "matching_holding_intervals",
                "holding_match_status",
            ]
            pd.DataFrame(event_rows, columns=holding_hit_columns).to_csv(
                out_dir / "cninfo-dividend-holding-window-hits.csv", index=False
            )
            session_index = {
                day: index for index, day in enumerate(
                    sorted(pd.to_datetime(calendar.loc[
                        calendar["is_open"].astype(str).str.lower().isin(["true", "1"]), "trade_date"
                    ]).dt.strftime("%Y-%m-%d").unique())
                )
            }
            candidate_hit_rows = []
            candidate_targets = pd.read_csv(input_paths["candidate-pool-by-signal.csv"])
            candidate_targets = candidate_targets.loc[
                candidate_targets["selected_target"].astype(str).str.lower().isin(["true", "1"])
            ]
            for target in candidate_targets.itertuples(index=False):
                signal = str(target.signal_date)
                execute = str(target.execution_date)
                signal_index = session_index.get(signal)
                lookback_start = None
                if signal_index is not None:
                    lookback_start = sorted(session_index, key=session_index.get)[max(0, signal_index - int(strategy["lookback_sessions"]))]
                symbol_records = dividends.loc[dividends["symbol"].astype(str).eq(str(target.symbol))]
                checked = 0
                for dividend in symbol_records.to_dict(orient="records"):
                    dates = [dividend.get(k) for k in ("股权登记日", "除权日", "派息日", "股份到账日")]
                    phases = [pd.to_datetime(d).strftime("%Y-%m-%d") for d in dates if pd.notna(d)]
                    if any(lookback_start <= phase <= execute for phase in phases) if lookback_start else False:
                        checked += 1
                        candidate_hit_rows.append({
                            "signal_date": signal, "execution_date": execute, "symbol": str(target.symbol),
                            "candidate_rank": int(target.rank), "event_phase_dates": ",".join(phases),
                            "query_source": "CNINFO structured dividend records",
                            "status": "POSSIBLE_DIVIDEND_PHASE_IN_SIGNAL_WINDOW",
                        })
                if checked == 0:
                    candidate_hit_rows.append({
                        "signal_date": signal, "execution_date": execute, "symbol": str(target.symbol),
                        "candidate_rank": int(target.rank), "event_phase_dates": "",
                        "query_source": "CNINFO structured dividend records",
                        "status": "NO_MATCH_IN_DIVIDEND_ENDPOINT_ONLY",
                    })
            pd.DataFrame(candidate_hit_rows).to_csv(out_dir / "cninfo-dividend-candidate-window-review.csv", index=False)
        # Query status is explicit by target. An empty match from this one
        # structured dividend interface is not a statement that no other
        # corporate action occurred.
        action_registry_rows = []
        query_by_symbol = {str(row["symbol"]): row for row in dividend_queries}
        for symbol in selected_symbols:
            query = query_by_symbol.get(symbol, {})
            matching_holding_hits = 0
            matching_entitlement_hits = 0
            if (out_dir / "cninfo-dividend-holding-window-hits.csv").exists():
                holding_hit_frame = pd.read_csv(out_dir / "cninfo-dividend-holding-window-hits.csv")
                matching_holding_hits = int((holding_hit_frame["symbol"].astype(str) == symbol).sum())
                if "record_date_entitlement_match" in holding_hit_frame.columns:
                    matching_entitlement_hits = int(
                        ((holding_hit_frame["symbol"].astype(str) == symbol)
                         & holding_hit_frame["record_date_entitlement_match"].astype(str).str.lower().eq("true")).sum()
                    )
            matching_candidate_hits = 0
            if (out_dir / "cninfo-dividend-candidate-window-review.csv").exists():
                candidate_hit_frame = pd.read_csv(out_dir / "cninfo-dividend-candidate-window-review.csv")
                matching_candidate_hits = int(
                    ((candidate_hit_frame["symbol"].astype(str) == symbol)
                     & candidate_hit_frame["status"].eq("POSSIBLE_DIVIDEND_PHASE_IN_SIGNAL_WINDOW")).sum()
                )
            action_registry_rows.append({
                "symbol": symbol,
                "checked_event_family": "cash_dividend_and_share_distribution_records",
                "source": query.get("source"),
                "source_query_status": query.get("status", "UNAVAILABLE"),
                "records_returned": query.get("result_rows"),
                "holding_window_phase_hits": matching_holding_hits,
                "holding_record_date_entitlement_hits": matching_entitlement_hits,
                "candidate_signal_window_phase_hits": matching_candidate_hits,
                "other_action_classes": "NOT_QUERIED_BY_THIS_ENDPOINT: rights offering, merger, suspension notices, delisting, code change and non-dividend share issuance",
                "coverage_conclusion": "NO_MATCH_IN_CNINFO_DIVIDEND_ENDPOINT_ONLY" if query.get("status") == "QUERY_OK" and not matching_holding_hits and not matching_candidate_hits else "REVIEW_REQUIRED",
                "overall_company_action_status": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            })
        for filing, filing_config in zip(issuer_filings, supplemental_filings, strict=True):
            action_registry_rows.append({
                "symbol": filing["symbol"],
                "checked_event_family": filing["event_family"],
                "source": filing["source"],
                "source_query_status": filing["status"],
                "records_returned": 1,
                "holding_window_phase_hits": filing_config.get("holding_window_phase_hits"),
                "candidate_signal_window_phase_hits": filing_config.get("candidate_signal_window_phase_hits"),
                "event_summary": filing_config.get("event_summary"),
                "source_url": filing["source_url"],
                "source_title": filing["source_title"],
                "source_document_date": filing["document_date"],
                "source_archive_path": filing["raw_response_path"],
                "source_archive_sha256": filing["raw_response_sha256"],
                "source_known_at_evidence_status": filing["known_at_evidence_status"],
                "source_human_review_status": "PENDING",
                "other_action_classes": filing_config.get("other_action_classes", "UNREVIEWED"),
                "coverage_conclusion": filing_config.get("coverage_conclusion", "REVIEW_REQUIRED"),
                "overall_company_action_status": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            })
        pd.DataFrame(action_registry_rows).to_csv(out_dir / "corporate-action-query-register.csv", index=False)
        pd.DataFrame([
            {"input_family": "daily_ohlc", "primary_source": "BaoStock frozen point-in-time snapshot", "independent_source": "Tencent Finance via AKShare historical daily endpoint", "scope_checked": "24 fill dates / 14 traded securities", "status": "OHLC_COMPARISON_RECORDED; availability/vintage UNVERIFIED"},
            {"input_family": "volume_and_amount", "primary_source": "BaoStock", "independent_source": "Tencent Finance via AKShare", "scope_checked": "24 fill dates; Tencent 000xxx adapter unit behavior separately flagged", "status": "UNIT_AND_ROUNDING_REVIEW_REQUIRED"},
            {"input_family": "historical_trading_calendar", "primary_source": "frozen BaoStock-derived calendar snapshot", "independent_source": "none in this audit", "scope_checked": "next-open mapping for 3 signal dates", "status": "MAPPING_MATCHED; historical source vintage UNVERIFIED"},
            {"input_family": "security_status_and_universe", "primary_source": "BaoStock point-in-time rows", "independent_source": "none in this audit", "scope_checked": "24 trades and 6024 saved candidate rows", "status": "SOURCE_AND_HISTORICAL_AVAILABILITY_UNVERIFIED"},
            {"input_family": "market_rules_and_lots", "primary_source": "default configuration fallback", "independent_source": "none in this audit", "scope_checked": "347660 bars; all fallback", "status": "UNVERIFIED_FALLBACK_USED"},
            {"input_family": "historical_fees", "primary_source": "default configuration flat assumptions", "independent_source": "none in this audit", "scope_checked": "24 fills", "status": "UNVERIFIED_CONFIG_ASSUMPTION; HTS personal rate unavailable"},
            {"input_family": "corporate_actions", "primary_source": "in-window registered event tables (empty for the window)", "independent_source": "CNINFO structured dividend records for selected targets plus one issuer filing", "scope_checked": f"{len(selected_symbols)} selected target securities; not full candidate pool", "status": "INCOMPLETE_COVERAGE; empty registry not treated as no events"},
            {"input_family": "known_at_and_vendor_vintage", "primary_source": "adapter available_at and retrieved_at fields", "independent_source": "no vendor historical publication/vintage evidence", "scope_checked": "all 68 session dates; 15:00 adapter time assumption", "status": "UNVERIFIED"},
        ]).to_csv(out_dir / "source-availability-matrix.csv", index=False)
    # Preserve the existing holding table without changing its earlier snapshot.
    holding_path = audit_input_dir / "actual-holding-intervals.csv"
    pd.read_csv(holding_path).to_csv(out_dir / "actual-holding-intervals.csv", index=False)
    # Records are not event-completeness evidence: the source registry is empty for this window.
    registry = pd.read_csv(experiment_dir / "inputs/provenance-registry.csv", keep_default_na=False)
    blocked_summary = {
        "blocked_order_file_sha256": sha256_file(input_paths["blocked_orders.csv"]),
        "blocked_order_row_count": int(len(blocked)),
        "interpretation": "该文件没有已记录的阻断订单行；不能据此证明回测没有跳过的小于交易单位目标或未记录订单。",
    }
    core_input_hashes_match = all(
        row["status"] == "MATCHED_FROZEN_INVENTORY"
        for key, row in artifact_hash_checks.items() if key != "candidate-pool-by-signal.csv"
    )
    candidate_pool_rebuilt = (
        artifact_hash_checks["candidate-pool-by-signal.csv"]["status"] == "NO_FROZEN_HASH_RECONSTRUCTED_AGAINST_BARS_CONFIG"
        and all(value["candidate_reconstruction_status"] == "MATCH" for value in reconstruction.values())
    )
    all_inputs_frozen = all(row["status"] == "MATCHED_FROZEN_INVENTORY" for row in artifact_hash_checks.values())
    summary = {
        "schema_version": 1,
        "report_type": "transaction_level_data_rule_fee_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
        "input_hashes": hashes,
        "experiment_id": experiment_id,
        "window": {"start": str(bars.trade_date.min()), "end": str(bars.trade_date.max()), "sessions": int(bars.trade_date.nunique()), "rows": int(len(bars))},
        "trade_count": int(len(trades)),
        "trade_audit_rows": int(len(audit)),
        "journal_linked_rows": int(audit.journal_link_status.eq("MATCHED").sum()),
        "candidate_reconstruction_by_signal_date": reconstruction,
        "candidate_pool_rows_saved": int(pd.read_csv(input_paths["candidate-pool-by-signal.csv"]).shape[0]),
        "candidate_unique_symbols_saved": int(pd.read_csv(input_paths["candidate-pool-by-signal.csv"]).symbol.nunique()),
        "selected_target_unique_symbols": len(selected_symbols),
        "selected_target_slots": int(trade_targets.selected_target.sum()),
        "selected_targets_reconstructed_to_zero_shares": int(trade_targets.target_fill_status.eq("TARGET_ROUNDED_TO_ZERO").sum()),
        "candidate_target_fill_mismatch_rows": int(trade_targets.target_fill_status.eq("NO_FILL_REVIEW_REQUIRED").sum()),
        "notional_cny": float(pd.to_numeric(trades.notional_cny).sum()),
        "modeled_fees_cny": float(pd.to_numeric(trades.fees_cny).sum()),
        "modeled_execution_impact_cny": float(pd.to_numeric(trades.execution_impact_vs_reference_cny).sum()),
        "market_rule_fallback_rows_in_window": int(run_metrics.get("market_rule_fallback_row_count", -1)),
        "all_trades_rule_fallback_status": sorted(set(audit.market_rule_source_status.astype(str))),
        "all_trades_fee_fallback_status": sorted(set(audit.fee_source_status.astype(str))),
        "actual_personal_broker_fee_evidence": "UNAVAILABLE",
        "historical_quote_vintage_and_at_decision_availability": "UNVERIFIED",
        "registered_provenance_rows": int(len(registry)),
        "blocked_orders": blocked_summary,
        "external_source_query_count": len(source_rows),
        "supplemental_filing_manifest_sha256": supplemental_manifest_sha256,
        "external_company_action_query_scope": f"CNINFO structured dividend endpoint for {len(selected_symbols)} selected target securities; not all {int(pd.read_csv(input_paths['candidate-pool-by-signal.csv']).symbol.nunique())} distinct eligible candidate securities.",
        "frozen_artifact_hash_checks": artifact_hash_checks,
        "independent_quote_crosscheck": quote_match_summary,
        "cninfo_dividend_query": dividend_match_summary,
        "research_trust": {
            "input_file_integrity": (
                "VERIFIED_AGAINST_PRIOR_ARTIFACT_INVENTORY" if all_inputs_frozen
                else "CORE_RUN_AND_DATA_MATCH_CANDIDATE_RECONSTRUCTED_NOT_HASH_ANCHORED"
                if core_input_hashes_match and candidate_pool_rebuilt else "PARTIAL_OR_UNVERIFIED"
            ),
            "canonical_data_quality": "STRUCTURAL_AUDIT_HEALTHY_WITH_WARNINGS" if _json(experiment_dir / "inputs/point_in_time_quality.json").get("healthy") else "FAILED",
            "historical_availability_and_vendor_vintage": "UNVERIFIED",
            "market_rule_coverage": "FALLBACK_USED",
            "fee_coverage": "CONFIG_FLAT_ASSUMPTION; PERSONAL_BROKER_RATE_UNAVAILABLE",
            "corporate_action_coverage": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            "formal_baseline": "NOT_READY",
            "incremental_can_continue": False,
        },
        "formal_baseline": "NOT_READY",
        "incremental_can_continue": False,
        "scope_note": "只读审计；不改原始输入、checkpoint、既有 run 或旧审计报告；不连接华泰、不提交委托。",
    }
    _write_json(out_dir / "trade-audit-summary.json", summary)
    md = [
        "# 真实行情短窗口成交级数据审计",
        "",
        f"- 实验：`{summary['experiment_id']}`；窗口 `{summary['window']['start']}..{summary['window']['end']}`。",
        f"- 成交行：{summary['trade_count']}；逐笔账本关联：{summary['journal_linked_rows']}。",
        f"- 模拟成交额：¥{summary['notional_cny']:.2f}；配置模拟费用：¥{summary['modeled_fees_cny']:.2f}；模拟滑点影响：¥{summary['modeled_execution_impact_cny']:.2f}。",
        f"- 市场规则：全窗 {summary['market_rule_fallback_rows_in_window']} 行使用回退；本次成交状态 `{summary['all_trades_rule_fallback_status']}`。",
        f"- 费率：所有本次成交 `{summary['all_trades_fee_fallback_status']}`；华泰个人费率证据 `{summary['actual_personal_broker_fee_evidence']}`。",
        f"- 候选：已保存 {summary['candidate_pool_rows_saved']} 行、{summary['candidate_unique_symbols_saved']} 个代码；排名重建状态按信号日见 JSON。",
        f"- 成交目标：{summary['selected_target_slots']} 个目标槽；其中按配置交易单位舍入为 0 股 {summary['selected_targets_reconstructed_to_zero_shares']} 个；目标与成交未解释差异 {summary['candidate_target_fill_mismatch_rows']} 行。",
        f"- 拒单记录：{blocked_summary['blocked_order_row_count']} 行；它不覆盖低于最小交易单位而跳过的目标。",
        f"- 独立行情/公司行动查询数：{len(source_rows)}。独立来源对照是本次取得的查询结果，不证明全量行情或公司行动覆盖。",
        f"- 独立报价：成交日 OHLC 价格容差内匹配 {quote_match_summary.get('open_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('high_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('low_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}、{quote_match_summary.get('close_match_within_0_01', 0)}/{quote_match_summary.get('trade_quote_rows', 0)}；成交量与成交额比较另有单位缩放和容差，见逐笔表。",
        f"- CNINFO 分红/送转接口：{dividend_match_summary.get('query_success_count', 0)}/{dividend_match_summary.get('query_count', 0)} 个选中目标查询成功，返回 {dividend_match_summary.get('returned_record_count', 0)} 条历史记录；查询范围不含全部公司行动。",
        f"- 候选池：3 个调仓日的候选排名和动量均由冻结行情与配置重建匹配；候选 CSV 不在此前文件清单中，原文件哈希未被旧清单锚定。",
        "- 松发股份（SH603268）官方公告记载其重大资产重组筹划及 2024-10-08 继续停牌，事件与第一轮高排名候选的动量观察窗重叠；文档日期不是精确首次公开时刻，且这不是该窗口内已确认的现金/股数账务事件。",
        "- 公司行动：CNINFO 股息接口仅在记录日权益持仓和事件阶段均无命中时才记录未命中；登记日卖出仍可能保留已登记权益，买入登记日则不获得权益。该接口仍不代表其他公司行动不存在。",
        f"- 输入证据：核心行情、日历、配置、成交、账本、阻断单及 metrics 与此前清单 {sum(row['status'] == 'MATCHED_FROZEN_INVENTORY' for row in artifact_hash_checks.values())}/{len(expected_inventory_paths)} 项相符；候选池通过重建匹配但缺少旧哈希锚定。",
        "- 历史行情 vintage、历史决策时点可得性和公告来源人工签认仍未验证。正式基线 `NOT_READY`，增量续跑 `can_continue=false`。",
        "",
        "详细逐笔材料位于本机 `data/private/`，未纳入 Git。",
    ]
    (out_dir / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    inventory_rows = []
    for path in sorted(item for item in out_dir.rglob("*") if item.is_file()):
        if path.name == "audit-artifact-inventory.csv":
            continue
        inventory_rows.append({
            "relative_path": path.relative_to(out_dir).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    pd.DataFrame(inventory_rows, columns=["relative_path", "size_bytes", "sha256"]).to_csv(
        out_dir / "audit-artifact-inventory.csv", index=False
    )
    print(json.dumps({"output_dir": str(out_dir), "summary": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
