from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_FORBIDDEN_KEYS = {
    "order", "orders", "order_plan", "broker_command", "submit_order",
    "place_order", "execution_instruction", "real_trade_instruction",
}


def _reject_order_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"研究假设不得包含真实委托或下单字段：{path}.{key}")
            _reject_order_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_order_fields(child, f"{path}[{index}]")


def validate_strategy_hypothesis(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a proposed research hypothesis without applying it to a strategy."""
    required = {
        "schema_version", "hypothesis_id", "experiment_id", "input_data_digest",
        "problem_observed", "proposed_changes", "rationale", "evidence",
        "required_data", "known_limitations", "validation_plan", "status",
        "human_review_required", "may_modify_active_strategy", "may_generate_orders",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError("研究假设缺少字段：" + ", ".join(missing))
    _reject_order_fields(payload)
    if payload["schema_version"] != 1:
        raise ValueError("strategy hypothesis schema_version 必须为 1")
    for field in ("hypothesis_id", "experiment_id", "problem_observed", "rationale", "validation_plan"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"研究假设字段 {field} 必须是非空字符串")
    if not isinstance(payload["input_data_digest"], str) or not _SHA256.fullmatch(payload["input_data_digest"]):
        raise ValueError("input_data_digest 必须是 64 位十六进制摘要")
    if not isinstance(payload["proposed_changes"], list) or not payload["proposed_changes"]:
        raise ValueError("proposed_changes 必须是非空列表")
    for index, change in enumerate(payload["proposed_changes"]):
        if not isinstance(change, Mapping):
            raise ValueError(f"proposed_changes[{index}] 必须是对象")
        if not isinstance(change.get("config_path"), str) or not change["config_path"].strip():
            raise ValueError(f"proposed_changes[{index}].config_path 必须是非空字符串")
        if "proposed_value" not in change:
            raise ValueError(f"proposed_changes[{index}] 缺少 proposed_value")
    if not isinstance(payload["evidence"], list):
        raise ValueError("evidence 必须是列表")
    if not isinstance(payload["required_data"], list) or not isinstance(payload["known_limitations"], list):
        raise ValueError("required_data 与 known_limitations 必须是列表")
    if payload["status"] not in {"PROPOSED", "NEEDS_DATA", "REJECTED_FOR_REVIEW"}:
        raise ValueError("status 必须是 PROPOSED、NEEDS_DATA 或 REJECTED_FOR_REVIEW")
    if payload["human_review_required"] is not True:
        raise ValueError("研究假设必须保留 human_review_required=true")
    if payload["may_modify_active_strategy"] is not False:
        raise ValueError("Agent 不得直接修改冻结或生效策略")
    if payload["may_generate_orders"] is not False:
        raise ValueError("Agent 不得生成真实委托")
    return dict(payload)
