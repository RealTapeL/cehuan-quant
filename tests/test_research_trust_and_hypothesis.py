from __future__ import annotations

import json
from pathlib import Path

import pytest

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis
from cehuan.research.trust_labels import build_research_trust_labels


def test_trust_labels_do_not_treat_empty_event_list_as_complete_coverage():
    labels = build_research_trust_labels(
        quality_healthy=True,
        input_file_sha256="a" * 64,
        source_file_checked_during_run=True,
        checkpoint_bound=True,
        market_rule_mode="config_fallback",
        market_rule_fallback_rows=12,
        fee_mode="config_flat_assumption",
        accounting_event_count=0,
        formal_readiness_gate_passed=False,
    )
    assert labels["input_file_integrity"]["status"] == "VERIFIED_DURING_RUN"
    assert labels["historical_availability_and_vendor_vintage"]["status"] == "UNVERIFIED"
    assert labels["market_rule_coverage"]["status"] == "FALLBACK_USED"
    assert labels["fee_coverage"]["status"] == "CONFIG_FLAT_ASSUMPTION"
    assert labels["corporate_action_coverage"]["status"] == "UNVERIFIED_COVERAGE"
    assert labels["corporate_action_coverage"]["typed_events_consumed"] == 0
    assert labels["formal_baseline"]["status"] == "NOT_READY"
    assert labels["overall"] == "UNVERIFIED"


def test_trust_labels_keep_source_review_unknown_even_when_schedules_are_consumed():
    labels = build_research_trust_labels(
        quality_healthy=True,
        input_file_sha256=None,
        source_file_checked_during_run=False,
        checkpoint_bound=False,
        market_rule_mode="effective_dated_schedule",
        market_rule_fallback_rows=0,
        fee_mode="effective_dated_schedule",
        accounting_event_count=2,
        formal_readiness_gate_passed=True,
    )
    assert labels["input_file_integrity"]["status"] == "UNVERIFIED"
    assert labels["market_rule_coverage"]["status"] == "SCHEDULE_CONSUMED_SOURCE_REVIEW_UNVERIFIED"
    assert labels["fee_coverage"]["status"] == "SCHEDULE_CONSUMED_HISTORICAL_AND_PERSONAL_RATE_UNVERIFIED"
    assert labels["corporate_action_coverage"]["status"] == "UNVERIFIED_COVERAGE"
    assert labels["formal_baseline"]["status"] == "READINESS_GATE_PASSED"


def test_synthetic_hypothesis_is_validated_without_applying_it():
    path = Path(__file__).parents[1] / "examples/strategy-research-hypothesis.synthetic.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = validate_strategy_hypothesis(payload)
    assert result == payload
    assert result["status"] == "PROPOSED"
    assert result["may_modify_active_strategy"] is False
    assert result["may_generate_orders"] is False


def test_synthetic_agent_input_matches_trust_and_action_contract():
    root = Path(__file__).parents[1]
    payload = json.loads((root / "examples/strategy-research-agent-input.synthetic.json").read_text(encoding="utf-8"))
    schema = json.loads((root / "examples/strategy-research-agent-input.schema.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert set(schema["required"]).issubset(payload)
    assert set(schema["properties"]["data_trust"]["required"]).issubset(payload["data_trust"])
    assert payload["allowed_actions"]
    assert set(payload["forbidden_actions"]) == {
        "MODIFY_ACTIVE_STRATEGY",
        "EXECUTE_BACKTEST_WITHOUT_APPROVAL",
        "GENERATE_BROKER_ORDER",
        "CONNECT_BROKER_ACCOUNT",
    }
    assert payload["data_trust"]["formal_baseline"]["status"] == "NOT_READY"


def test_hypothesis_rejects_strategy_override_and_broker_order_fields():
    path = Path(__file__).parents[1] / "examples/strategy-research-hypothesis.synthetic.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["may_modify_active_strategy"] = True
    with pytest.raises(ValueError, match="不得直接修改"):
        validate_strategy_hypothesis(payload)
    payload["may_modify_active_strategy"] = False
    payload["proposed_changes"][0]["order_plan"] = {"symbol": "SH600000"}
    with pytest.raises(ValueError, match="不得包含真实委托"):
        validate_strategy_hypothesis(payload)
