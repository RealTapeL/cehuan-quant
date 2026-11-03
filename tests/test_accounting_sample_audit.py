from __future__ import annotations

import csv
import hashlib
from pathlib import Path

from cehuan.data.accounting_sample_audit import verify_accounting_sample_manifest
from cehuan.data.accounting_sample_audit import build_accounting_sample_review_pack


def _manifest(tmp_path: Path, archive_bytes: bytes) -> Path:
    root = tmp_path / "repo"
    evidence = root / "docs" / "evidence"
    evidence.mkdir(parents=True)
    archive = root / "data" / "private" / "source.pdf"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(archive_bytes)
    extracted = root / "data" / "private" / "source.txt"
    extracted.write_text("证券代码 600101 股权登记日 2024/7/4", encoding="utf-8")
    manifest = evidence / "samples.csv"
    row = {
        "sample_id": "sample-1",
        "event_type": "CASH_DIVIDEND",
        "symbol": "SH600101",
        "source_published_date": "2024-06-28",
        "known_at_timestamp_status": "DATE_ONLY",
        "record_date": "2024-07-04",
        "source_title": "test",
        "source_url": "https://example.invalid/source.pdf",
        "archive_path": "data/private/source.pdf",
        "source_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "page_reference": "1",
        "extracted_text_path": "data/private/source.txt",
        "required_text_tokens": "600101|2024/7/4",
        "machine_extracted_values": "record=2024-07-04",
        "review_status": "PENDING_INDEPENDENT_HUMAN_SIGNOFF",
        "reviewed_by": "test",
        "reviewed_at": "2026-11-03",
        "manual_review_notes": "fixture",
    }
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row), lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
    return manifest


def test_sample_audit_separates_hash_token_matches_from_human_signoff(tmp_path: Path):
    manifest = _manifest(tmp_path, b"synthetic official-notice fixture")
    result = verify_accounting_sample_manifest(manifest)
    assert result["automated_pass_count"] == 1
    assert result["manual_signoff_pending_count"] == 1
    sample = result["samples"][0]
    assert sample["source_hash_matches"] is True
    assert sample["token_match"] is True
    assert sample["manual_review_status"] == "PENDING_INDEPENDENT_HUMAN_SIGNOFF"


def test_sample_audit_detects_changed_archive_hash(tmp_path: Path):
    manifest = _manifest(tmp_path, b"synthetic official-notice fixture")
    archive = manifest.parents[2] / "data" / "private" / "source.pdf"
    archive.write_bytes(b"modified fixture")
    result = verify_accounting_sample_manifest(manifest)
    assert result["automated_pass_count"] == 0
    assert result["samples"][0]["source_hash_matches"] is False


def test_review_pack_copies_sources_adds_candidate_excerpt_and_keeps_signoff_pending(tmp_path: Path):
    manifest = _manifest(tmp_path, b"synthetic official-notice fixture")
    repo = manifest.parents[2]
    fields = manifest.parent / "field-review.csv"
    rows = [
        {
            "sample_id": "sample-1", "event_type": "CASH_DIVIDEND", "field_name": "record_date",
            "official_value": "2024-07-04", "source_published_date": "2024-06-28",
            "source_title": "test", "source_url": "https://example.invalid/source.pdf",
            "archive_path": "data/private/source.pdf", "source_sha256": hashlib.sha256(b"synthetic official-notice fixture").hexdigest(),
            "page_reference": "PDF第1页", "machine_extracted_value": "2024/7/4",
            "review_status": "PENDING", "reviewed_by": "", "reviewed_at": "", "notes": "",
        },
        {
            "sample_id": "sample-1", "event_type": "CASH_DIVIDEND", "field_name": "account_receipt",
            "official_value": "Requires actual account statement; not in announcement", "source_published_date": "2024-06-28",
            "source_title": "test", "source_url": "https://example.invalid/source.pdf",
            "archive_path": "data/private/source.pdf", "source_sha256": hashlib.sha256(b"synthetic official-notice fixture").hexdigest(),
            "page_reference": "", "machine_extracted_value": "ACCOUNT_RESULT_REQUIRED",
            "review_status": "PENDING", "reviewed_by": "", "reviewed_at": "", "notes": "",
        },
    ]
    with fields.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)

    output = repo / "outputs" / "review-pack"
    result = build_accounting_sample_review_pack(manifest, fields, output)
    assert result["source_archive_hashes_verified"] is True
    assert result["manual_signoff_pending_fields"] == 2
    assert result["signoff_state_changed"] is False
    assert (output / "originals/sample-1.pdf").read_bytes() == b"synthetic official-notice fixture"
    with (output / "review_fields.csv").open(encoding="utf-8", newline="") as handle:
        review_rows = list(csv.DictReader(handle))
    assert "股权登记日" in review_rows[0]["candidate_excerpt_or_locator_status"]
    assert review_rows[0]["review_status"] == "PENDING"
    assert review_rows[1]["field_scope"] == "ACCOUNT_EVIDENCE_REQUIRED"
    assert "没有改变输入" in (output / "README.md").read_text(encoding="utf-8") or "保持输入值" in (
        output / "README.md"
    ).read_text(encoding="utf-8")
