"""策环测试模块：验证成交复盘中的无证据、重复导入、失败恢复、并发和未知状态。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

from datetime import datetime
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
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
CLI_MODULE = importlib.import_module("cehuan.cli.main")
REGISTRY_MODULE = importlib.import_module("cehuan.review.import_registry")


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
    """验证合成周期异常场景可追溯。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`scenario`：场景场景输入；`expected_status`：预期状态场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证没有成交缺少订单证据不已取消。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证状态证据区分明确取消。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证导入登记簿拒绝完全一致重复缺少第二计算。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    fills_path = tmp_path / "fills.csv"
    fills_path.write_text("trade_date,symbol,side,quantity,price\n2026-11-03,SH600000,BUY,100,10\n", encoding="utf-8")
    registry = tmp_path / "imports.json"
    manifest_path = tmp_path / "review" / "manifest.json"
    manifest_path.parent.mkdir()
    fills_digest = hashlib.sha256(fills_path.read_bytes()).hexdigest()
    (manifest_path.parent / "review.csv").write_text("review", encoding="utf-8")
    (manifest_path.parent / "review.md").write_text("review", encoding="utf-8")
    manifest_path.write_text(json.dumps({
        "cycle_run_id": "cycle-1",
        "result_type": "manual_account_reconciliation_csv_not_broker_connected",
        "input_sha256": {"fills": fills_digest},
        "fill_import_reference": {
            "import_id": f"fill-import-{fills_digest[:16]}",
            "source_sha256": fills_digest,
        },
    }), encoding="utf-8")
    receipt = register_fill_import(
        fills_path, registry, row_count=1, cycle_run_id="cycle-1", output_manifest_path=manifest_path,
    )
    assert receipt["row_count"] == 1
    with pytest.raises(DuplicateFillImportError, match="本次未重复计入"):
        register_fill_import(fills_path, registry, row_count=1, cycle_run_id="cycle-2")
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert len(document["imports"]) == 1
    assert document["imports"][0]["output_manifest_path"] == str(manifest_path.resolve())


def test_empty_fill_csv_is_supported_as_no_fill(tmp_path):
    """验证空成交CSV已支持为没有成交。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    path = tmp_path / "empty.csv"
    pd.DataFrame(columns=["trade_date", "symbol", "side", "quantity", "price"]).to_csv(path, index=False)
    assert read_fills(path).empty


def test_plan_revision_diff_preserves_old_and_new_quantities():
    """验证计划修订差异保留旧与新数量。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证复核CLI写入唯一清单修订差异与未知持仓。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`capsys`：pytest 提供的标准输出捕获工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证复核CLI核验与哈希订单状态来源文件。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`capsys`：pytest 提供的标准输出捕获工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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


def _write_review_inputs(root: Path) -> tuple[Path, Path]:
    """私有测试辅助函数，用于写入复核输入。 参数：`root`：代码标识“root”（含义待核实）场景输入。 返回：tuple[Path, Path]，供调用处构造夹具或断言复用。"""
    plan_path = root / "plan.csv"
    pd.DataFrame([{
        "plan_id": "plan-a", "symbol": "SH600000", "action": "BUY",
        "planned_shares": 100, "target_shares": 100, "reference_price": 10.0,
    }]).to_csv(plan_path, index=False)
    fills_path = root / "fills.csv"
    pd.DataFrame([{
        "trade_date": "2026-11-03", "order_id": "order-a", "symbol": "SH600000",
        "side": "BUY", "quantity": 100, "price": 10.0,
    }]).to_csv(fills_path, index=False)
    return plan_path, fills_path


def test_review_cli_write_failure_does_not_register_and_retry_succeeds(tmp_path, monkeypatch, capsys):
    """验证复核CLI写入失败不登记与重试成功。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具；`capsys`：pytest 提供的标准输出捕获工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    plan_path, fills_path = _write_review_inputs(tmp_path)
    registry = tmp_path / "fill-imports.json"
    output = tmp_path / "review-output"
    original_write_json = CLI_MODULE._write_json

    def fail_manifest_write(value, path):
        """实现测试替身test_review_cli_write_failure_does_not_register_and_retry_succeeds的失败清单写入行为；用于控制被测代码与断言，不访问真实服务。 参数：`value`：值场景输入；`path`：路径场景输入。 返回值供调用处构造夹具或断言复用。"""
        if Path(path).name == "manifest.json":
            raise OSError("injected manifest write failure")
        return original_write_json(value, path)

    monkeypatch.setattr(CLI_MODULE, "_write_json", fail_manifest_write)
    failed = cli_main([
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--import-registry", str(registry), "--output-dir", str(output),
    ])
    capsys.readouterr()

    assert failed == 1
    assert not registry.exists()
    assert not output.exists()
    assert not list(tmp_path.glob(".review-output.staging-*"))

    monkeypatch.setattr(CLI_MODULE, "_write_json", original_write_json)
    retried = cli_main([
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--import-registry", str(registry), "--output-dir", str(output),
    ])
    capsys.readouterr()

    assert retried == 0
    import_record = json.loads(registry.read_text(encoding="utf-8"))["imports"][0]
    manifest_path = Path(import_record["output_manifest_path"])
    assert manifest_path.is_file()
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == import_record["output_manifest_sha256"]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["fill_import_reference"]["source_sha256"] == import_record["source_sha256"]


def test_review_cli_registry_write_failure_removes_report_and_retry_succeeds(tmp_path, monkeypatch, capsys):
    """验证复核CLI登记簿写入失败移除报告与重试成功。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具；`capsys`：pytest 提供的标准输出捕获工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    plan_path, fills_path = _write_review_inputs(tmp_path)
    registry = tmp_path / "fill-imports.json"
    output = tmp_path / "review-output"
    original_atomic_write = REGISTRY_MODULE._atomic_write

    def fail_registry_write(path, payload):
        """实现测试替身test_review_cli_registry_write_failure_removes_report_and_retry_succeeds的失败登记簿写入行为；用于控制被测代码与断言，不访问真实服务。 参数：`path`：路径场景输入；`payload`：载荷场景输入。 返回值供调用处构造夹具或断言复用。"""
        raise OSError("injected registry write failure")

    monkeypatch.setattr(REGISTRY_MODULE, "_atomic_write", fail_registry_write)
    failed = cli_main([
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--import-registry", str(registry), "--output-dir", str(output),
    ])
    capsys.readouterr()

    assert failed == 1
    assert not registry.exists()
    assert not output.exists()
    assert not list(tmp_path.glob(".review-output.staging-*"))

    monkeypatch.setattr(REGISTRY_MODULE, "_atomic_write", original_atomic_write)
    retried = cli_main([
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--import-registry", str(registry), "--output-dir", str(output),
    ])
    capsys.readouterr()

    assert retried == 0
    registered = json.loads(registry.read_text(encoding="utf-8"))["imports"]
    assert len(registered) == 1
    assert Path(registered[0]["output_manifest_path"]).is_file()


def test_concurrent_review_import_registers_exactly_one_report(tmp_path, monkeypatch):
    """验证并发复核导入登记恰好一项报告。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    plan_path, fills_path = _write_review_inputs(tmp_path)
    registry = tmp_path / "shared-imports.json"
    monkeypatch.setattr(CLI_MODULE, "REPO_ROOT", tmp_path)
    mapping = Path.cwd() / "configs" / "huatai_columns.toml"

    def run_review() -> int:
        """实现测试替身test_concurrent_review_import_registers_exactly_one_report的运行复核行为；用于控制被测代码与断言，不访问真实服务。 返回：int，供调用处构造夹具或断言复用。"""
        return cli_main([
            "review", "--plan", str(plan_path), "--fills", str(fills_path),
            "--mapping", str(mapping), "--import-registry", str(registry),
        ])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted([executor.submit(run_review), executor.submit(run_review)], key=lambda future: id(future))
        exit_codes = sorted(future.result() for future in results)

    assert exit_codes == [0, 1]
    records = json.loads(registry.read_text(encoding="utf-8"))["imports"]
    assert len(records) == 1
    registered_manifest = Path(records[0]["output_manifest_path"])
    assert registered_manifest.is_file()
    review_dirs = list((tmp_path / "outputs" / "reviews").glob("review-*"))
    assert review_dirs == [registered_manifest.parent]


@pytest.mark.parametrize("crash_after_registry_commit", [False, True])
def test_review_cli_recovers_after_process_exit_around_registry_commit(
    tmp_path, monkeypatch, capsys, crash_after_registry_commit,
):
    """验证复核CLI恢复之后进程退出附近登记簿提交。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具；`capsys`：pytest 提供的标准输出捕获工具；`crash_after_registry_commit`：崩溃之后登记簿提交场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    plan_path, fills_path = _write_review_inputs(tmp_path)
    registry = tmp_path / "fill-imports.json"
    output = tmp_path / "review-output"
    original_register = CLI_MODULE.register_fill_import

    def crash_at_commit(*args, **kwargs):
        """实现测试替身test_review_cli_recovers_after_process_exit_around_registry_commit的崩溃于提交行为；用于控制被测代码与断言，不访问真实服务。 参数：`*args`：参数场景输入；`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
        if crash_after_registry_commit:
            original_register(*args, **kwargs)
        raise SystemExit("injected process exit at commit boundary")

    monkeypatch.setattr(CLI_MODULE, "register_fill_import", crash_at_commit)
    argv = [
        "review", "--plan", str(plan_path), "--fills", str(fills_path),
        "--import-registry", str(registry), "--output-dir", str(output),
    ]
    with pytest.raises(SystemExit, match="injected process exit"):
        cli_main(argv)
    capsys.readouterr()
    assert (output / "manifest.json").is_file()
    records_before_retry = json.loads(registry.read_text(encoding="utf-8"))["imports"] if registry.exists() else []
    assert len(records_before_retry) == int(crash_after_registry_commit)

    monkeypatch.setattr(CLI_MODULE, "register_fill_import", original_register)
    resumed = cli_main(argv)
    capsys.readouterr()

    assert resumed == 0
    records = json.loads(registry.read_text(encoding="utf-8"))["imports"]
    assert len(records) == 1
    assert Path(records[0]["output_manifest_path"]) == output / "manifest.json"
