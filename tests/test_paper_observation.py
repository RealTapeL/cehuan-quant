from __future__ import annotations

import json
from pathlib import Path

import pytest

from cehuan.research.paper_observation import (
    build_synthetic_paper_observation,
    validate_paper_observation_file,
)


def test_synthetic_paper_observation_rehearsal_tracks_missing_and_retry(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    result = build_synthetic_paper_observation(output)
    report = result["validation"]
    assert report["status"] == "VALID"
    assert report["attempt_count"] == 5
    assert report["recovered_after_failure_count"] == 1
    assert report["final_missing_count"] == 1
    assert report["historical_point_in_time_claim"].startswith("NOT_ESTABLISHED")
    assert validate_paper_observation_file(output / "paper-observation.json")["orders_or_broker_calls"] is False
    with pytest.raises(FileExistsError):
        build_synthetic_paper_observation(output)


def test_paper_observation_rejects_raw_response_hash_mismatch(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    raw = output / "raw/decision-quote-retry.json"
    raw.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        validate_paper_observation_file(output / "paper-observation.json")


def test_paper_observation_rejects_overwritten_retry_sequence(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record = output / "paper-observation.json"
    document = json.loads(record.read_text(encoding="utf-8"))
    document["observations"][1]["attempt_no"] = 1
    record.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="attempt_no"):
        validate_paper_observation_file(record)
