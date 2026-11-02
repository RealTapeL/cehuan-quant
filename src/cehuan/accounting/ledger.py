from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Callable, Iterable, Mapping

from cehuan.accounting.events import (
    ActionSource,
    CashDividendEvent,
    CodeChangeEvent,
    DelistingEvent,
    RightsDisposition,
    RightsOfferingEvent,
    ShareDistributionEvent,
    ShareSwapMergerEvent,
)
from cehuan.accounting.models import (
    CashReceivable,
    EntitledLot,
    EntitlementSnapshot,
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
    """Pure-Python research ledger with atomic, idempotent state transitions.

    The methods are synthetic accounting primitives; this class is not yet
    connected to the production backtest path. Monetary values use Decimal,
    and unknown valuations never silently become zero.
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
        self._action_entitlements: dict[str, EntitlementSnapshot] = {}
        self._journal: list[LedgerEntry] = []
        self._operations: dict[str, tuple[str, tuple[LedgerEntry, ...]]] = {}
        self._event_families: dict[str, str] = {}
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
        candidate._action_entitlements = self._action_entitlements.copy()
        candidate._journal = self._journal.copy()
        candidate._operations = self._operations.copy()
        candidate._event_families = self._event_families.copy()
        return candidate

    def _adopt(self, candidate: PortfolioLedger) -> None:
        self._cash_cny = candidate._cash_cny
        self._positions = candidate._positions
        self._receivables = candidate._receivables
        self._claims = candidate._claims
        self._action_entitlements = candidate._action_entitlements
        self._journal = candidate._journal
        self._operations = candidate._operations
        self._event_families = candidate._event_families

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
        if phase == "FILL":
            event_family = "TRADE_FILL"
        elif phase.startswith("DIVIDEND_") or phase == "DIVIDEND_PAYMENT":
            event_family = "CASH_DIVIDEND"
        elif phase.startswith("DISTRIBUTION_") or phase == "SHARE_DISTRIBUTION":
            event_family = "SHARE_DISTRIBUTION"
        elif phase.startswith("RIGHTS_"):
            event_family = "RIGHTS_OFFERING"
        elif phase.startswith("SHARE_SWAP_"):
            event_family = "SHARE_SWAP_MERGER"
        elif phase.startswith("DELISTING_"):
            event_family = "DELISTING"
        elif phase.startswith("RECEIVABLE_PAYMENT:"):
            event_family = self._event_families.get(event_id, "CASH_RECEIVABLE")
        elif phase == "IDENTITY_MIGRATION":
            event_family = "CODE_CHANGE_1_TO_1"
        else:
            event_family = phase
        operation_key = f"{self.portfolio_id}:{event_id}:{phase}"
        digest = self._digest(payload)
        known_family = self._event_families.get(event_id)
        if known_family is not None and known_family != event_family:
            raise ValueError(f"event_id {event_id} 已用于其他事件类型：{known_family}")
        previous = self._operations.get(operation_key)
        if previous is not None:
            if previous[0] != digest:
                raise ValueError(f"幂等键 {operation_key} 已用于不同输入")
            return previous[1]

        candidate = self._copy()
        candidate._event_families[event_id] = event_family
        entries = mutate(candidate, operation_key)
        latest_booking = max((entry.booking_date for entry in candidate._journal), default=None)
        if latest_booking is not None and any(entry.booking_date < latest_booking for entry in entries):
            raise ValueError("账本拒绝晚到的历史过账；请从事件日期起重放受影响周期")
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
            entitlements=tuple(
                self._action_entitlements[key] for key in sorted(self._action_entitlements)
            ),
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

    @staticmethod
    def _source_details(source: ActionSource) -> dict[str, str]:
        return {
            "source_id": source.source_id,
            "source_url": source.url,
            "source_sha256": source.sha256,
            "source_title": source.title,
            "source_page_reference": source.page_reference,
            "reviewed_by": source.reviewed_by,
            "reviewed_at": source.reviewed_at.isoformat(),
        }

    @classmethod
    def _prefixed_source_details(cls, source: ActionSource, prefix: str) -> dict[str, str]:
        return {f"{prefix}_{key}": value for key, value in cls._source_details(source).items()}

    @staticmethod
    def _cent(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _capture_entitlement(
        self,
        *,
        event_id: str,
        symbol: str,
        record_date: date,
        phase: str,
        payload: Mapping[str, object],
        source: ActionSource,
        source_details_extra: Mapping[str, object] | None = None,
    ) -> tuple[LedgerEntry, ...]:
        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if event_id in candidate._action_entitlements:
                raise ValueError(f"公司行动 event_id 已有持仓权利快照：{event_id}")
            lots = tuple(sorted(
                (
                    EntitledLot(
                        lot_id=lot.lot_id,
                        quantity=lot.total_shares,
                        cost_basis_cny=lot.cost_basis_cny,
                        acquired_on=lot.acquired_on,
                        sellable_from=lot.sellable_from,
                    )
                    for lot in candidate._positions.values()
                    if lot.symbol == symbol
                ),
                key=lambda item: (item.acquired_on, item.lot_id),
            ))
            entitlement = EntitlementSnapshot(event_id, symbol, record_date, lots)
            candidate._action_entitlements[event_id] = entitlement
            return (candidate._entry(
                operation_key, event_id, phase, record_date, "ENTITLEMENT_SNAPSHOT",
                Decimal("0"), (), {
                    "symbol": symbol,
                    "eligible_shares": entitlement.total_shares,
                    "eligible_lots": len(lots),
                    **self._source_details(source),
                    **(source_details_extra or {}),
                },
            ),)

        return self._apply_once(event_id, phase, payload, mutate)

    def _require_entitlement(
        self, event_id: str, symbol: str, record_date: date
    ) -> EntitlementSnapshot:
        entitlement = self._action_entitlements.get(event_id)
        if entitlement is None:
            raise ValueError(f"{event_id}: 缺少登记日收盘持仓快照")
        if entitlement.symbol != symbol or entitlement.captured_on != record_date:
            raise ValueError(f"{event_id}: 事件证券或登记日与持仓快照不一致")
        return entitlement

    @staticmethod
    def _event_source_payload(source: ActionSource) -> dict[str, str]:
        return {
            "source_id": source.source_id,
            "source_url": source.url,
            "source_sha256": source.sha256,
            "source_title": source.title,
            "source_page_reference": source.page_reference,
            "reviewed_by": source.reviewed_by,
            "reviewed_at": source.reviewed_at.isoformat(),
        }

    def record_cash_dividend(
        self, event: CashDividendEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, CashDividendEvent):
            raise TypeError("record_cash_dividend 只接受 CashDividendEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.record_date:
            raise ValueError("分红权利快照只能在登记日收盘处理")
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "record_date": event.record_date.isoformat(),
            "ex_date": event.ex_date.isoformat(),
            "payable_date": event.payable_date.isoformat(),
            "gross_per_share_cny": str(event.gross_per_share_cny),
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.source),
        }
        return self._capture_entitlement(
            event_id=event.event_id,
            symbol=event.symbol,
            record_date=event.record_date,
            phase="DIVIDEND_RECORD",
            payload=payload,
            source=event.source,
        )

    def record_share_distribution(
        self, event: ShareDistributionEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareDistributionEvent):
            raise TypeError("record_share_distribution 只接受 ShareDistributionEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.record_date:
            raise ValueError("送转权利快照只能在登记日收盘处理")
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "record_date": event.record_date.isoformat(),
            "effective_date": event.effective_date.isoformat(),
            "tradable_date": event.tradable_date.isoformat(),
            "ratio": str(event.shares_per_old_share),
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.source),
        }
        return self._capture_entitlement(
            event_id=event.event_id,
            symbol=event.symbol,
            record_date=event.record_date,
            phase="DISTRIBUTION_RECORD",
            payload=payload,
            source=event.source,
        )

    def record_rights_offering(
        self, event: RightsOfferingEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, RightsOfferingEvent):
            raise TypeError("record_rights_offering 只接受 RightsOfferingEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.record_date:
            raise ValueError("配股权利快照只能在登记日收盘处理")
        claim_id = f"rights:{event.event_id}"
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "record_date": event.record_date.isoformat(),
            "rights_per_old_share": str(event.rights_per_old_share),
            "subscription_price_cny": str(event.subscription_price_cny),
            "tradable_date": event.tradable_date.isoformat(),
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if event.event_id in candidate._action_entitlements:
                raise ValueError(f"公司行动 event_id 已有持仓权利快照：{event.event_id}")
            if claim_id in candidate._claims:
                raise ValueError(f"配股权利已存在：{claim_id}")
            lots = tuple(sorted(
                (
                    EntitledLot(
                        lot_id=lot.lot_id,
                        quantity=lot.total_shares,
                        cost_basis_cny=lot.cost_basis_cny,
                        acquired_on=lot.acquired_on,
                        sellable_from=lot.sellable_from,
                    )
                    for lot in candidate._positions.values()
                    if lot.symbol == event.symbol
                ),
                key=lambda item: (item.acquired_on, item.lot_id),
            ))
            entitlement = EntitlementSnapshot(event.event_id, event.symbol, event.record_date, lots)
            candidate._action_entitlements[event.event_id] = entitlement
            exact_rights = Decimal(entitlement.total_shares) * event.rights_per_old_share
            candidate._claims[claim_id] = NoncashClaim(
                claim_id=claim_id,
                event_id=event.event_id,
                old_symbol=event.symbol,
                new_symbol=event.symbol,
                exact_entitlement=exact_rights,
                final_allocated_shares=None,
                tradable_from=event.tradable_date,
                valuation_cny=None,
                valuation_source=None,
                status="RIGHTS_DISPOSITION_REQUIRED" if exact_rights else "CANCELLED",
                acquired_on=event.record_date,
            )
            return (candidate._entry(
                operation_key, event.event_id, "RIGHTS_RECORD", event.record_date,
                "RIGHTS_ENTITLEMENT", Decimal("0"), (), {
                    "claim_id": claim_id,
                    "eligible_old_shares": entitlement.total_shares,
                    "exact_rights": exact_rights,
                    "eligible_lots": len(lots),
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "RIGHTS_RECORD", payload, mutate)

    def accrue_cash_dividend(
        self,
        event: CashDividendEvent,
        *,
        as_of: date | str,
        tax_cny: Decimal | int | str | None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, CashDividendEvent):
            raise TypeError("accrue_cash_dividend 只接受 CashDividendEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.ex_date:
            raise ValueError("分红应收只能在除息日确认")
        entitlement = self._require_entitlement(event.event_id, event.symbol, event.record_date)
        gross = self._cent(event.gross_per_share_cny * entitlement.total_shares)
        tax = None if tax_cny is None else self._cent(decimal_value(tax_cny, "tax_cny", nonnegative=True))
        if gross == 0:
            tax = Decimal("0")
        if tax is not None and tax > gross:
            raise ValueError("分红税费不能大于应收税前金额")
        receivable_id = f"dividend:{event.event_id}"
        payload = {
            "event_id": event.event_id,
            "receivable_id": receivable_id,
            "ex_date": event.ex_date.isoformat(),
            "gross_cny": str(gross),
            "tax_cny": None if tax is None else str(tax),
            "eligible_shares": entitlement.total_shares,
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if event.ex_date < entitlement.captured_on:
                raise ValueError("分红除息日早于登记日权利快照")
            if receivable_id in candidate._receivables:
                raise ValueError(f"分红应收款已存在：{receivable_id}")
            receivable = CashReceivable(
                receivable_id=receivable_id,
                event_id=event.event_id,
                gross_cny=gross,
                tax_cny=tax,
                recognized_on=event.ex_date,
                payable_on=event.payable_date,
            )
            candidate._receivables[receivable_id] = receivable
            return (candidate._entry(
                operation_key, event.event_id, "DIVIDEND_ACCRUAL", event.ex_date,
                "CASH_DIVIDEND_RECEIVABLE", Decimal("0"), (), {
                    "receivable_id": receivable_id,
                    "eligible_shares": entitlement.total_shares,
                    "gross_cny": gross,
                    "tax_cny": "UNKNOWN" if tax is None else tax,
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "DIVIDEND_ACCRUAL", payload, mutate)

    def settle_receivable(
        self, receivable_id: str, *, settled_on: date | str
    ) -> tuple[LedgerEntry, ...]:
        identifier = str(receivable_id).strip()
        if not identifier:
            raise ValueError("receivable_id 不能为空")
        day = calendar_date(settled_on, "settled_on")
        receivable = self._receivables.get(identifier)
        if receivable is None:
            raise ValueError(f"应收款不存在：{identifier}")
        payload = {
            "receivable_id": identifier,
            "event_id": receivable.event_id,
            "settled_on": day.isoformat(),
            "gross_cny": str(receivable.gross_cny),
            "tax_cny": None if receivable.tax_cny is None else str(receivable.tax_cny),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            current = candidate._receivables[identifier]
            if current.status != "PENDING":
                raise ValueError(f"应收款 {identifier} 当前状态不是 PENDING")
            if current.tax_cny is None:
                raise ValueError(f"应收款 {identifier} 税费未知，不能确认到账净额")
            if current.payable_on is None or day < current.payable_on:
                raise ValueError(f"应收款 {identifier} 尚未到可确认到账日")
            net = current.gross_cny - current.tax_cny
            candidate._cash_cny += net
            candidate._receivables[identifier] = replace(current, status="SETTLED")
            return (candidate._entry(
                operation_key, current.event_id, "RECEIVABLE_SETTLEMENT", day,
                "CASH_RECEIVABLE_SETTLED", net, (), {
                    "receivable_id": identifier,
                    "gross_cny": current.gross_cny,
                    "tax_cny": current.tax_cny,
                    "net_cash_cny": net,
                },
            ),)

        return self._apply_once(
            receivable.event_id, f"RECEIVABLE_PAYMENT:{identifier}", payload, mutate
        )

    def apply_share_distribution(
        self,
        event: ShareDistributionEvent,
        *,
        as_of: date | str,
        allocated_new_shares: int | None = None,
        allocation_evidence: str | None = None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareDistributionEvent):
            raise TypeError("apply_share_distribution 只接受 ShareDistributionEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.effective_date:
            raise ValueError("送转股数与成本只能在事件生效日处理")
        entitlement = self._require_entitlement(event.event_id, event.symbol, event.record_date)
        exact = Decimal(entitlement.total_shares) * event.shares_per_old_share
        floor_shares = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        ceiling_shares = int(exact.to_integral_value(rounding=ROUND_CEILING))
        if allocated_new_shares is None:
            if floor_shares != ceiling_shares:
                raise ValueError("送转产生非整数股，必须提供已核实的账户层最终获配股数")
            allocated_new_shares = floor_shares
        if isinstance(allocated_new_shares, bool) or not isinstance(allocated_new_shares, int):
            raise ValueError("allocated_new_shares 必须是整数股数")
        if not floor_shares <= allocated_new_shares <= ceiling_shares:
            raise ValueError("账户层最终获配股数不在精确送转权利的整数边界内")
        if floor_shares != ceiling_shares and not str(allocation_evidence or "").strip():
            raise ValueError("非整数送转必须提供账户层最终分配依据")
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "effective_date": event.effective_date.isoformat(),
            "tradable_date": event.tradable_date.isoformat(),
            "ratio": str(event.shares_per_old_share),
            "exact_entitlement": str(exact),
            "allocated_new_shares": allocated_new_shares,
            "allocation_evidence": allocation_evidence,
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if event.effective_date < entitlement.captured_on:
                raise ValueError("送转生效日早于登记日权利快照")
            if entitlement.total_shares == 0 and allocated_new_shares != 0:
                raise ValueError("无登记日持仓时不能分配送转股")
            for old_lot in entitlement.lots:
                current = candidate._positions.get(old_lot.lot_id)
                if current is None or current.total_shares != old_lot.quantity or current.cost_basis_cny != old_lot.cost_basis_cny:
                    raise ValueError(
                        f"{old_lot.lot_id}: 登记日后持仓已变化；当前送转处理不支持事件前后未重放的交易"
                    )

            lot_allocations: dict[str, int] = {}
            if allocated_new_shares:
                raw_allocations = [
                    (lot, Decimal(allocated_new_shares) * Decimal(lot.quantity) / Decimal(entitlement.total_shares))
                    for lot in entitlement.lots
                ]
                lot_allocations = {lot.lot_id: int(raw.to_integral_value(rounding=ROUND_FLOOR)) for lot, raw in raw_allocations}
                remainder = allocated_new_shares - sum(lot_allocations.values())
                ranked = sorted(
                    raw_allocations,
                    key=lambda pair: (-(pair[1] - pair[1].to_integral_value(rounding=ROUND_FLOOR)), pair[0].acquired_on, pair[0].lot_id),
                )
                for lot, _raw in ranked[:remainder]:
                    lot_allocations[lot.lot_id] += 1

            position_delta = allocated_new_shares
            for old_lot in entitlement.lots:
                new_shares = lot_allocations.get(old_lot.lot_id, 0)
                if new_shares <= 0:
                    continue
                current = candidate._positions[old_lot.lot_id]
                combined_shares = current.total_shares + new_shares
                new_basis = current.cost_basis_cny * Decimal(new_shares) / Decimal(combined_shares)
                old_basis = current.cost_basis_cny - new_basis
                candidate._positions[old_lot.lot_id] = replace(current, cost_basis_cny=old_basis)
                new_lot_id = f"action:{event.event_id}:{old_lot.lot_id}"
                if new_lot_id in candidate._positions:
                    raise ValueError(f"送转新股批次已存在：{new_lot_id}")
                candidate._positions[new_lot_id] = PositionLot(
                    lot_id=new_lot_id,
                    symbol=event.symbol,
                    total_shares=new_shares,
                    sellable_shares=0,
                    cost_basis_cny=new_basis,
                    acquired_on=old_lot.acquired_on,
                    sellable_from=event.tradable_date,
                    source_event_id=event.event_id,
                )

            return (candidate._entry(
                operation_key, event.event_id, "SHARE_DISTRIBUTION", event.effective_date,
                "SHARE_DISTRIBUTION", Decimal("0"), ((event.symbol, position_delta),), {
                    "eligible_old_shares": entitlement.total_shares,
                    "exact_new_shares": exact,
                    "allocated_new_shares": allocated_new_shares,
                    "cost_basis_before_cny": entitlement.total_cost_basis_cny,
                    "tradable_date": event.tradable_date.isoformat(),
                    "allocation_evidence": allocation_evidence or "EXACT_INTEGER_RATIO",
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "SHARE_DISTRIBUTION", payload, mutate)

    def decide_rights_offering(
        self,
        event: RightsOfferingEvent,
        *,
        disposition: RightsDisposition,
        as_of: date | str,
        allocated_rights_shares: int | None = None,
        allocation_evidence: str | None = None,
        subscription_fee_cny: Decimal | int | str,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, RightsOfferingEvent):
            raise TypeError("decide_rights_offering 只接受 RightsOfferingEvent")
        if disposition not in {"SUBSCRIBE", "DECLINE"}:
            raise ValueError("配股处置必须显式选择 SUBSCRIBE 或 DECLINE")
        day = calendar_date(as_of, "as_of")
        claim_id = f"rights:{event.event_id}"
        entitlement = self._require_entitlement(event.event_id, event.symbol, event.record_date)
        exact = Decimal(entitlement.total_shares) * event.rights_per_old_share
        max_shares = int(exact.to_integral_value(rounding=ROUND_CEILING))
        if disposition == "SUBSCRIBE":
            if not event.subscription_start_date <= day <= event.subscription_end_date:
                raise ValueError("配股认购只能在公告的认购期间内处理")
            if allocated_rights_shares is None:
                if exact != exact.to_integral_value():
                    raise ValueError("配股权利为非整数时必须提供已核实的账户层股数")
                allocated_rights_shares = int(exact)
            if isinstance(allocated_rights_shares, bool) or not isinstance(allocated_rights_shares, int):
                raise ValueError("allocated_rights_shares 必须是整数股数")
            floor_shares = int(exact.to_integral_value(rounding=ROUND_FLOOR))
            if allocated_rights_shares <= 0 or not floor_shares <= allocated_rights_shares <= max_shares:
                raise ValueError("认购股数必须是账户层精确配股权利的有效整数分配")
            if exact != exact.to_integral_value() and not str(allocation_evidence or "").strip():
                raise ValueError("非整数配股权利必须登记账户层分配依据")
        else:
            if day < event.subscription_end_date:
                raise ValueError("放弃配股只能在认购期限结束后确认权利失效")
            if allocated_rights_shares is not None:
                raise ValueError("放弃配股不能同时提供认购股数")

        fee = self._cent(decimal_value(subscription_fee_cny, "subscription_fee_cny", nonnegative=True))
        notional = (
            self._cent(event.subscription_price_cny * allocated_rights_shares + fee)
            if disposition == "SUBSCRIBE" and allocated_rights_shares is not None
            else Decimal("0")
        )
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "disposition": disposition,
            "as_of": day.isoformat(),
            "allocated_rights_shares": allocated_rights_shares,
            "allocation_evidence": allocation_evidence,
            "subscription_fee_cny": str(fee),
            "cash_out_cny": str(notional),
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            claim = candidate._claims.get(claim_id)
            if claim is None or claim.status != "RIGHTS_DISPOSITION_REQUIRED":
                raise ValueError(f"配股权利 {claim_id} 不在待处置状态")
            if disposition == "DECLINE":
                candidate._claims[claim_id] = replace(claim, status="CANCELLED")
                entry_type = "RIGHTS_DECLINED"
                cash_delta = Decimal("0")
                details = {
                    "claim_id": claim_id,
                    "disposition": disposition,
                    "expired_after_subscription_deadline": True,
                }
            else:
                if candidate._cash_cny < notional:
                    raise ValueError("配股认购后现金不足")
                candidate._cash_cny -= notional
                candidate._claims[claim_id] = replace(
                    claim,
                    final_allocated_shares=allocated_rights_shares,
                    cost_basis_cny=notional,
                    status="SUBSCRIBED_PENDING_ALLOTMENT",
                )
                entry_type = "RIGHTS_SUBSCRIBED"
                cash_delta = -notional
                details = {
                    "claim_id": claim_id,
                    "disposition": disposition,
                    "allocated_rights_shares": allocated_rights_shares,
                    "subscription_price_cny": event.subscription_price_cny,
                    "subscription_fee_cny": fee,
                    "cash_out_cny": notional,
                    "allocation_evidence": allocation_evidence or "EXACT_INTEGER_ENTITLEMENT",
                }
            return (candidate._entry(
                operation_key, event.event_id, "RIGHTS_DECISION", day,
                entry_type, cash_delta, (), {
                    **details,
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "RIGHTS_DECISION", payload, mutate)

    def allot_rights_shares(
        self,
        event: RightsOfferingEvent,
        *,
        as_of: date | str,
        final_allocated_shares: int | None = None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, RightsOfferingEvent):
            raise TypeError("allot_rights_shares 只接受 RightsOfferingEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.tradable_date:
            raise ValueError("配股新股只能在公告的可流通日登记")
        claim_id = f"rights:{event.event_id}"
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ValueError(f"配股权利不存在：{claim_id}")
        shares = claim.final_allocated_shares
        if final_allocated_shares is not None:
            if isinstance(final_allocated_shares, bool) or not isinstance(final_allocated_shares, int):
                raise ValueError("final_allocated_shares 必须是整数股数")
            if shares is not None and shares != final_allocated_shares:
                raise ValueError("最终登记股数与已确认认购股数不一致")
            shares = final_allocated_shares
        if shares is None or shares <= 0:
            raise ValueError("最终配股股数未确认，不能发放可交易新股")
        lot_id = f"rights-allotment:{event.event_id}"
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "final_allocated_shares": shares,
            "cost_basis_cny": str(claim.cost_basis_cny),
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            current_claim = candidate._claims[claim_id]
            if current_claim.status != "SUBSCRIBED_PENDING_ALLOTMENT":
                raise ValueError(f"配股权利 {claim_id} 不在待登记状态")
            if lot_id in candidate._positions:
                raise ValueError(f"配股新股批次已存在：{lot_id}")
            candidate._positions[lot_id] = PositionLot(
                lot_id=lot_id,
                symbol=event.symbol,
                total_shares=shares,
                sellable_shares=0,
                cost_basis_cny=current_claim.cost_basis_cny,
                acquired_on=event.tradable_date,
                sellable_from=event.tradable_date,
                source_event_id=event.event_id,
            )
            candidate._claims[claim_id] = replace(
                current_claim,
                final_allocated_shares=shares,
                status="SETTLED",
            )
            return (candidate._entry(
                operation_key, event.event_id, "RIGHTS_ALLOTMENT", event.tradable_date,
                "RIGHTS_SHARES_ALLOTTED", Decimal("0"), ((event.symbol, shares),), {
                    "claim_id": claim_id,
                    "lot_id": lot_id,
                    "shares": shares,
                    "cost_basis_cny": current_claim.cost_basis_cny,
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "RIGHTS_ALLOTMENT", payload, mutate)

    def record_share_swap(
        self, event: ShareSwapMergerEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareSwapMergerEvent):
            raise TypeError("record_share_swap 只接受 ShareSwapMergerEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.record_date:
            raise ValueError("换股资格快照只能在换股登记日收盘处理")
        source_details = {
            **self._prefixed_source_details(event.old_last_trade_source, "old_last_trade"),
        }
        payload = {
            "event_id": event.event_id,
            "old_symbol": event.old_symbol,
            "new_symbol": event.new_symbol,
            "old_last_trade_date": event.old_last_trade_date.isoformat(),
            "record_date": event.record_date.isoformat(),
            "old_delisting_date": event.old_delisting_date.isoformat(),
            "new_share_registration_date": event.new_share_registration_date.isoformat(),
            "new_shares_tradable_date": event.new_shares_tradable_date.isoformat(),
            "allocation_result_announcement_date": event.allocation_result_announcement_date.isoformat(),
            "share_ratio": str(event.share_ratio),
            "integer_allocation_method": event.integer_allocation_method,
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.implementation_source),
            **self._prefixed_source_details(event.old_last_trade_source, "old_last_trade"),
        }
        return self._capture_entitlement(
            event_id=event.event_id,
            symbol=event.old_symbol,
            record_date=event.record_date,
            phase="SHARE_SWAP_RECORD",
            payload=payload,
            source=event.implementation_source,
            source_details_extra=source_details,
        )

    def begin_share_swap(
        self, event: ShareSwapMergerEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareSwapMergerEvent):
            raise TypeError("begin_share_swap 只接受 ShareSwapMergerEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.old_delisting_date:
            raise ValueError("旧股换股权利只能在旧股摘牌日从持仓转出")
        entitlement = self._require_entitlement(event.event_id, event.old_symbol, event.record_date)
        claim_id = f"swap:{event.event_id}"
        source_details = {
            **self._event_source_payload(event.implementation_source),
            **self._prefixed_source_details(event.old_last_trade_source, "old_last_trade"),
        }
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "old_symbol": event.old_symbol,
            "new_symbol": event.new_symbol,
            "eligible_old_shares": entitlement.total_shares,
            "cost_basis_cny": str(entitlement.total_cost_basis_cny),
            "source_lots": [
                (lot.lot_id, lot.quantity, str(lot.cost_basis_cny), lot.acquired_on.isoformat())
                for lot in entitlement.lots
            ],
            "share_ratio": str(event.share_ratio),
            "new_shares_tradable_date": event.new_shares_tradable_date.isoformat(),
            **source_details,
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if claim_id in candidate._claims:
                raise ValueError(f"換股權利已存在：{claim_id}")
            entitled_ids = {lot.lot_id for lot in entitlement.lots}
            current_old_lots = {
                lot.lot_id: lot for lot in candidate._positions.values()
                if lot.symbol == event.old_symbol
            }
            if set(current_old_lots) != entitled_ids:
                raise ValueError("登记日后旧股持仓批次发生变化，不能安全转入换股权利")
            for entitled_lot in entitlement.lots:
                current = current_old_lots[entitled_lot.lot_id]
                if (current.total_shares != entitled_lot.quantity
                        or current.cost_basis_cny != entitled_lot.cost_basis_cny):
                    raise ValueError(
                        f"{entitled_lot.lot_id}: 登记日后旧股数量或成本变化，不能安全转入换股权利"
                    )
            for lot_id in entitled_ids:
                candidate._positions.pop(lot_id)
            exact = Decimal(entitlement.total_shares) * event.share_ratio
            if entitlement.total_shares:
                acquired_on = min(lot.acquired_on for lot in entitlement.lots)
                candidate._claims[claim_id] = NoncashClaim(
                    claim_id=claim_id,
                    event_id=event.event_id,
                    old_symbol=event.old_symbol,
                    new_symbol=event.new_symbol,
                    exact_entitlement=exact,
                    final_allocated_shares=None,
                    tradable_from=event.new_shares_tradable_date,
                    valuation_cny=None,
                    valuation_source=None,
                    status="ALLOCATION_UNRESOLVED",
                    cost_basis_cny=entitlement.total_cost_basis_cny,
                    acquired_on=acquired_on,
                )
            return (candidate._entry(
                operation_key, event.event_id, "SHARE_SWAP_DELISTING", day,
                "SHARE_SWAP_CLAIM_CREATED", Decimal("0"),
                ((event.old_symbol, -entitlement.total_shares),), {
                    "claim_id": claim_id if entitlement.total_shares else "NO_ELIGIBLE_SHARES",
                    "eligible_old_shares": entitlement.total_shares,
                    "exact_new_shares": exact,
                    "source_lots": json.dumps(
                        [
                            {
                                "lot_id": lot.lot_id,
                                "shares": lot.quantity,
                                "cost_basis_cny": str(lot.cost_basis_cny),
                                "acquired_on": lot.acquired_on.isoformat(),
                            }
                            for lot in entitlement.lots
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    "cash_delta_cny": "0",
                    "trade_or_commission_created": False,
                    **source_details,
                },
            ),)

        return self._apply_once(event.event_id, "SHARE_SWAP_DELISTING", payload, mutate)

    def confirm_share_swap_allocation(
        self,
        event: ShareSwapMergerEvent,
        *,
        as_of: date | str,
        final_allocated_new_shares: int,
        allocation_evidence: str,
        account_allocation_source: ActionSource,
        valuation_cny: Decimal | int | str | None = None,
        valuation_source: str | None = None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareSwapMergerEvent):
            raise TypeError("confirm_share_swap_allocation 只接受 ShareSwapMergerEvent")
        day = calendar_date(as_of, "as_of")
        if not event.allocation_result_announcement_date <= day <= event.new_shares_tradable_date:
            raise ValueError("账户层换股股数只能在分配结果公告日至新股可流通日确认")
        if not isinstance(account_allocation_source, ActionSource):
            raise ValueError("账户最终获配股数必须绑定账户/结算结果来源")
        entitlement = self._require_entitlement(event.event_id, event.old_symbol, event.record_date)
        exact = Decimal(entitlement.total_shares) * event.share_ratio
        floor_shares = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        ceiling_shares = int(exact.to_integral_value(rounding=ROUND_CEILING))
        if isinstance(final_allocated_new_shares, bool) or not isinstance(final_allocated_new_shares, int):
            raise ValueError("final_allocated_new_shares 必须是整数股数")
        if not floor_shares <= final_allocated_new_shares <= ceiling_shares:
            raise ValueError("账户最终获配股数不在公告换股比例的整数边界内")
        evidence = str(allocation_evidence).strip()
        if not evidence:
            raise ValueError("账户最终获配股数必须登记独立证据")
        value = None if valuation_cny is None else decimal_value(
            valuation_cny, "valuation_cny", nonnegative=True
        )
        value_source = str(valuation_source).strip() if valuation_source else None
        if value is not None and not value_source:
            raise ValueError("换股权利有估值时必须提供 valuation_source")
        claim_id = f"swap:{event.event_id}"
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "final_allocated_new_shares": final_allocated_new_shares,
            "exact_entitlement": str(exact),
            "allocation_evidence": evidence,
            "issuer_allocation_source": self._event_source_payload(event.allocation_source),
            "account_allocation_source": self._event_source_payload(account_allocation_source),
            "valuation_cny": None if value is None else str(value),
            "valuation_source": value_source,
            **self._prefixed_source_details(event.allocation_source, "allocation_result"),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            claim = candidate._claims.get(claim_id)
            if claim is None or claim.status != "ALLOCATION_UNRESOLVED":
                raise ValueError(f"換股權利 {claim_id} 不在待確認分配狀態")
            candidate._claims[claim_id] = replace(
                claim,
                final_allocated_shares=final_allocated_new_shares,
                valuation_cny=value,
                valuation_source=value_source,
                valuation_as_of=day if value is not None else None,
                status="ALLOCATED_NOT_YET_TRADABLE",
            )
            return (candidate._entry(
                operation_key, event.event_id, "SHARE_SWAP_ALLOCATION", day,
                "SHARE_SWAP_ALLOCATION_CONFIRMED", Decimal("0"), (), {
                    "claim_id": claim_id,
                    "exact_new_shares": exact,
                    "final_allocated_new_shares": final_allocated_new_shares,
                    "allocation_evidence": evidence,
                    **self._prefixed_source_details(account_allocation_source, "account_allocation"),
                    "valuation_cny": "UNKNOWN" if value is None else value,
                    "valuation_source": value_source or "UNKNOWN",
                    **self._source_details(event.allocation_source),
                },
            ),)

        return self._apply_once(event.event_id, "SHARE_SWAP_ALLOCATION", payload, mutate)

    def settle_share_swap(
        self,
        event: ShareSwapMergerEvent,
        *,
        as_of: date | str,
        final_allocated_new_shares: int | None = None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, ShareSwapMergerEvent):
            raise TypeError("settle_share_swap 只接受 ShareSwapMergerEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.new_shares_tradable_date:
            raise ValueError("换股新股只能在公告的新股可流通日登记")
        claim_id = f"swap:{event.event_id}"
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ValueError(f"换股权利不存在：{claim_id}")
        shares = claim.final_allocated_shares
        if shares is None:
            raise ValueError("账户最终获配股数未确认，不能生成可交易新股")
        if final_allocated_new_shares is not None:
            if isinstance(final_allocated_new_shares, bool) or not isinstance(final_allocated_new_shares, int):
                raise ValueError("final_allocated_new_shares 必须是整数股数")
            if final_allocated_new_shares != shares:
                raise ValueError("新股登记数量与已确认账户最终获配股数不一致")
        lot_id = f"swap:{event.event_id}:allotment"
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "final_allocated_new_shares": shares,
            "cost_basis_cny": str(claim.cost_basis_cny),
            "acquired_on": claim.acquired_on.isoformat() if claim.acquired_on else None,
            **self._prefixed_source_details(event.allocation_source, "allocation_result"),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            current_claim = candidate._claims[claim_id]
            if current_claim.status != "ALLOCATED_NOT_YET_TRADABLE":
                raise ValueError(f"換股權利 {claim_id} 尚未確認分配或已完成結算")
            if lot_id in candidate._positions:
                raise ValueError(f"換股新股批次已存在：{lot_id}")
            if shares > 0:
                candidate._positions[lot_id] = PositionLot(
                    lot_id=lot_id,
                    symbol=event.new_symbol,
                    total_shares=shares,
                    sellable_shares=0,
                    cost_basis_cny=current_claim.cost_basis_cny,
                    acquired_on=current_claim.acquired_on or event.record_date,
                    sellable_from=event.new_shares_tradable_date,
                    source_event_id=event.event_id,
                )
            candidate._claims[claim_id] = replace(
                current_claim,
                status="SETTLED",
                valuation_cny=None,
                valuation_source=None,
                valuation_as_of=None,
            )
            return (candidate._entry(
                operation_key, event.event_id, "SHARE_SWAP_SETTLEMENT", day,
                "SHARE_SWAP_SETTLED", Decimal("0"), ((event.new_symbol, shares),), {
                    "claim_id": claim_id,
                    "lot_id": lot_id if shares else "NO_NEW_SHARES_ALLOCATED",
                    "shares": shares,
                    "cost_basis_cny_transferred": current_claim.cost_basis_cny,
                    "acquired_on": current_claim.acquired_on or event.record_date,
                    "cash_delta_cny": "0",
                    "trade_or_commission_created": False,
                    "basis_method": "AGGREGATE_OLD_BASIS_ON_SWAP_DERIVED_LOT",
                    **self._source_details(event.allocation_source),
                },
            ),)

        return self._apply_once(event.event_id, "SHARE_SWAP_SETTLEMENT", payload, mutate)

    def record_delisting(
        self, event: DelistingEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, DelistingEvent):
            raise TypeError("record_delisting 只接受 DelistingEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.last_trade_date:
            raise ValueError("退市持仓快照只能在最后交易日收盘处理")
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "last_trade_date": event.last_trade_date.isoformat(),
            "delisting_date": event.delisting_date.isoformat(),
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.source),
        }
        return self._capture_entitlement(
            event_id=event.event_id,
            symbol=event.symbol,
            record_date=event.last_trade_date,
            phase="DELISTING_RECORD",
            payload=payload,
            source=event.source,
        )

    def apply_delisting(
        self,
        event: DelistingEvent,
        *,
        as_of: date | str,
        tax_cny: Decimal | int | str | None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, DelistingEvent):
            raise TypeError("apply_delisting 只接受 DelistingEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.delisting_date:
            raise ValueError("退市证券必须在公告摘牌日停止持仓估值和交易")
        entitlement = self._require_entitlement(event.event_id, event.symbol, event.last_trade_date)
        claim_id = f"delisting:{event.event_id}"
        receivable_id = claim_id
        settlement_cutoff = datetime.combine(
            day, time(15, 0), tzinfo=timezone(timedelta(hours=8))
        ).astimezone(timezone.utc)
        settlement_available = bool(
            event.settlement_known_at_utc is not None
            and event.settlement_known_at_utc <= settlement_cutoff
        )
        gross = (
            self._cent(event.cash_settlement_per_share_cny * entitlement.total_shares)
            if event.cash_settlement_per_share_cny is not None and settlement_available
            else None
        )
        tax = None if tax_cny is None else self._cent(decimal_value(tax_cny, "tax_cny", nonnegative=True))
        if gross == 0:
            tax = Decimal("0")
        if gross is not None and tax is not None and tax > gross:
            raise ValueError("退市结算税费不能大于现金结算金额")
        if event.cash_settlement_per_share_cny is None and tax_cny is not None:
            raise ValueError("缺少退市现金结算时不能单独登记税费")
        if event.cash_settlement_per_share_cny is not None and not settlement_available and tax_cny is not None:
            raise ValueError("退市结算金额在摘牌日收盘后才可得，不能提前入账税费或应收")
        source_details = self._event_source_payload(event.source)
        if settlement_available and event.settlement_source is not None:
            source_details.update(self._prefixed_source_details(event.settlement_source, "settlement"))
        payload = {
            "event_id": event.event_id,
            "symbol": event.symbol,
            "as_of": day.isoformat(),
            "shares": entitlement.total_shares,
            "cost_basis_cny": str(entitlement.total_cost_basis_cny),
            "gross_settlement_cny": None if gross is None else str(gross),
            "tax_cny": None if tax is None else str(tax),
            "settlement_available": settlement_available,
            **source_details,
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            current_lots = {
                lot.lot_id: lot for lot in candidate._positions.values() if lot.symbol == event.symbol
            }
            expected_lots = {lot.lot_id: lot for lot in entitlement.lots}
            if set(current_lots) != set(expected_lots):
                raise ValueError("最后交易日后退市持仓批次发生变化，不能安全结算")
            for lot_id, old_lot in expected_lots.items():
                current = current_lots[lot_id]
                if current.total_shares != old_lot.quantity or current.cost_basis_cny != old_lot.cost_basis_cny:
                    raise ValueError(f"{lot_id}: 退市前数量或成本与最后交易日快照不同")
            for lot_id in expected_lots:
                candidate._positions.pop(lot_id)
            if gross is not None:
                if receivable_id in candidate._receivables:
                    raise ValueError(f"退市现金应收款已存在：{receivable_id}")
                candidate._receivables[receivable_id] = CashReceivable(
                    receivable_id=receivable_id,
                    event_id=event.event_id,
                    gross_cny=gross,
                    tax_cny=tax,
                    recognized_on=event.delisting_date,
                    payable_on=event.payable_date,
                )
                claim_type = "DELISTING_CASH_RECEIVABLE"
                claim_status = "PENDING_SETTLEMENT"
            elif entitlement.total_shares:
                if claim_id in candidate._claims:
                    raise ValueError(f"退市权利已存在：{claim_id}")
                candidate._claims[claim_id] = NoncashClaim(
                    claim_id=claim_id,
                    event_id=event.event_id,
                    old_symbol=event.symbol,
                    new_symbol=None,
                    exact_entitlement=Decimal(entitlement.total_shares),
                    final_allocated_shares=entitlement.total_shares,
                    tradable_from=None,
                    valuation_cny=None,
                    valuation_source=None,
                    status="UNVALUED_DELISTED_CLAIM",
                    cost_basis_cny=entitlement.total_cost_basis_cny,
                    acquired_on=min(lot.acquired_on for lot in entitlement.lots),
                )
                claim_type = "UNVALUED_DELISTED_CLAIM"
                claim_status = "UNVALUED"
            else:
                claim_type = "DELISTING_NO_POSITION"
                claim_status = "NO_ENTITLEMENT"
            return (candidate._entry(
                operation_key, event.event_id, "DELISTING_SETTLEMENT", event.delisting_date,
                claim_type, Decimal("0"), ((event.symbol, -entitlement.total_shares),), {
                    "shares_removed": entitlement.total_shares,
                    "cost_basis_cny": entitlement.total_cost_basis_cny,
                    "cash_settlement_gross_cny": "UNKNOWN" if gross is None else gross,
                    "tax_cny": "UNKNOWN" if tax is None else tax,
                    "claim_status": claim_status,
                    "old_last_price_reused": False,
                    **source_details,
                },
            ),)

        return self._apply_once(event.event_id, "DELISTING_SETTLEMENT", payload, mutate)

    def resolve_delisted_cash_settlement(
        self,
        event: DelistingEvent,
        *,
        as_of: date | str,
        tax_cny: Decimal | int | str | None,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, DelistingEvent):
            raise TypeError("resolve_delisted_cash_settlement 只接受 DelistingEvent")
        day = calendar_date(as_of, "as_of")
        if (event.cash_settlement_per_share_cny is None or event.settlement_known_at_utc is None
                or event.payable_date is None or event.settlement_source is None):
            raise ValueError("退市现金结算缺少金额、可得时间、付款日或来源")
        known_day = event.settlement_known_at_utc.astimezone(
            timezone(timedelta(hours=8))
        ).date()
        known_local = event.settlement_known_at_utc.astimezone(
            timezone(timedelta(hours=8))
        )
        settlement_first_available_day = (
            known_day + timedelta(days=1) if known_local.time() > time(15, 0) else known_day
        )
        if day < settlement_first_available_day or day < event.delisting_date:
            raise ValueError("退市结算结果在当前会计日尚不可得")
        claim_id = f"delisting:{event.event_id}"
        gross_value = decimal_value(
            event.cash_settlement_per_share_cny, "cash_settlement_per_share_cny", nonnegative=True
        )
        tax = None if tax_cny is None else self._cent(decimal_value(tax_cny, "tax_cny", nonnegative=True))
        exact_shares = Decimal("0")
        current_claim = self._claims.get(claim_id)
        if current_claim is None or current_claim.status in {"SETTLED", "CANCELLED"}:
            raise ValueError(f"退市权利 {claim_id} 不存在或已结清")
        if current_claim.exact_entitlement is None:
            raise ValueError("退市权利缺少可核实股数")
        exact_shares = current_claim.exact_entitlement
        if exact_shares != exact_shares.to_integral_value():
            raise ValueError("退市结算持股数量不是整数股")
        gross = self._cent(gross_value * exact_shares)
        if gross == 0:
            tax = Decimal("0")
        if tax is not None and tax > gross:
            raise ValueError("退市结算税费不能大于现金结算金额")
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "shares": str(exact_shares),
            "gross_cny": str(gross),
            "tax_cny": None if tax is None else str(tax),
            "payable_date": event.payable_date.isoformat(),
            **self._prefixed_source_details(event.settlement_source, "settlement"),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            if claim_id in candidate._receivables:
                raise ValueError(f"退市现金应收款已存在：{claim_id}")
            claim = candidate._claims[claim_id]
            candidate._receivables[claim_id] = CashReceivable(
                receivable_id=claim_id,
                event_id=event.event_id,
                gross_cny=gross,
                tax_cny=tax,
                recognized_on=day,
                payable_on=event.payable_date,
            )
            candidate._claims[claim_id] = replace(
                claim,
                status="SETTLED",
                valuation_cny=None,
                valuation_source=None,
                valuation_as_of=None,
            )
            return (candidate._entry(
                operation_key, event.event_id, "DELISTING_CASH_RESOLUTION", day,
                "DELISTED_CLAIM_TO_CASH_RECEIVABLE", Decimal("0"), (), {
                    "claim_id": claim_id,
                    "shares": exact_shares,
                    "gross_cny": gross,
                    "tax_cny": "UNKNOWN" if tax is None else tax,
                    "payable_date": event.payable_date.isoformat(),
                    "old_last_price_reused": False,
                    **self._source_details(event.source),
                    **self._prefixed_source_details(event.settlement_source, "settlement"),
                },
            ),)

        return self._apply_once(event.event_id, f"DELISTING_CASH_RESOLUTION:{day.isoformat()}", payload, mutate)

    def resolve_delisted_claim(
        self,
        event: DelistingEvent,
        *,
        as_of: date | str,
        valuation_cny: Decimal | int | str,
        valuation_source: str,
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, DelistingEvent):
            raise TypeError("resolve_delisted_claim 只接受 DelistingEvent")
        day = calendar_date(as_of, "as_of")
        value = decimal_value(valuation_cny, "valuation_cny", nonnegative=True)
        source = str(valuation_source).strip()
        if not source:
            raise ValueError("退市权利估值必须登记来源")
        claim_id = f"delisting:{event.event_id}"
        payload = {
            "event_id": event.event_id,
            "claim_id": claim_id,
            "as_of": day.isoformat(),
            "valuation_cny": str(value),
            "valuation_source": source,
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            claim = candidate._claims.get(claim_id)
            if claim is None or claim.status in {"SETTLED", "CANCELLED"}:
                raise ValueError(f"退市权利 {claim_id} 不存在或已结清")
            candidate._claims[claim_id] = replace(
                claim,
                valuation_cny=value,
                valuation_source=source,
                valuation_as_of=day,
                status="VALUED_DELISTED_CLAIM",
            )
            return (candidate._entry(
                operation_key, event.event_id, "DELISTING_VALUATION", day,
                "DELISTED_CLAIM_VALUED", Decimal("0"), (), {
                    "claim_id": claim_id,
                    "valuation_cny": value,
                    "valuation_source": source,
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(
            event.event_id, f"DELISTING_VALUATION:{day.isoformat()}", payload, mutate
        )

    def apply_code_change(
        self, event: CodeChangeEvent, *, as_of: date | str
    ) -> tuple[LedgerEntry, ...]:
        if not isinstance(event, CodeChangeEvent):
            raise TypeError("apply_code_change 只接受 CodeChangeEvent")
        day = calendar_date(as_of, "as_of")
        if day != event.effective_date:
            raise ValueError("1:1 代码迁移只能在已登记的生效日执行")
        payload = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "old_symbol": event.old_symbol,
            "new_symbol": event.new_symbol,
            "effective_date": event.effective_date.isoformat(),
            "security_identity": event.security_identity,
            "share_ratio": str(event.share_ratio),
            "known_at_utc": event.known_at_utc.isoformat(),
            **self._event_source_payload(event.source),
        }

        def mutate(candidate: PortfolioLedger, operation_key: str) -> tuple[LedgerEntry, ...]:
            old_lots = [lot for lot in candidate._positions.values() if lot.symbol == event.old_symbol]
            migrated_shares = 0
            for lot in old_lots:
                candidate._positions[lot.lot_id] = replace(lot, symbol=event.new_symbol)
                migrated_shares += lot.total_shares
            return (candidate._entry(
                operation_key, event.event_id, "IDENTITY_MIGRATION", event.effective_date,
                "CODE_CHANGE_1_TO_1", Decimal("0"), (
                    (event.old_symbol, -migrated_shares), (event.new_symbol, migrated_shares)
                ), {
                    "security_identity": event.security_identity,
                    "migrated_lots": len(old_lots),
                    "migrated_shares": migrated_shares,
                    "ratio": "1:1",
                    **self._source_details(event.source),
                },
            ),)

        return self._apply_once(event.event_id, "IDENTITY_MIGRATION", payload, mutate)

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
        latest_booking = max((entry.booking_date for entry in self._journal), default=None)
        if latest_booking is not None and day < latest_booking:
            raise ValueError("估值日早于账本最新过账日；当前状态不能用于历史回算")
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
            elif claim.valuation_as_of != day:
                reasons.append(f"{claim.claim_id}: 权利估值日期与估值日不一致")
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
