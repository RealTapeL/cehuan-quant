from __future__ import annotations

from pathlib import Path

import pandas as pd

import cehuan.cli.main as cli


ROOT = Path(__file__).resolve().parents[1]


def test_plan_cli_defaults_to_non_actionable_research_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = cli.main([
        "plan", "--data", str(ROOT / "examples/demo_daily.csv"),
        "--cash", "100000", "--config", str(ROOT / "configs/default.toml"),
    ])
    assert result == 0
    folders = list((tmp_path / "outputs/plans").glob("chq-plan-*"))
    assert len(folders) == 1
    plan = pd.read_csv(folders[0] / "plan.csv")
    assert set(plan["plan_purpose"]) == {"RESEARCH_ONLY"}
    assert not plan["action"].isin(["BUY", "SELL"]).any()
    assert plan["planned_shares"].eq(0).all()
    assert plan["target_shares"].isna().all()
    assert not (tmp_path / "outputs/latest_plan.csv").exists()
    assert (folders[0] / "plan-manifest.json").is_file()


def test_manual_plan_cli_writes_readiness_failure_without_plan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    result = cli.main([
        "plan", "--data", str(ROOT / "examples/demo_daily.csv"),
        "--cash", "100000", "--config", str(ROOT / "configs/default.toml"),
        "--purpose", "MANUAL_REVIEW",
    ])
    assert result == 1
    folders = list((tmp_path / "outputs/plans").glob("readiness-*"))
    assert len(folders) == 1
    report = pd.read_json(folders[0] / "plan-readiness.json", typ="series")
    assert report["status"] == "NOT_READY"
    assert any("正式基线 readiness" in reason for reason in report["blocking_reasons"])
    assert not list(folders[0].glob("plan.csv"))
