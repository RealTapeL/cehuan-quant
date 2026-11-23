"""策环测试模块：验证合成公司行动诊断产物可审计，且人工审核保持未签认。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from pathlib import Path
import runpy

import pandas as pd


def test_synthetic_dividend_and_distribution_diagnostic_writes_auditable_outputs(tmp_path):
    """验证合成分红与送转诊断写入可审计输出。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证公告字段复核矩阵有效与未签认。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    matrix = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "corporate-action-sample-field-review.csv"
    fields = pd.read_csv(matrix, dtype=str, keep_default_na=False)

    assert len(fields) >= 20
    assert fields["sample_id"].nunique() == 3
    assert set(fields["review_status"]) == {"PENDING"}
    assert not fields["reviewed_by"].str.strip().any()
    assert not fields["reviewed_at"].str.strip().any()
    assert set(fields["suggested_review_outcome"]) <= {
        "VERIFIED_FROM_ANNOUNCEMENT", "NOT_IN_ANNOUNCEMENT", "NEEDS_OTHER_SOURCE", "UNRESOLVED",
    }
    assert fields["review_guidance"].str.strip().all()
    assert not fields["reviewer_excerpt"].str.strip().any()
    assert not fields["reviewer_reason"].str.strip().any()
    assert not fields["reviewed_source_sha256"].str.strip().any()
    assert len(fields) == 23
