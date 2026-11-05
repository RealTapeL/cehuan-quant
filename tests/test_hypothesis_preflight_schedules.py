from __future__ import annotations

from pathlib import Path

import pandas as pd

from cehuan.research.schedule_coverage import audit_schedule_file


def _rule_file(path: Path) -> None:
    pd.DataFrame([{
        "symbol": "SH600001", "board": "MAIN", "valid_from": "2024-01-01", "valid_to": "",
        "is_listed": "true", "is_suspended": "false", "is_st": "false", "has_price_limit": "true",
        "price_limit_pct": "0.1", "buy_min_shares": "100", "buy_step_shares": "100",
        "sell_min_shares": "100", "sell_step_shares": "100", "source": "synthetic official copy",
        "source_sha256": "a" * 64, "review_status": "REVIEWED", "reviewed_by": "synthetic reviewer",
        "reviewed_at": "2026-11-05T00:00:00Z",
    }]).to_csv(path, index=False)


def _fee_file(path: Path) -> None:
    pd.DataFrame([{
        "valid_from": "2024-01-01", "valid_to": "", "market": "A_SHARE",
        "commission_rate": "0.0003", "minimum_commission_cny": "5", "stamp_duty_sell_rate": "0.0005",
        "transfer_fee_rate": "0.00001", "source": "synthetic official copy", "source_sha256": "b" * 64,
        "review_status": "REVIEWED", "reviewed_by": "synthetic reviewer", "reviewed_at": "2026-11-05T00:00:00Z",
    }]).to_csv(path, index=False)


def test_schedule_status_distinguishes_not_provided_missing_and_scope_coverage(tmp_path: Path) -> None:
    pair = {("SH600001", "2025-01-02")}
    dates = {"2025-01-02"}
    not_provided, _, _ = audit_schedule_file(path=None, label="market_rules", required_pairs=pair, required_dates=dates)
    missing, _, _ = audit_schedule_file(path=tmp_path / "absent.csv", label="market_rules", required_pairs=pair, required_dates=dates)
    assert not_provided["status"] == "NOT_PROVIDED"
    assert missing["status"] == "MISSING"

    rules_path, fees_path = tmp_path / "rules.csv", tmp_path / "fees.csv"
    _rule_file(rules_path)
    _fee_file(fees_path)
    rules, _, _ = audit_schedule_file(path=rules_path, label="market_rules", required_pairs=pair, required_dates=dates)
    fees, _, _ = audit_schedule_file(path=fees_path, label="fee_schedule", required_pairs=pair, required_dates=dates)
    assert rules["status"] == "VERIFIED_FOR_SCOPE"
    assert fees["status"] == "VERIFIED_FOR_SCOPE"
    assert rules["source_semantics_review"] == "NOT_INDEPENDENTLY_VERIFIED"


def test_schedule_reports_uncovered_symbol_date_without_global_missing_claim(tmp_path: Path) -> None:
    path = tmp_path / "rules.csv"
    _rule_file(path)
    result, _, _ = audit_schedule_file(
        path=path, label="market_rules", required_pairs={("SZ000001", "2025-01-02")}, required_dates={"2025-01-02"},
    )
    assert result["status"] == "AVAILABLE_BUT_UNVERIFIED"
    assert result["scope_coverage"] == "INCOMPLETE"
    assert result["missing_scope_examples"] == [{"symbol_or_market": "SZ000001", "date": "2025-01-02"}]
