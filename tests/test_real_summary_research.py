from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cehuan.config import load_config
from cehuan.research.real_summary_research import (
    REAL_SUMMARY_CLASSIFICATION,
    _build_agent_input,
    _load_frozen_summary,
    _proposal_review,
)
from cehuan.research.research_agent import (
    ModelReply,
    ResearchHypothesisRejected,
    ResearchModelRequestError,
    build_hypothesis_prompt,
    generate_validated_hypothesis,
)


FAKE_SUMMARY_SHA256 = hashlib.sha256(b"sanitized test summary").hexdigest()
FACT_SHA256 = hashlib.sha256(b"recorded local test artifact").hexdigest()


def _summary() -> dict:
    return {
        "summary_id": "summary-test-01",
        "classification": REAL_SUMMARY_CLASSIFICATION,
        "scope": {
            "start_date": "2024-10-08",
            "end_date": "2025-01-10",
            "universe_snapshot_id": "test-local-universe",
        },
        "dataset": {"date_count": 68, "row_count": 100},
        "raw_quality_audit": {},
        "point_in_time_audit": {},
        "strategy": {"config_sha256": "a" * 64},
        "signal_observations": {},
        "configured_fallbacks_not_historically_verified": {},
        "known_limitations": ["历史可得时间 UNVERIFIED", "正式基线 NOT_READY"],
        "prior_research_state": {"formal_baseline": "NOT_READY"},
        "evidence_catalog": [{
            "fact_id": "TEST_REAL_FACT",
            "statement": "本地合成测试夹具中的程序事实，只用于测试结构校验。",
            "artifact_sha256": FACT_SHA256,
            "evidence_status": "RECORDED",
        }],
    }


def _hypothesis(digest: str, experiment_id: str, fact_sha: str = FACT_SHA256) -> dict:
    return {
        "schema_version": 1,
        "hypothesis_id": "summary-test-01-initial-hypothesis",
        "experiment_id": experiment_id,
        "input_data_digest": digest,
        "problem_observed": "样本的停牌与动量观测需要分层核验。",
        "proposed_changes": [{
            "config_path": "strategy.lookback_sessions",
            "current_value": 20,
            "proposed_value": 15,
            "reason": "仅作为待验证的一因子研究候选。",
        }],
        "rationale": "短区间内的候选排序可能受观察窗状态影响。",
        "evidence": [{
            "description": "引用摘要中的明确事实。",
            "artifact_sha256": fact_sha,
            "evidence_status": "RECORDED",
            "source_fact_id": "TEST_REAL_FACT",
        }],
        "required_data": ["核验历史停牌状态及价格口径"],
        "known_limitations": [],
        "validation_plan": "补齐数据后，在相同冻结样本中比较信号稳定性。",
        "status": "NEEDS_DATA",
        "human_review_required": True,
        "may_modify_active_strategy": False,
        "may_generate_orders": False,
    }


class DeterministicTestModel:
    provider = "test-only"
    model = "deterministic-test-model"
    safe_endpoint = None

    def __init__(self, fact_sha: str = FACT_SHA256):
        self.fact_sha = fact_sha
        self.prompts: list[str] = []

    def generate(self, *, phase: str, prompt: str, context: dict) -> ModelReply:
        self.prompts.append(prompt)
        result = _hypothesis(context["input_data_digest"], f"{context['research_id']}-test", self.fact_sha)
        # The orchestrator supplies its registered experiment id in the prompt/context contract.
        result["experiment_id"] = context["experiment_id"]
        result["hypothesis_id"] = f"{context['research_id']}-{phase}-hypothesis"
        return ModelReply(json.dumps(result), self.provider, self.model, usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})


def _agent_input() -> dict:
    config = load_config()
    return _build_agent_input(_summary(), FAKE_SUMMARY_SHA256, config, "test-real-research")


def test_real_summary_agent_input_is_not_synthetic_and_uses_live_capability_map():
    payload = _agent_input()
    assert payload["data_trust"]["overall"] == REAL_SUMMARY_CLASSIFICATION
    assert payload["market_research_summary"]["summary_sha256"] == FAKE_SUMMARY_SHA256
    assert payload["experiment_capabilities"]["supported_parameters"]
    assert "SYNTHETIC_ONLY" not in payload["data_trust"]["overall"]
    assert "data/private" not in json.dumps(payload)
    assert "api_key" not in json.dumps(payload).lower()


def test_real_summary_prompt_uses_catalog_and_does_not_request_synthetic_evidence():
    payload = _agent_input()
    prompt = build_hypothesis_prompt(
        input_payload=payload,
        phase="initial",
        input_data_digest=FAKE_SUMMARY_SHA256,
        experiment_id="test-experiment",
    )
    assert "只引用 market_research_summary.evidence_catalog 中的 fact_id" in prompt
    assert "source_fact_id" in prompt
    assert "evidence_status=SYNTHETIC" not in prompt
    assert REAL_SUMMARY_CLASSIFICATION in prompt


def test_real_hypothesis_requires_exact_summary_fact_binding():
    payload = _agent_input()
    model = DeterministicTestModel()
    hypothesis, audit = generate_validated_hypothesis(
        model,
        input_payload=payload,
        phase="initial",
        input_data_digest=FAKE_SUMMARY_SHA256,
        experiment_id="test-experiment",
        max_retries=0,
    )
    assert hypothesis["status"] == "NEEDS_DATA"
    assert hypothesis["evidence"][0]["source_fact_id"] == "TEST_REAL_FACT"
    assert audit["attempt_count"] == 1
    assert _proposal_review(hypothesis, load_config())["status"] == "VALIDATED_PROPOSAL_NOT_EXECUTED"

    invalid_model = DeterministicTestModel("b" * 64)
    with pytest.raises(ResearchHypothesisRejected, match="真实行情摘要 evidence 校验失败"):
        generate_validated_hypothesis(
            invalid_model,
            input_payload=payload,
            phase="initial",
            input_data_digest=FAKE_SUMMARY_SHA256,
            experiment_id="test-experiment",
            max_retries=0,
        )


def test_unsupported_rule_proposal_is_explicitly_pending_implementation():
    hypothesis = _hypothesis(FAKE_SUMMARY_SHA256, "test")
    hypothesis["proposed_changes"][0]["config_path"] = "strategy.unimplemented_rule"
    result = _proposal_review(hypothesis, load_config())
    assert result["status"] == "PENDING_IMPLEMENTATION"
    assert result["items"][0]["status"] == "PENDING_IMPLEMENTATION"


def test_frozen_summary_rechecks_all_source_hashes_before_reuse(tmp_path: Path):
    (tmp_path / "outputs").mkdir()
    source_ids = {
        "bars", "snapshot_manifest", "quality", "universe", "universe_audit",
        "point_in_time_quality", "strategy_config", "trade_audit", "candidate_ranks",
    }
    sources = []
    for source_id in sorted(source_ids):
        relative = f"private/{source_id}.csv"
        source_path = tmp_path / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source_id, encoding="utf-8")
        sources.append({
            "source_id": source_id,
            "path": relative,
            "sha256": hashlib.sha256(source_id.encode()).hexdigest(),
        })
    summary = {
        "schema": "cehuan.real_market_research_summary.v1",
        "classification": REAL_SUMMARY_CLASSIFICATION,
        "sources": sources,
        "evidence_catalog": [{"artifact_sha256": sources[0]["sha256"]}],
        "safety": {
            "account_data_included": False,
            "broker_fill_files_included": False,
            "raw_market_bars_included_in_agent_input": False,
            "orders_generated": False,
        },
    }
    summary_path = tmp_path / "outputs" / "market_summary.json"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    loaded = _load_frozen_summary(summary_path, repository_root=tmp_path, verify_reconstruction=False)
    assert loaded["classification"] == REAL_SUMMARY_CLASSIFICATION

    rebuilt = dict(summary)
    altered = dict(summary)
    altered["operator_note"] = "unbound injected text"
    summary_path.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="重建的内容不一致"):
        _load_frozen_summary(
            summary_path,
            repository_root=tmp_path,
            summary_rebuilder=lambda summary_id: rebuilt,
        )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    (tmp_path / sources[0]["path"]).write_text("changed after summary freeze", encoding="utf-8")
    with pytest.raises(ValueError, match="来源文件缺失或哈希变化"):
        _load_frozen_summary(summary_path, repository_root=tmp_path, verify_reconstruction=False)


def test_real_summary_hypothesis_cannot_claim_ready_or_contain_order_fields():
    payload = _agent_input()

    class UnsafeTestModel:
        provider = "test-only"
        model = "unsafe-test-model"
        safe_endpoint = None

        def generate(self, *, phase: str, prompt: str, context: dict) -> ModelReply:
            invalid = _hypothesis(context["input_data_digest"], context["experiment_id"])
            invalid["status"] = "PROPOSED"
            invalid["orders"] = []
            return ModelReply(json.dumps(invalid), self.provider, self.model)

    with pytest.raises(ResearchHypothesisRejected):
        generate_validated_hypothesis(
            UnsafeTestModel(),
            input_payload=payload,
            phase="initial",
            input_data_digest=FAKE_SUMMARY_SHA256,
            experiment_id="test-experiment",
            max_retries=0,
        )


def test_real_summary_network_failure_is_classified_as_request_failure():
    payload = _agent_input()

    class NoNetworkTestModel:
        provider = "test-only"
        model = "offline-test-model"
        safe_endpoint = None

        def generate(self, *, phase: str, prompt: str, context: dict) -> ModelReply:
            raise ResearchModelRequestError("LLM 请求失败：TimeoutError")

    with pytest.raises(ResearchHypothesisRejected) as raised:
        generate_validated_hypothesis(
            NoNetworkTestModel(),
            input_payload=payload,
            phase="initial",
            input_data_digest=FAKE_SUMMARY_SHA256,
            experiment_id="test-experiment",
            max_retries=1,
        )
    assert raised.value.audit["response_classification"] == "MODEL_REQUEST_FAILED"
    assert raised.value.audit["attempt_count"] == 1
