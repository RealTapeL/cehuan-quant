"""策环测试模块：验证 10 日与 20 日动量配对排名使用共同候选和同一冻结输入。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from cehuan.data.security_identity import IDENTITY_EVENT_COLUMNS
from cehuan.research.lookback_pair import (
    EXPERIMENT_ID,
    FIXED_SYMBOLS,
    create_lookback_pair_ranking,
)


def _sha(path: Path) -> str:
    """私有测试辅助函数，用于SHA。 参数：`path`：路径场景输入。 返回：str，供调用处构造夹具或断言复用。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path: Path) -> dict[str, Path]:
    """私有测试辅助函数，用于输入。 参数：`tmp_path`：pytest 提供的临时目录。 返回：dict[str, Path]，供调用处构造夹具或断言复用。"""
    repo = Path(__file__).resolve().parents[1]
    signal = "2026-12-17"
    dates = pd.bdate_range(end=signal, periods=21).strftime("%Y-%m-%d").tolist()
    endpoints = [
        (100, 120, 130), (100, 100, 125), (100, 115, 125), (100, 100, 120),
        (100, 110, 118), (100, 100, 116), (100, 108, 114), (100, 100, 112),
        (100, 109, 111), (100, 100, 110),
    ]
    rows = []
    for symbol, (start, mid, end) in zip(FIXED_SYMBOLS, endpoints, strict=True):
        for index, day in enumerate(dates):
            if index <= 10:
                close = start + (mid - start) * index / 10
            else:
                close = mid + (end - mid) * (index - 10) / 10
            rows.append({
                "trade_date": day, "symbol": symbol, "name": symbol,
                "open": close, "high": close + 0.1, "low": close - 0.1,
                "close": close, "adjusted_close": close, "volume_shares": 10000,
                "amount_cny": close * 10000, "is_listed": True,
                "is_suspended": False, "is_st": False,
                "data_source": "synthetic_fixture", "retrieved_at_utc": f"{day}T08:00:00Z",
                "available_at": f"{day}T15:00:00+08:00",
            })
    bars_path = tmp_path / "bars.csv"
    pd.DataFrame(rows).to_csv(bars_path, index=False)

    calendar_path = tmp_path / "calendar.csv"
    pd.DataFrame({
        "trade_date": dates, "is_open": True, "source": "synthetic_calendar",
    }).to_csv(calendar_path, index=False)

    universe_path = tmp_path / "universe.csv"
    pd.DataFrame([
        {"trade_date": day, "symbol": symbol, "pool_status": "STOCK_MEMBER"}
        for day in dates for symbol in FIXED_SYMBOLS
    ]).to_csv(universe_path, index=False)

    pool_path = tmp_path / "selected_pool.csv"
    pd.DataFrame({"symbol": FIXED_SYMBOLS}).to_csv(pool_path, index=False)

    identity_path = tmp_path / "identity-events.csv"
    pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS).to_csv(identity_path, index=False)

    config_path = tmp_path / "default.toml"
    config_path.write_bytes((repo / "configs/default.toml").read_bytes())

    freeze_path = tmp_path / "registration-freeze.json"
    freeze = {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "signal_observation": {"fixed_signal_date": "2026-12-17"},
        "research_pool": {"symbols": list(FIXED_SYMBOLS)},
        "frozen_input_files": {
            "strategy_config": {"path": str(config_path), "sha256": _sha(config_path)},
            "pool": {"path": str(pool_path), "sha256": _sha(pool_path)},
            "calendar": {"path": str(calendar_path), "sha256": _sha(calendar_path)},
        },
    }
    freeze_path.write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")
    freeze_path.with_suffix(".sha256").write_text(f"{_sha(freeze_path)}  registration-freeze.json\n", encoding="ascii")

    capture_path = tmp_path / "capture-manifest.json"
    paths = {
        "bars": bars_path,
        "calendar": calendar_path,
        "universe": universe_path,
        "pool": pool_path,
        "identity_events": identity_path,
        "registration_freeze": freeze_path,
    }
    capture_path.write_text(json.dumps({
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "signal_date": signal,
        "provider": "synthetic-fixture",
        "captured_at_utc": "2026-12-17T07:10:00Z",
        "provider_published_at_utc": None,
        "provider_publication_evidence": "UNVERIFIED",
        "trusted_external_timestamp": "UNVERIFIED",
        "input_sha256": {key: _sha(path) for key, path in paths.items()},
    }, indent=2) + "\n", encoding="utf-8")
    return {
        "bars": bars_path, "calendar": calendar_path, "universe": universe_path,
        "pool": pool_path, "identity": identity_path, "config": config_path,
        "freeze": freeze_path, "capture": capture_path,
    }


def _run(inputs: dict[str, Path], output: Path, *, mode: str = "SYNTHETIC") -> dict:
    """私有测试辅助函数，用于运行。 参数：`inputs`：输入场景输入；`output`：输出场景输入；`mode`：模式场景输入。 返回：dict，供调用处构造夹具或断言复用。"""
    return create_lookback_pair_ranking(
        bars_path=inputs["bars"],
        config_path=inputs["config"],
        calendar_path=inputs["calendar"],
        universe_path=inputs["universe"],
        pool_path=inputs["pool"],
        identity_events_path=inputs["identity"],
        registration_freeze_path=inputs["freeze"],
        capture_manifest_path=inputs["capture"],
        signal_date="2026-12-17",
        output_dir=output,
        mode=mode,
        now_utc=datetime(2026, 12, 17, 8, 0, tzinfo=timezone.utc),
    )


def _refresh_capture(inputs: dict[str, Path]) -> None:
    """私有测试辅助函数，用于刷新采集。 参数：`inputs`：输入场景输入。 返回：None，供调用处构造夹具或断言复用。"""
    capture_path = inputs["capture"]
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    for key, source_key in (
        ("bars", "bars"), ("calendar", "calendar"), ("universe", "universe"),
        ("pool", "pool"), ("identity_events", "identity"), ("registration_freeze", "freeze"),
    ):
        capture["input_sha256"][key] = _sha(inputs[source_key])
    capture_path.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")


def test_paired_rankings_use_same_candidates_and_only_change_lookback(tmp_path: Path) -> None:
    """验证配对排名使用相同候选与仅变更回看窗口。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    result = _run(inputs, tmp_path / "pair-run")
    out = Path(result["output_dir"])
    manifest = result["manifest"]

    assert manifest["status"] == "CAPTURED_SYNTHETIC_RANKING_ONLY"
    assert manifest["classification"] == "SYNTHETIC_ONLY"
    assert manifest["common_candidate_count"] == 10
    assert manifest["candidate_count_20d"] == 10
    assert manifest["candidate_count_10d"] == 10
    assert manifest["top5"]["20d"] != manifest["top5"]["10d"]
    assert manifest["top5"]["only_20d"]
    assert manifest["top5"]["only_10d"]

    diff = json.loads((out / "config-difference.json").read_text(encoding="utf-8"))
    assert diff["differences"] == [{
        "path": "strategy.lookback_sessions", "baseline": 20, "candidate": 10,
    }]
    rank20 = pd.read_csv(out / "ranking-20d.csv")
    rank10 = pd.read_csv(out / "ranking-10d.csv")
    difference = pd.read_csv(out / "ranking-difference.csv")
    assert rank20["symbol"].tolist() == manifest["top5"]["20d"] + rank20.loc[rank20["rank"].gt(5), "symbol"].tolist()
    assert set(rank20["symbol"]) == set(rank10["symbol"])
    assert len(difference) == 10
    assert set(difference["symbol"]) == set(FIXED_SYMBOLS)
    assert {"candidate_eligible_20d", "candidate_eligible_10d", "rank_delta_10d_minus_20d"}.issubset(difference.columns)
    assert not {"shares", "quantity", "order_id", "planned_shares"}.intersection(rank20.columns)
    assert manifest["strategy_returns_computed"] is False
    assert manifest["forward_outcomes_computed"] is False
    assert manifest["orders_generated"] is False
    assert manifest["output_files"]["ranking-20d.csv"] == _sha(out / "ranking-20d.csv")
    assert manifest["output_files"]["ranking-10d.csv"] == _sha(out / "ranking-10d.csv")
    assert (out / "manifest.sha256").read_text().split()[0] == _sha(out / "manifest.json")


def test_known_suspension_is_excluded_from_both_arms_without_shrinking_silently(tmp_path: Path) -> None:
    """验证已知停牌排除从两组两组缺少缩小静默地。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    bars = pd.read_csv(inputs["bars"])
    date = bars.loc[bars["symbol"].eq(FIXED_SYMBOLS[0]), "trade_date"].iloc[4]
    bars.loc[bars["symbol"].eq(FIXED_SYMBOLS[0]) & bars["trade_date"].eq(date), "is_suspended"] = True
    bars.to_csv(inputs["bars"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "suspended-run")
    out = Path(result["output_dir"])
    assert result["manifest"]["status"] == "CAPTURED_SYNTHETIC_RANKING_ONLY"
    assert result["manifest"]["common_candidate_count"] == 9
    audit = pd.read_csv(out / "per-security-audit.csv")
    row = audit.loc[audit["symbol"].eq(FIXED_SYMBOLS[0])].iloc[0]
    assert row["eligibility_status"] == "EXCLUDED_KNOWN_SUSPENSION_IN_COMMON_WINDOW"
    assert date in row["suspended_sessions"]
    assert FIXED_SYMBOLS[0] not in set(pd.read_csv(out / "ranking-20d.csv")["symbol"])
    assert FIXED_SYMBOLS[0] not in set(pd.read_csv(out / "ranking-10d.csv")["symbol"])


def test_known_nonmember_and_signal_day_st_are_excluded_symmetrically(tmp_path: Path) -> None:
    """验证已知非成员与信号日ST状态排除对称。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    universe = pd.read_csv(inputs["universe"])
    nonmember = FIXED_SYMBOLS[0]
    universe.loc[universe["symbol"].eq(nonmember) & universe["trade_date"].eq("2026-12-10"), "pool_status"] = "NOT_STOCK_MEMBER"
    universe.to_csv(inputs["universe"], index=False)
    bars = pd.read_csv(inputs["bars"])
    st_symbol = FIXED_SYMBOLS[1]
    bars.loc[bars["symbol"].eq(st_symbol) & bars["trade_date"].eq("2026-12-17"), "is_st"] = True
    bars.to_csv(inputs["bars"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "known-exclusions-run")
    out = Path(result["output_dir"])
    assert result["manifest"]["status"] == "CAPTURED_SYNTHETIC_RANKING_ONLY"
    assert result["manifest"]["common_candidate_count"] == 8
    audit = pd.read_csv(out / "per-security-audit.csv").set_index("symbol")
    assert audit.loc[nonmember, "eligibility_status"] == "EXCLUDED_KNOWN_NONMEMBER_IN_COMMON_WINDOW"
    assert audit.loc[st_symbol, "eligibility_status"] == "EXCLUDED_SIGNAL_DATE_ST"
    for file_name in ("ranking-20d.csv", "ranking-10d.csv"):
        ranked = set(pd.read_csv(out / file_name)["symbol"])
        assert nonmember not in ranked and st_symbol not in ranked


def test_unknown_status_blocks_whole_signal_and_writes_no_rankings(tmp_path: Path) -> None:
    """验证未知状态阻断整个信号与写入没有排名。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    bars = pd.read_csv(inputs["bars"])
    target = (bars["symbol"].eq(FIXED_SYMBOLS[0]) & bars["trade_date"].eq("2026-12-17"))
    bars["is_suspended"] = bars["is_suspended"].astype(object)
    bars.loc[target, "is_suspended"] = pd.NA
    bars.to_csv(inputs["bars"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "unknown-run")
    out = Path(result["output_dir"])
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert not (out / "ranking-20d.csv").exists()
    assert not (out / "ranking-10d.csv").exists()
    audit = pd.read_csv(out / "per-security-audit.csv")
    row = audit.loc[audit["symbol"].eq(FIXED_SYMBOLS[0])].iloc[0]
    assert row["eligibility_status"] == "BLOCKED"
    assert "2026-12-17" in row["unknown_status_sessions"]


def test_unknown_membership_blocks_instead_of_treating_as_nonmember(tmp_path: Path) -> None:
    """验证未知成员资格阻断而非的视为为非成员。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    universe = pd.read_csv(inputs["universe"])
    target = universe["symbol"].eq(FIXED_SYMBOLS[1]) & universe["trade_date"].eq("2026-12-17")
    universe.loc[target, "pool_status"] = "UNKNOWN"
    universe.to_csv(inputs["universe"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "unknown-member-run")
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert "成员状态未知或缺失" in " ".join(result["manifest"]["blocking_reasons"])
    assert not (Path(result["output_dir"]) / "ranking-20d.csv").exists()


def test_missing_and_duplicate_session_block_the_paired_day(tmp_path: Path) -> None:
    """验证缺失与重复交易日阻断配对日。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    for case in ("missing", "duplicate"):
        root = tmp_path / case
        root.mkdir()
        inputs = _inputs(root)
        bars = pd.read_csv(inputs["bars"])
        mask = bars["symbol"].eq(FIXED_SYMBOLS[2]) & bars["trade_date"].eq("2026-12-10")
        if case == "missing":
            bars = bars.loc[~mask]
        else:
            bars = pd.concat([bars, bars.loc[mask]], ignore_index=True)
        bars.to_csv(inputs["bars"], index=False)
        _refresh_capture(inputs)

        result = _run(inputs, root / "output")
        assert result["manifest"]["status"] == "NEEDS_DATA"
        assert not (Path(result["output_dir"]) / "ranking-20d.csv").exists()
        reason = " ".join(result["manifest"]["blocking_reasons"])
        assert ("成员且非停牌日缺少行情" in reason) if case == "missing" else ("重复行情日" in reason)


def test_invalid_adjusted_price_blocks_instead_of_falling_back_to_raw(tmp_path: Path) -> None:
    """验证无效复权价格阻断而非的递补回到到原始。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    bars = pd.read_csv(inputs["bars"])
    target = bars["symbol"].eq(FIXED_SYMBOLS[3]) & bars["trade_date"].eq("2026-12-10")
    bars.loc[target, "adjusted_close"] = float("nan")
    bars.to_csv(inputs["bars"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "invalid-price-run")
    assert result["manifest"]["status"] == "NEEDS_DATA"
    audit = pd.read_csv(Path(result["output_dir"]) / "per-security-audit.csv")
    row = audit.loc[audit["symbol"].eq(FIXED_SYMBOLS[3])].iloc[0]
    assert row["price_basis_status"] == "INVALID_OR_MIXED"
    assert not (Path(result["output_dir"]) / "ranking-10d.csv").exists()


def test_identity_event_inside_signal_window_blocks_without_auto_stitching(tmp_path: Path) -> None:
    """验证身份事件范围内信号窗口阻断缺少自动拼接。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    event = {column: "" for column in IDENTITY_EVENT_COLUMNS}
    event.update({
        "event_id": "synthetic-code-change",
        "event_type": "CODE_CHANGE_1_TO_1",
        "old_symbol": FIXED_SYMBOLS[0],
        "new_symbol": "SH600099",
        "effective_date": "2026-12-10",
        "share_ratio": "1.0",
        "ratio_basis": "old_shares_to_new_shares",
        "security_identity": "synthetic-security",
        "event_status": "CONFIRMED",
        "evidence_type": "synthetic-test-only",
        "evidence_url": "https://example.invalid/synthetic",
        "evidence_summary": "synthetic fixture; not market evidence",
    })
    pd.DataFrame([event], columns=IDENTITY_EVENT_COLUMNS).to_csv(inputs["identity"], index=False)
    _refresh_capture(inputs)

    result = _run(inputs, tmp_path / "identity-event-run")
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert "身份事件" in " ".join(result["manifest"]["blocking_reasons"])
    assert not (Path(result["output_dir"]) / "ranking-20d.csv").exists()


def test_capture_hash_mismatch_is_blocked_and_inputs_are_bound(tmp_path: Path) -> None:
    """验证采集哈希不符阻断与输入绑定。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    capture = json.loads(inputs["capture"].read_text(encoding="utf-8"))
    capture["input_sha256"]["bars"] = "0" * 64
    inputs["capture"].write_text(json.dumps(capture) + "\n", encoding="utf-8")

    result = _run(inputs, tmp_path / "bad-hash-run")
    out = Path(result["output_dir"])
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert "bars SHA-256" in " ".join(result["manifest"]["blocking_reasons"])
    assert not (out / "ranking-20d.csv").exists()
    assert result["manifest"]["input_sha256"]["bars"] == _sha(inputs["bars"])


def test_frozen_momentum_implementation_hash_mismatch_blocks(tmp_path: Path) -> None:
    """验证冻结动量实现哈希不符阻断。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    implementation = tmp_path / "frozen-strategy.py"
    implementation.write_text("# reviewed implementation v1\n", encoding="utf-8")
    freeze = json.loads(inputs["freeze"].read_text(encoding="utf-8"))
    freeze["frozen_input_files"]["strategy_implementation"] = {
        "path": str(implementation), "sha256": _sha(implementation),
    }
    inputs["freeze"].write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")
    inputs["freeze"].with_suffix(".sha256").write_text(
        f"{_sha(inputs['freeze'])}  registration-freeze.json\n", encoding="ascii",
    )
    _refresh_capture(inputs)
    implementation.write_text("# changed implementation\n", encoding="utf-8")

    result = _run(inputs, tmp_path / "strategy-hash-run")
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert "动量计算实现" in " ".join(result["manifest"]["blocking_reasons"])
    assert not (Path(result["output_dir"]) / "ranking-20d.csv").exists()


def test_forward_mode_rejects_non_baostock_capture_before_ranking(tmp_path: Path) -> None:
    """验证前瞻模式拒绝非BaoStock采集之前排名。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    inputs = _inputs(tmp_path)
    result = _run(inputs, tmp_path / "wrong-provider-run", mode="FORWARD_PAPER")
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert "provider 必须为 BaoStock" in " ".join(result["manifest"]["blocking_reasons"])
    assert not (Path(result["output_dir"]) / "ranking-20d.csv").exists()
