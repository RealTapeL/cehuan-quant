"""策环测试模块：验证候选池清单、哈希和原始运行锚点的证据等级。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from cehuan.research.candidate_artifacts import (
    classify_candidate_inventory,
    inspect_original_run_candidate_anchor,
    reconstruct_candidate_pool,
)


def _sha(path: Path) -> str:
    """私有测试辅助函数，用于SHA。 参数：`path`：路径场景输入。 返回：str，供调用处构造夹具或断言复用。"""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_candidate_inventory_match_is_not_an_original_run_anchor(tmp_path: Path) -> None:
    """验证候选清单匹配不原始运行锚点。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("signal_date,symbol,rank\n2025-01-01,SH600001,1\n", encoding="utf-8")
    inventory = tmp_path / "artifact-inventory.csv"
    pd.DataFrame([{"relative_path": "candidate-pool-by-signal.csv", "sha256": _sha(candidate)}]).to_csv(inventory, index=False)

    result = classify_candidate_inventory(candidate, inventory)

    assert result["status"] == "MATCHED_SUPPLIED_INVENTORY"
    assert result["original_run_time_anchor"] == "UNVERIFIED"


def test_candidate_inventory_duplicate_name_is_ambiguous(tmp_path: Path) -> None:
    """验证候选清单重复名称有歧义。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("content\n", encoding="utf-8")
    inventory = tmp_path / "artifact-inventory.csv"
    pd.DataFrame([
        {"relative_path": "run-a/candidate-pool-by-signal.csv", "sha256": _sha(candidate)},
        {"relative_path": "run-b/candidate-pool-by-signal.csv", "sha256": _sha(candidate)},
    ]).to_csv(inventory, index=False)
    assert classify_candidate_inventory(candidate, inventory)["status"] == "AMBIGUOUS"


def test_candidate_inventory_hash_mismatch_and_not_provided_are_distinct(tmp_path: Path) -> None:
    """验证候选清单哈希不符与不提供不同。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("content\n", encoding="utf-8")
    inventory = tmp_path / "artifact-inventory.csv"
    pd.DataFrame([{"relative_path": candidate.name, "sha256": "0" * 64}]).to_csv(inventory, index=False)
    assert classify_candidate_inventory(candidate, inventory)["status"] == "HASH_MISMATCH"
    assert classify_candidate_inventory(candidate, None)["status"] == "NOT_PROVIDED"


def test_original_run_manifest_without_runtime_candidate_hash_is_unverified(tmp_path: Path) -> None:
    """验证原始运行清单缺少运行时候选哈希未。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("content\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"runtime_artifacts": {}}), encoding="utf-8")

    result = inspect_original_run_candidate_anchor(manifest, candidate)

    assert result["status"] == "UNVERIFIED"
    assert result["manifest_status"] == "AVAILABLE_NO_RUNTIME_CANDIDATE_HASH"


def test_matching_original_run_hash_claim_is_still_unverified_without_trusted_parent(tmp_path: Path) -> None:
    """验证匹配原始运行哈希声明仍然未缺少可信上游。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("content\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "runtime_artifacts": {candidate.name: {"sha256": _sha(candidate)}}
    }), encoding="utf-8")

    result = inspect_original_run_candidate_anchor(manifest, candidate)

    assert result["status"] == "UNVERIFIED"
    assert result["manifest_status"] == "RUNTIME_HASH_CLAIM_MATCHED_BUT_MANIFEST_AUTHENTICITY_UNVERIFIED"


def test_original_run_hash_mismatch_duplicate_and_invalid_manifest_fail(tmp_path: Path) -> None:
    """验证原始运行哈希不符重复与无效清单失败。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    candidate = tmp_path / "candidate-pool-by-signal.csv"
    candidate.write_text("content\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "runtime_artifacts": {candidate.name: {"sha256": "0" * 64}}
    }), encoding="utf-8")
    assert inspect_original_run_candidate_anchor(manifest, candidate)["status"] == "FAILED"

    manifest.write_text(json.dumps({
        "runtime_artifacts": {
            f"run-a/{candidate.name}": {"sha256": _sha(candidate)},
            f"run-b/{candidate.name}": {"sha256": _sha(candidate)},
        }
    }), encoding="utf-8")
    duplicate = inspect_original_run_candidate_anchor(manifest, candidate)
    assert duplicate["status"] == "FAILED"
    assert duplicate["manifest_status"] == "AMBIGUOUS_RUNTIME_BINDING"

    manifest.write_text("not json", encoding="utf-8")
    invalid = inspect_original_run_candidate_anchor(manifest, candidate)
    assert invalid["status"] == "FAILED"
    assert invalid["manifest_status"] == "INVALID_MANIFEST"


def test_candidate_pool_can_be_rebuilt_from_frozen_bars_and_config(tmp_path: Path) -> None:
    """验证候选股票池可以重建从冻结行情与配置。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    dates = pd.bdate_range("2025-01-02", periods=22).strftime("%Y-%m-%d")
    bars = []
    for symbol, multiplier in (("SH600001", 1.0), ("SZ000001", 1.3)):
        for index, day in enumerate(dates):
            close = 10 + multiplier * index
            bars.append({
                "trade_date": day, "symbol": symbol, "name": symbol,
                "open": close, "high": close, "low": close, "close": close,
                "adjusted_close": close, "volume_shares": 10000,
                "is_listed": True, "is_suspended": False, "is_st": False,
                "available_at": f"{day}T15:00:00+08:00", "data_source": "synthetic",
            })
    bars_path = tmp_path / "bars.csv"
    pd.DataFrame(bars).to_csv(bars_path, index=False)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "strategy": {"lookback_sessions": 20, "max_positions": 1, "min_momentum": 0},
        "market": {"require_point_in_time_status": True, "exclude_st": True},
    }), encoding="utf-8")
    signal_date = dates[20]
    seed = pd.DataFrame([{
        "signal_date": signal_date, "execution_date": dates[21],
        "symbol": "SZ000001", "momentum": 0.0, "rank": 1, "selected_target": True,
    }])
    first, rebuilt = reconstruct_candidate_pool(bars_path, config_path, seed)
    assert first["status"] == "MISMATCH"
    assert len(rebuilt) == 2

    matching, _ = reconstruct_candidate_pool(bars_path, config_path, rebuilt)
    assert matching["status"] == "MATCHED_FROZEN_INPUT_RECONSTRUCTION"
    assert matching["proves_original_run_time_anchor"] is False

    changed = rebuilt.copy()
    changed.loc[0, "rank"] = 999
    mismatch, _ = reconstruct_candidate_pool(bars_path, config_path, changed)
    assert mismatch["status"] == "MISMATCH"
