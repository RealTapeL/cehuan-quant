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
    """Create conservative, machine-derived trust labels for an experiment.

    These labels summarize what the engine checked. They do not infer that a
    historical vendor vintage, complete corporate-action universe, source
    interpretation, or personal brokerage rate has been verified.
    """
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
