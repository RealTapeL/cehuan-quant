from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pandas as pd

from cehuan.backtest.checkpoint import BacktestCheckpoint, read_backtest_checkpoint
from cehuan.backtest.engine import run_backtest
from cehuan.backtest.input_hashes import (
    accounting_events_digest,
    chain_data_window_digest,
    canonical_payload_digest,
    compact_config_digest,
    normalized_bars_digest,
    normalized_event_table_digest,
    normalized_table_digest,
)
from cehuan.backtest.input_proof_builder import rebuild_checkpoint_input_proof
from cehuan.backtest.incremental_inputs import audit_incremental_inputs
from cehuan.config import DEFAULT_CONFIG, load_config
from cehuan.data.canonical import BAR_COLUMNS, read_bars
from cehuan.data.corporate_actions import CORPORATE_ACTION_COLUMNS, normalize_corporate_actions
from cehuan.data.security_identity import IDENTITY_EVENT_COLUMNS, normalize_security_identity_events


def _source_bars(days: int = 25) -> pd.DataFrame:
    rows = []
    start = date(2025, 1, 2)
    for index in range(days):
        close = 10.0 + index * 0.1
        rows.append({
            "trade_date": (start + timedelta(days=index)).isoformat(),
            "symbol": "SH600001",
            "name": "synthetic",
            "open": close - 0.05,
            "high": close + 0.05,
            "low": close - 0.1,
            "close": close,
            "adjusted_close": close,
            "volume_shares": 100000,
            "amount_cny": close * 100000,
            "is_listed": True,
            "is_suspended": False,
            "is_st": False,
            "has_price_limit": True,
            "price_limit_pct": 0.1,
            "buy_min_shares": 100,
            "buy_step_shares": 100,
            "sell_min_shares": 100,
            "sell_step_shares": 100,
            "board": "MAIN",
            "market_rule_source": "synthetic",
            "data_source": "synthetic_input_proof_test",
            "retrieved_at_utc": "2025-01-10T00:00:00Z",
        })
    return pd.DataFrame(rows)


def _fixture(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    shutil.copyfile(DEFAULT_CONFIG, config_path)
    config = load_config(config_path)
    bars_path = tmp_path / "bars.csv"
    _source_bars().to_csv(bars_path, index=False)
    bars = read_bars(bars_path)
    calendar_path = tmp_path / "calendar.csv"
    calendar = pd.DataFrame({
        "trade_date": [(date(2025, 1, 2) + timedelta(days=i)).isoformat() for i in range(26)],
        "is_open": True,
        "source": "synthetic_calendar_only",
    })
    calendar.to_csv(calendar_path, index=False)
    identity_path = tmp_path / "identity.csv"
    pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS).to_csv(identity_path, index=False)
    actions_path = tmp_path / "actions.csv"
    pd.DataFrame(columns=CORPORATE_ACTION_COLUMNS).to_csv(actions_path, index=False)
    rule_path = tmp_path / "rules.csv"
    rules = pd.DataFrame([{
        "symbol": "SH600001", "board": "MAIN", "valid_from": "2025-01-01", "valid_to": "2025-01-31",
        "is_listed": "true", "is_suspended": "false", "is_st": "false", "has_price_limit": "true",
        "price_limit_pct": "0.10", "buy_min_shares": "100", "buy_step_shares": "100",
        "sell_min_shares": "100", "sell_step_shares": "100", "source": "synthetic_rule",
        "source_sha256": "a" * 64, "review_status": "REVIEWED", "reviewed_by": "test",
        "reviewed_at": "2026-01-01",
    }])
    rules.to_csv(rule_path, index=False)
    fees_path = tmp_path / "fees.csv"
    fees = pd.DataFrame([{
        "valid_from": "2025-01-01", "valid_to": "2025-01-31", "market": "A_SHARE",
        "commission_rate": "0.0002", "minimum_commission_cny": "5", "stamp_duty_sell_rate": "0.0005",
        "transfer_fee_rate": "0.00001", "source": "synthetic_fee", "source_sha256": "b" * 64,
        "review_status": "REVIEWED", "reviewed_by": "test", "reviewed_at": "2026-01-01",
    }])
    fees.to_csv(fees_path, index=False)
    result = run_backtest(
        bars, 100_000, config,
        accounting_mode="ledger_v2",
        security_identity_events=pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS),
        corporate_actions=pd.DataFrame(columns=CORPORATE_ACTION_COLUMNS),
        market_rule_schedule=rules,
        fee_schedule=fees,
        trading_calendar=calendar,
        source_data_path=bars_path,
    )
    checkpoint: BacktestCheckpoint = result["backtest_checkpoint"]
    manifest_path = tmp_path / "history-manifest.json"
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "windows": [{"bars_path": str(bars_path), "data_hash_mode": "raw_file"}],
    }), encoding="utf-8")
    return checkpoint, config_path, bars_path, calendar_path, identity_path, actions_path, manifest_path, rule_path, fees_path


def _rebuild(tmp_path: Path, fixture, *, claimed: Path | None = None):
    checkpoint, config, bars, calendar, identity, actions, manifest, rules, fees = fixture
    return rebuild_checkpoint_input_proof(
        checkpoint,
        config_path=config,
        bars_path=bars,
        calendar_path=calendar,
        identity_events_path=identity,
        corporate_actions_path=actions,
        market_rules_path=rules,
        fees_path=fees,
        history_manifest_path=manifest,
        claimed_proof_path=claimed,
    )


def _event_proof_fixture(tmp_path: Path):
    fixture = _fixture(tmp_path)
    checkpoint, config, bars, calendar, identity_path, actions_path, manifest, rules, fees = fixture
    identity_row = {
        "event_id": "synthetic-code-change-2025-01-20",
        "event_type": "CODE_CHANGE_1_TO_1",
        "old_symbol": "SH600001",
        "new_symbol": "SZ600002",
        "effective_date": "2025-01-20",
        "share_ratio": "1.0",
        "ratio_basis": "old_shares_to_new_shares",
        "security_identity": "synthetic issuer",
        "event_status": "CONFIRMED",
        "evidence_type": "synthetic_notice",
        "evidence_url": "https://example.invalid/identity.pdf",
        "evidence_summary": "synthetic test only",
    }
    identity = pd.DataFrame([identity_row], columns=IDENTITY_EVENT_COLUMNS)
    identity.to_csv(identity_path, index=False)

    implementation = tmp_path / "implementation-notice.pdf"
    implementation.write_bytes(b"synthetic implementation evidence")
    old_last_trade = tmp_path / "old-last-trade-notice.pdf"
    old_last_trade.write_bytes(b"synthetic last trade evidence")
    action_row = {
        "event_id": "synthetic-merger-2025",
        "event_type": "SHARE_SWAP_MERGER",
        "old_symbol": "SH600001",
        "new_symbol": "SZ600002",
        "record_date": "2025-01-07",
        "old_last_trade_date": "2025-01-06",
        "old_delisting_date": "2025-01-08",
        "new_share_registration_date": "2025-01-09",
        "new_shares_tradable_date": "2025-01-10",
        "allocation_result_announcement_date": "2025-01-09",
        "share_ratio": "0.5",
        "ratio_basis": "old_shares_to_new_shares",
        "integer_allocation_method": "FRACTIONAL_REMAINDER_RANKING_RANDOM_TIEBREAK",
        "allocation_status": "UNRESOLVED_ACCOUNT_ALLOCATION",
        "evidence_url": "https://example.invalid/implementation.pdf",
        "evidence_title": "Synthetic merger implementation notice",
        "evidence_page_reference": "p. 1",
        "evidence_sha256": hashlib.sha256(implementation.read_bytes()).hexdigest(),
        "old_last_trade_evidence_url": "https://example.invalid/last-trade.pdf",
        "old_last_trade_evidence_title": "Synthetic last trade notice",
        "old_last_trade_evidence_page_reference": "p. 2",
        "old_last_trade_evidence_sha256": hashlib.sha256(old_last_trade.read_bytes()).hexdigest(),
        "evidence_summary": "synthetic test only",
        "review_status": "PENDING",
        "reviewed_by": "synthetic fixture author",
        "reviewed_at": "2026-11-03",
    }
    actions = pd.DataFrame([action_row], columns=CORPORATE_ACTION_COLUMNS)
    actions.to_csv(actions_path, index=False)
    normalized_actions = normalize_corporate_actions(actions)
    checkpoint = replace(
        checkpoint,
        identity_events_sha256=normalized_event_table_digest(identity),
        corporate_actions_sha256=normalized_event_table_digest(normalized_actions),
        state_sha256="",
    ).sealed()
    fixture = (checkpoint, config, bars, calendar, identity_path, actions_path, manifest, rules, fees)
    sources = {
        "identity_notice": tmp_path / "identity-notice.pdf",
        "implementation_notice": implementation,
        "old_last_trade_notice": old_last_trade,
    }
    sources["identity_notice"].write_bytes(b"synthetic identity evidence")
    return fixture, sources


def _event_proof(fixture, registry: Path | None = None):
    checkpoint, config, bars, calendar, identity, actions, manifest, rules, fees = fixture
    return rebuild_checkpoint_input_proof(
        checkpoint,
        config_path=config,
        bars_path=bars,
        calendar_path=calendar,
        identity_events_path=identity,
        corporate_actions_path=actions,
        market_rules_path=rules,
        fees_path=fees,
        history_manifest_path=manifest,
        provenance_registry_path=registry,
    )


def _write_event_registry(tmp_path: Path, proof: dict, sources: dict[str, Path], *, review_status: str = "REVIEWED") -> Path:
    rows = []
    for family, input_type in (("identity_events", "SECURITY_IDENTITY_EVENT"), ("corporate_actions", "CORPORATE_ACTION")):
        for event in proof["inputs"][family]:
            for source in event["sources"]:
                role = source["source_role"]
                archive = sources[role]
                rows.append({
                    "input_type": input_type,
                    "row_id": event["row_id"],
                    "source_role": role,
                    "canonical_row_sha256": event["content_sha256"],
                    "source_id": f"source-{role}",
                    "source_url": source["source_url"],
                    "source_title": source.get("source_title") or f"Synthetic {role}",
                    "evidence_location": source.get("evidence_location") or "synthetic p. 1",
                    "source_file_path": archive.name,
                    "source_file_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "known_at_utc": "2025-01-01T08:00:00Z",
                    "known_at_evidence": "synthetic test evidence; not market evidence",
                    "review_status": review_status,
                    "reviewed_by": "synthetic test reviewer",
                    "reviewed_at": "2026-11-03",
                })
    path = tmp_path / "event-provenance.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def test_shared_checkpoint_v1_digest_functions_preserve_previous_serialization(tmp_path):
    config = load_config()
    compact = {
        name: config[name]
        for name in ("data", "strategy", "market", "costs", "backtest", "benchmark")
        if name in config
    }
    old_config_digest = hashlib.sha256(
        json.dumps(compact, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    assert compact_config_digest(config) == old_config_digest

    frame = pd.DataFrame({"x": ["b", "a"], "y": [2, 1]})
    old_table = frame.sort_values(list(frame.columns), kind="stable", na_position="first").copy()
    for column in old_table.columns:
        old_table[column] = old_table[column].astype("string").fillna("")
    old_table_digest = hashlib.sha256(old_table.to_csv(index=False, lineterminator="\n").encode()).hexdigest()
    assert normalized_table_digest(frame) == old_table_digest
    old_event = frame.copy()
    for column in old_event.columns:
        old_event[column] = old_event[column].astype("string").fillna("")
    old_event_digest = hashlib.sha256(old_event.to_csv(index=False, lineterminator="\n").encode()).hexdigest()
    assert normalized_event_table_digest(frame) == old_event_digest
    assert normalized_event_table_digest(frame) != normalized_table_digest(frame)

    bars = read_bars(_bars_file(tmp_path))
    old_bars = hashlib.sha256(
        pd.util.hash_pandas_object(bars[BAR_COLUMNS].reset_index(drop=True), index=False).values.tobytes()
    ).hexdigest()
    assert normalized_bars_digest(bars) == old_bars
    old_accounting_payload = {
        "events": [], "dividend_tax_cny_by_event": {}, "dividend_tax_treatment_by_event": {},
        "dividend_payment_withholding_per_share_cny_by_event": {},
    }
    old_accounting_digest = hashlib.sha256(json.dumps(
        old_accounting_payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert accounting_events_digest(()) == old_accounting_digest
    assert chain_data_window_digest("", "a", "b", None) == hashlib.sha256(b":a:b:peer_from_input").hexdigest()


def _bars_file(tmp_path: Path) -> Path:
    path = tmp_path / "digest-bars.csv"
    _source_bars().to_csv(path, index=False)
    return path


def test_builder_reconstructs_checkpoint_hashes_and_leaves_missing_proofs_unverified(tmp_path):
    fixture = _fixture(tmp_path)
    proof, report = _rebuild(tmp_path, fixture)

    assert proof["schema_version"] == 2
    assert report["historical_data_chain"]["status"] == "VERIFIED"
    assert report["checkpoint_binding_valid"] == "VERIFIED"
    assert report["canonical_payload_digest_valid"] == "VERIFIED"
    assert report["normalized_inputs_binding_valid"] == "VERIFIED"
    assert report["source_content_review_status"] == "UNVERIFIED"
    assert report["known_at_evidence_status"] == "UNVERIFIED"
    assert report["provided_input_artifact_hashes_valid"] == "VERIFIED"
    assert report["source_provenance_evidence_file_hash_valid"] == "UNVERIFIED"
    assert report["decision_cutoff_evidence_file_hash_valid"] == "UNVERIFIED"
    assert report["input_file_hashes_valid"] == "UNVERIFIED"
    assert report["input_files_binding_valid"] == "UNVERIFIED"
    assert report["input_file_hashes_summary"]["overall_gate_unchanged"] is True
    assert report["can_continue"] is False
    comparison = audit_incremental_inputs(proof, proof)
    assert comparison["can_continue"] is False


def test_v1_checkpoint_remains_readable_and_rebuildable_with_original_state_hash(tmp_path):
    fixture = list(_fixture(tmp_path))
    checkpoint: BacktestCheckpoint = fixture[0]
    legacy = replace(
        checkpoint,
        schema_version=1,
        pending_targets_active=None,
        state_sha256="",
    ).sealed()
    fixture[0] = legacy
    checkpoint_path = tmp_path / "legacy-checkpoint.json"
    checkpoint_path.write_text(json.dumps(legacy.to_dict(), default=str), encoding="utf-8")
    original_sha = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

    loaded = read_backtest_checkpoint(checkpoint_path)
    proof, report = _rebuild(tmp_path, tuple(fixture))

    assert loaded.schema_version == 1
    assert "pending_targets_active" not in json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == original_sha
    assert report["checkpoint_binding_valid"] == "VERIFIED"
    assert report["normalized_inputs_binding_valid"] == "VERIFIED"
    assert proof["checkpoint"]["checkpoint_schema_version"] == 1
    assert report["can_continue"] is False


def test_missing_history_manifest_is_unverified_not_reconstructed_from_final_hash(tmp_path):
    fixture = list(_fixture(tmp_path))
    fixture[-3] = tmp_path / "missing-history.json"
    proof, report = _rebuild(tmp_path, tuple(fixture))

    assert proof["inputs"]["historical_bars_prefix_sha256"] is None
    assert report["historical_data_chain"]["status"] == "UNVERIFIED"
    assert report["checkpoint_binding_valid"] == "UNVERIFIED"
    assert report["normalized_inputs_binding_valid"] == "UNVERIFIED"
    assert any(
        row.get("reason") and ("未提供" in row["reason"] or "不存在" in row["reason"])
        for row in report["input_hash_results"]
    )


def test_identity_and_corporate_actions_bind_stable_event_rows_and_multiple_sources(tmp_path):
    fixture, sources = _event_proof_fixture(tmp_path)
    proof, report = _event_proof(fixture)

    assert proof["inputs"]["identity_events"][0]["row_id"] == (
        "security_identity:synthetic-code-change-2025-01-20"
    )
    merger = proof["inputs"]["corporate_actions"][0]
    assert merger["row_id"] == "corporate_action:synthetic-merger-2025"
    assert {row["source_role"] for row in merger["sources"]} == {
        "implementation_notice", "old_last_trade_notice",
    }
    assert report["source_review_by_family"]["security_identity_events"] == "UNVERIFIED"
    assert report["source_review_by_family"]["corporate_actions"] == "UNVERIFIED"
    assert report["can_continue"] is False

    registry = _write_event_registry(tmp_path, proof, sources)
    rebuilt, signed_report = _event_proof(fixture, registry)
    assert all(row["source_status"] == "VERIFIED" for row in rebuilt["inputs"]["identity_events"])
    assert all(row["source_status"] == "VERIFIED" for row in rebuilt["inputs"]["corporate_actions"])
    assert signed_report["source_review_by_family"]["security_identity_events"] == "VERIFIED"
    assert signed_report["source_review_by_family"]["corporate_actions"] == "VERIFIED"
    assert signed_report["can_continue"] is False


def test_event_source_hash_conflict_fails_but_pending_review_stays_unverified(tmp_path):
    fixture, sources = _event_proof_fixture(tmp_path)
    proof, _ = _event_proof(fixture)
    registry = _write_event_registry(tmp_path, proof, sources, review_status="PENDING")
    _, pending_report = _event_proof(fixture, registry)
    assert pending_report["source_review_by_family"]["corporate_actions"] == "UNVERIFIED"
    assert pending_report["source_review_by_family"]["security_identity_events"] == "UNVERIFIED"

    sources["old_last_trade_notice"].write_bytes(b"changed source bytes")
    _, mismatch_report = _event_proof(fixture, registry)
    assert mismatch_report["source_review_by_family"]["corporate_actions"] == "FAILED"
    assert any(
        row.get("source_role") == "old_last_trade_notice" and row["row_status"] == "FAILED"
        for row in mismatch_report["provenance_rows"]
    )


def test_event_source_review_can_pass_while_missing_known_at_stays_unverified(tmp_path):
    fixture, sources = _event_proof_fixture(tmp_path)
    proof, _ = _event_proof(fixture)
    registry = _write_event_registry(tmp_path, proof, sources)
    frame = pd.read_csv(registry, dtype=str, keep_default_na=False)
    frame.loc[0, "known_at_utc"] = ""
    frame.loc[0, "known_at_evidence"] = ""
    frame.to_csv(registry, index=False)

    _, report = _event_proof(fixture, registry)

    assert report["source_review_by_family"]["security_identity_events"] == "VERIFIED"
    assert report["known_at_evidence_status"] == "UNVERIFIED"
    assert report["can_continue"] is False


def test_event_content_change_invalidates_registered_canonical_row(tmp_path):
    fixture, sources = _event_proof_fixture(tmp_path)
    original_proof, _ = _event_proof(fixture)
    registry = _write_event_registry(tmp_path, original_proof, sources)
    checkpoint, config, bars, calendar, identity_path, actions_path, manifest, rules, fees = fixture
    identity = pd.read_csv(identity_path, dtype=str, keep_default_na=False)
    identity.loc[0, "security_identity"] = "changed synthetic issuer"
    identity.to_csv(identity_path, index=False)
    checkpoint = replace(
        checkpoint,
        identity_events_sha256=normalized_event_table_digest(normalize_security_identity_events(identity)),
        state_sha256="",
    ).sealed()
    changed_fixture = (checkpoint, config, bars, calendar, identity_path, actions_path, manifest, rules, fees)
    _, report = _event_proof(changed_fixture, registry)
    assert report["source_review_by_family"]["security_identity_events"] == "FAILED"
    assert any(
        row.get("input_type") == "SECURITY_IDENTITY_EVENT" and row["row_status"] == "FAILED"
        for row in report["provenance_rows"]
    )


def test_event_provenance_registry_rejects_duplicate_source_role(tmp_path):
    fixture, sources = _event_proof_fixture(tmp_path)
    proof, _ = _event_proof(fixture)
    registry = _write_event_registry(tmp_path, proof, sources)
    frame = pd.read_csv(registry, dtype=str, keep_default_na=False)
    frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    frame.to_csv(registry, index=False)
    _, report = _event_proof(fixture, registry)
    assert report["provenance_registry"]["status"] == "FAILED"
    assert report["can_continue"] is False


def test_self_consistent_claimed_proof_cannot_substitute_fabricated_normalized_rows(tmp_path):
    fixture = _fixture(tmp_path)
    proof, _ = _rebuild(tmp_path, fixture)
    claimed = deepcopy(proof)
    # Re-seal fabricated canonical content. Its own row and proof digests remain
    # valid, but the adapter rebuild must still compare it to the parsed rule CSV.
    claimed_rule = claimed["inputs"]["rules"][0]
    claimed_rule["canonical_content"]["board"] = "FABRICATED"
    claimed_rule["board"] = "FABRICATED"
    claimed_rule["content_sha256"] = canonical_payload_digest(claimed_rule["canonical_content"])
    claimed.pop("canonical_payload_sha256")
    claimed["canonical_payload_sha256"] = canonical_payload_digest(claimed)
    claimed_path = tmp_path / "claimed-proof.json"
    claimed_path.write_text(json.dumps(claimed, ensure_ascii=False), encoding="utf-8")

    _, report = _rebuild(tmp_path, fixture, claimed=claimed_path)

    assert report["proof_comparison"]["status"] == "FAILED", report["proof_comparison"]
    assert "rules" in report["proof_comparison"]["different_input_families"]
    assert report["canonical_payload_digest_valid"] == "VERIFIED"
    assert report["normalized_inputs_binding_valid"] == "FAILED"
    assert report["can_continue"] is False


def test_changed_file_bytes_and_unchanged_normalized_config_are_reported_separately(tmp_path):
    fixture = _fixture(tmp_path)
    proof, _ = _rebuild(tmp_path, fixture)
    claimed_path = tmp_path / "claimed-proof.json"
    claimed_path.write_text(json.dumps(proof, ensure_ascii=False), encoding="utf-8")
    config_path = fixture[1]
    config_path.write_text(config_path.read_text(encoding="utf-8") + "\n# formatting-only change\n", encoding="utf-8")

    _, report = _rebuild(tmp_path, fixture, claimed=claimed_path)

    assert report["input_file_hashes_valid"] == "FAILED"
    assert report["normalized_inputs_binding_valid"] == "VERIFIED"
    assert report["checkpoint_binding_valid"] == "VERIFIED"
    assert report["can_continue"] is False


def test_rebuild_cli_writes_expected_artifacts_without_mutating_checkpoint(tmp_path):
    fixture = _fixture(tmp_path)
    checkpoint, config, bars, calendar, identity, actions, manifest, rules, fees = fixture
    assert checkpoint.schema_version == 3
    assert checkpoint.pending_targets_active is not None
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(json.dumps(checkpoint.to_dict(), default=str), encoding="utf-8")
    original_checkpoint_sha = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    output_dir = tmp_path / "proof-output"
    script = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_checkpoint_input_proof.py"
    result = subprocess.run(
        [
            sys.executable, str(script),
            "--checkpoint", str(checkpoint_path),
            "--config", str(config),
            "--bars", str(bars),
            "--calendar", str(calendar),
            "--identity-events", str(identity),
            "--corporate-actions", str(actions),
            "--market-rules", str(rules),
            "--fees", str(fees),
            "--history-manifest", str(manifest),
            "--output-dir", str(output_dir),
        ],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    proof_path = output_dir / "input-proof-baseline.json"
    report_path = output_dir / "input-proof-rebuild-report.json"
    assert proof_path.is_file() and report_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["can_continue"] is False
    assert report["checkpoint_binding_valid"] == "VERIFIED"
    assert report["baseline_proof_file_sha256"] == hashlib.sha256(proof_path.read_bytes()).hexdigest()
    assert hashlib.sha256(checkpoint_path.read_bytes()).hexdigest() == original_checkpoint_sha

    audit_script = Path(__file__).resolve().parents[1] / "scripts" / "audit_checkpoint_incremental_inputs.py"
    audit_path = output_dir / "incremental-preflight.json"
    audit = subprocess.run(
        [
            sys.executable, str(audit_script),
            "--checkpoint", str(checkpoint_path),
            "--baseline-proof", str(proof_path),
            "--candidate-proof", str(proof_path),
            "--baseline-rebuild-report", str(report_path),
            "--candidate-rebuild-report", str(report_path),
            "--output", str(audit_path),
        ],
        check=False, capture_output=True, text=True,
    )
    assert audit.returncode == 0, audit.stderr
    preflight = json.loads(audit_path.read_text(encoding="utf-8"))
    assert preflight["normalized_inputs_binding_valid"] == "VERIFIED"
    assert preflight["provided_input_artifact_hashes_valid"] == "VERIFIED"
    assert preflight["source_provenance_evidence_file_hash_valid"] == "UNVERIFIED"
    assert preflight["decision_cutoff_evidence_file_hash_valid"] == "UNVERIFIED"
    assert preflight["input_file_hashes_valid"] == "UNVERIFIED"
    assert preflight["input_files_binding_valid"] == "UNVERIFIED"
    assert preflight["input_file_hashes_summary"]["overall_gate_unchanged"] is True
    assert preflight["can_continue"] is False
    assert preflight["engine_resume_supported"] is False
