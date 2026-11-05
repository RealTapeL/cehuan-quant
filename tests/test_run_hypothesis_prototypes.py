from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest


_BUILD_RUN = runpy.run_path("scripts/run_hypothesis_prototypes.py")["build_run"]


def test_synthetic_hypothesis_run_is_registered_and_reproducible(tmp_path: Path) -> None:
    output = tmp_path / "run"
    result = _BUILD_RUN(output)
    manifest = result["manifest"]

    assert manifest["run_type"] == "SYNTHETIC_PROGRAM_BEHAVIOR_DIAGNOSTIC_NOT_STRATEGY_BACKTEST"
    assert manifest["input_data_status"] == "SYNTHETIC_ONLY"
    assert manifest["results"]["h1_eligible_rows"] == 1
    assert manifest["results"]["h1_excluded_rows"] == 4
    assert manifest["results"]["h2_simulated_fills_under_open_assumption"] == 1
    assert manifest["release_status"] == {
        "formal_baseline": "NOT_READY",
        "can_continue": False,
        "independent_validation": "NOT_PERFORMED",
    }
    for name, expected in manifest["output_sha256"].items():
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        assert digest == expected
    control_candidate = json.loads((output / "h2-control-candidate-diff.json").read_text())
    assert control_candidate["control"]["replacement_selected"] is False
    assert control_candidate["candidate"]["same_decision_session_open_fill_allowed"] is False
    assert (output / "h2-decision-open-quotes.csv").is_file()
    assert (output / "h2-next-session-open-quotes.csv").is_file()
    assert (output / "h2-trading-calendar.csv").is_file()


def test_synthetic_hypothesis_run_refuses_to_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _BUILD_RUN(output)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"

