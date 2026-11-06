"""Small, replaceable structured-output adapter for the strategy research agent."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Protocol

from cehuan.research.controlled_experiments import (
    MAX_CHANGES_PER_EXPERIMENT,
    SUPPORTED_HYPOTHESIS_PATHS,
)
from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


INPUT_REQUIRED = {
    "schema_version", "research_id", "objective", "scope", "data_trust",
    "frozen_strategy", "prior_experiments", "known_data_gaps", "allowed_actions",
    "forbidden_actions",
}
INPUT_OPTIONAL = {"review_summary", "experiment_capabilities", "market_research_summary"}
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
    extra = set(payload) - INPUT_REQUIRED - INPUT_OPTIONAL
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
    if "market_research_summary" in payload:
        summary = payload["market_research_summary"]
        if not isinstance(summary, Mapping) or set(summary) != {
            "schema", "summary_id", "summary_sha256", "classification", "facts", "evidence_catalog",
        }:
            raise ValueError("market_research_summary 字段不符合真实摘要输入契约")
        if summary.get("schema") != "cehuan.real_market_research_summary_input.v1":
            raise ValueError("market_research_summary schema 不支持")
        if summary.get("classification") != "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST":
            raise ValueError("真实摘要 classification 不正确")
        if not isinstance(summary.get("summary_id"), str) or not summary["summary_id"].strip():
            raise ValueError("market_research_summary.summary_id 必须为非空字符串")
        summary_digest = summary.get("summary_sha256")
        if not isinstance(summary_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", summary_digest):
            raise ValueError("market_research_summary.summary_sha256 必须是 SHA-256")
        if not isinstance(summary.get("facts"), Mapping):
            raise ValueError("market_research_summary.facts 必须是对象")
        catalog = summary.get("evidence_catalog")
        if not isinstance(catalog, list) or not catalog:
            raise ValueError("真实摘要必须包含非空 evidence_catalog")
        fact_ids: set[str] = set()
        for index, fact in enumerate(catalog):
            if not isinstance(fact, Mapping) or set(fact) != {
                "fact_id", "statement", "artifact_sha256", "evidence_status",
            }:
                raise ValueError(f"market_research_summary.evidence_catalog[{index}] 字段无效")
            fact_id = fact.get("fact_id")
            digest = fact.get("artifact_sha256")
            status = fact.get("evidence_status")
            if not isinstance(fact_id, str) or not fact_id.strip() or fact_id in fact_ids:
                raise ValueError(f"market_research_summary.evidence_catalog[{index}].fact_id 无效或重复")
            if not isinstance(fact.get("statement"), str) or not fact["statement"].strip():
                raise ValueError(f"market_research_summary.evidence_catalog[{index}].statement 无效")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ValueError(f"market_research_summary.evidence_catalog[{index}].artifact_sha256 必须是 SHA-256")
            if status not in {"RECORDED", "RECONSTRUCTED", "UNVERIFIED"}:
                raise ValueError(f"market_research_summary.evidence_catalog[{index}].evidence_status 无效")
            fact_ids.add(fact_id)
        if trust.get("overall") != "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST":
            raise ValueError("真实行情摘要必须标记为 REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST")
    if "experiment_capabilities" in payload:
        capabilities = payload["experiment_capabilities"]
        if not isinstance(capabilities, Mapping) or set(capabilities) != {
            "source", "maximum_changes_per_experiment", "supported_parameters",
        }:
            raise ValueError("experiment_capabilities 字段与能力清单契约不一致")
        if not isinstance(capabilities["source"], str) or not capabilities["source"]:
            raise ValueError("experiment_capabilities.source 必须标明程序清单来源")
        maximum_changes = capabilities["maximum_changes_per_experiment"]
        if isinstance(maximum_changes, bool) or maximum_changes != MAX_CHANGES_PER_EXPERIMENT:
            raise ValueError("experiment_capabilities 改动数与受控实验器契约不一致")
        parameters = capabilities["supported_parameters"]
        if not isinstance(parameters, list) or not parameters:
            raise ValueError("experiment_capabilities.supported_parameters 必须是非空列表")
        if [item.get("config_path") for item in parameters if isinstance(item, Mapping)] != list(SUPPORTED_HYPOTHESIS_PATHS):
            raise ValueError("experiment_capabilities 参数路径与受控实验器实际支持清单不一致")
        seen_paths: set[str] = set()
        for index, parameter in enumerate(parameters):
            if not isinstance(parameter, Mapping) or set(parameter) != {
                "config_path", "value_type", "minimum_inclusive", "maximum_inclusive", "current_value",
            }:
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}] 字段无效")
            path = parameter["config_path"]
            value_type = parameter["value_type"]
            if not isinstance(path, str) or not path or path in seen_paths:
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}].config_path 无效或重复")
            seen_paths.add(path)
            if value_type not in {"integer", "number"}:
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}].value_type 不受支持")
            contract = SUPPORTED_HYPOTHESIS_PATHS[path]
            if (
                value_type != contract["kind"]
                or parameter["minimum_inclusive"] != contract["minimum"]
                or parameter["maximum_inclusive"] != contract["maximum"]
            ):
                raise ValueError(f"experiment_capabilities 参数范围与受控实验器实际契约不一致：{path}")
            bounds = [parameter["minimum_inclusive"], parameter["maximum_inclusive"], parameter["current_value"]]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in bounds):
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}] 范围和当前值必须是数值")
            if not all(math.isfinite(float(value)) for value in bounds):
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}] 数值必须有限")
            low, high, current = map(float, bounds)
            if low > high or not low <= current <= high:
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}] 范围或当前值无效")
            if value_type == "integer" and any(not float(value).is_integer() for value in bounds):
                raise ValueError(f"experiment_capabilities.supported_parameters[{index}] 整数参数不能含小数")
            if path.startswith("strategy.") and frozen["parameters"].get(path.split(".", 1)[1]) != parameter["current_value"]:
                raise ValueError(f"experiment_capabilities 当前值与冻结策略参数不一致：{path}")
    return dict(payload)


def _is_synthetic_research_input(payload: Mapping[str, Any]) -> bool:
    trust = payload.get("data_trust", {})
    if not isinstance(trust, Mapping):
        return False
    if str(trust.get("overall", "")).startswith("SYNTHETIC"):
        return True
    input_integrity = trust.get("input_file_integrity", {})
    return isinstance(input_integrity, Mapping) and input_integrity.get("status") == "SYNTHETIC"


@dataclass(frozen=True)
class ModelReply:
    content: str
    provider: str
    model: str
    endpoint: str | None = None
    usage: dict[str, int] | None = None


class ResearchModelRequestError(RuntimeError):
    """Transport/API failure where retrying may duplicate a billable request."""


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
        capabilities = context.get("experiment_capabilities") or {}
        supported = capabilities.get("supported_parameters", [])
        if not supported:
            raise ValueError("Stub 研究 Agent 需要输入的动态 experiment_capabilities 清单")
        index = 0 if phase == "initial" else min(1, len(supported) - 1)
        capability = supported[index]
        path = capability["config_path"]
        current_value = capability["current_value"]
        minimum = capability["minimum_inclusive"]
        maximum = capability["maximum_inclusive"]
        if capability["value_type"] == "integer":
            proposed = current_value + 1 if current_value < maximum else current_value - 1
        else:
            step = min(0.01, (maximum - minimum) / 10)
            proposed = current_value + step if current_value + step <= maximum else current_value - step
        if phase == "initial":
            observation = "合成闭环没有市场证据；Stub 仅用于验证结构化接口和受控实验串接。"
        else:
            summary = (context.get("research_input") or {}).get("review_summary") or {}
            facts = summary.get("program_facts", {})
            experiment_status = facts.get("experiment", {}).get("status", "UNKNOWN")
            reconciliation = facts.get("reconciliation", {})
            snapshot_status = reconciliation.get("position_snapshot_status", "UNKNOWN")
            observation = (
                f"程序记录的合成候选实验状态为 {experiment_status}；持仓快照状态为 {snapshot_status}。"
                "复盘与实验都只是合成流程事实，不能支持真实策略结论。"
            )
        payload = {
            "schema_version": 1,
            "hypothesis_id": f"{context['research_id']}-{phase}-hypothesis",
            "experiment_id": context["experiment_id"],
            "input_data_digest": context["input_data_digest"],
            "problem_observed": observation,
            "proposed_changes": [{
                "config_path": path,
                "current_value": current_value,
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

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120,
        reasoning_effort: str | None = None,
    ) -> None:
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
        if reasoning_effort is not None and reasoning_effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max",
        }:
            raise ValueError("LLM reasoning_effort 不受支持")
        self.reasoning_effort = reasoning_effort
        self._api_key = api_key

    @property
    def safe_endpoint(self) -> str:
        parsed = urllib.parse.urlsplit(self.base_url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    def generate(self, *, phase: str, prompt: str, context: Mapping[str, Any]) -> ModelReply:
        url = f"{self.base_url}/chat/completions"
        request_payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "你是策环的研究假设助手。只输出符合给定 JSON 契约的对象；不生成订单、不调用工具、不声称合成数据证明收益。"},
                {"role": "user", "content": prompt},
            ],
        }
        if self.reasoning_effort is not None:
            request_payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(request_payload).encode("utf-8")
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
            raise ResearchModelRequestError(f"LLM 请求失败，HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ResearchModelRequestError(f"LLM 请求失败：{type(exc).__name__}") from None
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
    reasoning_effort_env = str(settings.get("reasoning_effort_env", "CEHUAN_LLM_REASONING_EFFORT"))
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
        "reasoning_effort": os.environ.get(reasoning_effort_env, "").strip() or None,
        "timeout_seconds": float(settings.get("timeout_seconds", 120)),
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
        timeout_seconds=float(settings.get("timeout_seconds", 120)),
        reasoning_effort=audit["reasoning_effort"],
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
    real_summary = input_payload.get("market_research_summary")
    synthetic_input = _is_synthetic_research_input(input_payload)
    phase_instruction = (
        "基于真实市场数据的脱敏研究摘要提出一条可检验假设；只引用 market_research_summary.evidence_catalog 中的 fact_id。因为历史 vintage、时点、市场规则、费用和公司行动存在未核实项，本路径的假设状态必须为 NEEDS_DATA，不运行收益比较。参数变化只能来自 experiment_capabilities.supported_parameters。"
        if real_summary else
        "根据研究目标提出假设；参数必须来自输入中的 experiment_capabilities.supported_parameters，且只提出能力清单允许数量的配置改动。"
        if phase == "initial" else
        "根据复盘摘要中的程序事实提出下一轮待验证研究假设；只使用 program_facts 明示的实验/成交事实，null 或缺失指标代表不可用，不得补写成事实；参数必须来自 experiment_capabilities.supported_parameters。证据不足时使用 NEEDS_DATA。"
    )
    prompt_lines = [
        "输出一个 JSON 对象，匹配 strategy-research-hypothesis.schema.json，不要 Markdown 围栏。",
        "输出 JSON Schema：",
        json.dumps(output_schema, ensure_ascii=False, sort_keys=True),
        f"阶段：{phase}。{phase_instruction}",
        f"hypothesis_id 必须是 {input_payload['research_id']}-{phase}-hypothesis。",
        f"experiment_id 必须是 {experiment_id}。",
        f"input_data_digest 必须是 {input_data_digest}。",
        "proposed_changes 只能引用 experiment_capabilities 中的参数；未列入的规则可以作为待实现建议说明，但不能写入拟执行改动。不要输出订单、交易股数、券商指令、代码或工具调用。",
        "Agent 不能修改当前有效策略；human_review_required=true、may_modify_active_strategy=false、may_generate_orders=false。",
    ]
    if real_summary:
        prompt_lines.extend([
            "本次是基于真实历史行情文件摘要的研究，不是合成行情，也不是完整真实收益验证。输入只包含脱敏聚合摘要和少量重建排名，不含原始行情行、华泰数据、账户资料或订单。",
            "evidence 必须至少一项；每一项都必须带 source_fact_id，且该 ID 必须存在于 market_research_summary.evidence_catalog。artifact_sha256 和 evidence_status 必须与所引用目录条目逐字一致，不得另造来源。",
            "模型对数据的解释属于建议；可量化事实只能引用摘要目录。不得把回退规则、名称字段、公告日期或适配器 available_at 假设说成已核实的历史事实。",
            "保留所有 UNVERIFIED、FALLBACK_USED、NOT_READY 和事后重建限制；status 必须是 NEEDS_DATA。",
        ])
    elif synthetic_input:
        synthetic_evidence_template = {
            "description": "仅用于验证流程的策环合成数据，不是市场证据",
            "artifact_sha256": input_data_digest,
            "evidence_status": "SYNTHETIC",
        }
        prompt_lines.extend([
            "上述行情、规则、基线、候选、计划、成交和持仓均为合成或项目默认假设。",
            "evidence 是必填字段。当前研究输入为合成数据，evidence 必须至少包含一项，且每项必须使用 evidence_status=SYNTHETIC、artifact_sha256 精确等于本条消息中的 input_data_digest。请逐字使用以下程序提供的证据对象，不要改写散列或标签：",
            json.dumps(synthetic_evidence_template, ensure_ascii=False, sort_keys=True),
            "保留 data_trust 中的 UNVERIFIED、FALLBACK_USED、NOT_READY 和所有合成数据限制。",
        ])
    else:
        prompt_lines.extend([
            "程序计算事实来自保存的 CSV/JSON；Agent 文字是研究建议，不是数据事实。",
            "保留 data_trust 中的 UNVERIFIED、FALLBACK_USED、NOT_READY 和所有数据限制。",
        ])
    prompt_lines.extend([
        "研究输入 JSON：",
        json.dumps(input_payload, ensure_ascii=False, sort_keys=True),
    ])
    return "\n".join(prompt_lines)


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
    base_prompt = build_hypothesis_prompt(
        input_payload=validated_input,
        phase=phase,
        input_data_digest=input_data_digest,
        experiment_id=experiment_id,
    )
    attempts: list[dict[str, Any]] = []
    prompt = base_prompt
    for attempt in range(max_retries + 1):
        reply: ModelReply | None = None
        try:
            reply = model.generate(phase=phase, prompt=prompt, context={
                "research_id": validated_input["research_id"],
                "experiment_id": experiment_id,
                "input_data_digest": input_data_digest,
                "strategy_parameters": validated_input["frozen_strategy"]["parameters"],
                "experiment_capabilities": validated_input.get("experiment_capabilities"),
                "research_input": validated_input,
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
            synthetic_input = _is_synthetic_research_input(validated_input)
            if synthetic_input:
                invalid_status = [
                    index for index, evidence in enumerate(hypothesis["evidence"])
                    if evidence["evidence_status"] != "SYNTHETIC"
                ]
                invalid_digest = [
                    index for index, evidence in enumerate(hypothesis["evidence"])
                    if evidence["artifact_sha256"] != input_data_digest
                ]
                if not hypothesis["evidence"] or invalid_status or invalid_digest:
                    details = []
                    if not hypothesis["evidence"]:
                        details.append("evidence 不能为空")
                    if invalid_status:
                        details.append(f"evidence_status 必须为 SYNTHETIC（错误项 {invalid_status}）")
                    if invalid_digest:
                        details.append(f"artifact_sha256 必须等于本轮输入摘要（错误项 {invalid_digest}）")
                    raise ValueError("合成研究 evidence 校验失败：" + "；".join(details))
            elif "market_research_summary" in validated_input:
                market_summary = validated_input["market_research_summary"]
                catalog = {
                    item["fact_id"]: item for item in market_summary["evidence_catalog"]
                }
                if not hypothesis["evidence"]:
                    raise ValueError("真实行情摘要研究必须引用至少一项 evidence_catalog 事实")
                invalid_refs = []
                invalid_hashes = []
                invalid_statuses = []
                missing_refs = []
                for index, evidence in enumerate(hypothesis["evidence"]):
                    fact_id = evidence.get("source_fact_id")
                    if not isinstance(fact_id, str) or fact_id not in catalog:
                        missing_refs.append(index)
                        continue
                    source_fact = catalog[fact_id]
                    if evidence["artifact_sha256"] != source_fact["artifact_sha256"]:
                        invalid_hashes.append(index)
                    if evidence["evidence_status"] != source_fact["evidence_status"]:
                        invalid_statuses.append(index)
                    if evidence["evidence_status"] == "SYNTHETIC":
                        invalid_statuses.append(index)
                if missing_refs or invalid_hashes or invalid_statuses:
                    problems = []
                    if missing_refs:
                        problems.append(f"source_fact_id 不在摘要证据目录中（错误项 {missing_refs}）")
                    if invalid_hashes:
                        problems.append(f"artifact_sha256 与所引事实来源不一致（错误项 {invalid_hashes}）")
                    if invalid_statuses:
                        problems.append(f"evidence_status 与摘要事实不一致或误标为 SYNTHETIC（错误项 {sorted(set(invalid_statuses))}）")
                    raise ValueError("真实行情摘要 evidence 校验失败：" + "；".join(problems))
                if hypothesis["status"] != "NEEDS_DATA":
                    raise ValueError("当前真实摘要仍有时点、规则、费率或公司行动缺口，研究假设 status 必须为 NEEDS_DATA")
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
                "status": "REQUEST_FAILED" if isinstance(exc, ResearchModelRequestError) else "REJECTED",
                "reason": str(exc)[:300],
                "usage": reply.usage if reply else None,
            })
            if isinstance(exc, ResearchModelRequestError):
                # A timeout does not prove that the provider stopped processing the
                # request. Do not automatically submit a potentially billable duplicate.
                break
            if attempt >= max_retries:
                break
            correction = (
                "真实摘要路径要求 source_fact_id 必须来自 market_research_summary.evidence_catalog，artifact_sha256 和 evidence_status 必须与目录一致，status 必须为 NEEDS_DATA。"
                if "market_research_summary" in validated_input else
                f"本轮 evidence 必须使用 evidence_status=SYNTHETIC 且 artifact_sha256={input_data_digest}。"
                if _is_synthetic_research_input(validated_input) else
                "证据必须引用输入中真实存在的来源，不得编造来源或把限制改写为通过。"
            )
            prompt = "\n".join([
                base_prompt,
                "上一条模型输出未通过策环本地校验。请修正后重新输出完整 JSON，不要解释错误，不要改变其他安全约束。",
                f"校验错误：{str(exc)[:300]}",
                correction,
            ])
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
        "response_classification": (
            "MODEL_REQUEST_FAILED"
            if attempts and attempts[-1].get("status") == "REQUEST_FAILED"
            else "REJECTED_STRUCTURED_HYPOTHESIS"
        ),
        "api_cost_estimate": None,
        "api_cost_estimate_reason": "提供商费率未在本地配置；调用次数和返回 token 用量单独记录。",
    }
    raise ResearchHypothesisRejected(
        "研究 Agent 输出未通过结构校验：" + attempts[-1]["reason"],
        audit=audit,
    )
