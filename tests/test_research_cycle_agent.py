from __future__ import annotations

import copy
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cehuan.research.controlled_experiments import (
    MAX_CHANGES_PER_EXPERIMENT,
    SUPPORTED_HYPOTHESIS_PATHS,
    _apply_one_change,
    build_experiment_capabilities,
)
from cehuan.research.research_agent import (
    ModelReply,
    OpenAICompatibleResearchModel,
    ResearchModelRequestError,
    StubResearchModel,
    build_hypothesis_prompt,
    generate_validated_hypothesis,
    resolve_research_model,
    validate_strategy_research_input,
)
from cehuan.research.research_cycle import (
    _build_next_round_review_summary,
    run_research_cycle,
)
from cehuan.research.synthetic_cycle import synthetic_cycle_config


ROOT = Path(__file__).parents[1]


def _agent_input() -> dict:
    payload = json.loads((ROOT / "examples/strategy-research-agent-input.synthetic.json").read_text(encoding="utf-8"))
    config = synthetic_cycle_config()
    payload["frozen_strategy"]["parameters"] = config["strategy"]
    payload["experiment_capabilities"] = build_experiment_capabilities(config)
    return payload


def test_agent_input_schema_and_stub_hypothesis_are_validated():
    payload = validate_strategy_research_input(_agent_input())
    digest = "a" * 64
    context = {
        "research_id": payload["research_id"],
        "experiment_id": "stub-experiment-001",
        "input_data_digest": digest,
        "strategy_parameters": payload["frozen_strategy"]["parameters"],
        "experiment_capabilities": payload["experiment_capabilities"],
        "research_input": payload,
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


def test_model_prompt_capability_list_is_generated_from_executor_contract():
    payload = _agent_input()
    capability = payload["experiment_capabilities"]
    paths = [item["config_path"] for item in capability["supported_parameters"]]
    assert paths == list(SUPPORTED_HYPOTHESIS_PATHS)
    assert capability["maximum_changes_per_experiment"] == MAX_CHANGES_PER_EXPERIMENT
    for item in capability["supported_parameters"]:
        assert item["current_value"] == payload["frozen_strategy"]["parameters"][item["config_path"].split(".", 1)[1]]
        assert item["minimum_inclusive"] == SUPPORTED_HYPOTHESIS_PATHS[item["config_path"]]["minimum"]
        assert item["maximum_inclusive"] == SUPPORTED_HYPOTHESIS_PATHS[item["config_path"]]["maximum"]

    prompt = build_hypothesis_prompt(
        input_payload=payload, phase="initial", input_data_digest="a" * 64,
        experiment_id="capability-test",
    )
    for path in paths:
        assert path in prompt
    assert '"maximum_changes_per_experiment": 1' in prompt
    assert '"artifact_sha256": "' + "a" * 64 + '"' in prompt
    assert '"evidence_status": "SYNTHETIC"' in prompt


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
    monkeypatch.setenv("CEHUAN_LLM_REASONING_EFFORT", "none")
    model, audit = resolve_research_model({"mode": "auto"})
    assert audit["selected_mode"] == "llm"
    assert audit["model"] == "example-model"
    assert audit["endpoint"] == "https://llm.example/v1"
    assert audit["api_key_configured"] is True
    assert audit["reasoning_effort"] == "none"
    assert audit["timeout_seconds"] == 120
    assert "synthetic-secret-for-test" not in json.dumps(audit)
    assert "synthetic-secret-for-test" not in repr(model)


def test_openai_compatible_adapter_uses_json_request_and_keeps_key_out_of_body():
    captured = {}
    response_body = json.dumps({
        "choices": [{"message": {"content": '{"synthetic": true}'}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
    }).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            captured.setdefault("requests", []).append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": self.rfile.read(int(self.headers["Content-Length"])),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api_key = "local-test-key-must-not-be-in-payload"
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        model = OpenAICompatibleResearchModel(model="local-fixture-model", base_url=base_url, api_key=api_key)
        reply = model.generate(phase="initial", prompt="synthetic-only prompt", context={})
        reduced_reasoning = OpenAICompatibleResearchModel(
            model="local-fixture-model", base_url=base_url, api_key=api_key,
            reasoning_effort="none",
        )
        reduced_reasoning.generate(phase="initial", prompt="synthetic-only prompt", context={})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(captured["requests"]) == 2
    request_record = captured["requests"][0]
    assert request_record["path"] == "/v1/chat/completions"
    assert request_record["authorization"] == f"Bearer {api_key}"
    assert api_key.encode() not in request_record["body"]
    request = json.loads(request_record["body"])
    assert request["model"] == "local-fixture-model"
    assert request["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in request
    assert "synthetic-only prompt" in request["messages"][1]["content"]
    assert reply.content == '{"synthetic": true}'
    assert reply.endpoint == base_url
    assert reply.usage == {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29}
    assert json.loads(captured["requests"][1]["body"])["reasoning_effort"] == "none"


def test_llm_transport_timeout_is_not_retried_to_avoid_duplicate_requests():
    payload = _agent_input()
    digest = "c" * 64

    class TimeoutModel:
        provider = "test"
        model = "timeout-fixture"
        safe_endpoint = "https://llm.example/v1"

        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            raise ResearchModelRequestError("LLM 请求失败：TimeoutError")

    model = TimeoutModel()
    with pytest.raises(ValueError, match="TimeoutError") as exc:
        generate_validated_hypothesis(
            model,
            input_payload=payload,
            phase="initial",
            input_data_digest=digest,
            experiment_id="timeout-fixture-exp",
            max_retries=1,
        )
    assert model.calls == 1
    assert exc.value.audit["attempt_count"] == 1


def test_invalid_synthetic_evidence_retry_includes_specific_binding_feedback():
    payload = _agent_input()
    payload["data_trust"]["overall"] = "SYNTHETIC_ONLY_NOT_MARKET_EVIDENCE"
    digest = "d" * 64
    good, _ = generate_validated_hypothesis(
        StubResearchModel(), input_payload=payload, phase="initial",
        input_data_digest=digest, experiment_id="evidence-retry-exp", max_retries=0,
    )

    class CorrectingModel:
        provider = "test"
        model = "evidence-retry-fixture"

        def __init__(self):
            self.prompts = []

        def generate(self, *, phase, prompt, context):
            self.prompts.append(prompt)
            response = copy.deepcopy(good)
            if len(self.prompts) == 1:
                response["evidence"][0]["artifact_sha256"] = "e" * 64
            return ModelReply(json.dumps(response), self.provider, self.model)

    model = CorrectingModel()
    hypothesis, audit = generate_validated_hypothesis(
        model,
        input_payload=payload,
        phase="initial",
        input_data_digest=digest,
        experiment_id="evidence-retry-exp",
        max_retries=1,
    )
    assert audit["attempt_count"] == 2
    assert hypothesis["evidence"][0]["artifact_sha256"] == digest
    assert "artifact_sha256 必须等于本轮输入摘要" in model.prompts[1]
    assert digest in model.prompts[1]


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
    summary = next_input["review_summary"]
    program_facts = summary["program_facts"]
    assert program_facts["experiment"]["status"] == experiment["status"]
    assert program_facts["experiment"]["changed_parameters"] == experiment["changed_parameters"]
    assert program_facts["metrics_status"] == "AVAILABLE_FROM_HASH_BOUND_PERSISTED_METRICS"
    baseline_metrics = json.loads((run_dir / "baseline_metrics.json").read_text(encoding="utf-8"))
    candidate_metrics = json.loads((run_dir / "candidate_metrics.json").read_text(encoding="utf-8"))
    assert program_facts["baseline_metrics"] == baseline_metrics
    assert program_facts["candidate_metrics"] == candidate_metrics
    assert program_facts["metric_comparison"]["total_return"]["baseline"] == baseline_metrics["total_return"]
    assert program_facts["metric_comparison"]["total_return"]["candidate"] == candidate_metrics["total_return"]
    assert program_facts["reconciliation"]["position_snapshot_status"] == "NOT_PROVIDED"
    assert summary["agent_authored_context"]["classification"] == "AGENT_TEXT_NOT_PROGRAM_FACT"
    source_hashes = {item["role"]: item.get("sha256") for item in program_facts["source_artifacts"]}
    assert source_hashes["candidate_metrics"] == hashlib.sha256((run_dir / "candidate_metrics.json").read_bytes()).hexdigest()
    assert source_hashes["trade_reconciliation"] == hashlib.sha256((run_dir / "reconciliation.csv").read_bytes()).hexdigest()
    assert "SYNTHETIC ONLY" in review
    assert "NOT_READY" in report
    assert "## 成交与持仓复盘事实" in report
    assert "候选合成净值收益字段" in report
    assert "不代表 A 股策略收益" in report
    for artifact_name, expected_sha256 in manifest["artifacts"].items():
        actual_sha256 = hashlib.sha256((run_dir / artifact_name).read_bytes()).hexdigest()
        assert actual_sha256 == expected_sha256, artifact_name
    assert "remain `NOT_READY`" not in report
    assert (ROOT / "configs/default.toml").read_bytes() == before


@pytest.mark.parametrize("status", ["FAILED", "NEEDS_IMPLEMENTATION_OR_REVIEW", "BLOCKED_BY_HYPOTHESIS_STATUS"])
def test_next_round_context_does_not_invent_candidate_metrics_when_experiment_not_completed(tmp_path, status):
    baseline_metrics = {"total_return": 0.12, "trade_count": 4}
    baseline_path = tmp_path / "baseline_metrics.json"
    baseline_path.write_text(json.dumps(baseline_metrics), encoding="utf-8")
    registration = {
        "schema": "cehuan.controlled_synthetic_experiment.v1",
        "experiment_id": "failed-experiment",
        "hypothesis_id": "failed-hypothesis",
        "status": status,
        "baseline_metrics_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "changed_parameters": [],
        "pending_implementation": ["synthetic test status"],
    }
    (tmp_path / "candidate_experiment.json").write_text(json.dumps(registration), encoding="utf-8")
    (tmp_path / "candidate_metrics.json").write_text(json.dumps({"total_return": 999}), encoding="utf-8")
    facts = {"position_snapshot_status": "NOT_PROVIDED", "reconciliation_facts": []}
    hypothesis = {"hypothesis_id": "initial", "status": "PROPOSED", "problem_observed": "agent words"}

    context = _build_next_round_review_summary(
        tmp_path, reconciliation_facts=facts, initial_hypothesis=hypothesis,
    )

    program = context["program_facts"]
    assert program["experiment"]["status"] == status
    assert program["metrics_status"] == "EXPERIMENT_NOT_COMPLETED"
    assert program["baseline_metrics"] == baseline_metrics
    assert program["candidate_metrics"] is None
    assert program["metric_comparison"] is None
    assert program["reconciliation"] == facts
    assert context["agent_authored_context"]["classification"] == "AGENT_TEXT_NOT_PROGRAM_FACT"


def test_next_round_context_blocks_metrics_with_mismatched_artifact_hash(tmp_path):
    baseline_path = tmp_path / "baseline_metrics.json"
    candidate_path = tmp_path / "candidate_metrics.json"
    baseline_path.write_text(json.dumps({"total_return": 0.1}), encoding="utf-8")
    candidate_path.write_text(json.dumps({"total_return": 0.2}), encoding="utf-8")
    (tmp_path / "candidate_experiment.json").write_text(json.dumps({
        "status": "COMPLETED_SYNTHETIC_ONLY",
        "baseline_metrics_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "candidate_metrics_sha256": "0" * 64,
    }), encoding="utf-8")

    context = _build_next_round_review_summary(
        tmp_path,
        reconciliation_facts={"reconciliation_facts": [], "position_snapshot_status": "UNKNOWN"},
        initial_hypothesis={},
    )

    assert context["program_facts"]["metrics_status"] == "CANDIDATE_METRIC_ARTIFACT_HASH_MISMATCH"
    assert context["program_facts"]["baseline_metrics"] is not None
    assert context["program_facts"]["candidate_metrics"] is None
    assert context["program_facts"]["metric_comparison"] is None
