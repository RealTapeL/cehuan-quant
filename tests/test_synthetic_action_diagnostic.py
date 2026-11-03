from pathlib import Path
import runpy

import pandas as pd


def test_synthetic_dividend_and_distribution_diagnostic_writes_auditable_outputs(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_synthetic_action_diagnostic.py"
    run = runpy.run_path(str(script), run_name="synthetic_action_fixture")["run"]

    output = run(tmp_path / "out")

    manifest = pd.read_json(output / "manifest.json", typ="series")
    assert manifest["synthetic_only"]
    assert not manifest["private_market_data_used"]
    metrics = pd.read_json(output / "diagnostic_metrics.json", typ="series")
    assert metrics["accounting_mode"] == "ledger_v2"
    assert metrics["dividend_tax_treatment_by_event"]["synthetic-dividend-2024-07"] == "DEFERRED_BY_HOLDING_PERIOD"
    curve = pd.read_csv(output / "equity_curve.csv")
    ex_date = curve.loc[curve["trade_date"] == "2024-07-05"].iloc[0]
    assert ex_date["tax_review_status"] == "VALUED_WITH_UNRESOLVED_DIVIDEND_TAX"
    assert ex_date["valuation_tax_scope"] == "GROSS_CASH_EXCLUDING_UNRESOLVED_FUTURE_DIVIDEND_TAX"
    journal = pd.read_csv(output / "accounting_journal.csv")
    assert set(journal.loc[journal["booking_date"] == "2024-07-05", "event_id"]) == {
        "synthetic-dividend-2024-07", "synthetic-transfer-2024-07",
    }


def test_announcement_field_review_matrix_is_valid_and_unsigned():
    matrix = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "corporate-action-sample-field-review.csv"
    fields = pd.read_csv(matrix, dtype=str, keep_default_na=False)

    assert len(fields) >= 20
    assert fields["sample_id"].nunique() == 3
    assert set(fields["review_status"]) == {"PENDING"}
    assert not fields["reviewed_by"].str.strip().any()
    assert not fields["reviewed_at"].str.strip().any()
