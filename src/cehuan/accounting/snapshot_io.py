"""Read a previously exported ledger-v2 state for a later research window."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from cehuan.accounting.models import (
    CashReceivable,
    DividendTaxLotRecord,
    EntitledLot,
    EntitlementSnapshot,
    LedgerEntry,
    LedgerSnapshot,
    NoncashClaim,
    PositionLot,
    calendar_date,
)


def _date(value: Any, field: str, *, optional: bool = False) -> date | None:
    if value is None or value == "":
        if optional:
            return None
        raise ValueError(f"ledger snapshot 缺少 {field}")
    return calendar_date(value, field)


def ledger_snapshot_from_dict(value: Mapping[str, Any]) -> LedgerSnapshot:
    """Validate and hydrate the JSON shape emitted by ``cehuan backtest``."""
    if not isinstance(value, Mapping):
        raise ValueError("ledger snapshot JSON 顶层必须是对象")
    required = {"portfolio_id", "cash_cny", "positions", "receivables", "claims", "journal", "entitlements"}
    missing = required.difference(value)
    if missing:
        raise ValueError("ledger snapshot 缺少字段：" + ", ".join(sorted(missing)))

    positions = tuple(PositionLot(
        lot_id=str(row["lot_id"]),
        symbol=str(row["symbol"]),
        total_shares=int(row["total_shares"]),
        sellable_shares=int(row["sellable_shares"]),
        cost_basis_cny=Decimal(str(row["cost_basis_cny"])),
        acquired_on=_date(row["acquired_on"], "positions.acquired_on"),
        sellable_from=_date(row["sellable_from"], "positions.sellable_from"),
        source_event_id=row.get("source_event_id"),
    ) for row in value["positions"])

    receivables = tuple(CashReceivable(
        receivable_id=str(row["receivable_id"]),
        event_id=str(row["event_id"]),
        gross_cny=Decimal(str(row["gross_cny"])),
        tax_cny=None if row.get("tax_cny") is None else Decimal(str(row["tax_cny"])),
        recognized_on=_date(row["recognized_on"], "receivables.recognized_on"),
        payable_on=_date(row.get("payable_on"), "receivables.payable_on", optional=True),
        status=str(row.get("status", "PENDING")),
        tax_treatment=row.get("tax_treatment"),
        eligible_shares=row.get("eligible_shares"),
        payment_withholding_per_share_cny=(
            None if row.get("payment_withholding_per_share_cny") is None
            else Decimal(str(row["payment_withholding_per_share_cny"]))
        ),
        payment_withheld_tax_cny=(
            None if row.get("payment_withheld_tax_cny") is None
            else Decimal(str(row["payment_withheld_tax_cny"]))
        ),
        payment_withholding_is_account_evidence=bool(row.get("payment_withholding_is_account_evidence", False)),
        payment_evidence_id=row.get("payment_evidence_id"),
        payment_evidence_sha256=row.get("payment_evidence_sha256"),
    ) for row in value["receivables"])

    dividend_tax_records = tuple(DividendTaxLotRecord(
        record_id=str(row["record_id"]),
        event_id=str(row["event_id"]),
        symbol=str(row["symbol"]),
        lot_id=str(row["lot_id"]),
        acquired_on=_date(row["acquired_on"], "dividend_tax_records.acquired_on"),
        entitled_shares=int(row["entitled_shares"]),
        remaining_shares=int(row["remaining_shares"]),
        sold_shares=int(row["sold_shares"]),
        gross_dividend_cny=Decimal(str(row["gross_dividend_cny"])),
        tax_treatment=str(row.get("tax_treatment", "ACCOUNT_EVIDENCE_REQUIRED")),
        payment_withheld_tax_cny=(
            None if row.get("payment_withheld_tax_cny") is None
            else Decimal(str(row["payment_withheld_tax_cny"]))
        ),
        payment_withholding_is_account_evidence=bool(row.get("payment_withholding_is_account_evidence", False)),
        final_tax_cny=None if row.get("final_tax_cny") is None else Decimal(str(row["final_tax_cny"])),
        tax_reconciled_shares=int(row.get("tax_reconciled_shares", 0)),
        tax_status=str(row.get("tax_status", "ENTITLEMENT_RECORDED")),
        policy_source_id=row.get("policy_source_id"),
        policy_source_sha256=row.get("policy_source_sha256"),
        account_evidence_id=row.get("account_evidence_id"),
        account_evidence_sha256=row.get("account_evidence_sha256"),
    ) for row in value.get("dividend_tax_records", ()))

    claims = tuple(NoncashClaim(
        claim_id=str(row["claim_id"]),
        event_id=str(row["event_id"]),
        old_symbol=str(row["old_symbol"]),
        new_symbol=row.get("new_symbol"),
        exact_entitlement=None if row.get("exact_entitlement") is None else Decimal(str(row["exact_entitlement"])),
        final_allocated_shares=(
            None if row.get("final_allocated_shares") is None else int(row["final_allocated_shares"])
        ),
        tradable_from=_date(row.get("tradable_from"), "claims.tradable_from", optional=True),
        valuation_cny=None if row.get("valuation_cny") is None else Decimal(str(row["valuation_cny"])),
        valuation_source=row.get("valuation_source"),
        status=str(row["status"]),
        cost_basis_cny=Decimal(str(row.get("cost_basis_cny", "0"))),
        acquired_on=_date(row.get("acquired_on"), "claims.acquired_on", optional=True),
        valuation_as_of=_date(row.get("valuation_as_of"), "claims.valuation_as_of", optional=True),
        lot_allocations=tuple(
            (str(item[0]), int(item[1]), Decimal(str(item[2])), _date(item[3], "claims.lot_allocations.acquired_on"))
            for item in row.get("lot_allocations", ())
        ),
        next_phase_date=_date(row.get("next_phase_date"), "claims.next_phase_date", optional=True),
        source_id=row.get("source_id"),
        source_sha256=row.get("source_sha256"),
    ) for row in value["claims"])

    journal_rows: list[LedgerEntry] = []
    for row in value["journal"]:
        details = row.get("details", ())
        detail_pairs = tuple(sorted((str(key), str(item)) for key, item in details.items())) if isinstance(details, Mapping) else tuple(
            (str(pair[0]), str(pair[1])) for pair in details
        )
        journal_rows.append(LedgerEntry(
            entry_id=str(row["entry_id"]),
            operation_key=str(row["operation_key"]),
            event_id=str(row["event_id"]),
            phase=str(row["phase"]),
            booking_date=_date(row["booking_date"], "journal.booking_date"),
            entry_type=str(row["entry_type"]),
            cash_delta_cny=Decimal(str(row["cash_delta_cny"])),
            position_deltas=tuple((str(pair[0]), int(pair[1])) for pair in row.get("position_deltas", ())),
            details=detail_pairs,
        ))

    entitlements = tuple(EntitlementSnapshot(
        event_id=str(row["event_id"]),
        symbol=str(row["symbol"]),
        captured_on=_date(row["captured_on"], "entitlements.captured_on"),
        lots=tuple(EntitledLot(
            lot_id=str(lot["lot_id"]),
            quantity=int(lot["quantity"]),
            cost_basis_cny=Decimal(str(lot["cost_basis_cny"])),
            acquired_on=_date(lot["acquired_on"], "entitlements.lots.acquired_on"),
            sellable_from=_date(lot["sellable_from"], "entitlements.lots.sellable_from"),
        ) for lot in row.get("lots", ())),
    ) for row in value["entitlements"])

    return LedgerSnapshot(
        portfolio_id=str(value["portfolio_id"]),
        cash_cny=Decimal(str(value["cash_cny"])),
        positions=positions,
        receivables=receivables,
        claims=claims,
        journal=tuple(journal_rows),
        entitlements=entitlements,
        dividend_tax_records=dividend_tax_records,
        as_of_date=_date(value.get("as_of_date"), "as_of_date", optional=True),
    )


def read_ledger_snapshot(path: str | Path) -> LedgerSnapshot:
    snapshot_path = Path(path).expanduser().resolve()
    if not snapshot_path.is_file():
        raise FileNotFoundError(f"ledger opening snapshot 不存在：{snapshot_path}")
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"ledger opening snapshot JSON 无法解析：{snapshot_path}") from exc
    return ledger_snapshot_from_dict(value)
