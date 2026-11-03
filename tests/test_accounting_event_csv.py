from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from cehuan.data.accounting_event_csv import ACCOUNTING_EVENT_CSV_COLUMNS, read_accounting_event_csv


def _event_row(archive: Path) -> dict[str, str]:
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return {
        "event_id": "div-001", "event_type": "CASH_DIVIDEND", "known_at_utc": "2025-01-02T07:00:00+00:00",
        "symbol": "SH600000", "record_date": "2025-01-02", "ex_date": "2025-01-03",
        "payable_date": "2025-01-06", "gross_per_share_cny": "0.1", "old_symbol": "", "new_symbol": "",
        "effective_date": "", "security_identity": "", "share_ratio": "", "tax_cny": "0.02",
        "source_id": "notice-001", "source_url": "https://example.com/notice/1", "source_sha256": digest,
        "source_title": "Synthetic dividend notice", "source_page_reference": "p. 1",
        "source_archive_file": archive.name, "review_status": "REVIEWED", "reviewed_by": "test reviewer",
        "reviewed_at": "2026-11-03",
    }


def test_accounting_event_csv_binds_each_event_to_archived_source_bytes(tmp_path):
    archive = tmp_path / "notice.pdf"
    archive.write_bytes(b"synthetic source bytes")
    event_csv = tmp_path / "events.csv"
    pd.DataFrame([_event_row(archive)], columns=ACCOUNTING_EVENT_CSV_COLUMNS).to_csv(event_csv, index=False)

    bundle = read_accounting_event_csv(event_csv)

    assert len(bundle.events) == 1
    assert bundle.events[0].event_id == "div-001"
    assert bundle.dividend_tax_cny_by_event["div-001"] == Decimal("0.02")
    assert bundle.source_audit.iloc[0]["event_id"] == "div-001"
    assert bundle.source_audit.iloc[0]["source_hash_matches"]
    assert len(bundle.source_csv_sha256) == 64


def test_accounting_event_csv_rejects_source_hash_mismatch(tmp_path):
    archive = tmp_path / "notice.pdf"
    archive.write_bytes(b"original")
    row = _event_row(archive)
    archive.write_bytes(b"changed after registration")
    event_csv = tmp_path / "events.csv"
    pd.DataFrame([row], columns=ACCOUNTING_EVENT_CSV_COLUMNS).to_csv(event_csv, index=False)

    with pytest.raises(ValueError, match="事件键与来源原件 SHA-256 不匹配"):
        read_accounting_event_csv(event_csv)


def test_accounting_event_csv_rejects_unreviewed_source(tmp_path):
    archive = tmp_path / "notice.pdf"
    archive.write_bytes(b"source")
    row = _event_row(archive)
    row["review_status"] = "PENDING"
    event_csv = tmp_path / "events.csv"
    pd.DataFrame([row], columns=ACCOUNTING_EVENT_CSV_COLUMNS).to_csv(event_csv, index=False)

    with pytest.raises(ValueError, match="必须为 REVIEWED"):
        read_accounting_event_csv(event_csv)


def test_accounting_event_csv_reads_share_distribution_and_requires_all_event_dates(tmp_path):
    archive = tmp_path / "distribution-notice.pdf"
    archive.write_bytes(b"synthetic distribution source bytes")
    row = _event_row(archive)
    row.update({
        "event_id": "dist-001",
        "event_type": "SHARE_DISTRIBUTION",
        "symbol": "SH600000",
        "record_date": "2025-01-02",
        "effective_date": "2025-01-03",
        "registration_date": "2025-01-06",
        "tradable_date": "2025-01-07",
        "share_ratio": "0.3",
        "fractional_share_method": "ACCOUNT_RESULT_REQUIRED",
        "ex_date": "",
        "payable_date": "",
        "gross_per_share_cny": "",
        "old_symbol": "",
        "new_symbol": "",
        "security_identity": "",
        "tax_cny": "",
    })
    event_csv = tmp_path / "events.csv"
    pd.DataFrame([row], columns=ACCOUNTING_EVENT_CSV_COLUMNS).to_csv(event_csv, index=False)

    bundle = read_accounting_event_csv(event_csv)

    event = bundle.events[0]
    assert event.event_id == "dist-001"
    assert event.registration_date.isoformat() == "2025-01-06"
    assert event.tradable_date.isoformat() == "2025-01-07"
    assert event.shares_per_old_share == Decimal("0.3")

    row["registration_date"] = ""
    pd.DataFrame([row], columns=ACCOUNTING_EVENT_CSV_COLUMNS).to_csv(event_csv, index=False)
    with pytest.raises(ValueError, match="缺少 registration_date"):
        read_accounting_event_csv(event_csv)
