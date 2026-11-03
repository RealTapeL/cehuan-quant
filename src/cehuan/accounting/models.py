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
    tax_treatment: Literal[
        "FINAL_TAX_KNOWN", "DEFERRED_BY_HOLDING_PERIOD", "ACCOUNT_EVIDENCE_REQUIRED"
    ] | None = None
    eligible_shares: int | None = None
    payment_withholding_per_share_cny: Decimal | None = None
    payment_withheld_tax_cny: Decimal | None = None
    payment_withholding_is_account_evidence: bool = False
    payment_evidence_id: str | None = None
    payment_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        if not str(self.receivable_id).strip() or not str(self.event_id).strip():
            raise ValueError("应收款 receivable_id/event_id 不能为空")
        gross = decimal_value(self.gross_cny, "gross_cny", nonnegative=True)
        tax = None if self.tax_cny is None else decimal_value(self.tax_cny, "tax_cny", nonnegative=True)
        if tax is not None and tax > gross:
            raise ValueError("tax_cny 不能大于 gross_cny")
        if self.status not in {"PENDING", "SETTLED", "CANCELLED"}:
            raise ValueError("应收状态必须是 PENDING/SETTLED/CANCELLED")
        tax_treatment = self.tax_treatment or (
            "FINAL_TAX_KNOWN" if tax is not None else "ACCOUNT_EVIDENCE_REQUIRED"
        )
        if tax_treatment not in {
            "FINAL_TAX_KNOWN", "DEFERRED_BY_HOLDING_PERIOD", "ACCOUNT_EVIDENCE_REQUIRED",
        }:
            raise ValueError("未知分红税务处理状态")
        if tax_treatment == "FINAL_TAX_KNOWN" and tax is None:
            raise ValueError("FINAL_TAX_KNOWN 必须提供税额")
        eligible_shares = None if self.eligible_shares is None else share_count(
            self.eligible_shares, "eligible_shares", allow_zero=True
        )
        withholding_per_share = (
            None if self.payment_withholding_per_share_cny is None
            else decimal_value(self.payment_withholding_per_share_cny, "payment_withholding_per_share_cny", nonnegative=True)
        )
        payment_withheld = (
            None if self.payment_withheld_tax_cny is None
            else decimal_value(self.payment_withheld_tax_cny, "payment_withheld_tax_cny", nonnegative=True)
        )
        evidence_sha256 = self.payment_evidence_sha256
        if evidence_sha256 is not None:
            evidence_sha256 = str(evidence_sha256).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
                raise ValueError("payment_evidence_sha256 必须为 SHA-256")
        if self.payment_withholding_is_account_evidence and (
            payment_withheld is None or not self.payment_evidence_id or evidence_sha256 is None
        ):
            raise ValueError("实际到账税费金额必须绑定账户证据编号和 SHA-256")
        if tax_treatment == "DEFERRED_BY_HOLDING_PERIOD" and (
            eligible_shares is None or withholding_per_share is None
        ):
            raise ValueError("递延税务状态必须记录享有分红的股数与发放日每股预扣规则")
        object.__setattr__(self, "receivable_id", str(self.receivable_id).strip())
        object.__setattr__(self, "event_id", str(self.event_id).strip())
        object.__setattr__(self, "gross_cny", gross)
        object.__setattr__(self, "tax_cny", tax)
        object.__setattr__(self, "recognized_on", calendar_date(self.recognized_on, "recognized_on"))
        object.__setattr__(self, "payable_on", calendar_date(self.payable_on, "payable_on", optional=True))
        object.__setattr__(self, "tax_treatment", tax_treatment)
        object.__setattr__(self, "eligible_shares", eligible_shares)
        object.__setattr__(self, "payment_withholding_per_share_cny", withholding_per_share)
        object.__setattr__(self, "payment_withheld_tax_cny", payment_withheld)
        object.__setattr__(self, "payment_evidence_id", str(self.payment_evidence_id).strip() if self.payment_evidence_id else None)
        object.__setattr__(self, "payment_evidence_sha256", evidence_sha256)


@dataclass(frozen=True)
class DividendTaxLotRecord:
    record_id: str
    event_id: str
    symbol: str
    lot_id: str
    acquired_on: date
    entitled_shares: int
    remaining_shares: int
    sold_shares: int
    gross_dividend_cny: Decimal
    tax_treatment: Literal[
        "ACCOUNT_EVIDENCE_REQUIRED", "FINAL_TAX_KNOWN", "DEFERRED_BY_HOLDING_PERIOD"
    ] = "ACCOUNT_EVIDENCE_REQUIRED"
    payment_withheld_tax_cny: Decimal | None = None
    payment_withholding_is_account_evidence: bool = False
    final_tax_cny: Decimal | None = None
    tax_reconciled_shares: int = 0
    tax_status: Literal[
        "ENTITLEMENT_RECORDED", "ACCRUED", "RECEIVED_TAX_PENDING",
        "PARTIALLY_SOLD_TAX_PENDING", "SOLD_TAX_PENDING_REVIEW", "FINAL_TAX_MODELED", "RECONCILED",
    ] = "ENTITLEMENT_RECORDED"
    policy_source_id: str | None = None
    policy_source_sha256: str | None = None
    account_evidence_id: str | None = None
    account_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        for field in ("record_id", "event_id", "lot_id"):
            if not str(getattr(self, field)).strip():
                raise ValueError(f"DividendTaxLotRecord.{field} 不能为空")
            object.__setattr__(self, field, str(getattr(self, field)).strip())
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "DividendTaxLotRecord.symbol"))
        object.__setattr__(self, "acquired_on", calendar_date(self.acquired_on, "DividendTaxLotRecord.acquired_on"))
        entitled = share_count(self.entitled_shares, "entitled_shares", allow_zero=True)
        remaining = share_count(self.remaining_shares, "remaining_shares", allow_zero=True)
        sold = share_count(self.sold_shares, "sold_shares", allow_zero=True)
        reconciled = share_count(self.tax_reconciled_shares, "tax_reconciled_shares", allow_zero=True)
        if remaining + sold != entitled:
            raise ValueError("DividendTaxLotRecord.remaining_shares + sold_shares 必须等于 entitled_shares")
        if reconciled > sold:
            raise ValueError("tax_reconciled_shares 不能大于 sold_shares")
        gross = decimal_value(self.gross_dividend_cny, "gross_dividend_cny", nonnegative=True)
        withheld = None if self.payment_withheld_tax_cny is None else decimal_value(
            self.payment_withheld_tax_cny, "payment_withheld_tax_cny", nonnegative=True
        )
        final_tax = None if self.final_tax_cny is None else decimal_value(
            self.final_tax_cny, "final_tax_cny", nonnegative=True
        )
        if final_tax is not None and final_tax > gross:
            raise ValueError("DividendTaxLotRecord.final_tax_cny 不能大于税前分红")
        if self.tax_treatment not in {
            "ACCOUNT_EVIDENCE_REQUIRED", "FINAL_TAX_KNOWN", "DEFERRED_BY_HOLDING_PERIOD",
        }:
            raise ValueError("未知 DividendTaxLotRecord.tax_treatment")
        if self.tax_status not in {
            "ENTITLEMENT_RECORDED", "ACCRUED", "RECEIVED_TAX_PENDING",
            "PARTIALLY_SOLD_TAX_PENDING", "SOLD_TAX_PENDING_REVIEW", "FINAL_TAX_MODELED", "RECONCILED",
        }:
            raise ValueError("未知 DividendTaxLotRecord.tax_status")
        policy_sha = self.policy_source_sha256
        evidence_sha = self.account_evidence_sha256
        for field, value in (("policy_source_sha256", policy_sha), ("account_evidence_sha256", evidence_sha)):
            if value is not None:
                value = str(value).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError(f"DividendTaxLotRecord.{field} 必须为 SHA-256")
                if field == "policy_source_sha256":
                    policy_sha = value
                else:
                    evidence_sha = value
        if self.tax_status == "RECONCILED" and (
            final_tax is None or not self.account_evidence_id or evidence_sha is None
        ):
            raise ValueError("RECONCILED 税务记录必须有税额和账户证据")
        if self.payment_withholding_is_account_evidence and (
            withheld is None or not self.account_evidence_id or evidence_sha is None
        ):
            raise ValueError("批次实际发放日预扣金额必须绑定账户证据编号和 SHA-256")
        if self.tax_status == "FINAL_TAX_MODELED" and final_tax is None:
            raise ValueError("FINAL_TAX_MODELED 必须保存情景税额")
        object.__setattr__(self, "entitled_shares", entitled)
        object.__setattr__(self, "remaining_shares", remaining)
        object.__setattr__(self, "sold_shares", sold)
        object.__setattr__(self, "tax_reconciled_shares", reconciled)
        object.__setattr__(self, "gross_dividend_cny", gross)
        object.__setattr__(self, "payment_withheld_tax_cny", withheld)
        object.__setattr__(self, "final_tax_cny", final_tax)
        object.__setattr__(self, "policy_source_id", str(self.policy_source_id).strip() if self.policy_source_id else None)
        object.__setattr__(self, "policy_source_sha256", policy_sha)
        object.__setattr__(self, "account_evidence_id", str(self.account_evidence_id).strip() if self.account_evidence_id else None)
        object.__setattr__(self, "account_evidence_sha256", evidence_sha)


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
    valuation_as_of: date | None = None
    lot_allocations: tuple[tuple[str, int, Decimal, date], ...] = ()
    next_phase_date: date | None = None
    source_id: str | None = None
    source_sha256: str | None = None

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
        valuation_as_of = calendar_date(self.valuation_as_of, "valuation_as_of", optional=True)
        next_phase_date = calendar_date(self.next_phase_date, "next_phase_date", optional=True)
        basis = decimal_value(self.cost_basis_cny, "cost_basis_cny", nonnegative=True)
        lot_allocations: list[tuple[str, int, Decimal, date]] = []
        for allocation in self.lot_allocations:
            if not isinstance(allocation, tuple) or len(allocation) != 4:
                raise ValueError("lot_allocations 每项必须是 (lot_id, shares, cost_basis_cny, acquired_on)")
            lot_id, shares, lot_basis, acquired = allocation
            lot_id = str(lot_id).strip()
            if not lot_id:
                raise ValueError("lot_allocations 的 lot_id 不能为空")
            shares = share_count(shares, "lot_allocations.shares", allow_zero=True)
            lot_basis = decimal_value(lot_basis, "lot_allocations.cost_basis_cny", nonnegative=True)
            acquired = calendar_date(acquired, "lot_allocations.acquired_on")
            lot_allocations.append((lot_id, shares, lot_basis, acquired))
        if len({item[0] for item in lot_allocations}) != len(lot_allocations):
            raise ValueError("lot_allocations 的 lot_id 不能重复")
        if lot_allocations and sum(item[1] for item in lot_allocations) != (allocated or 0):
            raise ValueError("lot_allocations 股数与 final_allocated_shares 不一致")
        if lot_allocations and sum((item[2] for item in lot_allocations), start=Decimal("0")) != basis:
            raise ValueError("lot_allocations 成本与 claim 成本不一致")
        if valuation is not None and not str(self.valuation_source or "").strip():
            raise ValueError("有估值的权利必须登记 valuation_source")
        if valuation is not None and valuation_as_of is None:
            raise ValueError("有估值的权利必须登记 valuation_as_of")
        if valuation is None and valuation_as_of is not None:
            raise ValueError("未估值权利不能登记 valuation_as_of")
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
        object.__setattr__(self, "valuation_as_of", valuation_as_of)
        object.__setattr__(self, "lot_allocations", tuple(lot_allocations))
        object.__setattr__(self, "next_phase_date", next_phase_date)
        object.__setattr__(self, "source_id", str(self.source_id).strip() if self.source_id else None)
        source_sha256 = str(self.source_sha256).strip().lower() if self.source_sha256 else None
        if source_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            raise ValueError("NoncashClaim.source_sha256 必须是 SHA-256")
        object.__setattr__(self, "source_sha256", source_sha256)
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
    status: Literal["VALUED", "VALUED_WITH_UNRESOLVED_DIVIDEND_TAX", "UNVALUED"]
    valuation_date: date
    nav_cny: Decimal | None
    cash_cny: Decimal
    positions_value_cny: Decimal | None
    receivables_value_cny: Decimal | None
    claims_value_cny: Decimal | None
    stale_price_count: int
    reasons: tuple[str, ...]
    unresolved_dividend_tax_count: int = 0
    valuation_tax_scope: str = "NET_OF_KNOWN_TAX_ONLY"


@dataclass(frozen=True)
class LedgerSnapshot:
    portfolio_id: str
    cash_cny: Decimal
    positions: tuple[PositionLot, ...]
    receivables: tuple[CashReceivable, ...]
    claims: tuple[NoncashClaim, ...]
    journal: tuple[LedgerEntry, ...]
    entitlements: tuple[EntitlementSnapshot, ...] = ()
    dividend_tax_records: tuple[DividendTaxLotRecord, ...] = ()
    as_of_date: date | None = None

    def __post_init__(self) -> None:
        portfolio_id = str(self.portfolio_id).strip()
        if not portfolio_id:
            raise ValueError("LedgerSnapshot.portfolio_id 不能为空")
        object.__setattr__(self, "portfolio_id", portfolio_id)
        object.__setattr__(self, "cash_cny", decimal_value(self.cash_cny, "snapshot.cash_cny", nonnegative=True))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "receivables", tuple(self.receivables))
        object.__setattr__(self, "claims", tuple(self.claims))
        object.__setattr__(self, "journal", tuple(self.journal))
        object.__setattr__(self, "entitlements", tuple(self.entitlements))
        object.__setattr__(self, "dividend_tax_records", tuple(self.dividend_tax_records))
        as_of = calendar_date(self.as_of_date, "snapshot.as_of_date", optional=True)
        if as_of is not None and any(entry.booking_date > as_of for entry in self.journal):
            raise ValueError("LedgerSnapshot.journal 含晚于 as_of_date 的账务流水")
        object.__setattr__(self, "as_of_date", as_of)
