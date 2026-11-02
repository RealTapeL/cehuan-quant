from datetime import date
from decimal import Decimal

import pytest

from cehuan.accounting import (
    ActionSource,
    CashDividendEvent,
    CodeChangeEvent,
    PortfolioLedger,
    PriceMark,
    RightsOfferingEvent,
    ShareDistributionEvent,
    TradeFill,
    PositionLot,
)


def _source() -> ActionSource:
    return ActionSource(
        source_id="synthetic-notice-1",
        url="https://example.invalid/notice.pdf",
        sha256="a" * 64,
        title="Synthetic corporate action notice",
        page_reference="p.1",
        reviewed_by="synthetic reviewer",
        reviewed_at="2026-11-02",
    )


def _lot(lot_id: str = "lot-1", *, shares: int = 100, basis: str = "1000") -> PositionLot:
    return PositionLot(
        lot_id=lot_id,
        symbol="SH600000",
        total_shares=shares,
        sellable_shares=shares,
        cost_basis_cny=Decimal(basis),
        acquired_on=date(2026, 1, 5),
        sellable_from=date(2026, 1, 5),
    )


def _price(price: str, day: str) -> PriceMark:
    return PriceMark(Decimal(price), date.fromisoformat(day), "synthetic fixture", 0)


def _dividend() -> CashDividendEvent:
    return CashDividendEvent(
        event_id="div-1",
        symbol="SH600000",
        record_date="2026-10-29",
        ex_date="2026-10-30",
        payable_date="2026-11-10",
        gross_per_share_cny=Decimal("0.50"),
        known_at_utc="2026-10-20T08:00:00Z",
        source=_source(),
    )


def _distribution(*, ratio: str = "0.3") -> ShareDistributionEvent:
    return ShareDistributionEvent(
        event_id="dist-1",
        symbol="SH600000",
        record_date="2026-10-29",
        effective_date="2026-10-30",
        tradable_date="2026-11-05",
        shares_per_old_share=Decimal(ratio),
        known_at_utc="2026-10-20T08:00:00Z",
        source=_source(),
    )


def _rights() -> RightsOfferingEvent:
    return RightsOfferingEvent(
        event_id="rights-1",
        symbol="SH600000",
        record_date="2026-10-29",
        subscription_start_date="2026-10-30",
        subscription_end_date="2026-11-03",
        tradable_date="2026-11-10",
        rights_per_old_share=Decimal("0.2"),
        subscription_price_cny=Decimal("5"),
        known_at_utc="2026-10-20T08:00:00Z",
        source=_source(),
    )


def test_dividend_accrual_and_payment_do_not_double_count_nav():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _dividend()

    with pytest.raises(ValueError, match="登记日收盘"):
        ledger.record_cash_dividend(event, as_of="2026-10-28")
    ledger.record_cash_dividend(event, as_of="2026-10-29")
    assert ledger.mark_to_market(
        "2026-10-29", {"SH600000": _price("10", "2026-10-29")}
    ).nav_cny == Decimal("1500")

    accrual = ledger.accrue_cash_dividend(event, as_of="2026-10-30", tax_cny=Decimal("0"))
    assert accrual[0].cash_delta_cny == Decimal("0")
    ex_date_nav = ledger.mark_to_market(
        "2026-10-30", {"SH600000": _price("9.50", "2026-10-30")}
    )
    assert ex_date_nav.nav_cny == Decimal("1500.00")
    with pytest.raises(ValueError, match="尚未到可确认到账日"):
        ledger.settle_receivable("dividend:div-1", settled_on="2026-11-09")

    first = ledger.settle_receivable("dividend:div-1", settled_on="2026-11-10")
    second = ledger.settle_receivable("dividend:div-1", settled_on="2026-11-10")
    assert first == second
    assert ledger.cash_cny == Decimal("550.00")
    assert ledger.mark_to_market(
        "2026-11-10", {"SH600000": _price("9.50", "2026-11-10")}
    ).nav_cny == Decimal("1500.00")


def test_unknown_dividend_tax_blocks_nav_and_cash_settlement():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _dividend()
    ledger.record_cash_dividend(event, as_of=event.record_date)
    ledger.accrue_cash_dividend(event, as_of=event.ex_date, tax_cny=None)

    nav = ledger.mark_to_market(
        event.ex_date, {"SH600000": _price("9.50", event.ex_date.isoformat())}
    )
    assert nav.status == "UNVALUED" and nav.nav_cny is None
    with pytest.raises(ValueError, match="税费未知"):
        ledger.settle_receivable("dividend:div-1", settled_on="2026-11-10")


def test_share_distribution_preserves_cost_and_blocks_trading_new_shares_until_date():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _distribution()
    ledger.record_share_distribution(event, as_of=event.record_date)
    (entry,) = ledger.apply_share_distribution(event, as_of=event.effective_date)
    assert ledger.apply_share_distribution(event, as_of=event.effective_date) == (entry,)

    assert entry.cash_delta_cny == Decimal("0")
    assert sum(lot.total_shares for lot in ledger.positions) == 130
    assert sum((lot.cost_basis_cny for lot in ledger.positions), Decimal("0")) == Decimal("1000")
    new_lot = next(lot for lot in ledger.positions if lot.source_event_id == event.event_id)
    assert new_lot.total_shares == 30
    assert new_lot.sellable_on(date(2026, 11, 4)) == 0
    assert new_lot.sellable_on(date(2026, 11, 5)) == 30

    with pytest.raises(ValueError, match="可卖股数不足"):
        ledger.apply_fill(TradeFill(
            fill_id="too-early-distribution-sale",
            trade_date="2026-11-04",
            symbol="SH600000",
            side="SELL",
            quantity=101,
            price_cny=Decimal("8"),
        ))
    assert ledger.cash_cny == Decimal("500")
    assert len(ledger.journal) == 2


def test_share_distribution_preserves_separate_existing_lots_and_total_basis():
    first = _lot("lot-a", shares=50, basis="450")
    second = PositionLot(
        lot_id="lot-b", symbol="SH600000", total_shares=50, sellable_shares=50,
        cost_basis_cny=Decimal("550"), acquired_on="2026-02-01", sellable_from="2026-02-02",
    )
    ledger = PortfolioLedger(Decimal("0"), positions=(first, second))
    event = _distribution()
    ledger.record_share_distribution(event, as_of=event.record_date)
    ledger.apply_share_distribution(event, as_of=event.effective_date)

    assert {lot.lot_id for lot in ledger.positions} == {
        "lot-a", "lot-b", "action:dist-1:lot-a", "action:dist-1:lot-b"
    }
    assert sum(lot.total_shares for lot in ledger.positions) == 130
    assert sum((lot.cost_basis_cny for lot in ledger.positions), Decimal("0")) == Decimal("1000")


def test_fractional_share_distribution_requires_account_result_and_evidence():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _distribution(ratio="0.1339")
    ledger.record_share_distribution(event, as_of=event.record_date)

    with pytest.raises(ValueError, match="账户层最终获配股数"):
        ledger.apply_share_distribution(event, as_of=event.effective_date)
    with pytest.raises(ValueError, match="分配依据"):
        ledger.apply_share_distribution(
            event, as_of=event.effective_date, allocated_new_shares=13
        )
    with pytest.raises(ValueError, match="整数边界"):
        ledger.apply_share_distribution(
            event, as_of=event.effective_date, allocated_new_shares=12,
            allocation_evidence="synthetic final allocation",
        )
    ledger.apply_share_distribution(
        event, as_of=event.effective_date, allocated_new_shares=13,
        allocation_evidence="synthetic account allocation record",
    )
    assert sum(lot.total_shares for lot in ledger.positions) == 113


def test_rights_must_be_explicitly_subscribed_or_declined():
    event = _rights()
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    ledger.record_rights_offering(event, as_of=event.record_date)
    assert ledger.mark_to_market(
        event.record_date, {"SH600000": _price("10", event.record_date.isoformat())}
    ).status == "UNVALUED"

    with pytest.raises(ValueError, match="认购期间"):
        ledger.decide_rights_offering(
            event, disposition="SUBSCRIBE", as_of="2026-11-04",
            subscription_fee_cny=Decimal("0"),
        )
    with pytest.raises(ValueError, match="期限结束"):
        ledger.decide_rights_offering(
            event, disposition="DECLINE", as_of="2026-10-30",
            subscription_fee_cny=Decimal("0"),
        )

    ledger.decide_rights_offering(
        event, disposition="DECLINE", as_of="2026-11-03",
        subscription_fee_cny=Decimal("0"),
    )
    assert ledger.mark_to_market(
        "2026-11-03", {"SH600000": _price("10", "2026-11-03")}
    ).nav_cny == Decimal("1500")


def test_rights_subscription_is_cash_out_then_separate_untradable_claim_then_lot():
    event = _rights()
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    ledger.record_rights_offering(event, as_of=event.record_date)
    ledger.decide_rights_offering(
        event, disposition="SUBSCRIBE", as_of=event.subscription_start_date,
        subscription_fee_cny=Decimal("0.25"),
    )
    assert ledger.cash_cny == Decimal("399.75")
    assert ledger.claims[0].status == "SUBSCRIBED_PENDING_ALLOTMENT"
    assert ledger.mark_to_market(
        event.subscription_start_date,
        {"SH600000": _price("9.1666667", event.subscription_start_date.isoformat())},
    ).status == "UNVALUED"

    allotment = ledger.allot_rights_shares(event, as_of=event.tradable_date)
    assert ledger.allot_rights_shares(event, as_of=event.tradable_date) == allotment
    lot = next(item for item in ledger.positions if item.source_event_id == event.event_id)
    assert lot.total_shares == 20
    assert lot.cost_basis_cny == Decimal("100.25")
    assert lot.sellable_on(date(2026, 11, 9)) == 0
    assert lot.sellable_on(date(2026, 11, 10)) == 20
    nav = ledger.mark_to_market(
        event.tradable_date,
        {"SH600000": _price("9.1666667", event.tradable_date.isoformat())},
    )
    assert nav.status == "VALUED"


def test_code_change_migrates_each_lot_without_cash_or_trade_and_rejects_merger_ratio():
    source = _source()
    event = CodeChangeEvent(
        event_id="code-change-1",
        old_symbol="SZ300114",
        new_symbol="SZ302132",
        effective_date="2025-02-17",
        security_identity="synthetic same issuer",
        share_ratio=Decimal("1"),
        known_at_utc="2025-02-14T08:00:00Z",
        source=source,
    )
    old_lot = PositionLot(
        lot_id="old-security-lot", symbol="SZ300114", total_shares=200,
        sellable_shares=200, cost_basis_cny=Decimal("3000"),
        acquired_on="2025-01-01", sellable_from="2025-01-02",
    )
    ledger = PortfolioLedger(Decimal("1000"), positions=(old_lot,))
    ledger.apply_code_change(event, as_of=event.effective_date)
    ledger.apply_code_change(event, as_of=event.effective_date)

    assert ledger.cash_cny == Decimal("1000")
    assert len(ledger.journal) == 1
    assert ledger.journal[0].entry_type == "CODE_CHANGE_1_TO_1"
    assert ledger.positions[0].symbol == "SZ302132"
    assert ledger.positions[0].cost_basis_cny == Decimal("3000")

    with pytest.raises(ValueError, match="必须严格等于 1"):
        CodeChangeEvent(
            event_id="merger-misclassified-as-identity",
            old_symbol="SH601989",
            new_symbol="SH600150",
            effective_date="2025-09-16",
            security_identity="different issuer",
            share_ratio=Decimal("0.1339"),
            known_at_utc="2025-09-12T08:00:00Z",
            source=source,
        )

    with pytest.raises(ValueError, match="本地允许时点"):
        CodeChangeEvent(
            event_id="too-late-known-time",
            old_symbol="SZ300114", new_symbol="SZ302132", effective_date="2025-02-17",
            security_identity="synthetic same issuer", share_ratio=Decimal("1"),
            known_at_utc="2025-02-17T01:16:00Z", source=source,
        )


def test_event_id_cannot_be_reused_for_a_different_event_family():
    ledger = PortfolioLedger(Decimal("2000"))
    ledger.apply_fill(TradeFill(
        fill_id="shared-event-id", trade_date="2026-11-02", symbol="SH600000",
        side="BUY", quantity=100, price_cny=Decimal("10"), sellable_from="2026-11-03",
    ))
    event = CodeChangeEvent(
        event_id="shared-event-id", old_symbol="SZ300114", new_symbol="SZ302132",
        effective_date="2025-02-17", security_identity="synthetic identity",
        share_ratio=Decimal("1"), known_at_utc="2025-02-14T08:00:00Z", source=_source(),
    )
    with pytest.raises(ValueError, match="已用于其他事件类型"):
        ledger.apply_code_change(event, as_of=event.effective_date)


def test_action_event_requires_timestamped_source_and_explicit_booking_date():
    with pytest.raises(ValueError, match="必须包含时区"):
        CashDividendEvent(
            event_id="bad-time", symbol="SH600000", record_date="2026-10-29",
            ex_date="2026-10-30", payable_date="2026-11-10",
            gross_per_share_cny=Decimal("0.5"), known_at_utc="2026-10-20T08:00:00",
            source=_source(),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        ActionSource(
            source_id="bad", url="https://example.invalid/a.pdf", sha256="deadbeef",
            title="title", page_reference="p.1", reviewed_by="reviewer", reviewed_at="2026-11-02",
        )
