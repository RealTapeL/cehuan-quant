from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cehuan.accounting import (
    CashReceivable,
    FeeItem,
    NoncashClaim,
    PortfolioLedger,
    PositionLot,
    PriceMark,
    TradeFill,
)


def _lot(lot_id: str, quantity: int, basis: str, acquired: str, *, sellable: int | None = None) -> PositionLot:
    return PositionLot(
        lot_id=lot_id,
        symbol="SH600000",
        total_shares=quantity,
        sellable_shares=quantity if sellable is None else sellable,
        cost_basis_cny=Decimal(basis),
        acquired_on=date.fromisoformat(acquired),
        sellable_from=date.fromisoformat(acquired),
    )


def _mark(price: str, day: str, *, age: int = 0) -> PriceMark:
    return PriceMark(
        price_cny=Decimal(price),
        price_date=date.fromisoformat(day),
        source="synthetic price fixture",
        age_sessions=age,
    )


def test_buy_then_next_session_sell_accounts_for_fees_and_cost_basis():
    ledger = PortfolioLedger(Decimal("2000"))
    buy = TradeFill(
        fill_id="buy-1",
        trade_date="2026-11-02",
        symbol="600000.SH",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        fees=(FeeItem("commission", Decimal("5")), FeeItem("transfer", Decimal("1"))),
        sellable_from="2026-11-03",
    )

    buy_entries = ledger.apply_fill(buy)
    assert ledger.cash_cny == Decimal("994")
    assert ledger.positions[0].cost_basis_cny == Decimal("1006")
    assert ledger.positions[0].sellable_shares == 0
    assert buy_entries[0].cash_delta_cny == Decimal("-1006")
    assert ledger.mark_to_market("2026-11-02", {"SH600000": _mark("10", "2026-11-02")}).nav_cny == Decimal("1994")

    sell = TradeFill(
        fill_id="sell-1",
        trade_date="2026-11-03",
        symbol="SH600000",
        side="SELL",
        quantity=50,
        price_cny=Decimal("11"),
        fees=(FeeItem("commission", Decimal("5.5")),),
    )
    sell_entries = ledger.apply_fill(sell)

    assert ledger.cash_cny == Decimal("1538.5")
    assert ledger.positions[0].total_shares == 50
    assert ledger.positions[0].sellable_shares == 50
    details = dict(sell_entries[0].details)
    assert details["allocated_cost_basis_cny"] == "503"
    assert details["realized_pnl_cny"] == "41.5"
    assert ledger.mark_to_market("2026-11-03", {"SH600000": _mark("11", "2026-11-03")}).nav_cny == Decimal("2088.5")


def test_insufficient_cash_failure_is_atomic():
    ledger = PortfolioLedger(Decimal("100"))
    fill = TradeFill(
        fill_id="too-large",
        trade_date="2026-11-02",
        symbol="SH600000",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        sellable_from="2026-11-03",
    )

    with pytest.raises(ValueError, match="现金不足"):
        ledger.apply_fill(fill)

    assert ledger.cash_cny == Decimal("100")
    assert ledger.positions == ()
    assert ledger.journal == ()


def test_same_fill_is_idempotent_but_changed_payload_is_rejected():
    ledger = PortfolioLedger(Decimal("2000"))
    fill = TradeFill(
        fill_id="same-id",
        trade_date="2026-11-02",
        symbol="SH600000",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        sellable_from="2026-11-03",
    )
    first = ledger.apply_fill(fill)
    second = ledger.apply_fill(fill)

    assert first == second
    assert len(ledger.journal) == 1
    assert ledger.cash_cny == Decimal("1000")

    altered = TradeFill(
        fill_id="same-id",
        trade_date="2026-11-02",
        symbol="SH600000",
        side="BUY",
        quantity=100,
        price_cny=Decimal("9.99"),
        sellable_from="2026-11-03",
    )
    with pytest.raises(ValueError, match="不同输入"):
        ledger.apply_fill(altered)
    assert len(ledger.journal) == 1
    assert ledger.cash_cny == Decimal("1000")


def test_t1_blocks_sale_of_shares_bought_today():
    ledger = PortfolioLedger(Decimal("2000"))
    ledger.apply_fill(TradeFill(
        fill_id="buy-today",
        trade_date="2026-11-02",
        symbol="SH600000",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        sellable_from="2026-11-03",
    ))

    with pytest.raises(ValueError, match="可卖股数不足"):
        ledger.apply_fill(TradeFill(
            fill_id="sell-today",
            trade_date="2026-11-02",
            symbol="SH600000",
            side="SELL",
            quantity=100,
            price_cny=Decimal("10"),
        ))
    assert ledger.positions[0].total_shares == 100
    assert len(ledger.journal) == 1


def test_sell_allocates_cost_fifo_without_merging_lots():
    ledger = PortfolioLedger(Decimal("0"), positions=(
        _lot("old", 50, "500", "2026-01-01"),
        _lot("new", 50, "600", "2026-02-01"),
    ))

    (entry,) = ledger.apply_fill(TradeFill(
        fill_id="fifo-sale",
        trade_date="2026-03-01",
        symbol="SH600000",
        side="SELL",
        quantity=75,
        price_cny=Decimal("15"),
    ))

    assert [lot.lot_id for lot in ledger.positions] == ["new"]
    assert ledger.positions[0].total_shares == 25
    assert ledger.positions[0].cost_basis_cny == Decimal("300")
    assert dict(entry.details)["allocated_cost_basis_cny"] == "800"
    assert dict(entry.details)["lot_ids_fifo"] == "old,new"


@pytest.mark.parametrize("quantity", [1.5, Decimal("1.5"), "1.5"])
def test_fractional_share_input_is_rejected_before_integer_conversion(quantity):
    with pytest.raises(ValueError, match="整数股数|float"):
        TradeFill(
            fill_id="fractional",
            trade_date="2026-11-02",
            symbol="SH600000",
            side="BUY",
            quantity=quantity,
            price_cny=Decimal("10"),
            sellable_from="2026-11-03",
        )


def test_money_rejects_float_inputs_to_avoid_preconverted_binary_values():
    with pytest.raises(ValueError, match="不能传 float"):
        FeeItem("commission", 0.1)


def test_ledger_invariants_reconcile_cash_and_position_state_to_journal():
    ledger = PortfolioLedger(Decimal("2000"))
    ledger.apply_fill(TradeFill(
        fill_id="invariant-buy",
        trade_date="2026-11-02",
        symbol="SH600000",
        side="BUY",
        quantity=100,
        price_cny=Decimal("10"),
        sellable_from="2026-11-03",
    ))
    ledger.assert_invariants()

    ledger._cash_cny += Decimal("1")
    with pytest.raises(ValueError, match="现金与分录"):
        ledger.assert_invariants()

    ledger._cash_cny -= Decimal("1")
    ledger._positions.clear()
    with pytest.raises(ValueError, match="持仓与分录"):
        ledger.assert_invariants()


def test_missing_or_stale_price_returns_unvalued_instead_of_zero_or_old_mark():
    ledger = PortfolioLedger(Decimal("500"), positions=(_lot("held", 100, "1000", "2026-10-01"),))

    missing = ledger.mark_to_market("2026-11-02", {})
    stale = ledger.mark_to_market(
        "2026-11-02", {"SH600000": _mark("10", "2026-10-30", age=1)}, max_stale_age_sessions=0
    )

    assert missing.status == "UNVALUED" and missing.nav_cny is None
    assert "缺少有效估值价格" in missing.reasons[0]
    assert stale.status == "UNVALUED" and stale.nav_cny is None
    assert stale.stale_price_count == 1


def test_unknown_receivable_tax_or_unvalued_claim_blocks_nav():
    receivable_ledger = PortfolioLedger(Decimal("500"), receivables=(
        CashReceivable("dividend-1", "event-1", Decimal("50"), None, "2026-10-30", "2026-11-10"),
    ))
    claim_ledger = PortfolioLedger(Decimal("500"), claims=(
        NoncashClaim(
            "merger-claim", "event-2", "SH601989", "SH600150", Decimal("13.39"),
            None, "2026-11-10", None, None, "ALLOCATION_UNRESOLVED", Decimal("1000"), "2026-01-01",
        ),
    ))

    receivable_nav = receivable_ledger.mark_to_market("2026-11-02", {})
    claim_nav = claim_ledger.mark_to_market("2026-11-02", {})

    assert receivable_nav.status == "UNVALUED" and receivable_nav.nav_cny is None
    assert "应收税费未知" in receivable_nav.reasons[0]
    assert claim_nav.status == "UNVALUED" and claim_nav.nav_cny is None
    assert "权利没有可信估值" in claim_nav.reasons[0]


def test_claim_with_value_must_have_source_and_nav_includes_it_once():
    claim = NoncashClaim(
        "claim-1", "event-1", "SH600000", "SH600000", Decimal("10"), 10,
        "2026-11-05", Decimal("95"), "synthetic valuation", "PENDING_TRADABLE",
    )
    ledger = PortfolioLedger(Decimal("100"), claims=(claim,))

    result = ledger.mark_to_market("2026-11-02", {})

    assert result.status == "VALUED"
    assert result.nav_cny == Decimal("195")

    with pytest.raises(ValueError, match="valuation_source"):
        NoncashClaim(
            "claim-2", "event-2", "SH600000", "SH600000", Decimal("1"), 1,
            "2026-11-05", Decimal("1"), None, "PENDING_TRADABLE",
        )
