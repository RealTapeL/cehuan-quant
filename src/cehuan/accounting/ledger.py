from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable, Mapping

from cehuan.accounting.models import (
    CashReceivable,
    LedgerEntry,
    LedgerSnapshot,
    NavResult,
    NoncashClaim,
    PositionLot,
    PriceMark,
    TradeFill,
    calendar_date,
    decimal_value,
    security_symbol,
)


class PortfolioLedger:
    """Small pure-Python ledger with atomic, idempotent fill postings.

    Corporate-action handlers are deliberately separate from this T1 core.
    All monetary values are Decimal; prices and balances are never silently
    converted from float or defaulted to zero.
    """

    def __init__(
        self,
        cash_cny: Decimal | int | str,
        *,
        portfolio_id: str = "research",
        positions: Iterable[PositionLot] = (),
        receivables: Iterable[CashReceivable] = (),
        claims: Iterable[NoncashClaim] = (),
    ) -> None:
        self.portfolio_id = str(portfolio_id).strip()
        if not self.portfolio_id:
            raise ValueError("portfolio_id 不能为空")
        self._cash_cny = decimal_value(cash_cny, "cash_cny", nonnegative=True)
        self._positions = self._index_unique(positions, "lot_id", "持仓批次")
        self._initial_cash_cny = self._cash_cny
        self._initial_position_shares = self._position_totals(self._positions.values())
        self._receivables = self._index_unique(receivables, "receivable_id", "应收款")
        self._claims = self._index_unique(claims, "claim_id", "非现金权利")
        self._journal: list[LedgerEntry] = []
        self._operations: dict[str, tuple[str, tuple[LedgerEntry, ...]]] = {}
        self.assert_invariants()

    @staticmethod
    def _index_unique(items: Iterable[object], key: str, label: str) -> dict[str, object]:
        result: dict[str, object] = {}
        for item in items:
            identifier = str(getattr(item, key, ""))
            if not identifier:
                raise ValueError(f"{label}缺少 {key}")
            if identifier in result:
                raise ValueError(f"{label} {key} 重复：{identifier}")
            result[identifier] = item
        return result

    def _copy(self) -> PortfolioLedger:
        candidate = object.__new__(PortfolioLedger)
        candidate.portfolio_id = self.portfolio_id
        candidate._cash_cny = self._cash_cny
        candidate._initial_cash_cny = self._initial_cash_cny
        candidate._initial_position_shares = self._initial_position_shares.copy()
        candidate._positions = self._positions.copy()
        candidate._receivables = self._receivables.copy()
        candidate._claims = self._claims.copy()
        candidate._journal = self._journal.copy()
        candidate._operations = self._operations.copy()
        return candidate

    def _adopt(self, candidate: PortfolioLedger) -> None:
        self._cash_cny = candidate._cash_cny
        self._positions = candidate._positions
        self._receivables = candidate._receivables
        self._claims = candidate._claims
        self._journal = candidate._journal
        self._operations = candidate._operations

    @staticmethod
    def _digest(payload: Mapping[str, object]) -> str:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _entry(
        self,
        operation_key: str,
        event_id: str,
        phase: str,
        booking_date: date,
        entry_type: str,
        cash_delta_cny: Decimal,
        position_deltas: tuple[tuple[str, int], ...],
        details: Mapping[str, object],
    ) -> LedgerEntry:
        sequence = len(self._journal)
        entry_id = hashlib.sha256(f"{operation_key}:{sequence}".encode("utf-8")).hexdigest()
        return LedgerEntry(
            entry_id=entry_id,
            operation_key=operation_key,
            event_id=event_id,
            phase=phase,
            booking_date=booking_date,
            entry_type=entry_type,
            cash_delta_cny=cash_delta_cny,
            position_deltas=position_deltas,
            details=tuple(sorted((str(k), str(v)) for k, v in details.items())),
        )

    def _apply_once(
        self,
        event_id: str,
        phase: str,
        payload: Mapping[str, object],
        mutate: Callable[[PortfolioLedger, str], tuple[LedgerEntry, ...]],
    ) -> tuple[LedgerEntry, ...]:
        event_id = str(event_id).strip()
        phase = str(phase).strip().upper()
        if not event_id or not phase:
            raise ValueError("账本操作必须有 event_id 和 phase")
        operation_key = f"{self.portfolio_id}:{event_id}:{phase}"
        digest = self._digest(payload)
        previous = self._operations.get(operation_key)
        if previous is not None:
            if previous[0] != digest:
                raise ValueError(f"幂等键 {operation_key} 已用于不同输入")
            return previous[1]

        candidate = self._copy()
        entries = mutate(candidate, operation_key)
        candidate._journal.extend(entries)
        candidate._operations[operation_key] = (digest, entries)
        candidate.assert_invariants()
        self._adopt(candidate)
        return entries

    @property
    def cash_cny(self) -> Decimal:
        return self._cash_cny

    @property
    def positions(self) -> tuple[PositionLot, ...]:
        return tuple(sorted(self._positions.values(), key=lambda lot: (lot.symbol, lot.acquired_on, lot.lot_id)))

    @property
    def receivables(self) -> tuple[CashReceivable, ...]:
        return tuple(sorted(self._receivables.values(), key=lambda item: item.receivable_id))

    @property
    def claims(self) -> tuple[NoncashClaim, ...]:
        return tuple(sorted(self._claims.values(), key=lambda item: item.claim_id))

    @property
    def journal(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._journal)

    @staticmethod
    def _position_totals(lots: Iterable[PositionLot]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for lot in lots:
            totals[lot.symbol] = totals.get(lot.symbol, 0) + lot.total_shares
        return totals

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            portfolio_id=self.portfolio_id,
            cash_cny=self._cash_cny,
            positions=self.positions,
            receivables=self.receivables,
            claims=self.claims,
            journal=self.journal,
        )

    def assert_invariants(self) -> None:
        if not self.portfolio_id:
            raise ValueError("portfolio_id 不能为空")
        if not self._cash_cny.is_finite() or self._cash_cny < 0:
            raise ValueError("账本现金必须为有限非负数")
        for key, lot in self._positions.items():
            if key != lot.lot_id or lot.total_shares <= 0:
                raise ValueError("持仓批次键、股数不合法")
            if not 0 <= lot.sellable_shares <= lot.total_shares:
                raise ValueError(f"{lot.lot_id} 可卖股数超出持仓")
            if lot.cost_basis_cny < 0 or not lot.cost_basis_cny.is_finite():
                raise ValueError(f"{lot.lot_id} 成本基础必须为有限非负数")
        for key, receivable in self._receivables.items():
            if key != receivable.receivable_id:
                raise ValueError("应收款索引键不一致")
        for key, claim in self._claims.items():
            if key != claim.claim_id:
                raise ValueError("非现金权利索引键不一致")

        journal_cash_delta = sum(
            (entry.cash_delta_cny for entry in self._journal), start=Decimal("0")
        )
        if self._cash_cny != self._initial_cash_cny + journal_cash_delta:
            raise ValueError("账本现金与分录现金变动不一致")

        expected_positions = self._initial_position_shares.copy()
        for entry in self._journal:
            for symbol, quantity_delta in entry.position_deltas:
                normalized = security_symbol(symbol, "journal position symbol")
                if isinstance(quantity_delta, bool) or not isinstance(quantity_delta, int):
                    raise ValueError("分录持仓变化必须是整数股数")
                expected_positions[normalized] = expected_positions.get(normalized, 0) + quantity_delta
        expected_positions = {symbol: shares for symbol, shares in expected_positions.items() if shares}
        actual_positions = self._position_totals(self._positions.values())
        if any(shares < 0 for shares in expected_positions.values()):
            raise ValueError("账本分录导致证券持仓为负")
        if actual_positions != expected_positions:
            raise ValueError("账本持仓与分录股数变化不一致")

    def apply_fill(self, fill: TradeFill) -> tuple[LedgerEntry, ...]:
        if not isinstance(fill, TradeFill):
            raise TypeError("apply_fill 只接受 TradeFill")
        notional = fill.price_cny * fill.quantity
        total_fees = sum((item.amount_cny for item in fill.fees), start=Decimal("0"))
        fee_details = {f"fee:{item.name}": item.amount_cny for item in fill.fees}
        payload = {
            "fill_id": fill.fill_id,
            "trade_date": fill.trade_date.isoformat(),
            "symbol": fill.symbol,
            "side": fill.side,
            "quantity": fill.quantity,
            "price_cny": str(fill.price_cny),
            "fees": [(item.name, str(item.amount_cny)) for item in fill.fees],
            "sellable_from": fill.sellable_from.isoformat() if fill.sellable_from else None,
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if fill.side == "BUY":
                cash_out = notional + total_fees
                if candidate._cash_cny < cash_out:
                    raise ValueError("买入成交后现金不足")
                lot_id = f"fill:{fill.fill_id}"
                if lot_id in candidate._positions:
                    raise ValueError(f"买入 lot_id 已存在：{lot_id}")
                assert fill.sellable_from is not None
                candidate._cash_cny -= cash_out
                candidate._positions[lot_id] = PositionLot(
                    lot_id=lot_id,
                    symbol=fill.symbol,
                    total_shares=fill.quantity,
                    sellable_shares=0,
                    cost_basis_cny=cash_out,
                    acquired_on=fill.trade_date,
                    sellable_from=fill.sellable_from,
                    source_event_id=fill.fill_id,
                )
                details = {
                    "notional_cny": notional,
                    "fees_cny": total_fees,
                    "cash_after_cny": candidate._cash_cny,
                    "lot_id": lot_id,
                    **fee_details,
                }
                return (candidate._entry(
                    operation_key, fill.fill_id, "FILL", fill.trade_date, "BUY",
                    -cash_out, ((fill.symbol, fill.quantity),), details,
                ),)

            sellable_lots = sorted(
                (
                    lot for lot in candidate._positions.values()
                    if lot.symbol == fill.symbol
                    and lot.sellable_on(fill.trade_date) > 0
                ),
                key=lambda lot: (lot.acquired_on, lot.lot_id),
            )
            available = sum(lot.sellable_on(fill.trade_date) for lot in sellable_lots)
            if fill.quantity > available:
                raise ValueError(f"可卖股数不足：请求 {fill.quantity}，可卖 {available}")

            remaining = fill.quantity
            allocated_basis = Decimal("0")
            affected_lots: list[str] = []
            for lot in sellable_lots:
                if remaining == 0:
                    break
                sellable_now = lot.sellable_on(fill.trade_date)
                quantity = min(remaining, sellable_now)
                basis = (
                    lot.cost_basis_cny
                    if quantity == lot.total_shares
                    else lot.cost_basis_cny * Decimal(quantity) / Decimal(lot.total_shares)
                )
                new_total = lot.total_shares - quantity
                new_sellable = sellable_now - quantity
                allocated_basis += basis
                affected_lots.append(lot.lot_id)
                remaining -= quantity
                if new_total == 0:
                    candidate._positions.pop(lot.lot_id)
                else:
                    candidate._positions[lot.lot_id] = PositionLot(
                        lot_id=lot.lot_id,
                        symbol=lot.symbol,
                        total_shares=new_total,
                        sellable_shares=new_sellable,
                        cost_basis_cny=lot.cost_basis_cny - basis,
                        acquired_on=lot.acquired_on,
                        sellable_from=lot.sellable_from,
                        source_event_id=lot.source_event_id,
                    )

            cash_in = notional - total_fees
            candidate._cash_cny += cash_in
            details = {
                "notional_cny": notional,
                "fees_cny": total_fees,
                "allocated_cost_basis_cny": allocated_basis,
                "realized_pnl_cny": cash_in - allocated_basis,
                "cash_after_cny": candidate._cash_cny,
                "lot_ids_fifo": ",".join(affected_lots),
                **fee_details,
            }
            return (candidate._entry(
                operation_key, fill.fill_id, "FILL", fill.trade_date, "SELL",
                cash_in, ((fill.symbol, -fill.quantity),), details,
            ),)

        return self._apply_once(fill.fill_id, "FILL", payload, mutate)

    def mark_to_market(
        self,
        valuation_date: date | str,
        prices: Mapping[str, PriceMark],
        *,
        max_stale_age_sessions: int = 0,
    ) -> NavResult:
        day = calendar_date(valuation_date, "valuation_date")
        if isinstance(max_stale_age_sessions, bool) or not isinstance(max_stale_age_sessions, int):
            raise ValueError("max_stale_age_sessions 必须为非负整数")
        if max_stale_age_sessions < 0:
            raise ValueError("max_stale_age_sessions 必须为非负整数")
        normalized_prices: dict[str, PriceMark] = {}
        for symbol, mark in prices.items():
            normalized = security_symbol(symbol, "price symbol")
            if normalized in normalized_prices:
                raise ValueError(f"价格映射包含重复规范证券代码：{normalized}")
            normalized_prices[normalized] = mark
        reasons: list[str] = []
        positions_value = Decimal("0")
        stale_count = 0
        position_prices_valid = True
        symbols = {lot.symbol for lot in self._positions.values()}
        for symbol in sorted(symbols):
            mark = normalized_prices.get(symbol)
            if not isinstance(mark, PriceMark):
                reasons.append(f"{symbol}: 缺少有效估值价格")
                position_prices_valid = False
                continue
            if mark.price_date > day:
                reasons.append(f"{symbol}: 估值价格日期晚于估值日")
                position_prices_valid = False
                continue
            if mark.age_sessions > max_stale_age_sessions:
                reasons.append(f"{symbol}: 价格已滞后 {mark.age_sessions} 个交易日")
                stale_count += 1
                position_prices_valid = False
                continue
            shares = sum(lot.total_shares for lot in self._positions.values() if lot.symbol == symbol)
            positions_value += mark.price_cny * shares

        receivable_value = Decimal("0")
        receivables_valid = True
        for receivable in self._receivables.values():
            if receivable.status != "PENDING":
                continue
            if receivable.tax_cny is None:
                reasons.append(f"{receivable.receivable_id}: 应收税费未知")
                receivables_valid = False
            else:
                receivable_value += receivable.gross_cny - receivable.tax_cny

        claims_value = Decimal("0")
        claims_valid = True
        for claim in self._claims.values():
            if claim.status in {"SETTLED", "CANCELLED"}:
                continue
            if claim.valuation_cny is None or not claim.valuation_source:
                reasons.append(f"{claim.claim_id}: 权利没有可信估值")
                claims_valid = False
            else:
                claims_value += claim.valuation_cny

        if position_prices_valid and receivables_valid and claims_valid:
            nav = self._cash_cny + positions_value + receivable_value + claims_value
            status: Literal["VALUED", "UNVALUED"] = "VALUED"
            pos_value: Decimal | None = positions_value
            receivable_total: Decimal | None = receivable_value
            claims_total: Decimal | None = claims_value
        else:
            nav = None
            status = "UNVALUED"
            pos_value = positions_value if position_prices_valid else None
            receivable_total = receivable_value if receivables_valid else None
            claims_total = claims_value if claims_valid else None
        return NavResult(
            status=status,
            valuation_date=day,
            nav_cny=nav,
            cash_cny=self._cash_cny,
            positions_value_cny=pos_value,
            receivables_value_cny=receivable_total,
            claims_value_cny=claims_total,
            stale_price_count=stale_count,
            reasons=tuple(reasons),
        )

    def _seed_receivable(self, receivable: CashReceivable) -> None:
        """Internal hook for event handlers; T2 will provide atomic public operations."""
        if receivable.receivable_id in self._receivables:
            raise ValueError(f"应收款已存在：{receivable.receivable_id}")
        self._receivables[receivable.receivable_id] = receivable
        self.assert_invariants()

    def _seed_claim(self, claim: NoncashClaim) -> None:
        """Internal hook for event handlers; T2 will provide atomic public operations."""
        if claim.claim_id in self._claims:
            raise ValueError(f"非现金权利已存在：{claim.claim_id}")
        self._claims[claim.claim_id] = claim
        self.assert_invariants()
