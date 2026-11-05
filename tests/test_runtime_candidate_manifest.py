from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from cehuan.cli.main import main as cli_main
from cehuan.research.candidate_artifacts import (
    classify_candidate_inventory,
    inspect_original_run_candidate_anchor,
)
from test_core_workflow import bars


def test_backtest_cli_seals_candidate_pool_in_run_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    import cehuan.cli.main as cli_module

    data_path = tmp_path / "bars.csv"
    bars(days=30).to_csv(data_path, index=False)
    output_root = tmp_path / "project-output"
    monkeypatch.setattr(cli_module, "REPO_ROOT", output_root)
    repo = Path(__file__).resolve().parents[1]

    code = cli_main([
        "backtest", "--diagnostic", "--data", str(data_path), "--cash", "100000",
        "--config", str(repo / "configs/default.toml"),
        "--identity-events", str(repo / "configs/security_identity_events.csv"),
        "--corporate-actions", str(repo / "configs/corporate_actions.csv"),
    ])
    assert code == 0
    output = json.loads(capsys.readouterr().out)["experiment_dir"]
    output_dir = Path(output)
    candidate = output_dir / "candidate-pool-by-signal.csv"
    manifest_path = output_dir / "experiment-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest["runtime_artifacts"]["candidate-pool-by-signal.csv"]["sha256"]
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == expected
    assert manifest["runtime_artifacts"]["candidate-pool-by-signal.csv"]["row_count"] > 0
    assert manifest["authenticity"]["trusted_external_timestamp"] == "UNVERIFIED"
    assert manifest["broker_connected"] is False
    assert manifest["orders_generated"] is False
    anchor = classify_candidate_inventory(candidate, None)
    assert anchor["status"] == "NOT_PROVIDED"
    assert anchor["original_run_time_anchor"] == "UNVERIFIED"
    runtime_anchor = inspect_original_run_candidate_anchor(manifest_path, candidate)
    assert runtime_anchor["status"] == "UNVERIFIED"
    assert runtime_anchor["manifest_status"] == "RUNTIME_HASH_CLAIM_MATCHED_BUT_MANIFEST_AUTHENTICITY_UNVERIFIED"
