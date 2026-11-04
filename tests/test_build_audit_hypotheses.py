from __future__ import annotations

import csv
import hashlib
import json
import runpy
from pathlib import Path

import pandas as pd
import pytest

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


_BUILD = runpy.run_path("scripts/build_audit_hypotheses.py")["build"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source_package(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    audit = tmp_path / "source-audit"
    audit.mkdir()
    config = tmp_path / "config.json"
    config_payload = {"strategy": {"lookback": 20, "max_positions": 3}}
    _json(config, config_payload)
    config_hash = _sha(config)
    files = {
        "suspension-candidate-window.csv": "symbol,trade_date,is_suspended\nAAA,2025-01-02,false\n",
        "suspension-candidate-rank-neighbors.csv": "symbol,rank,momentum\nAAA,1,0.2\nBBB,2,0.1\n",
        "zero-target-review.csv": (
            "symbol,rank,target_amount_cny,execution_open_price_cny,raw_target_shares_before_rounding,"
            "buy_minimum_shares_from_config_fallback,next_unselected_candidate_rank,next_unselected_candidate_symbol\n"
            "AAA,1,100,200,0,100,4,BBB\n"
        ),
    }
    for name, value in files.items():
        (audit / name).write_text(value, encoding="utf-8")
    summary = {
        "schema_version": 2,
        "experiment_id": "synthetic-source-test",
        "input_hashes": {"config.json": config_hash, "bars.csv": "a" * 64},
        "window": {"start": "2024-12-01", "end": "2025-01-10"},
        "suspension_candidate_diagnostic": {
            "symbol": "AAA",
            "window_state_counts_including_base": {"total_rows": 21, "suspended_rows": 1},
            "return_observation_state_counts_excluding_base": {"observed": 20},
            "session_window_audit": {"window_alignment_status": "ALIGNED"},
        },
        "market_rule_fallback_rows_in_window": 3,
        "record_date_entitlement_audit": {"typed_accounting_events_in_frozen_run": []},
        "selected_targets_reconstructed_to_zero_shares": 1,
        "zero_target_reviews": [],
        "research_trust": {"input_file_integrity": "MATCHED", "canonical_data_quality": "UNVERIFIED"},
    }
    _json(audit / "trade-audit-summary.json", summary)
    inventory_paths = ["trade-audit-summary.json", *files]
    with (audit / "audit-artifact-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "sha256"])
        writer.writeheader()
        writer.writerows({"relative_path": name, "sha256": _sha(audit / name)} for name in inventory_paths)
    before = {p.name: _sha(p) for p in audit.iterdir() if p.is_file()}
    return audit, config, before


def test_build_binds_actual_config_and_keeps_source_audit_immutable(tmp_path: Path) -> None:
    audit, config, before = _source_package(tmp_path)
    output = tmp_path / "new-hypotheses"
    result = _BUILD(audit, config, output)

    assert result["source_reference"]["config_binding_status"] == "MATCHED"
    assert result["source_reference"]["source_audit_mutated"] is False
    assert result["legacy_review"]["status"] == "UNVERIFIED"
    assert output.is_dir()
    assert {p.name: _sha(p) for p in audit.iterdir() if p.is_file()} == before
    assert {item["status"] for item in result["hypotheses"]} == {"NEEDS_DATA"}
    for hypothesis in result["hypotheses"]:
        validate_strategy_hypothesis(hypothesis)
    assert (output / "hypothesis-inventory.csv").is_file()
    assert json.loads((output / "source-audit-reference.json").read_text())["actual_config_sha256"] == _sha(config)


def test_different_config_is_rejected_without_creating_output(tmp_path: Path) -> None:
    audit, config, before = _source_package(tmp_path)
    other = tmp_path / "different-config.json"
    _json(other, {"strategy": {"lookback": 10, "max_positions": 3}})
    output = tmp_path / "should-not-exist"

    with pytest.raises(ValueError, match="SHA-256"):
        _BUILD(audit, other, output)

    assert not output.exists()
    assert {p.name: _sha(p) for p in audit.iterdir() if p.is_file()} == before


def test_same_config_bytes_at_another_path_are_accepted(tmp_path: Path) -> None:
    audit, config, _ = _source_package(tmp_path)
    copy = tmp_path / "config-copy.json"
    copy.write_bytes(config.read_bytes())
    result = _BUILD(audit, copy, tmp_path / "output")
    assert result["source_reference"]["config_binding_status"] == "MATCHED"


def test_missing_registered_config_hash_is_rejected(tmp_path: Path) -> None:
    audit, config, _ = _source_package(tmp_path)
    summary_path = audit / "trade-audit-summary.json"
    summary = json.loads(summary_path.read_text())
    summary["input_hashes"].pop("config.json")
    _json(summary_path, summary)
    inventory_path = audit / "audit-artifact-inventory.csv"
    rows = list(csv.DictReader(inventory_path.open(encoding="utf-8")))
    with inventory_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "sha256"])
        writer.writeheader()
        for row in rows:
            if row["relative_path"] == "trade-audit-summary.json":
                row["sha256"] = _sha(summary_path)
            writer.writerow(row)

    with pytest.raises(ValueError, match="缺少有效的 config.json"):
        _BUILD(audit, config, tmp_path / "output")


def test_source_evidence_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    audit, config, _ = _source_package(tmp_path)
    with (audit / "zero-target-review.csv").open("a", encoding="utf-8") as handle:
        handle.write("# changed\n")
    with pytest.raises(ValueError, match="哈希不匹配"):
        _BUILD(audit, config, tmp_path / "output")


def test_existing_or_nested_output_is_rejected(tmp_path: Path) -> None:
    audit, config, _ = _source_package(tmp_path)
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        _BUILD(audit, config, existing)
    with pytest.raises(ValueError, match="必须与来源审计包分离"):
        _BUILD(audit, config, audit / "nested-output")
