"""策环测试模块：验证合成 1:1 证券代码变更保留持仓批次且不生成虚假成交。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from pathlib import Path
import runpy

import pandas as pd


def test_synthetic_code_change_diagnostic_records_1_to_1_migration(tmp_path):
    """验证合成代码变更诊断记录1到1迁移。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
