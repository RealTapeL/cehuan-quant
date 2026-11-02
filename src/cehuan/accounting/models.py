from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from cehuan.data.canonical import canonical_symbol


Side = Literal["BUY", "SELL"]


def decimal_value(value: Decimal | int | str, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} 必须使用 Decimal、整数或十进制字符串，不能传 float")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是有效十进制数") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{field} 必须是有限非负数" if nonnegative else f"{field} 必须是有限数")
    return result


def share_count(value: int | Decimal | str, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} 必须是整数股数，不能传 bool/float")
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError(f"{field} 必须为整数股数")
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value.strip())
    else:
        raise ValueError(f"{field} 必须为整数股数")
    if result < 0 or (result == 0 and not allow_zero):
        qualifier = "非负" if allow_zero else "正"
        raise ValueError(f"{field} 必须为{qualifier}整数股数")
    return result


def calendar_date(value: date | str, field: str, *, optional: bool = False) -> date | None:
    if value is None and optional:
        return None
    if isinstance(value, datetime):
        raise ValueError(f"{field} 需要 date，不接受带时区/时间的 datetime")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} 必须是 ISO 日期 YYYY-MM-DD") from exc
        if parsed.isoformat() == value:
            return parsed
    raise ValueError(f"{field} 必须是 ISO 日期 YYYY-MM-DD")


def security_symbol(value: object, field: str) -> str:
    symbol = canonical_symbol(value)
    if not re.fullmatch(r"(?:SH|SZ|BJ)\d{6}", symbol):
        raise ValueError(f"{field} 必须是带沪深北交易所前缀的六位证券代码")
    return symbol


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    symbol: str
    total_shares: int
    sellable_shares: int
    cost_basis_cny: Decimal
    acquired_on: date
    sellable_from: date
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        lot_id = str(self.lot_id).strip()
        if not lot_id:
            raise ValueError("lot_id 不能为空")
        symbol = security_symbol(self.symbol, "PositionLot.symbol")
        total = share_count(self.total_shares, "total_shares")
        sellable = share_count(self.sellable_shares, "sellable_shares", allow_zero=True)
        if sellable > total:
            raise ValueError("sellable_shares 不能大于 total_shares")
        basis = decimal_value(self.cost_basis_cny, "cost_basis_cny", nonnegative=True)
        acquired_on = calendar_date(self.acquired_on, "acquired_on")
        sellable_from = calendar_date(self.sellable_from, "sellable_from")
        if sellable_from < acquired_on:
            raise ValueError("sellable_from 不能早于取得日")
        object.__setattr__(self, "lot_id", lot_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "total_shares", total)
        object.__setattr__(self, "sellable_shares", sellable)
        object.__setattr__(self, "cost_basis_cny", basis)
        object.__setattr__(self, "acquired_on", acquired_on)
        object.__setattr__(self, "sellable_from", sellable_from)
        if self.source_event_id is not None:
            object.__setattr__(self, "source_event_id", str(self.source_event_id).strip() or None)

    def sellable_on(self, trade_date: date) -> int:
        day = calendar_date(trade_date, "trade_date")
        if day >= self.sellable_from:
            return self.total_shares
        return self.sellable_shares


@dataclass(frozen=True)
class FeeItem:
    name: str
    amount_cny: Decimal

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ValueError("费用项目 name 不能为空")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "amount_cny", decimal_value(self.amount_cny, "amount_cny", nonnegative=True))


@dataclass(frozen=True)
class TradeFill:
    fill_id: str
    trade_date: date
    symbol: str
    side: Side
    quantity: int
    price_cny: Decimal
    fees: tuple[FeeItem, ...] = ()
    sellable_from: date | None = None

    def __post_init__(self) -> None:
        fill_id = str(self.fill_id).strip()
        if not fill_id:
            raise ValueError("fill_id 不能为空")
        side = str(self.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("fill side 只能是 BUY 或 SELL")
        fees = tuple(self.fees)
        if not all(isinstance(item, FeeItem) for item in fees):
            raise ValueError("fees 必须由 FeeItem 构成")
        if len({item.name for item in fees}) != len(fees):
            raise ValueError("同一成交的费用项目 name 不能重复")
        trade_date = calendar_date(self.trade_date, "trade_date")
        sellable_from = calendar_date(self.sellable_from, "sellable_from", optional=True)
        price = decimal_value(self.price_cny, "price_cny")
        if price <= 0:
            raise ValueError("成交价格必须大于 0")
        if side == "BUY" and sellable_from is None:
            raise ValueError("BUY 成交必须提供有效日期计算后的 sellable_from")
        if sellable_from is not None and sellable_from <= trade_date:
            raise ValueError("买入股数的 sellable_from 必须晚于成交日（T+1）")
        object.__setattr__(self, "fill_id", fill_id)
        object.__setattr__(self, "trade_date", trade_date)
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "TradeFill.symbol"))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", share_count(self.quantity, "quantity"))
        object.__setattr__(self, "price_cny", price)
        object.__setattr__(self, "fees", fees)
        object.__setattr__(self, "sellable_from", sellable_from)


@dataclass(frozen=True)
class CashReceivable:
    receivable_id: str
    event_id: str
    gross_cny: Decimal
    tax_cny: Decimal | None
    recognized_on: date
    payable_on: date | None
    status: Literal["PENDING", "SETTLED", "CANCELLED"] = "PENDING"

    def __post_init__(self) -> None:
        if not str(self.receivable_id).strip() or not str(self.event_id).strip():
            raise ValueError("应收款 receivable_id/event_id 不能为空")
        gross = decimal_value(self.gross_cny, "gross_cny", nonnegative=True)
        tax = None if self.tax_cny is None else decimal_value(self.tax_cny, "tax_cny", nonnegative=True)
        if tax is not None and tax > gross:
            raise ValueError("tax_cny 不能大于 gross_cny")
        if self.status not in {"PENDING", "SETTLED", "CANCELLED"}:
            raise ValueError("应收状态必须是 PENDING/SETTLED/CANCELLED")
        object.__setattr__(self, "receivable_id", str(self.receivable_id).strip())
        object.__setattr__(self, "event_id", str(self.event_id).strip())
        object.__setattr__(self, "gross_cny", gross)
        object.__setattr__(self, "tax_cny", tax)
        object.__setattr__(self, "recognized_on", calendar_date(self.recognized_on, "recognized_on"))
        object.__setattr__(self, "payable_on", calendar_date(self.payable_on, "payable_on", optional=True))


@dataclass(frozen=True)
class NoncashClaim:
    claim_id: str
    event_id: str
    old_symbol: str
    new_symbol: str | None
    exact_entitlement: Decimal | None
    final_allocated_shares: int | None
    tradable_from: date | None
    valuation_cny: Decimal | None
    valuation_source: str | None
    status: str
    cost_basis_cny: Decimal = Decimal("0")
    acquired_on: date | None = None

    def __post_init__(self) -> None:
        if not str(self.claim_id).strip() or not str(self.event_id).strip():
            raise ValueError("权利 claim_id/event_id 不能为空")
        exact = None if self.exact_entitlement is None else decimal_value(
            self.exact_entitlement, "exact_entitlement", nonnegative=True
        )
        allocated = None if self.final_allocated_shares is None else share_count(
            self.final_allocated_shares, "final_allocated_shares", allow_zero=True
        )
        valuation = None if self.valuation_cny is None else decimal_value(
            self.valuation_cny, "valuation_cny", nonnegative=True
        )
        basis = decimal_value(self.cost_basis_cny, "cost_basis_cny", nonnegative=True)
        if valuation is not None and not str(self.valuation_source or "").strip():
            raise ValueError("有估值的权利必须登记 valuation_source")
        object.__setattr__(self, "claim_id", str(self.claim_id).strip())
        object.__setattr__(self, "event_id", str(self.event_id).strip())
        object.__setattr__(self, "old_symbol", security_symbol(self.old_symbol, "old_symbol"))
        object.__setattr__(self, "new_symbol", security_symbol(self.new_symbol, "new_symbol") if self.new_symbol else None)
        object.__setattr__(self, "exact_entitlement", exact)
        object.__setattr__(self, "final_allocated_shares", allocated)
        object.__setattr__(self, "tradable_from", calendar_date(self.tradable_from, "tradable_from", optional=True))
        object.__setattr__(self, "valuation_cny", valuation)
        object.__setattr__(self, "valuation_source", str(self.valuation_source).strip() if self.valuation_source else None)
        object.__setattr__(self, "cost_basis_cny", basis)
        object.__setattr__(self, "acquired_on", calendar_date(self.acquired_on, "acquired_on", optional=True))
        status = str(self.status).strip()
        if not status:
            raise ValueError("权利状态不能为空")
        object.__setattr__(self, "status", status)


@dataclass(frozen=True)
class PriceMark:
    price_cny: Decimal
    price_date: date
    source: str
    age_sessions: int

    def __post_init__(self) -> None:
        price = decimal_value(self.price_cny, "price_cny")
        if price <= 0:
            raise ValueError("估值价格必须大于 0")
        if isinstance(self.age_sessions, bool) or not isinstance(self.age_sessions, int) or self.age_sessions < 0:
            raise ValueError("age_sessions 必须是非负整数")
        source = str(self.source).strip()
        if not source:
            raise ValueError("价格估值必须登记来源")
        object.__setattr__(self, "price_cny", price)
        object.__setattr__(self, "price_date", calendar_date(self.price_date, "price_date"))
        object.__setattr__(self, "source", source)


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    operation_key: str
    event_id: str
    phase: str
    booking_date: date
    entry_type: str
    cash_delta_cny: Decimal
    position_deltas: tuple[tuple[str, int], ...]
    details: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class EntitledLot:
    lot_id: str
    quantity: int
    cost_basis_cny: Decimal
    acquired_on: date
    sellable_from: date

    def __post_init__(self) -> None:
        lot_id = str(self.lot_id).strip()
        if not lot_id:
            raise ValueError("EntitledLot.lot_id 不能为空")
        quantity = share_count(self.quantity, "EntitledLot.quantity")
        basis = decimal_value(self.cost_basis_cny, "EntitledLot.cost_basis_cny", nonnegative=True)
        acquired = calendar_date(self.acquired_on, "EntitledLot.acquired_on")
        sellable = calendar_date(self.sellable_from, "EntitledLot.sellable_from")
        if sellable < acquired:
            raise ValueError("EntitledLot.sellable_from 不能早于 acquired_on")
        object.__setattr__(self, "lot_id", lot_id)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cost_basis_cny", basis)
        object.__setattr__(self, "acquired_on", acquired)
        object.__setattr__(self, "sellable_from", sellable)


@dataclass(frozen=True)
class EntitlementSnapshot:
    event_id: str
    symbol: str
    captured_on: date
    lots: tuple[EntitledLot, ...]

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        if not event_id:
            raise ValueError("EntitlementSnapshot.event_id 不能为空")
        lots = tuple(self.lots)
        if not all(isinstance(lot, EntitledLot) for lot in lots):
            raise ValueError("EntitlementSnapshot.lots 必须由 EntitledLot 构成")
        if len({lot.lot_id for lot in lots}) != len(lots):
            raise ValueError("EntitlementSnapshot 中 lot_id 不能重复")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "EntitlementSnapshot.symbol"))
        object.__setattr__(self, "captured_on", calendar_date(self.captured_on, "EntitlementSnapshot.captured_on"))
        object.__setattr__(self, "lots", lots)

    @property
    def total_shares(self) -> int:
        return sum(lot.quantity for lot in self.lots)

    @property
    def total_cost_basis_cny(self) -> Decimal:
        return sum((lot.cost_basis_cny for lot in self.lots), start=Decimal("0"))


@dataclass(frozen=True)
class NavResult:
    status: Literal["VALUED", "UNVALUED"]
    valuation_date: date
    nav_cny: Decimal | None
    cash_cny: Decimal
    positions_value_cny: Decimal | None
    receivables_value_cny: Decimal | None
    claims_value_cny: Decimal | None
    stale_price_count: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class LedgerSnapshot:
    portfolio_id: str
    cash_cny: Decimal
    positions: tuple[PositionLot, ...]
    receivables: tuple[CashReceivable, ...]
    claims: tuple[NoncashClaim, ...]
    journal: tuple[LedgerEntry, ...]
    entitlements: tuple[EntitlementSnapshot, ...] = ()
