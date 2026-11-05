from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from cehuan.research.paper_observation import (
    build_synthetic_paper_observation,
    derive_research_eligibility,
    validate_paper_observation_file,
)


def test_synthetic_paper_observation_rehearsal_tracks_missing_and_retry(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    result = build_synthetic_paper_observation(output)
    report = result["validation"]
    assert report["status"] == "VALID_WITH_LATE_OBSERVATIONS"
    assert report["attempt_count"] == 5
    assert report["recovered_after_failure_count"] == 1
    assert report["final_missing_count"] == 1
    assert report["payload_rebuilt_from_raw"] == "VERIFIED"
    assert report["payload_matches_record"] == "VERIFIED"
    assert report["historical_point_in_time_status"] == "NOT_ESTABLISHED_BY_CAPTURE_ALONE"
    assert report["late_observation_count"] > 0
    assert report["eligibility"][0]["state"] == "UNKNOWN"
    assert "MARKET_RULES_INCOMPLETE_OR_UNKNOWN" in report["eligibility"][0]["unknown_reasons"]
    assert "DECISION_OBSERVATION_NOT_AVAILABLE_BY_DECISION" in report["eligibility"][0]["unknown_reasons"]
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


def test_payload_must_rebuild_from_hash_matching_raw_response(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record_path = output / "paper-observation.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    document["observations"][1]["payload"]["open_price_cny"] = 11.25
    record_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="payload 与原始响应"):
        validate_paper_observation_file(record_path)


def test_parser_version_and_market_event_timeline_are_enforced(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record_path = output / "paper-observation.json"
    original = json.loads(record_path.read_text(encoding="utf-8"))
    changed = json.loads(json.dumps(original))
    changed["observations"][1]["source"]["parser_version"] = "99"
    record_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="解析器版本"):
        validate_paper_observation_file(record_path)

    changed = json.loads(json.dumps(original))
    changed["observations"][1]["request_started_at_utc"] = "2026-11-05T01:31:03Z"
    record_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="请求开始时间晚于"):
        validate_paper_observation_file(record_path)


def test_legacy_v1_remains_readable_but_payload_binding_is_unverified(tmp_path: Path) -> None:
    output = tmp_path / "legacy"
    output.mkdir()
    ranking = output / "ranking.json"
    ranking.write_text('{"symbols":[]}', encoding="utf-8")
    raw = output / "raw.json"
    raw.write_text('{"symbol":"SH600001","time":"old","price":10}', encoding="utf-8")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    document = {
        "schema_version": 1, "record_type": "PAPER_OBSERVATION", "classification": "SYNTHETIC",
        "observation_id": "legacy-v1", "signal": {
            "signal_date": "2026-11-04", "generated_at_utc": "2026-11-04T08:00:00Z",
            "ranking_artifact": {"path": "ranking.json", "sha256": sha(ranking)},
            "config_sha256": "0" * 64,
        }, "observations": [{
            "symbol": "SH600001", "phase": "DECISION", "observation_type": "QUOTE",
            "attempt_no": 1, "attempt_id": "legacy-1", "outcome": "AVAILABLE",
            "source": {"name": "legacy-synthetic", "event_time_utc": "2026-11-05T01:31:00Z", "provider_published_at_utc": None},
            "captured_at_utc": "2026-11-05T01:31:02Z",
            "raw_response": {"path": "raw.json", "sha256": sha(raw)},
            "payload": {"open_price_cny": 10.0},
        }],
    }
    record_path = output / "paper-observation.json"
    record_path.write_text(json.dumps(document), encoding="utf-8")
    result = validate_paper_observation_file(record_path)
    assert result["status"] == "VALID_LEGACY_UNVERIFIED_PAYLOAD_BINDING"
    assert result["payload_rebuilt_from_raw"] == "UNVERIFIED_LEGACY_SCHEMA_HAS_NO_PARSER_BINDING"


def test_can_buy_is_derived_and_distinguishes_blocked_from_unknown() -> None:
    unknown = derive_research_eligibility(
        quote_available=True, is_listed=True, is_suspended=False, is_st=False, exclude_st=True,
        market_rules_complete=None, price_within_limits=None, account_cash_sufficient=None,
    )
    assert unknown["state"] == "UNKNOWN"
    assert "MARKET_RULES_INCOMPLETE_OR_UNKNOWN" in unknown["unknown_reasons"]

    blocked = derive_research_eligibility(
        quote_available=True, is_listed=True, is_suspended=False, is_st=False, exclude_st=True,
        market_rules_complete=True, price_within_limits=True, account_cash_sufficient=False,
    )
    assert blocked["state"] == "BLOCKED"
    assert "INSUFFICIENT_ACCOUNT_CASH" in blocked["blocked_reasons"]


def test_parse_failed_preserves_response_but_does_not_publish_business_payload(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record_path = output / "paper-observation.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    entry = document["observations"][1]
    raw_path = output / entry["raw_response"]["path"]
    raw_path.write_text("not-json", encoding="utf-8")
    entry["raw_response"]["sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    entry["outcome"] = "PARSE_FAILED"
    entry["payload"] = None
    entry.pop("normalized_payload_sha256")
    entry["parse_error"] = "JSON parse failed"
    record_path.write_text(json.dumps(document), encoding="utf-8")
    result = validate_paper_observation_file(record_path)
    assert result["final_parse_failed_count"] == 1
    assert result["execution_status"] == "NO_BROKER_FILL_EVIDENCE"


def test_unknown_raw_status_cannot_be_reported_as_non_suspended(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record_path = output / "paper-observation.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    status_entry = document["observations"][4]
    raw_path = output / status_entry["raw_response"]["path"]
    raw_path.write_text(
        '{"symbol":"SH600001","event_time_utc":"2026-11-06T01:31:00Z","is_listed":true,"is_suspended":null,"is_st":false}\n',
        encoding="utf-8",
    )
    status_entry["raw_response"]["sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    record_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="必须明确包含 is_listed"):
        validate_paper_observation_file(record_path)


def test_attempt_array_order_does_not_change_latest_payload_selection(tmp_path: Path) -> None:
    output = tmp_path / "rehearsal"
    build_synthetic_paper_observation(output)
    record_path = output / "paper-observation.json"
    document = json.loads(record_path.read_text(encoding="utf-8"))
    document["observations"].reverse()
    record_path.write_text(json.dumps(document), encoding="utf-8")
    report = validate_paper_observation_file(record_path)
    decision_quote = next(
        row for row in report["keys"]
        if row["phase"] == "DECISION" and row["observation_type"] == "QUOTE"
    )
    assert decision_quote["final_outcome"] == "AVAILABLE"
    assert report["eligibility"][0]["state"] == "UNKNOWN"
    assert "DECISION_OBSERVATION_NOT_AVAILABLE_BY_DECISION" in report["eligibility"][0]["unknown_reasons"]
