from __future__ import annotations

import json
import runpy
from pathlib import Path

import pandas as pd

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


_build = runpy.run_path(Path(__file__).parents[1] / "scripts/build_audit_hypotheses.py")["build"]


def test_build_audit_hypotheses_uses_existing_contract_and_keeps_research_only(tmp_path: Path):
    audit_dir = tmp_path / "private-audit"
    audit_dir.mkdir()
    summary = {
        "schema_version": 2,
        "experiment_id": "synthetic-audit",
        "window": {"start": "2024-01-01", "end": "2024-03-31"},
        "input_hashes": {"config.json": "a" * 64, "bars.csv": "b" * 64},
        "research_trust": {
            "input_file_integrity": "PARTIAL_OR_UNVERIFIED",
            "canonical_data_quality": "STRUCTURAL_AUDIT_HEALTHY_WITH_WARNINGS",
        },
        "market_rule_fallback_rows_in_window": 100,
        "record_date_entitlement_audit": {"typed_accounting_events_in_frozen_run": []},
        "suspension_candidate_diagnostic": {
            "symbol": "SH603268",
            "window_state_counts_including_base": {"total_rows": 21, "suspended_rows": 7},
            "return_observation_state_counts_excluding_base": {"total_rows": 20, "suspended_rows": 6},
            "session_window_audit": {"window_alignment_status": "MATCHED_EXACT_SESSION_WINDOW"},
        },
        "fill_record_count": 2,
        "order_log_status": "UNKNOWN_NO_COMPLETE_ORDER_LOG",
        "selected_targets_reconstructed_to_zero_shares": 1,
    }
    (audit_dir / "trade-audit-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    pd.DataFrame({"trade_date": [f"2024-01-{i:02}" for i in range(1, 22)]}).to_csv(
        audit_dir / "suspension-candidate-window.csv", index=False,
    )
    pd.DataFrame([{
        "symbol": "SH603268", "rank": 1, "momentum": 2.8,
    }]).to_csv(audit_dir / "suspension-candidate-rank-neighbors.csv", index=False)
    pd.DataFrame([{
        "symbol": "SZ301600", "rank": 2, "target_amount_cny": 8067.54,
        "execution_open_price_cny": 196.60, "raw_target_shares_before_rounding": 41,
        "buy_minimum_shares_from_config_fallback": 100,
        "target_lifecycle_status": "ZERO_TARGET_NO_POSITION_CASH_RETAINED",
        "next_unselected_candidate_rank": 6,
        "next_unselected_candidate_symbol": "SZ300493",
    }]).to_csv(audit_dir / "zero-target-review.csv", index=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"strategy": {"lookback_sessions": 20}}), encoding="utf-8")

    result = _build(audit_dir, config_path)
    assert [item["status"] for item in result["hypotheses"]] == ["NEEDS_DATA", "NEEDS_DATA"]
    assert all(validate_strategy_hypothesis(item)["may_generate_orders"] is False for item in result["hypotheses"])
    assert result["input"]["data_trust"]["formal_baseline"]["status"] == "NOT_READY"
    assert "rank 6 SZ300493" in result["hypotheses"][1]["problem_observed"]
    assert result["input"]["forbidden_actions"] == [
        "MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL", "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT",
    ]
    updated = json.loads((audit_dir / "trade-audit-summary.json").read_text(encoding="utf-8"))
    assert updated["research_agent_artifacts"]["active_strategy_modified"] is False
    inventory = pd.read_csv(audit_dir / "audit-artifact-inventory.csv")
    assert "research-agent-input.json" in set(inventory.relative_path)
