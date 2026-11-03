from datetime import date
from decimal import Decimal

import pytest

from cehuan.accounting import (
    ActionSource,
    CashDividendEvent,
    CodeChangeEvent,
    DelistingEvent,
    PortfolioLedger,
    PriceMark,
    RightsOfferingEvent,
    ShareDistributionEvent,
    ShareSwapMergerEvent,
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


def _distribution(
    *, ratio: str = "0.3", effective_date: str = "2026-10-30",
    registration_date: str | None = None, tradable_date: str = "2026-11-05",
) -> ShareDistributionEvent:
    return ShareDistributionEvent(
        event_id="dist-1",
        symbol="SH600000",
        record_date="2026-10-29",
        effective_date=effective_date,
        registration_date=registration_date,
        tradable_date=tradable_date,
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


def _share_swap() -> ShareSwapMergerEvent:
    return ShareSwapMergerEvent(
        event_id="swap-1",
        old_symbol="SH601989",
        new_symbol="SH600150",
        old_last_trade_date="2025-08-12",
        record_date="2025-09-04",
        old_delisting_date="2025-09-05",
        new_share_registration_date="2025-09-11",
        new_shares_tradable_date="2025-09-16",
        allocation_result_announcement_date="2025-09-12",
        share_ratio=Decimal("0.1339"),
        integer_allocation_method="FRACTIONAL_REMAINDER_RANKING_RANDOM_TIEBREAK",
        known_at_utc="2025-08-10T08:00:00Z",
        implementation_source=_source(),
        old_last_trade_source=_source(),
        allocation_source=_source(),
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


def test_distribution_record_date_snapshot_uses_close_holdings_after_same_day_sale():
    ledger = PortfolioLedger(Decimal("0"), positions=(_lot(shares=200, basis="2000"),))
    event = _distribution()
    ledger.apply_fill(TradeFill(
        fill_id="record-date-sale",
        trade_date=event.record_date,
        symbol=event.symbol,
        side="SELL",
        quantity=100,
        price_cny=Decimal("10"),
    ))

    ledger.record_share_distribution(event, as_of=event.record_date)

    snapshot = next(item for item in ledger.snapshot().entitlements if item.event_id == event.event_id)
    claim = next(item for item in ledger.claims if item.event_id == event.event_id)
    assert snapshot.total_shares == 100
    assert claim.final_allocated_shares == 30
    assert ledger.cash_cny == Decimal("1000")


def test_distribution_without_record_date_holding_creates_no_trade_or_phantom_position():
    ledger = PortfolioLedger(Decimal("500"))
    event = _distribution()

    ledger.record_share_distribution(event, as_of=event.record_date)
    (entry,) = ledger.apply_share_distribution(event, as_of=event.effective_date)

    assert entry.entry_type == "SHARE_DISTRIBUTION_NO_ELIGIBLE_HOLDING"
    assert ledger.cash_cny == Decimal("500")
    assert ledger.positions == ()
    assert next(item for item in ledger.claims if item.event_id == event.event_id).status == "CANCELLED"


def test_fractional_share_distribution_requires_account_result_and_evidence():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _distribution(ratio="0.1339")
    with pytest.raises(ValueError, match="账户层最终获配股数"):
        ledger.record_share_distribution(event, as_of=event.record_date)
    with pytest.raises(ValueError, match="整数边界"):
        ledger.record_share_distribution(
            event, as_of=event.record_date, allocated_new_shares=12,
            allocation_evidence="synthetic final allocation",
        )
    with pytest.raises(ValueError, match="分配依据"):
        ledger.record_share_distribution(event, as_of=event.record_date, allocated_new_shares=13)
    ledger.record_share_distribution(
        event, as_of=event.record_date, allocated_new_shares=13,
        allocation_evidence="synthetic account allocation record",
    )
    ledger.apply_share_distribution(event, as_of=event.effective_date)
    assert sum(lot.total_shares for lot in ledger.positions) == 113


def test_distribution_claim_survives_record_date_sale_and_settles_on_registration():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot(),))
    event = _distribution(
        effective_date="2026-11-02", registration_date="2026-11-03", tradable_date="2026-11-04",
    )
    ledger.record_share_distribution(event, as_of=event.record_date)
    basis_after_record = sum((lot.cost_basis_cny for lot in ledger.positions), Decimal("0"))
    record_claim = next(item for item in ledger.claims if item.claim_id == "distribution:dist-1")
    assert basis_after_record + record_claim.cost_basis_cny == Decimal("1000")

    ledger.apply_fill(TradeFill(
        fill_id="sell-after-record",
        trade_date="2026-10-30",
        symbol="SH600000",
        side="SELL",
        quantity=100,
        price_cny=Decimal("9"),
    ))
    ledger.apply_share_distribution(event, as_of=event.effective_date)
    claim = next(item for item in ledger.claims if item.claim_id == "distribution:dist-1")
    assert claim.status == "DISTRIBUTION_PENDING_REGISTRATION"
    assert claim.final_allocated_shares == 30
    assert sum(lot.total_shares for lot in ledger.positions) == 0

    ledger.mark_share_distribution_claim(
        event,
        as_of=event.effective_date,
        underlying_close_cny=Decimal("7.7"),
        price_source="synthetic_underlying_close",
    )
    valued = ledger.mark_to_market(
        event.effective_date,
        {},
    )
    assert valued.status == "VALUED"
    assert valued.claims_value_cny == Decimal("231.0")

    ledger.register_share_distribution(event, as_of=event.registration_date)
    ledger.register_share_distribution(event, as_of=event.registration_date)
    new_lot = next(lot for lot in ledger.positions if lot.source_event_id == event.event_id)
    assert new_lot.total_shares == 30
    assert new_lot.sellable_on(date(2026, 11, 3)) == 0
    assert new_lot.sellable_on(date(2026, 11, 4)) == 30
    assert sum((lot.cost_basis_cny for lot in ledger.positions), Decimal("0")) == Decimal("230.7692307692307692307692308")
    assert ledger.cash_cny == Decimal("1400")


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


def test_share_swap_keeps_claim_untradable_then_creates_separate_new_lot():
    old_lot = PositionLot(
        lot_id="old-china-shipbuilding-lot", symbol="SH601989", total_shares=1000,
        sellable_shares=0, cost_basis_cny=Decimal("12000"),
        acquired_on="2024-01-03", sellable_from="2024-01-04",
    )
    preexisting_new_lot = PositionLot(
        lot_id="existing-new-code-lot", symbol="SH600150", total_shares=50,
        sellable_shares=50, cost_basis_cny=Decimal("5000"),
        acquired_on="2024-04-01", sellable_from="2024-04-02",
    )
    ledger = PortfolioLedger(Decimal("1000"), positions=(old_lot, preexisting_new_lot))
    event = _share_swap()
    ledger.record_share_swap(event, as_of=event.record_date)
    ledger.begin_share_swap(event, as_of=event.old_delisting_date)

    assert ledger.cash_cny == Decimal("1000")
    assert {lot.symbol for lot in ledger.positions} == {"SH600150"}
    assert len(ledger.positions) == 1
    assert ledger.claims[0].status == "ALLOCATION_UNRESOLVED"
    assert ledger.mark_to_market(
        event.old_delisting_date,
        {"SH600150": _price("20", event.old_delisting_date.isoformat())},
    ).status == "UNVALUED"

    with pytest.raises(ValueError, match="未确认"):
        ledger.settle_share_swap(event, as_of=event.new_shares_tradable_date)
    with pytest.raises(ValueError, match="整数边界"):
        ledger.confirm_share_swap_allocation(
            event, as_of=event.allocation_result_announcement_date,
            final_allocated_new_shares=132, allocation_evidence="synthetic allocation result",
            account_allocation_source=_source(),
        )
    with pytest.raises(ValueError, match="独立证据"):
        ledger.confirm_share_swap_allocation(
            event, as_of=event.allocation_result_announcement_date,
            final_allocated_new_shares=134, allocation_evidence="",
            account_allocation_source=_source(),
        )
    ledger.confirm_share_swap_allocation(
        event, as_of=event.allocation_result_announcement_date,
        final_allocated_new_shares=134,
        allocation_evidence="synthetic account-level allocation confirmation",
        account_allocation_source=_source(),
    )

    with pytest.raises(ValueError, match="可卖股数不足"):
        ledger.apply_fill(TradeFill(
            fill_id="sell-unlisted-swap-shares", trade_date="2025-09-15",
            symbol="SH600150", side="SELL", quantity=51, price_cny=Decimal("20"),
        ))
    allotment = ledger.settle_share_swap(event, as_of=event.new_shares_tradable_date)
    assert ledger.settle_share_swap(event, as_of=event.new_shares_tradable_date) == allotment
    swap_lot = next(lot for lot in ledger.positions if lot.source_event_id == event.event_id)
    existing_lot = next(lot for lot in ledger.positions if lot.lot_id == "existing-new-code-lot")
    assert swap_lot.total_shares == 134
    assert swap_lot.cost_basis_cny == Decimal("12000")
    assert swap_lot.acquired_on == date(2024, 1, 3)
    assert existing_lot.total_shares == 50
    assert existing_lot.cost_basis_cny == Decimal("5000")
    assert existing_lot.acquired_on == date(2024, 4, 1)
    assert swap_lot.sellable_on(date(2025, 9, 15)) == 0
    assert swap_lot.sellable_on(date(2025, 9, 16)) == 134
    assert ledger.cash_cny == Decimal("1000")
    assert all(entry.entry_type not in {"BUY", "SELL"} for entry in ledger.journal)


def test_share_swap_claim_valuation_is_for_one_date_only():
    old_lot = PositionLot(
        lot_id="old", symbol="SH601989", total_shares=1000, sellable_shares=0,
        cost_basis_cny=Decimal("12000"), acquired_on="2024-01-03", sellable_from="2024-01-04",
    )
    ledger = PortfolioLedger(Decimal("1000"), positions=(old_lot,))
    event = _share_swap()
    ledger.record_share_swap(event, as_of=event.record_date)
    ledger.begin_share_swap(event, as_of=event.old_delisting_date)
    ledger.confirm_share_swap_allocation(
        event, as_of=event.allocation_result_announcement_date,
        final_allocated_new_shares=134,
        allocation_evidence="synthetic allocation confirmation",
        account_allocation_source=_source(),
        valuation_cny=Decimal("2500"), valuation_source="synthetic same-day valuation fixture",
    )

    valued = ledger.mark_to_market(
        event.allocation_result_announcement_date,
        {"SH600150": _price("20", event.allocation_result_announcement_date.isoformat())},
    )
    stale = ledger.mark_to_market(
        "2025-09-15", {"SH600150": _price("20", "2025-09-15")}
    )
    assert valued.status == "VALUED"
    assert stale.status == "UNVALUED"
    assert any("估值日期" in reason for reason in stale.reasons)


def test_delisting_cash_settlement_moves_to_receivable_and_never_reuses_old_price():
    lot = PositionLot(
        lot_id="delisted-lot", symbol="SH600000", total_shares=100, sellable_shares=100,
        cost_basis_cny=Decimal("5000"), acquired_on="2024-01-03", sellable_from="2024-01-04",
    )
    event = DelistingEvent(
        event_id="delisting-cash", symbol="SH600000", last_trade_date="2026-10-30",
        delisting_date="2026-11-02", known_at_utc="2026-10-20T08:00:00Z", source=_source(),
        cash_settlement_per_share_cny=Decimal("30"), payable_date="2026-11-10",
        settlement_known_at_utc="2026-10-30T06:00:00Z", settlement_source=_source(),
    )
    ledger = PortfolioLedger(Decimal("500"), positions=(lot,))
    ledger.record_delisting(event, as_of=event.last_trade_date)
    ledger.apply_delisting(event, as_of=event.delisting_date, tax_cny=Decimal("0"))

    assert ledger.positions == ()
    before_payment = ledger.mark_to_market(event.delisting_date, {})
    assert before_payment.status == "VALUED"
    assert before_payment.nav_cny == Decimal("3500.00")
    assert ledger.cash_cny == Decimal("500")
    assert ledger.settle_receivable("delisting:delisting-cash", settled_on="2026-11-10")
    after_payment = ledger.mark_to_market("2026-11-10", {})
    assert ledger.cash_cny == Decimal("3500.00")
    assert after_payment.nav_cny == Decimal("3500.00")
    assert all(entry.entry_type not in {"BUY", "SELL"} for entry in ledger.journal)


def test_delisting_cash_amount_announced_after_close_cannot_backfill_delisting_nav():
    lot = PositionLot(
        lot_id="delisted-lot", symbol="SH600000", total_shares=100, sellable_shares=100,
        cost_basis_cny=Decimal("5000"), acquired_on="2024-01-03", sellable_from="2024-01-04",
    )
    event = DelistingEvent(
        event_id="late-delisting-cash", symbol="SH600000", last_trade_date="2026-10-30",
        delisting_date="2026-11-02", known_at_utc="2026-10-20T08:00:00Z", source=_source(),
        cash_settlement_per_share_cny=Decimal("30"), payable_date="2026-11-10",
        settlement_known_at_utc="2026-11-02T08:30:00Z", settlement_source=_source(),
    )
    ledger = PortfolioLedger(Decimal("500"), positions=(lot,))
    ledger.record_delisting(event, as_of=event.last_trade_date)
    ledger.apply_delisting(event, as_of=event.delisting_date, tax_cny=None)
    assert ledger.mark_to_market(event.delisting_date, {}).status == "UNVALUED"

    with pytest.raises(ValueError, match="尚不可得"):
        ledger.resolve_delisted_cash_settlement(event, as_of=event.delisting_date, tax_cny=Decimal("0"))
    ledger.resolve_delisted_cash_settlement(event, as_of="2026-11-03", tax_cny=Decimal("0"))
    with pytest.raises(ValueError, match="早于账本最新过账日"):
        ledger.mark_to_market("2026-11-02", {})
    assert ledger.mark_to_market("2026-11-03", {}).nav_cny == Decimal("3500.00")


def test_delisting_without_settlement_stays_unvalued_until_same_day_claim_mark():
    lot = PositionLot(
        lot_id="delisted-lot", symbol="SZ000001", total_shares=100, sellable_shares=0,
        cost_basis_cny=Decimal("5000"), acquired_on="2024-01-03", sellable_from="2024-01-04",
    )
    event = DelistingEvent(
        event_id="delisting-unresolved", symbol="SZ000001", last_trade_date="2026-10-30",
        delisting_date="2026-11-02", known_at_utc="2026-10-20T08:00:00Z", source=_source(),
    )
    ledger = PortfolioLedger(Decimal("500"), positions=(lot,))
    ledger.record_delisting(event, as_of=event.last_trade_date)
    ledger.apply_delisting(event, as_of=event.delisting_date, tax_cny=None)
    assert ledger.positions == ()
    assert ledger.mark_to_market(event.delisting_date, {}).status == "UNVALUED"

    ledger.resolve_delisted_claim(
        event, as_of="2026-11-03", valuation_cny=Decimal("3000"),
        valuation_source="synthetic reviewed transfer-right valuation",
    )
    valued = ledger.mark_to_market("2026-11-03", {})
    next_day = ledger.mark_to_market("2026-11-04", {})
    assert valued.status == "VALUED" and valued.nav_cny == Decimal("3500")
    assert next_day.status == "UNVALUED"
    assert any("估值日期" in reason for reason in next_day.reasons)


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
