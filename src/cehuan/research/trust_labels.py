"""实验可信度标签生成器。依据程序实际执行的检查生成保守标签，并明确输入完整性、历史可得性、规则费用及公司行动覆盖各自能证明的范围。"""

from __future__ import annotations

from typing import Any


def build_research_trust_labels(
    *,
    quality_healthy: bool,
    input_file_sha256: str | None,
    source_file_checked_during_run: bool,
    checkpoint_bound: bool,
    market_rule_mode: str,
    market_rule_fallback_rows: int,
    fee_mode: str,
    accounting_event_count: int,
    formal_readiness_gate_passed: bool,
) -> dict[str, Any]:
    """从运行时审计事实构建可信度标签。
    参数：quality_healthy 表示结构质量结果；input_file_sha256 和 source_file_checked_during_run 描述文件绑定；checkpoint_bound 表示是否使用绑定快照；market_rule_mode/fallback_rows 与 fee_mode 描述规则费用口径；accounting_event_count 为实际消费事件数；formal_readiness_gate_passed 表示正式门控状态。
    返回：分项可信度状态字典。
    限制：消费已登记事件不证明事件覆盖完整，适配器时间也不证明历史可得性。"""
    input_status = (
        "VERIFIED_DURING_RUN"
        if source_file_checked_during_run and input_file_sha256
        else "UNVERIFIED"
    )
    market_status = (
        "FALLBACK_USED"
        if market_rule_mode == "config_fallback" or market_rule_fallback_rows > 0
        else "SCHEDULE_CONSUMED_SOURCE_REVIEW_UNVERIFIED"
    )
    fee_status = (
        "CONFIG_FLAT_ASSUMPTION"
        if fee_mode == "config_flat_assumption"
        else "SCHEDULE_CONSUMED_HISTORICAL_AND_PERSONAL_RATE_UNVERIFIED"
    )
    readiness_status = "READINESS_GATE_PASSED" if formal_readiness_gate_passed else "NOT_READY"
    return {
        "schema_version": 1,
        "input_file_integrity": {
            "status": input_status,
            "sha256": input_file_sha256,
            "scope": "only the input file checked by the engine; does not verify upstream source completeness",
        },
        "canonical_data_quality": {
            "status": "STRUCTURAL_CHECKS_PASSED" if quality_healthy else "FAILED",
            "scope": "schema and configured quality checks only",
        },
        "checkpoint_binding": {
            "status": "BOUND" if checkpoint_bound else "NOT_USED",
            "scope": "checkpoint/input hash binding does not establish historical availability",
        },
        "historical_availability_and_vendor_vintage": {
            "status": "UNVERIFIED",
            "scope": "engine timestamps or adapter available_at values are not independent publication-time evidence",
        },
        "market_rule_coverage": {
            "status": market_status,
            "fallback_rows": int(market_rule_fallback_rows),
            "mode": market_rule_mode,
        },
        "fee_coverage": {
            "status": fee_status,
            "mode": fee_mode,
            "personal_brokerage_rate": "UNVERIFIED",
        },
        "corporate_action_coverage": {
            "status": "UNVERIFIED_COVERAGE",
            "typed_events_consumed": int(accounting_event_count),
            "scope": "consuming registered events does not prove all relevant events were registered",
        },
        "formal_baseline": {
            "status": readiness_status,
            "interpretation": "a passed readiness gate is a data/process gate, not evidence of strategy profitability",
        },
        "overall": "UNVERIFIED",
    }
