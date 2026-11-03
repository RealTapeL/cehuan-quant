from __future__ import annotations

from copy import deepcopy
from datetime import date
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from cehuan.backtest.incremental_inputs import audit_incremental_inputs


SHA = "a" * 64


def _proof() -> dict:
    return {
        "schema_version": 1,
        "increment_id": "increment-2025-01-13-v1",
        "checkpoint": {
            "state_sha256": "1" * 64,
            "as_of_date": "2025-01-13",
            "next_expected_session": "2025-01-14",
            "decision_cutoff_utc": "2025-01-13T08:00:00Z",
            "cutoff_source": "synthetic checkpoint decision log",
            "cutoff_source_sha256": "2" * 64,
            "applied_increment_ids": [],
            "applied_increment_sha256s": [],
        },
        "inputs": {
            "strategy_config_sha256": "3" * 64,
            "historical_bars_prefix_sha256": "4" * 64,
            "events": [{
                "event_id": "known-event",
                "content_sha256": "5" * 64,
                "first_impact_date": "2025-01-10",
                "known_at_utc": "2025-01-01T00:00:00Z",
                "source_sha256": "6" * 64,
            }],
            "calendar": [
                {"date": "2025-01-10", "is_open": True},
                {"date": "2025-01-13", "is_open": True},
                {"date": "2025-01-14", "is_open": True},
            ],
            "rules": [{
                "row_id": "rule-history",
                "valid_from": "2025-01-01",
                "valid_to": "2025-12-31",
                "content_sha256": "7" * 64,
                "known_at_utc": "2024-12-01T00:00:00Z",
                "source_sha256": "8" * 64,
            }],
            "fees": [{
                "row_id": "fee-history",
                "valid_from": "2025-01-01",
                "valid_to": "2025-12-31",
                "content_sha256": "9" * 64,
                "known_at_utc": "2024-12-01T00:00:00Z",
                "source_sha256": "a" * 64,
            }],
        },
    }


def _add_event(proof: dict, *, impact: str = "2025-01-15", known_at: str = "2025-01-13T08:01:00Z") -> None:
    proof["inputs"]["events"].append({
        "event_id": "new-event",
        "content_sha256": "b" * 64,
        "first_impact_date": impact,
        "known_at_utc": known_at,
        "source_sha256": "c" * 64,
    })


def _blocker_codes(report: dict) -> set[str]:
    return {row["code"] for row in report["blockers"]}


def test_future_event_first_available_after_cutoff_is_candidate_only_not_engine_resume():
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)

    report = audit_incremental_inputs(baseline, candidate)

    assert report["candidate_append_compatible"]
    assert report["additions"]["events"] == ["new-event"]
    assert report["can_continue"] is False
    assert report["engine_resume_supported"] is False


def test_new_event_discovered_after_cutoff_but_affecting_history_requires_recompute():
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, impact="2025-01-12")

    report = audit_incremental_inputs(baseline, candidate)

    assert "LATE_EVENT_AFFECTS_HISTORY" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-12"
    assert report["requires_recompute"]


def test_correction_to_already_used_event_requires_recompute():
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["events"][0]["content_sha256"] = "d" * 64

    report = audit_incremental_inputs(baseline, candidate)

    assert "USED_EVENT_CORRECTED" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-10"


@pytest.mark.parametrize(
    ("known_at", "is_compatible"),
    [
        ("2025-01-13T07:59:59Z", False),  # same Shanghai date, before cutoff
        ("2025-01-13T08:00:00Z", False),  # cutoff itself is not later
        ("2025-01-13T08:00:01Z", True),   # after cutoff
    ],
)
def test_intraday_event_order_uses_explicit_utc_cutoff(known_at: str, is_compatible: bool):
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, known_at=known_at)

    report = audit_incremental_inputs(baseline, candidate)

    assert report["candidate_append_compatible"] is is_compatible


def test_date_only_event_availability_is_unproven_and_rejected():
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, known_at="2025-01-13")

    report = audit_incremental_inputs(baseline, candidate)

    assert "EVENT_KNOWN_AT_UNPROVEN" in _blocker_codes(report)
    assert not report["candidate_append_compatible"]
    assert report["requires_recompute"]
    assert report["earliest_possible_affected_date"] is None


def test_baseline_event_without_known_at_provenance_cannot_be_used_for_incremental_claim():
    baseline = _proof()
    del baseline["inputs"]["events"][0]["known_at_utc"]

    report = audit_incremental_inputs(baseline, deepcopy(baseline))

    assert "BASELINE_EVENT_PROVENANCE_MISSING" in _blocker_codes(report)
    assert report["requires_recompute"]
    assert report["earliest_affected_date_precision"].startswith("UNKNOWN_")


def test_future_calendar_append_is_candidate_but_historical_status_change_is_rejected():
    baseline = _proof()
    appended = deepcopy(baseline)
    appended["inputs"]["calendar"].append({"date": "2025-01-15", "is_open": True})
    assert audit_incremental_inputs(baseline, appended)["candidate_append_compatible"]

    changed = deepcopy(baseline)
    changed["inputs"]["calendar"][1]["is_open"] = False
    assert "CALENDAR_HISTORY_CHANGED" in _blocker_codes(audit_incremental_inputs(baseline, changed))


def test_future_rule_append_requires_known_at_and_source_evidence():
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["rules"].append({
        "row_id": "rule-future",
        "valid_from": "2025-01-15",
        "valid_to": "2025-12-31",
        "content_sha256": "e" * 64,
        "known_at_utc": "2025-01-13T08:01:00Z",
        "source_sha256": "f" * 64,
    })
    assert audit_incremental_inputs(baseline, candidate)["candidate_append_compatible"]

    missing_evidence = deepcopy(candidate)
    del missing_evidence["inputs"]["rules"][1]["known_at_utc"]
    assert "SCHEDULE_APPEND_EVIDENCE_MISSING" in _blocker_codes(
        audit_incremental_inputs(baseline, missing_evidence)
    )


def test_historical_fee_edit_is_rejected():
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["fees"][0]["content_sha256"] = "f" * 64

    report = audit_incremental_inputs(baseline, candidate)

    assert "HISTORICAL_SCHEDULE_ROW_CHANGED" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-01"


def test_repeated_increment_id_is_reported_and_invalid_proofs_are_rejected():
    baseline = _proof()
    baseline["checkpoint"]["applied_increment_ids"] = [baseline["increment_id"]]
    report = audit_incremental_inputs(baseline, deepcopy(baseline))
    assert "INCREMENT_ALREADY_APPLIED" in _blocker_codes(report)

    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)
    first = audit_incremental_inputs(baseline, candidate)
    baseline["checkpoint"]["applied_increment_sha256s"] = [first["increment_sha256"]]
    candidate["checkpoint"]["applied_increment_sha256s"] = list(
        baseline["checkpoint"]["applied_increment_sha256s"]
    )
    candidate["increment_id"] = "different-id-for-same-content"
    report = audit_incremental_inputs(baseline, candidate)
    assert "INCREMENT_ALREADY_APPLIED" in _blocker_codes(report)

    bad_cutoff = _proof()
    bad_cutoff["checkpoint"]["decision_cutoff_utc"] = "2025-01-13"
    with pytest.raises(ValueError, match="包含时区"):
        audit_incremental_inputs(bad_cutoff, deepcopy(bad_cutoff))


def test_incremental_audit_cli_binds_the_proof_to_a_verified_checkpoint(tmp_path, monkeypatch):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "audit_checkpoint_incremental_inputs.py"
    spec = importlib.util.spec_from_file_location("incremental_audit_cli_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_text("synthetic checkpoint file", encoding="utf-8")
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "report.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    verified = SimpleNamespace(
        state_sha256="1" * 64,
        as_of_date=date(2025, 1, 13),
        next_expected_session=date(2025, 1, 14),
    )
    monkeypatch.setattr(module, "read_backtest_checkpoint", lambda path: verified)
    monkeypatch.setattr(sys, "argv", [
        "audit_checkpoint_incremental_inputs.py", "--checkpoint", str(checkpoint_file),
        "--baseline-proof", str(baseline_path), "--candidate-proof", str(candidate_path),
        "--output", str(report_path),
    ])

    assert module.main() == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["candidate_append_compatible"]
    assert report["can_continue"] is False
    assert report["input_files"]["checkpoint_sha256"]

    baseline["checkpoint"]["as_of_date"] = "2025-01-12"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    with pytest.raises(ValueError, match="as_of_date 不匹配"):
        module.main()
