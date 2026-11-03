from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cehuan.research.synthetic_cycle import run_synthetic_cycle
from cehuan.cli.main import main as cli_main
from cehuan.review.import_registry import DuplicateFillImportError, register_fill_import
from cehuan.review.reconcile import (
    compare_plan_and_fills,
    compare_plan_revisions,
    read_fills,
    read_order_statuses,
    review_markdown,
)


NOW = datetime(2026, 11, 3, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("no_fill", "NO_FILL"),
        ("duplicate_import", "PARTIAL_FILL"),
        ("partial_fill_cancelled", "PARTIAL_FILL_CANCELLED"),
        ("modified_plan", "PARTIAL_FILL"),
        ("missing_positions", "PARTIAL_FILL"),
    ],
)
def test_synthetic_cycle_exception_scenarios_are_traceable(tmp_path, scenario, expected_status):
    run_dir = run_synthetic_cycle(tmp_path, now=NOW, scenario=scenario)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    reconciliation = pd.read_csv(run_dir / "reconciliation.csv")
    review = (run_dir / "review.md").read_text(encoding="utf-8")
    assert manifest["scenario"] == scenario
    assert reconciliation.iloc[0]["reconciliation_status"] == expected_status
    assert reconciliation["cycle_run_id"].eq(manifest["run_id"]).all()

    if scenario == "no_fill":
        assert int(reconciliation.iloc[0]["filled_quantity"]) == 0
        assert reconciliation.iloc[0]["order_status"] == "UNKNOWN"
        assert "不能据此认定已取消" in review
        assert manifest["fictitious_fills"] == "fictitious_fills.csv"
    elif scenario == "duplicate_import":
        assert manifest["duplicate_import_attempt"]["rejected"] is True
        registry = json.loads((run_dir / "fill_import_registry.json").read_text(encoding="utf-8"))
        assert len(registry["imports"]) == 1
        assert int(reconciliation.iloc[0]["filled_quantity"]) > 0
    elif scenario == "partial_fill_cancelled":
        statuses = pd.read_csv(run_dir / "reconciliation.csv")
        assert statuses.iloc[0]["order_status"] == "CANCELLED"
        assert len(str(statuses.iloc[0]["order_status_evidence_sha256"])) == 64
        assert manifest["order_status_evidence"] == "synthetic_order_status_evidence.txt"
    elif scenario == "modified_plan":
        changes = pd.read_csv(run_dir / "plan_changes.csv")
        assert changes.iloc[0]["change_source"] == "synthetic_user_edit_after_plan_generation"
        original = pd.read_csv(run_dir / "plan_original.csv")
        current = pd.read_csv(run_dir / "plan.csv")
        assert not original["planned_shares"].equals(current["planned_shares"])
    elif scenario == "missing_positions":
        assert manifest["position_snapshot_status"] == "NOT_PROVIDED"
        assert "fictitious_positions.csv" not in manifest
        assert "均未知，不能视为零持仓" in review


def test_no_fill_without_order_evidence_is_not_cancelled():
    plan = pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 200, "reference_price": 10.0,
    }])
    fills = pd.DataFrame(columns=[
        "trade_date", "order_id", "symbol", "side", "quantity", "price", "fees_cny", "cost_basis_per_share",
    ])
    report = compare_plan_and_fills(plan, fills)
    assert report.iloc[0]["reconciliation_status"] == "NO_FILL"
    assert report.iloc[0]["order_status"] == "UNKNOWN"
    assert "不能据此认定已取消" in review_markdown(report)


def test_status_evidence_distinguishes_explicit_cancellation(tmp_path):
    plan = pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 200, "reference_price": 10.0,
    }])
    evidence = tmp_path / "order.txt"
    evidence.write_text("synthetic order state", encoding="utf-8")
    statuses_path = tmp_path / "statuses.csv"
    pd.DataFrame([{
        "order_id": "order-1", "plan_id": "plan-a", "symbol": "SH600000", "side": "BUY",
        "status": "CANCELLED", "reported_at_utc": "2026-11-03T08:00:00+00:00",
        "evidence_file": evidence.name,
        "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
    }]).to_csv(statuses_path, index=False)
    statuses = read_order_statuses(statuses_path)
    fills = pd.DataFrame(columns=[
        "trade_date", "order_id", "symbol", "side", "quantity", "price", "fees_cny", "cost_basis_per_share",
    ])
    report = compare_plan_and_fills(plan, fills, statuses)
    assert report.iloc[0]["reconciliation_status"] == "CANCELLED_NO_FILL"
    assert report.iloc[0]["order_status_evidence_sha256"] == statuses.iloc[0]["evidence_sha256"]


def test_import_registry_rejects_exact_duplicate_without_second_count(tmp_path):
    fills_path = tmp_path / "fills.csv"
    fills_path.write_text("trade_date,symbol,side,quantity,price\n2026-11-03,SH600000,BUY,100,10\n", encoding="utf-8")
    registry = tmp_path / "imports.json"
    receipt = register_fill_import(fills_path, registry, row_count=1, cycle_run_id="cycle-1")
    assert receipt["row_count"] == 1
    with pytest.raises(DuplicateFillImportError, match="本次未重复计入"):
        register_fill_import(fills_path, registry, row_count=1, cycle_run_id="cycle-2")
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert len(document["imports"]) == 1


def test_empty_fill_csv_is_supported_as_no_fill(tmp_path):
    path = tmp_path / "empty.csv"
    pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity", "price"]).to_csv(path, index=False)
    assert read_fills(path).empty


def test_plan_revision_diff_preserves_old_and_new_quantities():
    original = pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 200, "target_shares": 200, "reference_price": 10.0,
    }])
    revised = original.copy()
    revised["planned_shares"] = 100
    revised["target_shares"] = 100

    diff = compare_plan_revisions(original, revised)

    assert diff.iloc[0]["changed_fields"] == "planned_shares,target_shares"
    assert diff.iloc[0]["planned_shares_original"] == 200
    assert diff.iloc[0]["planned_shares_revised"] == 100


def test_review_cli_writes_unique_manifest_revision_diff_and_unknown_positions(tmp_path, capsys):
    original_path = tmp_path / "issued.csv"
    revised_path = tmp_path / "edited.csv"
    original = pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 200, "target_shares": 200, "reference_price": 10.0,
    }])
    revised = original.copy()
    revised["planned_shares"] = 100
    revised["target_shares"] = 100
    original.to_csv(original_path, index=False)
    revised.to_csv(revised_path, index=False)
    fills_path = tmp_path / "fills.csv"
    pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity", "price"]).to_csv(fills_path, index=False)
    output = tmp_path / "review-output"

    exit_code = cli_main([
        "review", "--plan", str(revised_path), "--original-plan", str(original_path),
        "--fills", str(fills_path), "--output-dir", str(output),
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "均未知，不能视为零持仓" in captured.out
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["position_snapshot_status"] == "NOT_PROVIDED"
    assert manifest["broker_connection_or_order"] is False
    assert len(manifest["input_sha256"]["fills"]) == 64
    diff = pd.read_csv(output / "plan_revision_diff.csv")
    assert diff.iloc[0]["planned_shares_original"] == 200


def test_review_cli_verifies_and_hashes_order_status_source_file(tmp_path, capsys):
    plan_path = tmp_path / "plan.csv"
    pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 200, "target_shares": 200, "reference_price": 10.0,
    }]).to_csv(plan_path, index=False)
    fills_path = tmp_path / "fills.csv"
    pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity", "price"]).to_csv(fills_path, index=False)
    evidence_path = tmp_path / "order.txt"
    evidence_path.write_text("synthetic cancellation record", encoding="utf-8")
    status_path = tmp_path / "statuses.csv"
    pd.DataFrame([{
        "order_id": "order-a", "plan_id": "plan-a", "symbol": "SH600000", "side": "BUY",
        "status": "CANCELLED", "reported_at_utc": "2026-11-03T08:00:00+00:00",
        "evidence_file": evidence_path.name,
        "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
    }]).to_csv(status_path, index=False)
    output = tmp_path / "review-status"

    exit_code = cli_main([
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--order-statuses", str(status_path), "--output-dir", str(output),
    ])
    capsys.readouterr()

    assert exit_code == 0
    report = pd.read_csv(output / "review.csv")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert report.iloc[0]["reconciliation_status"] == "CANCELLED_NO_FILL"
    assert manifest["input_sha256"]["order_status_evidence:order-a"] == hashlib.sha256(evidence_path.read_bytes()).hexdigest()
