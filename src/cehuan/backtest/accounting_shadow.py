from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

from cehuan.accounting import FeeItem, PortfolioLedger, PriceMark, TradeFill


def run_accounting_shadow(
    *,
    initial_cash_cny: Decimal | int | str,
    trades: pd.DataFrame,
    bars: pd.DataFrame,
    equity_curve: pd.DataFrame,
    open_positions: pd.DataFrame,
    tolerance_cny: Decimal = Decimal("0.01"),
) -> dict[str, Any]:
    """Replay an event-free research run through the ledger and compare daily state.

    The legacy engine remains authoritative in this diagnostic mode. Stale closes
    are permitted up to the input window to mirror its last-close carry behavior;
    this is deliberately not a production valuation policy.
    """
    ledger = PortfolioLedger(Decimal(str(initial_cash_cny)), portfolio_id="accounting-shadow")
    dates = [str(value) for value in equity_curve["trade_date"].tolist()]
    date_indices = {day: index for index, day in enumerate(dates)}
    bars_by_symbol = {
        str(symbol): group.sort_values("trade_date").reset_index(drop=True)
        for symbol, group in bars.groupby("symbol", sort=False, observed=True)
    }
    trades_by_date = {
        str(day): group
        for day, group in trades.groupby("trade_date", sort=False)
    } if not trades.empty else {}
    daily: list[dict[str, Any]] = []
    fill_number = 0
    mismatch_count = 0
    unvalued_count = 0

    for index, day in enumerate(dates):
        day_trades = trades_by_date.get(day)
        if day_trades is not None:
            for trade in day_trades.to_dict(orient="records"):
                fill_number += 1
                side = str(trade["side"]).upper()
                fill_date = day
                sellable_from = None
                if side == "BUY":
                    if index + 1 >= len(dates):
                        raise ValueError("影子账本无法为最后一个交易日买入计算 T+1 可卖日")
                    sellable_from = dates[index + 1]
                quantity_value = Decimal(str(trade["quantity"]))
                if quantity_value != quantity_value.to_integral_value() or quantity_value <= 0:
                    raise ValueError("影子回放成交股数必须为正整数")
                fee = Decimal(str(trade["fees_cny"]))
                ledger.apply_fill(TradeFill(
                    fill_id=f"shadow-fill-{fill_number:08d}",
                    trade_date=fill_date,
                    symbol=str(trade["symbol"]),
                    side=side,
                    quantity=int(quantity_value),
                    price_cny=Decimal(str(trade["estimated_fill_price"])),
                    fees=(FeeItem("aggregate_fees", fee),),
                    sellable_from=sellable_from,
                ))

        marks: dict[str, PriceMark] = {}
        for symbol in {lot.symbol for lot in ledger.positions}:
            history = bars_by_symbol.get(symbol)
            if history is None:
                continue
            observed = history.loc[
                history["trade_date"].astype(str).le(day)
                & history["close"].notna()
                & history["close"].astype(float).gt(0)
            ]
            if observed.empty:
                continue
            quote = observed.iloc[-1]
            quote_day = str(quote["trade_date"])
            age = index - date_indices.get(quote_day, index)
            marks[symbol] = PriceMark(
                price_cny=Decimal(str(quote["close"])),
                price_date=quote_day,
                source=str(quote.get("data_source", "normalized_input_bars")),
                age_sessions=max(0, age),
            )
        nav = ledger.mark_to_market(day, marks, max_stale_age_sessions=len(dates))
        legacy = equity_curve.loc[equity_curve["trade_date"].astype(str).eq(day)].iloc[0]
        legacy_cash = Decimal(str(legacy["cash_cny"]))
        cash_delta = ledger.cash_cny - legacy_cash
        ledger_symbol_count = len({lot.symbol for lot in ledger.positions})
        expected_symbol_count = int(legacy["holdings_count"])
        row_status = "MATCHED"
        equity_delta: Decimal | None = None
        if nav.status != "VALUED" or nav.nav_cny is None:
            row_status = "UNVALUED"
            unvalued_count += 1
        else:
            equity_delta = nav.nav_cny - Decimal(str(legacy["equity_cny"]))
        if abs(cash_delta) > tolerance_cny or ledger_symbol_count != expected_symbol_count:
            row_status = "MISMATCH"
        if equity_delta is not None and abs(equity_delta) > tolerance_cny:
            row_status = "MISMATCH"
        if row_status == "MISMATCH":
            mismatch_count += 1
        daily.append({
            "trade_date": day,
            "status": row_status,
            "ledger_cash_cny": ledger.cash_cny,
            "legacy_cash_cny": legacy_cash,
            "cash_delta_cny": cash_delta,
            "ledger_nav_cny": nav.nav_cny,
            "legacy_equity_cny": Decimal(str(legacy["equity_cny"])),
            "equity_delta_cny": equity_delta,
            "ledger_symbol_count": ledger_symbol_count,
            "legacy_symbol_count": expected_symbol_count,
            "stale_price_count": nav.stale_price_count,
            "unvalued_reasons": " | ".join(nav.reasons),
        })

    ledger.assert_invariants()
    legacy_final = {
        str(row["symbol"]): int(row["shares"])
        for row in open_positions.to_dict(orient="records")
    }
    ledger_final: dict[str, int] = {}
    for lot in ledger.positions:
        ledger_final[lot.symbol] = ledger_final.get(lot.symbol, 0) + lot.total_shares
    final_position_mismatches = [
        {
            "symbol": symbol,
            "ledger_shares": ledger_final.get(symbol, 0),
            "legacy_shares": legacy_final.get(symbol, 0),
        }
        for symbol in sorted(set(ledger_final) | set(legacy_final))
        if ledger_final.get(symbol, 0) != legacy_final.get(symbol, 0)
    ]
    if final_position_mismatches:
        mismatch_count += 1
    status = "MISMATCH" if mismatch_count else "UNVALUED" if unvalued_count else "MATCHED"
    journal = pd.DataFrame([
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
        for entry in ledger.journal
    ])
    return {
        "status": status,
        "result_type": "accounting_ledger_shadow_diagnostic_not_formal",
        "fills_replayed": fill_number,
        "daily_match_count": sum(row["status"] == "MATCHED" for row in daily),
        "daily_mismatch_count": mismatch_count,
        "daily_unvalued_count": unvalued_count,
        "tolerance_cny": tolerance_cny,
        "stale_price_policy": "allow input-window stale closes solely to mirror legacy last-close carry",
        "final_position_mismatches": final_position_mismatches,
        "daily_comparison": pd.DataFrame(daily),
        "journal": journal,
        "snapshot": ledger.snapshot(),
        "limitations": [
            "Shadow replays legacy fills; it does not replace the legacy execution state.",
            "Only runs with no identity changes or corporate actions are supported.",
            "Fees are replayed as one aggregate fee line; fee-component parity is not tested.",
            "Legacy last-close carry is allowed for parity and is not a production valuation policy.",
            "A match verifies ledger arithmetic for this replay, not backtest validity or strategy performance.",
        ],
    }
