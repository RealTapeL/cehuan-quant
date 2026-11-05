"""Small, replaceable structured-output adapter for the strategy research agent."""

from __future__ import annotations

import json
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Protocol

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


INPUT_REQUIRED = {
    "schema_version", "research_id", "objective", "scope", "data_trust",
    "frozen_strategy", "prior_experiments", "known_data_gaps", "allowed_actions",
    "forbidden_actions",
}
TRUST_FIELDS = {
    "input_file_integrity", "canonical_data_quality", "checkpoint_binding",
    "historical_availability_and_vendor_vintage", "market_rule_coverage", "fee_coverage",
    "corporate_action_coverage", "formal_baseline", "overall",
}
FORBIDDEN_ACTIONS = {
    "MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL",
    "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT",
}
PROMPT_VERSION = "cehuan-structured-research-v2"
HYPOTHESIS_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "examples" / "strategy-research-hypothesis.schema.json"


def validate_strategy_research_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the existing strategy-research-agent input contract without a new dependency."""
    missing = INPUT_REQUIRED - set(payload)
    extra = set(payload) - INPUT_REQUIRED - {"review_summary"}
    if missing or extra:
        raise ValueError(
            "研究 Agent 输入字段不符合 schema："
            + ("缺少 " + ", ".join(sorted(missing)) if missing else "")
            + ("；不支持 " + ", ".join(sorted(extra)) if extra else "")
        )
    if payload.get("schema_version") != 1:
        raise ValueError("研究 Agent 输入 schema_version 必须为 1")
    for key in ("research_id", "objective"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"研究 Agent 输入 {key} 必须为非空字符串")
    scope = payload.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {"market", "start_date", "end_date", "universe_id"}:
        raise ValueError("研究 Agent 输入 scope 字段与 schema 不一致")
    if scope.get("market") != "CN_A_SHARE" or not isinstance(scope.get("universe_id"), str):
        raise ValueError("研究 Agent 输入 scope 市场或股票池 ID 无效")
    try:
        for key in ("start_date", "end_date"):
            if not isinstance(scope[key], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scope[key]):
                raise ValueError("date must be YYYY-MM-DD")
        date.fromisoformat(scope["start_date"])
        date.fromisoformat(scope["end_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("研究 Agent 输入日期必须为 ISO 日期") from exc
    if scope["end_date"] < scope["start_date"]:
        raise ValueError("研究 Agent 输入 scope 结束日期早于开始日期")
    trust = payload.get("data_trust")
    if not isinstance(trust, Mapping) or not TRUST_FIELDS.issubset(trust):
        raise ValueError("研究 Agent 输入缺少结构化 data_trust 状态")
    for key in TRUST_FIELDS - {"overall"}:
        value = trust[key]
        if not isinstance(value, Mapping) or not isinstance(value.get("status"), str):
            raise ValueError(f"data_trust.{key}.status 必须是字符串")
    if not isinstance(trust["overall"], str):
        raise ValueError("data_trust.overall 必须是字符串")
    frozen = payload.get("frozen_strategy")
    if not isinstance(frozen, Mapping) or set(frozen) != {"strategy_id", "config_sha256", "parameters"}:
        raise ValueError("研究 Agent 输入 frozen_strategy 字段与 schema 不一致")
    if not isinstance(frozen["strategy_id"], str) or not isinstance(frozen["config_sha256"], str):
        raise ValueError("frozen_strategy 标识和配置散列必须是字符串")
    if len(frozen["config_sha256"]) != 64 or any(c not in "0123456789abcdefABCDEF" for c in frozen["config_sha256"]):
        raise ValueError("frozen_strategy.config_sha256 必须是 SHA-256")
    if not isinstance(frozen["parameters"], Mapping):
        raise ValueError("frozen_strategy.parameters 必须是对象")
    if not isinstance(payload.get("prior_experiments"), list):
        raise ValueError("prior_experiments 必须是列表")
    for index, experiment in enumerate(payload["prior_experiments"]):
        if not isinstance(experiment, Mapping) or set(experiment) != {"experiment_id", "result_type", "summary", "input_digest"}:
            raise ValueError(f"prior_experiments[{index}] 字段与 schema 不一致")
        if not all(isinstance(experiment[key], str) and experiment[key] for key in ("experiment_id", "result_type", "summary")):
            raise ValueError(f"prior_experiments[{index}] 文本字段无效")
        digest = experiment["input_digest"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ValueError(f"prior_experiments[{index}].input_digest 必须是 SHA-256")
    if not isinstance(payload.get("known_data_gaps"), list) or not all(
        isinstance(item, str) for item in payload["known_data_gaps"]
    ):
        raise ValueError("known_data_gaps 必须是字符串列表")
    if not isinstance(payload.get("allowed_actions"), list) or not payload["allowed_actions"]:
        raise ValueError("allowed_actions 必须是非空列表")
    if len(set(payload["allowed_actions"])) != len(payload["allowed_actions"]):
        raise ValueError("allowed_actions 不得重复")
    allowed = {"PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"}
    if set(payload["allowed_actions"]) - allowed:
        raise ValueError("研究 Agent 输入包含不支持的 allowed_actions")
    if set(payload.get("forbidden_actions", [])) != FORBIDDEN_ACTIONS:
        raise ValueError("研究 Agent 输入必须完整禁止策略修改、无审批回测和券商操作")
    if "review_summary" in payload and payload["review_summary"] is not None and not isinstance(payload["review_summary"], Mapping):
        raise ValueError("review_summary 必须是对象或 null")
    return dict(payload)


@dataclass(frozen=True)
class ModelReply:
    content: str
    provider: str
    model: str
    endpoint: str | None = None
    usage: dict[str, int] | None = None


class ResearchHypothesisRejected(ValueError):
    def __init__(self, message: str, *, audit: dict[str, Any]):
        self.audit = audit
        super().__init__(message)


class ResearchModel(Protocol):
    provider: str
    model: str

    def generate(self, *, phase: str, prompt: str, context: Mapping[str, Any]) -> ModelReply: ...


class StubResearchModel:
    provider = "stub"
    model = "cehuan-structured-stub-v1"

    def generate(self, *, phase: str, prompt: str, context: Mapping[str, Any]) -> ModelReply:
        current = context["strategy_parameters"]
        if phase == "initial":
            path = "strategy.rebalance_sessions"
            proposed = 10 if current.get("rebalance_sessions") != 10 else 15
            observation = "合成闭环没有市场证据；Stub 仅用于验证结构化接口和受控实验串接。"
        else:
            path = "strategy.lookback_sessions"
            proposed = 8 if current.get("lookback_sessions") != 8 else 10
            observation = "复盘只使用假成交与假持仓算术；单次合成场景不能支持真实策略调整。"
        payload = {
            "schema_version": 1,
            "hypothesis_id": f"{context['research_id']}-{phase}-hypothesis",
            "experiment_id": context["experiment_id"],
            "input_data_digest": context["input_data_digest"],
            "problem_observed": observation,
            "proposed_changes": [{
                "config_path": path,
                "current_value": current.get(path.split(".", 1)[1]),
                "proposed_value": proposed,
                "reason": "作为一项待验证参数假设；不修改当前生效策略。",
            }],
            "rationale": "在相同合成数据和初始条件下比较单一参数变化，观察程序指标是否按预期变化。",
            "evidence": [{
                "description": "合成行情与程序产物，仅证明流程运行，不是市场证据。",
                "artifact_sha256": context["input_data_digest"],
                "evidence_status": "SYNTHETIC",
            }],
            "required_data": ["独立、按时点冻结的历史行情", "有效日期交易规则和费用", "完整公司行动记录"],
            "known_limitations": ["输入为合成数据", "正式基线 NOT_READY", "结果不代表真实收益"],
            "validation_plan": "冻结输入与配置后，在相同交易假设下比较基线和候选；任何采用仍需人工审核。",
            "status": "PROPOSED",
            "human_review_required": True,
            "may_modify_active_strategy": False,
            "may_generate_orders": False,
        }
        return ModelReply(json.dumps(payload, ensure_ascii=False), self.provider, self.model)


class OpenAICompatibleResearchModel:
    provider = "openai-compatible-chat-completions"

    def __init__(self, *, model: str, base_url: str, api_key: str, timeout_seconds: float = 45) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("LLM base URL 必须是有效 HTTP(S) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM 凭据必须放在 API Key 环境变量中，base URL 不得包含凭据或 query")
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程 LLM endpoint 必须使用 HTTPS；HTTP 仅允许 loopback 本地服务")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key

    @property
    def safe_endpoint(self) -> str:
        parsed = urllib.parse.urlsplit(self.base_url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def generate(self, *, phase: str, prompt: str, context: Mapping[str, Any]) -> ModelReply:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps({
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是策环的研究假设助手。只输出符合给定 JSON 契约的对象；不生成订单、不调用工具、不声称合成数据证明收益。"},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"LLM 请求失败，HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"LLM 请求失败：{type(exc).__name__}") from None
        try:
            message = result["choices"][0]["message"]["content"]
            if not isinstance(message, str):
                raise TypeError("content is not a string")
        except (KeyError, IndexError, TypeError):
            raise ValueError("LLM 响应缺少 choices[0].message.content") from None
        usage_raw = result.get("usage") or {}
        usage = {
            key: int(usage_raw[key])
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage_raw.get(key), (int, float))
        }
        return ModelReply(message, self.provider, self.model, self.safe_endpoint, usage)


def resolve_research_model(settings: Mapping[str, Any], requested_mode: str | None = None) -> tuple[ResearchModel, dict[str, Any]]:
    mode = (requested_mode or str(settings.get("mode", "auto"))).lower()
    if mode not in {"auto", "stub", "llm"}:
        raise ValueError("研究 Agent mode 只能是 auto、stub 或 llm")
    model_env = str(settings.get("model_env", "CEHUAN_LLM_MODEL"))
    base_env = str(settings.get("base_url_env", "CEHUAN_LLM_BASE_URL"))
    key_env = str(settings.get("api_key_env", "CEHUAN_LLM_API_KEY"))
    model_name = os.environ.get(model_env, str(settings.get("model", ""))).strip()
    base_url = os.environ.get(base_env, str(settings.get("base_url", ""))).strip()
    api_key = os.environ.get(key_env, "")
    configured = bool(model_name and base_url and api_key)
    selected_mode = "llm" if mode == "llm" or (mode == "auto" and configured) else "stub"
    audit = {
        "requested_mode": mode,
        "selected_mode": selected_mode,
        "model": model_name if selected_mode == "llm" else StubResearchModel.model,
        "api_key_env": key_env if api_key else None,
        "api_key_configured": bool(api_key),
        "endpoint": None,
    }
    if selected_mode == "stub":
        if mode == "llm":
            raise ValueError(f"LLM 模式缺少配置；请设置 {model_env}、{base_env} 和 {key_env}")
        return StubResearchModel(), audit
    if not configured:
        raise ValueError(f"LLM 模式缺少配置；请设置 {model_env}、{base_env} 和 {key_env}")
    model = OpenAICompatibleResearchModel(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(settings.get("timeout_seconds", 45)),
    )
    audit["endpoint"] = model.safe_endpoint
    return model, audit


def build_hypothesis_prompt(
    *,
    input_payload: Mapping[str, Any],
    phase: str,
    input_data_digest: str,
    experiment_id: str,
) -> str:
    output_schema = json.loads(HYPOTHESIS_SCHEMA_PATH.read_text(encoding="utf-8"))
    phase_instruction = (
        "根据研究目标提出一项、且仅一项可以被当前实验映射清单验证的参数假设。"
        if phase == "initial" else
        "根据复盘摘要提出下一轮一项待验证研究假设；若证据不足，使用 NEEDS_DATA 并说明原因。"
    )
    return "\n".join([
        "输出一个 JSON 对象，匹配 strategy-research-hypothesis.schema.json，不要 Markdown 围栏。",
        "输出 JSON Schema：",
        json.dumps(output_schema, ensure_ascii=False, sort_keys=True),
        f"阶段：{phase}。{phase_instruction}",
        f"hypothesis_id 必须是 {input_payload['research_id']}-{phase}-hypothesis。",
        f"experiment_id 必须是 {experiment_id}。",
        f"input_data_digest 必须是 {input_data_digest}。",
        "proposed_changes 只能表达配置参数或已实现规则；不要输出订单、交易股数、券商指令、代码或工具调用。",
        "保留 data_trust 中的 UNVERIFIED、FALLBACK_USED、NOT_READY 和所有合成数据限制。",
        "Agent 不能修改当前有效策略；human_review_required=true、may_modify_active_strategy=false、may_generate_orders=false。",
        "研究输入 JSON：",
        json.dumps(input_payload, ensure_ascii=False, sort_keys=True),
    ])


def generate_validated_hypothesis(
    model: ResearchModel,
    *,
    input_payload: Mapping[str, Any],
    phase: str,
    input_data_digest: str,
    experiment_id: str,
    max_retries: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if max_retries < 0 or max_retries > 2:
        raise ValueError("模型重试次数必须在 0 到 2 之间")
    validated_input = validate_strategy_research_input(input_payload)
    prompt = build_hypothesis_prompt(
        input_payload=validated_input,
        phase=phase,
        input_data_digest=input_data_digest,
        experiment_id=experiment_id,
    )
    attempts: list[dict[str, Any]] = []
    for attempt in range(max_retries + 1):
        reply: ModelReply | None = None
        try:
            reply = model.generate(phase=phase, prompt=prompt, context={
                "research_id": validated_input["research_id"],
                "experiment_id": experiment_id,
                "input_data_digest": input_data_digest,
                "strategy_parameters": validated_input["frozen_strategy"]["parameters"],
            })
            payload = json.loads(reply.content)
            hypothesis = validate_strategy_hypothesis(payload)
            if hypothesis["input_data_digest"] != input_data_digest:
                raise ValueError("hypothesis input_data_digest 与本轮输入不一致")
            if hypothesis["experiment_id"] != experiment_id:
                raise ValueError("hypothesis experiment_id 与本轮登记不一致")
            trust = validated_input["data_trust"]
            mandatory_limitations = list(validated_input["known_data_gaps"])
            for trust_name, trust_item in trust.items():
                if isinstance(trust_item, Mapping):
                    state = str(trust_item.get("status", "UNKNOWN"))
                    if state not in {"VERIFIED", "NOT_USED"}:
                        mandatory_limitations.append(f"数据可信度 {trust_name}={state}；不得解释为已核验。")
            hypothesis["known_limitations"] = list(dict.fromkeys([
                *hypothesis["known_limitations"], *mandatory_limitations,
            ]))
            synthetic_input = str(trust.get("overall", "")).startswith("SYNTHETIC")
            if synthetic_input:
                if not hypothesis["evidence"] or any(
                    evidence["evidence_status"] != "SYNTHETIC"
                    or evidence["artifact_sha256"] != input_data_digest
                    for evidence in hypothesis["evidence"]
                ):
                    raise ValueError("合成研究输入的 evidence 必须全部标为 SYNTHETIC 并绑定本轮摘要")
            attempts.append({"attempt": attempt + 1, "status": "VALIDATED", "usage": reply.usage})
            audit = {
                "provider": reply.provider,
                "model": reply.model,
                "endpoint": reply.endpoint,
                "phase": phase,
                "attempt_count": attempt + 1,
                "max_retries": max_retries,
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "response_usage": reply.usage,
                "attempts": attempts,
                "response_classification": "STRUCTURED_HYPOTHESIS",
                "api_cost_estimate": None,
                "api_cost_estimate_reason": "提供商费率未在本地配置；记录调用次数和返回 token 用量，不猜测费用。",
            }
            return hypothesis, audit
        except Exception as exc:
            attempts.append({
                "attempt": attempt + 1,
                "status": "REJECTED",
                "reason": str(exc)[:300],
                "usage": reply.usage if reply else None,
            })
            if attempt >= max_retries:
                break
    audit = {
        "provider": getattr(model, "provider", "unknown"),
        "model": getattr(model, "model", "unknown"),
        "endpoint": getattr(model, "safe_endpoint", None),
        "phase": phase,
        "attempt_count": len(attempts),
        "max_retries": max_retries,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_usage": attempts[-1].get("usage"),
        "attempts": attempts,
        "response_classification": "REJECTED_STRUCTURED_HYPOTHESIS",
        "api_cost_estimate": None,
        "api_cost_estimate_reason": "提供商费率未在本地配置；调用次数和返回 token 用量单独记录。",
    }
    raise ResearchHypothesisRejected(
        "研究 Agent 输出未通过结构校验：" + attempts[-1]["reason"],
        audit=audit,
    )
