from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from cehuan.research.controlled_experiments import _apply_one_change
from cehuan.research.research_agent import (
    ModelReply,
    StubResearchModel,
    generate_validated_hypothesis,
    resolve_research_model,
    validate_strategy_research_input,
)
from cehuan.research.research_cycle import run_research_cycle
from cehuan.research.synthetic_cycle import synthetic_cycle_config


ROOT = Path(__file__).parents[1]


def _agent_input() -> dict:
    return json.loads((ROOT / "examples/strategy-research-agent-input.synthetic.json").read_text(encoding="utf-8"))


def test_agent_input_schema_and_stub_hypothesis_are_validated():
    payload = validate_strategy_research_input(_agent_input())
    digest = "a" * 64
    context = {
        "research_id": payload["research_id"],
        "experiment_id": "stub-experiment-001",
        "input_data_digest": digest,
        "strategy_parameters": payload["frozen_strategy"]["parameters"],
    }
    hypothesis, audit = generate_validated_hypothesis(
        StubResearchModel(), input_payload=payload, phase="initial",
        input_data_digest=digest, experiment_id="stub-experiment-001", max_retries=0,
    )
    assert hypothesis["experiment_id"] == "stub-experiment-001"
    assert hypothesis["input_data_digest"] == digest
    assert hypothesis["may_modify_active_strategy"] is False
    assert hypothesis["may_generate_orders"] is False
    assert audit["provider"] == "stub"
    assert audit["prompt_version"]


def test_research_agent_rejects_order_or_unknown_action_fields():
    payload = _agent_input()
    digest = "b" * 64
    good, _ = generate_validated_hypothesis(
        StubResearchModel(), input_payload=payload, phase="initial",
        input_data_digest=digest, experiment_id="expected-exp", max_retries=0,
    )

    class UnsafeModel:
        provider = "test"
        model = "unsafe-fixture"

        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            response = copy.deepcopy(good)
            response["action"] = "BUY"
            return ModelReply(json.dumps(response), self.provider, self.model)

    model = UnsafeModel()
    with pytest.raises(ValueError, match="schema 未支持字段或动作") as exc:
        generate_validated_hypothesis(
            model, input_payload=payload, phase="initial",
            input_data_digest=digest, experiment_id="expected-exp", max_retries=1,
        )
    assert model.calls == 2
    assert exc.value.audit["attempt_count"] == 2


def test_model_auto_mode_uses_stub_when_endpoint_or_key_is_missing(monkeypatch):
    monkeypatch.delenv("CEHUAN_LLM_MODEL", raising=False)
    monkeypatch.delenv("CEHUAN_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("CEHUAN_LLM_API_KEY", raising=False)
    model, audit = resolve_research_model({"mode": "auto"})
    assert isinstance(model, StubResearchModel)
    assert audit["selected_mode"] == "stub"
    assert "api_key" not in json.dumps(audit).lower() or audit["api_key_configured"] is False
    with pytest.raises(ValueError, match="LLM 模式缺少配置"):
        resolve_research_model({"mode": "llm"})


def test_configured_llm_metadata_never_contains_api_key(monkeypatch):
    monkeypatch.setenv("CEHUAN_LLM_MODEL", "example-model")
    monkeypatch.setenv("CEHUAN_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("CEHUAN_LLM_API_KEY", "synthetic-secret-for-test")
    model, audit = resolve_research_model({"mode": "auto"})
    assert audit["selected_mode"] == "llm"
    assert audit["model"] == "example-model"
    assert audit["endpoint"] == "https://llm.example/v1"
    assert audit["api_key_configured"] is True
    assert "synthetic-secret-for-test" not in json.dumps(audit)
    assert "synthetic-secret-for-test" not in repr(model)


def test_unknown_experiment_rule_is_marked_pending_not_ignored():
    config = synthetic_cycle_config()
    candidate, reason = _apply_one_change(config, {
        "config_path": "market.new_rule",
        "current_value": None,
        "proposed_value": "ENABLED",
    })
    assert candidate is None
    assert "PENDING_IMPLEMENTATION" in reason


def test_stub_completes_synthetic_research_experiment_review_and_next_hypothesis(tmp_path):
    before = (ROOT / "configs/default.toml").read_bytes()
    run_dir = run_research_cycle(
        output_root=tmp_path / "research",
        objective="验证有限参数假设能否进入同数据合成候选对照",
        scenario="missing_positions",
        model_mode="stub",
    )
    manifest = json.loads((run_dir / "research_cycle_manifest.json").read_text(encoding="utf-8"))
    initial = json.loads((run_dir / "hypothesis_initial.json").read_text(encoding="utf-8"))
    next_hypothesis = json.loads((run_dir / "hypothesis_next.json").read_text(encoding="utf-8"))
    next_input = json.loads((run_dir / "agent_input_next.json").read_text(encoding="utf-8"))
    experiment = json.loads((run_dir / "candidate_experiment.json").read_text(encoding="utf-8"))
    report = (run_dir / "research_report.md").read_text(encoding="utf-8")
    review = (run_dir / "review.md").read_text(encoding="utf-8")

    assert manifest["research_cycle_id"] == manifest["run_id"]
    assert manifest["model"]["selected_mode"] == "stub"
    assert manifest["model"]["provider"] == "stub"
    assert manifest["model"]["call_count"] == 2
    assert manifest["formal_baseline"] == "NOT_READY"
    assert manifest["account_data_used"] is False
    assert manifest["broker_connected"] is False and manifest["orders_generated"] is False
    assert experiment["status"] == "COMPLETED_SYNTHETIC_ONLY"
    assert experiment["same_data_and_initial_conditions"] is True
    assert experiment["changed_parameters"][0]["config_path"] == "strategy.rebalance_sessions"
    assert initial["experiment_id"] == experiment["experiment_id"]
    assert next_hypothesis["experiment_id"] == f"{manifest['research_cycle_id']}-next-round"
    assert next_input["research_id"] == manifest["research_cycle_id"]
    assert next_input["review_summary"]["position_snapshot_status"] == "NOT_PROVIDED"
    assert "SYNTHETIC ONLY" in review
    assert "NOT_READY" in report
    assert "候选合成净值收益字段" in report
    assert "不代表 A 股策略收益" in report
    for artifact_name, expected_sha256 in manifest["artifacts"].items():
        actual_sha256 = hashlib.sha256((run_dir / artifact_name).read_bytes()).hexdigest()
        assert actual_sha256 == expected_sha256, artifact_name
    assert "remain `NOT_READY`" not in report
    assert (ROOT / "configs/default.toml").read_bytes() == before
