from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from cehuan.data.unknown_types import audit_unknown_security_types


def test_unknown_type_audit_partitions_rows_without_inference_and_preserves_review(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH900001", "pool_status": "UNKNOWN_TYPE", "name_on_date_from_daily_list": "示例甲"},
        {"trade_date": "2025-01-03", "symbol": "SH900001", "pool_status": "UNKNOWN_TYPE", "name_on_date_from_daily_list": "示例甲"},
        {"trade_date": "2025-01-03", "symbol": "SZ900002", "pool_status": "UNKNOWN_TYPE", "name_on_date_from_daily_list": "示例乙"},
        {"trade_date": "2025-01-03", "symbol": "SH600000", "pool_status": "STOCK_MEMBER", "name_on_date_from_daily_list": "普通股"},
    ]).to_csv(universe / "securities_by_date.csv", index=False)
    bars = tmp_path / "bars.csv"
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH900001"},
        {"trade_date": "2025-01-03", "symbol": "SH900001"},
    ]).to_csv(bars, index=False)
    output = tmp_path / "unknown-review.csv"

    first = audit_unknown_security_types(universe, bars, output)
    rows = pd.read_csv(output, dtype=str, keep_default_na=False)
    assert first["unknown_rows"] == 3
    assert first["unknown_symbols"] == 2
    assert first["unresolved_rows"] == 3
    assert first["classification_partition_balances"] is True
    assert rows["classification"].eq("UNRESOLVED").all()
    assert rows.loc[rows["symbol"].eq("SH900001"), "bar_rows_in_supplied_file"].iloc[0] == "2"

    rows.loc[rows["symbol"].eq("SH900001"), "classification"] = "EXPLAINED_NON_STOCK"
    rows.loc[rows["symbol"].eq("SH900001"), "evidence_url"] = "https://example.invalid/notice.pdf"
    rows.loc[rows["symbol"].eq("SH900001"), "source_sha256"] = hashlib.sha256(b"archived evidence").hexdigest()
    rows.loc[rows["symbol"].eq("SH900001"), "reviewed_by"] = "reviewer"
    rows.loc[rows["symbol"].eq("SH900001"), "reviewed_at"] = "2025-01-04"
    rows.to_csv(output, index=False)
    second = audit_unknown_security_types(universe, bars, output)
    assert second["explained_non_stock_rows"] == 2
    assert second["unresolved_rows"] == 1
    assert second["classification_partition_balances"] is True
    summary = json.loads(output.with_suffix(".csv.summary.json").read_text(encoding="utf-8"))
    assert summary["unknown_rows"] == (
        summary["explained_non_stock_rows"] + summary["added_as_stock_rows"] + summary["unresolved_rows"]
    )


def test_unknown_type_review_rejects_classification_without_source_hash(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    pd.DataFrame([{
        "trade_date": "2025-01-02", "symbol": "SZ900002", "pool_status": "UNKNOWN_TYPE",
    }]).to_csv(universe / "securities_by_date.csv", index=False)
    bars = tmp_path / "bars.csv"
    pd.DataFrame(columns=["trade_date", "symbol"]).to_csv(bars, index=False)
    output = tmp_path / "review.csv"
    audit_unknown_security_types(universe, bars, output)
    rows = pd.read_csv(output, dtype=str, keep_default_na=False)
    rows.loc[0, "classification"] = "ADDED_AS_STOCK"
    rows.loc[0, "evidence_url"] = "https://example.invalid/notice.pdf"
    rows.loc[0, "reviewed_by"] = "reviewer"
    rows.loc[0, "reviewed_at"] = "2025-01-03"
    rows.to_csv(output, index=False)
    with pytest.raises(ValueError, match="SHA-256"):
        audit_unknown_security_types(universe, bars, output)
