from pathlib import Path
import runpy

import pandas as pd


def test_synthetic_code_change_diagnostic_records_1_to_1_migration(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_synthetic_code_change_diagnostic.py"
    run = runpy.run_path(str(script), run_name="synthetic_code_change_fixture")["run"]

    output = run(tmp_path / "out")

    manifest = pd.read_json(output / "manifest.json", typ="series")
    assert manifest["synthetic_only"]
    assert not manifest["private_market_data_used"]
    assert not manifest["broker_data_used"]
    metrics = pd.read_json(output / "diagnostic_metrics.json", typ="series")
    assert metrics["accounting_mode"] == "ledger_v2"
    security_events = pd.read_csv(output / "security_events.csv")
    migration = security_events.loc[security_events["event_type"] == "MIGRATE_POSITION"].iloc[0]
    assert migration["old_symbol"] == "SZ300114"
    assert migration["new_symbol"] == "SZ302132"
    assert migration["old_shares"] == migration["new_shares"]
    assert migration["cost_basis_before_cny"] == migration["cost_basis_after_cny"]
    assert migration["cash_before_cny"] == migration["cash_after_cny"]
    assert migration["trade_count_before"] == migration["trade_count_after"]
    journal = pd.read_csv(output / "accounting_journal.csv")
    assert "CODE_CHANGE_1_TO_1" in set(journal["entry_type"])
