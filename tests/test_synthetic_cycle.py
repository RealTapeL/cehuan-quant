from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.research.synthetic_cycle import run_synthetic_cycle


def test_synthetic_cycle_writes_plan_fictitious_fill_review_and_hypothesis(tmp_path: Path):
    run_dir = run_synthetic_cycle(
        tmp_path,
        now=datetime(2026, 11, 3, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    expected = {
        "synthetic_bars.csv", "baseline_metrics.json", "baseline_equity_curve.csv", "baseline_trades.csv",
        "plan.csv", "plan.md", "fictitious_fills.csv", "fictitious_positions.csv",
        "reconciliation.csv", "position_reconciliation.csv", "review.md",
        "next_hypothesis.json", "manifest.json",
    }
    assert expected.issubset({path.name for path in run_dir.iterdir()})
    plan = (run_dir / "plan.md").read_text(encoding="utf-8")
    review = (run_dir / "review.md").read_text(encoding="utf-8")
    hypothesis = json.loads((run_dir / "next_hypothesis.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    plan_frame = pd.read_csv(run_dir / "plan.csv")
    fills_frame = pd.read_csv(run_dir / "fictitious_fills.csv")
    reconciliation = pd.read_csv(run_dir / "reconciliation.csv")
    assert "SYNTHETIC ONLY" in plan
    assert "SYNTHETIC ONLY" in review
    assert hypothesis["result_type"] == "synthetic_workflow_demo_not_market_research"
    assert hypothesis["automatic_parameter_change"] is False
    assert hypothesis["broker_connection_or_order"] is False
    assert plan_frame["cycle_run_id"].eq(manifest["run_id"]).all()
    assert fills_frame["cycle_run_id"].eq(manifest["run_id"]).all()
    assert reconciliation["cycle_run_id"].eq(manifest["run_id"]).all()
    assert hypothesis["cycle_run_id"] == manifest["run_id"]
    assert hypothesis["source_plan_id"] == manifest["plan_id"]
