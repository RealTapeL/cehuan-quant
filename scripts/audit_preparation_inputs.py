#!/usr/bin/env python3
"""Recheck the private 2023-2025 preparation inputs and write a private manifest.

This audit is read-only with respect to source snapshots and archives. It writes
only new JSON reports under data/private/audits/.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import platform
import stat
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UNIVERSE_ID = "bs-universe-20230101-20251231-v1"
BARS_ID = "bs-bars-universe-20221201-20251231-v1"
DEFAULT_RUN_ID = "preparation-20261029-stage0-2"
PUBLIC_EVIDENCE = (
    "docs/evidence/universe-outdate-boundary-audit.csv",
    "docs/evidence/event-source-index.csv",
    "docs/evidence/unknown-type-top10-audit.csv",
    "docs/evidence/unknown-type-remaining-43-audit.csv",
    "docs/evidence/market-fee-evidence.csv",
    "docs/market-fee-audit.md",
    "docs/market-rules.md",
    "docs/data-preparation-2023-2025.md",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def git_value(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def package_version(*candidates: str) -> str | None:
    for candidate in candidates:
        try:
            return importlib.metadata.version(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def verify_catalog(root: Path, snapshot: Path, manifest_name: str, catalog_key: str) -> dict[str, Any]:
    manifest_path = snapshot / manifest_name
    manifest = read_json(manifest_path)
    catalog = manifest.get(catalog_key)
    if not isinstance(catalog, dict):
        raise ValueError(f"{manifest_path} has no dictionary {catalog_key}")

    missing: list[str] = []
    mismatched: list[str] = []
    unsafe_paths: list[str] = []
    for checked, (relative, expected) in enumerate(catalog.items(), start=1):
        if checked == 1 or checked % 2000 == 0:
            print(f"{snapshot.name}: checking {checked}/{len(catalog)} catalog files", file=sys.stderr, flush=True)
        candidate = (snapshot / relative).resolve()
        if not candidate.is_relative_to(snapshot.resolve()):
            unsafe_paths.append(relative)
            continue
        if not candidate.is_file():
            missing.append(relative)
        elif sha256_file(candidate) != expected:
            mismatched.append(relative)

    chunk_manifest_paths = sorted(snapshot.glob("chunk-*/manifest.json"))
    expected_chunk_ids = set(manifest.get("capture_policy_by_chunk", {}))
    actual_chunk_ids = {path.parent.name for path in chunk_manifest_paths}
    chunk_manifest_errors: list[str] = []
    chunk_manifest_sha256: dict[str, str] = {}
    for path in chunk_manifest_paths:
        chunk_id = path.parent.name
        chunk = read_json(path)
        chunk_manifest_sha256[f"{chunk_id}/manifest.json"] = sha256_file(path)
        if chunk.get("snapshot_id") != chunk_id:
            chunk_manifest_errors.append(f"{chunk_id}: snapshot_id mismatch")
        for relative, expected in chunk.get("file_sha256", {}).items():
            if catalog.get(f"{chunk_id}/{relative}") != expected:
                chunk_manifest_errors.append(f"{chunk_id}/{relative}: root/chunk manifest mismatch")
    catalog_files = set(catalog)
    chunk_metadata = {path.relative_to(snapshot).as_posix() for path in chunk_manifest_paths}
    operational_files = {manifest_name, "checkpoint.json", "batch_checkpoint.json"}
    actual_files = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file()
        and path.relative_to(snapshot).as_posix() not in operational_files
    } - chunk_metadata
    missing_chunks = sorted(expected_chunk_ids - actual_chunk_ids)
    unexpected_chunks = sorted(actual_chunk_ids - expected_chunk_ids)
    return {
        "snapshot_id": manifest.get("snapshot_id"),
        "provider": manifest.get("provider"),
        "date_range": manifest.get("date_range"),
        "manifest_path": manifest_path.relative_to(root).as_posix(),
        "manifest_sha256": sha256_file(manifest_path),
        "catalog_key": catalog_key,
        "expected_files": len(catalog),
        "chunk_manifest_count": len(chunk_manifest_paths),
        "chunk_manifest_sha256": chunk_manifest_sha256,
        "missing_chunks": missing_chunks,
        "unexpected_chunks": unexpected_chunks,
        "chunk_manifest_errors": chunk_manifest_errors,
        "missing": missing,
        "mismatched": mismatched,
        "unsafe_paths": unsafe_paths,
        "unlisted_files": sorted(actual_files - catalog_files),
        "catalog_files_not_present_in_tree": sorted(catalog_files - actual_files),
        "ok": not (
            missing or mismatched or unsafe_paths or actual_files - catalog_files
            or catalog_files - actual_files
            or missing_chunks or unexpected_chunks or chunk_manifest_errors
        ),
    }


def verify_backup(root: Path, source: Path, backup: Path, catalog: dict[str, str]) -> dict[str, Any]:
    missing: list[str] = []
    mismatched: list[str] = []
    for checked, (relative, expected) in enumerate(catalog.items(), start=1):
        if checked == 1 or checked % 2000 == 0:
            print(f"{backup.name} backup: checking {checked}/{len(catalog)} catalog files", file=sys.stderr, flush=True)
        candidate = backup / relative
        if not candidate.is_file():
            missing.append(relative)
        elif sha256_file(candidate) != expected:
            mismatched.append(relative)

    operational_name = "checkpoint.json" if (source / "checkpoint.json").is_file() else "batch_checkpoint.json"
    source_chunk_manifests = {
        path.relative_to(source).as_posix() for path in source.glob("chunk-*/manifest.json")
    }
    expected_files = set(catalog) | {"manifest.json", operational_name} | source_chunk_manifests
    backup_files = {
        path.relative_to(backup).as_posix() for path in backup.rglob("*") if path.is_file()
    }
    unlisted = sorted(backup_files - expected_files)
    absent = sorted(expected_files - backup_files)
    manifest_matches = (
        (source / "manifest.json").is_file()
        and (backup / "manifest.json").is_file()
        and sha256_file(source / "manifest.json") == sha256_file(backup / "manifest.json")
    )
    operational_matches = (
        (source / operational_name).is_file()
        and (backup / operational_name).is_file()
        and sha256_file(source / operational_name) == sha256_file(backup / operational_name)
    )
    chunk_manifest_mismatches = [
        relative for relative in source_chunk_manifests
        if not (backup / relative).is_file()
        or sha256_file(source / relative) != sha256_file(backup / relative)
    ]

    all_paths = [backup, *backup.rglob("*")]
    writable = []
    symlinks = []
    for path in all_paths:
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            symlinks.append(rel)
        try:
            if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                writable.append(rel)
        except FileNotFoundError:
            missing.append(rel)
    return {
        "source_snapshot": source.relative_to(root).as_posix(),
        "backup_snapshot": backup.relative_to(root).as_posix(),
        "catalog_files": len(catalog),
        "missing": missing,
        "mismatched": mismatched,
        "unlisted_files": unlisted,
        "absent_expected_files": absent,
        "manifest_matches_source": manifest_matches,
        "operational_checkpoint_matches_source": operational_matches,
        "chunk_manifest_count": len(source_chunk_manifests),
        "chunk_manifest_mismatches": sorted(chunk_manifest_mismatches),
        "writable_paths": writable,
        "symlinks": symlinks,
        "ok": not (
            missing or mismatched or unlisted or absent or not manifest_matches
            or not operational_matches or chunk_manifest_mismatches or writable or symlinks
        ),
    }


def archive_hash_map(root: Path, folder: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    if folder.exists():
        for path in folder.rglob("*"):
            if path.is_file():
                found.setdefault(sha256_file(path), []).append(path.relative_to(root).as_posix())
    return found


def verify_source_registers(root: Path) -> dict[str, Any]:
    evidence_root = root / "data/private/evidence/preparation-20261029"
    local_hashes = archive_hash_map(root, evidence_root)

    event_index_path = evidence_root / "event-source-archive-index.json"
    event_index = read_json(event_index_path)
    event_sources = event_index.get("sources", [])
    event_missing = [
        item.get("event_key", "") for item in event_sources
        if not item.get("sha256") or item["sha256"] not in local_hashes
    ]
    event_public_rows = csv_rows(root / "docs/evidence/event-source-index.csv")
    event_public_hashes = {row.get("archived_file_sha256", "") for row in event_public_rows}
    event_archive_hashes = {item.get("sha256", "") for item in event_sources}
    event_register_only = sorted(event_public_hashes - event_archive_hashes - {""})
    event_archive_only = sorted(event_archive_hashes - event_public_hashes - {""})

    unknown_rows = csv_rows(root / "docs/evidence/unknown-type-remaining-43-audit.csv")
    unknown_missing_hashes = sorted({
        row.get("archived_source_sha256", "") for row in unknown_rows
        if row.get("archived_source_sha256") and row["archived_source_sha256"] not in local_hashes
    })

    fee_rows = csv_rows(root / "docs/evidence/market-fee-evidence.csv")
    fee_hash_fields = ("primary_source_sha256", "corroborating_source_sha256")
    fee_populated_hashes = {
        row[field] for row in fee_rows for field in fee_hash_fields if row.get(field)
    }
    fee_unarchived_hashes = sorted(value for value in fee_populated_hashes if value not in local_hashes)

    return {
        "event_archive_index": event_index_path.relative_to(root).as_posix(),
        "event_archive_index_sha256": sha256_file(event_index_path),
        "event_archive_source_count": len(event_sources),
        "event_archived_sources_missing_or_hash_mismatched": event_missing,
        "public_event_rows": len(event_public_rows),
        "event_hashes_only_in_public_register": event_register_only,
        "event_hashes_only_in_private_archive_index": event_archive_only,
        "unknown_43_public_rows": len(unknown_rows),
        "unknown_source_hashes_missing_from_local_archive": unknown_missing_hashes,
        "fee_evidence_rows": len(fee_rows),
        "fee_source_hashes_populated": len(fee_populated_hashes),
        "fee_source_hashes_not_locally_archived": fee_unarchived_hashes,
        "user_signoff_pending": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output_dir = (args.output_dir or root / "data/private/audits" / args.run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    universe = root / "data/private/universes" / UNIVERSE_ID
    bars = root / "data/private/snapshots" / BARS_ID
    universe_backup = root / "data/private/backups/preparation-20261029" / UNIVERSE_ID
    bars_backup = root / "data/private/backups/preparation-20261029" / BARS_ID
    universe_manifest = read_json(universe / "manifest.json")
    bars_manifest = read_json(bars / "manifest.json")
    print("Verifying source snapshot catalogs and read-only backups; no snapshot files will be changed.", file=sys.stderr, flush=True)
    universe_integrity = verify_catalog(root, universe, "manifest.json", "files_sha256")
    bars_integrity = verify_catalog(root, bars, "manifest.json", "file_sha256")
    integrity = {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "snapshot_integrity": {
            "universe": universe_integrity,
            "bars": bars_integrity,
            "backup": {
                "universe": verify_backup(root, universe, universe_backup, universe_manifest["files_sha256"]),
                "bars": verify_backup(root, bars, bars_backup, bars_manifest["file_sha256"]),
            },
        },
    }
    integrity_checks = [
        integrity["snapshot_integrity"][kind]["ok"] for kind in ("universe", "bars")
    ] + [
        integrity["snapshot_integrity"]["backup"][kind]["ok"] for kind in ("universe", "bars")
    ]
    integrity["ok"] = all(integrity_checks)
    integrity_path = output_dir / "snapshot-integrity.json"
    integrity_path.write_text(json.dumps(integrity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    unknown_path = root / "data/private/audits/preparation-20261029/unknown-types.csv"
    unknown_summary_path = unknown_path.with_suffix(unknown_path.suffix + ".summary.json")
    unknown_summary = read_json(unknown_summary_path)
    unknown = {
        "review_csv": unknown_path.relative_to(root).as_posix(),
        "review_csv_sha256": sha256_file(unknown_path),
        "summary": unknown_summary,
        "partition_balances": unknown_summary.get("classification_partition_balances") is True,
        "unresolved_rows": unknown_summary.get("unresolved_rows"),
        "user_signoff_pending": True,
    }

    quality_path = root / "data/private/audits/preparation-20261029/universe-audit-v3/point_in_time_quality.json"
    quality = read_json(quality_path)
    public_docs = {
        relative: sha256_file(root / relative)
        for relative in PUBLIC_EVIDENCE
    }
    source_registers = verify_source_registers(root)
    lock_path = root / "uv.lock"
    config_path = root / "configs/default.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    input_paths = (
        "uv.lock",
        "configs/default.toml",
        "configs/security_identity_events.csv",
        "configs/corporate_actions.csv",
        "data/private/universes/bs-universe-20230101-20251231-v1/manifest.json",
        "data/private/universes/bs-universe-20230101-20251231-v1/checkpoint.json",
        "data/private/universes/bs-universe-20230101-20251231-v1/securities_by_date.csv",
        "data/private/snapshots/bs-bars-universe-20221201-20251231-v1/manifest.json",
        "data/private/snapshots/bs-bars-universe-20221201-20251231-v1/batch_checkpoint.json",
        "data/private/snapshots/bs-bars-universe-20221201-20251231-v1/bars.csv",
        "data/private/snapshots/bs-bars-universe-20221201-20251231-v1/benchmark.csv",
        "data/private/audits/preparation-20261029/unknown-types.csv",
        "data/private/audits/preparation-20261029/unknown-types.csv.summary.json",
        "data/private/audits/preparation-20261029/market-fee-source-status.csv",
        "data/private/audits/preparation-20261029/universe-audit-v3/point_in_time_bars.csv",
        "data/private/audits/preparation-20261029/universe-audit-v3/point_in_time_quality.json",
        "data/private/audits/preparation-20261029/universe-audit-v3/daily_price_coverage.csv",
        "data/private/audits/preparation-20261029/universe-audit-v3/universe_price_discrepancies.csv",
        "data/private/evidence/preparation-20261029/event-source-archive-index.json",
    )
    missing_input_paths = [relative for relative in input_paths if not (root / relative).is_file()]
    input_file_hashes = {
        relative: sha256_file(root / relative)
        for relative in input_paths
        if (root / relative).is_file()
    }
    input_hashes_complete = not missing_input_paths
    source_registers_ok = not any((
        source_registers["event_archived_sources_missing_or_hash_mismatched"],
        source_registers["event_hashes_only_in_public_register"],
        source_registers["event_hashes_only_in_private_archive_index"],
        source_registers["unknown_source_hashes_missing_from_local_archive"],
        source_registers["fee_source_hashes_not_locally_archived"],
    ))
    status = git_value(root, "status", "--porcelain")
    run = {
        "schema_version": 1,
        "run_id": args.run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "private snapshot manifest verification and stage 0-2 evidence refresh; no backtest, no strategy change, no personal trading plan",
        "git": {
            "head": git_value(root, "rev-parse", "HEAD"),
            "branch": git_value(root, "branch", "--show-current"),
            "working_tree_dirty": bool(status),
            "status_porcelain": status.splitlines(),
        },
        "environment": {
            "os": platform.platform(),
            "python": sys.version,
            "uv": subprocess.run(["uv", "--version"], cwd=root, capture_output=True, text=True).stdout.strip(),
            "uv_lock_sha256": sha256_file(lock_path),
            "packages": {
                distribution: package_version(*candidates)
                for distribution, candidates in {
                    "cehuan-quant": ("cehuan-quant",),
                    "baostock": ("baostock",),
                    "akshare": ("akshare",),
                    "qlib": ("pyqlib", "qlib"),
                    "pytest": ("pytest",),
                }.items()
            },
        },
        "snapshot_integrity": {
            "report": integrity_path.relative_to(root).as_posix(),
            "report_sha256": sha256_file(integrity_path),
            "ok": integrity["ok"],
            "universe": universe_integrity,
            "bars": bars_integrity,
            "backup_universe": integrity["snapshot_integrity"]["backup"]["universe"],
            "backup_bars": integrity["snapshot_integrity"]["backup"]["bars"],
        },
        "unknown_types": unknown,
        "point_in_time_quality": {
            "report": quality_path.relative_to(root).as_posix(),
            "sha256": sha256_file(quality_path),
            "healthy": quality.get("healthy"),
            "baseline_ready": quality.get("baseline_ready"),
            "blockers": quality.get("baseline_blockers", []),
            "classified_stock_member_days": quality.get("universe_reconciliation_quality", {}).get("daily_stock_member_rows"),
            "normalized_bar_rows": quality.get("raw_bar_quality", {}).get("row_count"),
            "unexplained_missing_bar_rows": quality.get("universe_reconciliation_quality", {}).get("unresolved_missing_bar_rows"),
        },
        "research_design": {
            "assessment_window": ["2023-01-01", "2025-12-31"],
            "warmup_data_start": "2022-12-01",
            "scope": "classified Shanghai/Shenzhen type=1 stock members; not full A-share coverage",
            "strategy_config": config.get("strategy", {}),
            "strategy_status": "frozen technical baseline configuration recorded; not run in this preparation task",
            "sample_cost_assumptions_only": config.get("costs", {}),
            "cost_status": "sample assumptions only; not calibrated to Huatai",
            "backtest_executed": False,
            "personal_trade_plan_generated": False,
        },
        "source_registers": source_registers,
        "input_file_sha256": input_file_hashes,
        "missing_required_input_files": missing_input_paths,
        "input_hashes_complete": input_hashes_complete,
        "public_evidence_sha256": public_docs,
        "release_status": "NOT_READY",
        "remaining_blockers": [
            "User/maintainer sign-off is pending for archived event and classification evidence.",
            "Effective-dated historical market rules do not cover every evaluated security-day.",
            "Historical fee schedule is incomplete; Huatai personal commission and minimum commission are uncalibrated.",
            "Corporate-action coverage and ledger are not consumed by the backtest engine; SH601989 allocation/valuation remains unresolved.",
            "Execution engine does not yet consume audited market rules, fees, and the complete corporate-action ledger.",
        ],
    }
    run_path = output_dir / "run-manifest.json"
    run_path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    digest = sha256_file(run_path)
    (output_dir / "run-manifest.sha256").write_text(f"{digest}  {run_path.name}\n", encoding="ascii")

    preparation_checks_ok = (
        integrity["ok"]
        and unknown["partition_balances"]
        and unknown["unresolved_rows"] == 0
        and unknown_summary.get("added_as_stock_rows") == 0
        and input_hashes_complete
        and source_registers_ok
        and quality.get("healthy") is True
    )
    print(json.dumps({
        "run_manifest": run_path.relative_to(root).as_posix(),
        "run_manifest_sha256": digest,
        "snapshot_integrity_report": integrity_path.relative_to(root).as_posix(),
        "snapshot_integrity_ok": integrity["ok"],
        "universe_files_checked": universe_integrity["expected_files"],
        "bars_files_checked": bars_integrity["expected_files"],
        "unknown_rows": unknown_summary.get("unknown_rows"),
        "unknown_symbols": unknown_summary.get("unknown_symbols"),
        "added_as_stock_rows": unknown_summary.get("added_as_stock_rows"),
        "unresolved_unknown_rows": unknown_summary.get("unresolved_rows"),
        "point_in_time_healthy": quality.get("healthy"),
        "baseline_ready": quality.get("baseline_ready"),
        "event_archive_sources": source_registers["event_archive_source_count"],
        "source_registers_ok": source_registers_ok,
        "required_inputs_complete": input_hashes_complete,
    }, ensure_ascii=False, indent=2))
    return 0 if preparation_checks_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
