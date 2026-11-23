"""策环测试模块：验证 checkpoint 增量输入预检对历史更正、未来追加和来源时点的分类。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from cehuan.backtest.incremental_inputs import audit_incremental_inputs


SHA = "a" * 64


def _seal(row: dict, content: dict) -> dict:
    """私有测试辅助函数，用于封存。 参数：`row`：行场景输入；`content`：内容场景输入。 返回：dict，供调用处构造夹具或断言复用。"""
    row["canonical_content"] = content
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    row["content_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return row


def _audit(baseline: dict, candidate: dict) -> dict:
    """私有测试辅助函数，用于审计。 参数：`baseline`：基线场景输入；`candidate`：候选场景输入。 返回：dict，供调用处构造夹具或断言复用。"""
    return audit_incremental_inputs(baseline, candidate)


def _proof() -> dict:
    """私有测试辅助函数，用于证明。 返回：dict，供调用处构造夹具或断言复用。"""
    known_event = _seal({
        "event_id": "known-event",
        "first_impact_date": "2025-01-10",
        "known_at_utc": "2025-01-01T00:00:00Z",
        "source_sha256": "6" * 64,
        "event_type": "CASH_DIVIDEND",
        "symbol": "SH600001",
        "gross_per_share_cny": "0.1",
    }, {
        "event_id": "known-event",
        "first_impact_date": "2025-01-10",
        "known_at_utc": "2025-01-01T00:00:00Z",
        "source_sha256": "6" * 64,
        "event_type": "CASH_DIVIDEND",
        "symbol": "SH600001",
        "record_date": "2025-01-10",
        "gross_per_share_cny": "0.1",
    })
    rule = _seal({
        "row_id": "rule-history", "valid_from": "2025-01-01", "valid_to": "2025-12-31",
        "known_at_utc": "2024-12-01T00:00:00Z", "source_sha256": "8" * 64,
        "symbol": "SH600001", "board": "MAIN", "buy_step_shares": 100,
    }, {
        "row_id": "rule-history", "valid_from": "2025-01-01", "valid_to": "2025-12-31",
        "known_at_utc": "2024-12-01T00:00:00Z", "source_sha256": "8" * 64,
        "symbol": "SH600001", "board": "MAIN", "buy_step_shares": 100,
    })
    fee = _seal({
        "row_id": "fee-history", "valid_from": "2025-01-01", "valid_to": "2025-12-31",
        "known_at_utc": "2024-12-01T00:00:00Z", "source_sha256": "a" * 64,
        "market": "A_SHARE", "commission_rate": "0.0002",
    }, {
        "row_id": "fee-history", "valid_from": "2025-01-01", "valid_to": "2025-12-31",
        "known_at_utc": "2024-12-01T00:00:00Z", "source_sha256": "a" * 64,
        "market": "A_SHARE", "commission_rate": "0.0002",
    })
    return {
        "schema_version": 1,
        "increment_id": "increment-2025-01-13-v1",
        "checkpoint": {
            "state_sha256": "1" * 64,
            "as_of_date": "2025-01-13",
            "next_expected_session": "2025-01-14",
            "decision_cutoff_utc": "2025-01-13T08:00:00Z",
            "cutoff_source": "synthetic checkpoint decision log",
            "cutoff_source_sha256": "2" * 64,
            "applied_increment_ids": [],
            "applied_increment_sha256s": [],
            "input_hashes": {
                "config_sha256": "3" * 64,
                "market_rules_sha256": None,
                "fee_schedule_sha256": None,
                "accounting_events_sha256": "b" * 64,
                "identity_events_sha256": "c" * 64,
                "corporate_actions_sha256": "d" * 64,
                "calendar_sha256": "e" * 64,
                "calendar_prefix_sha256": "f" * 64,
                "last_window_data_sha256": "1" * 64,
                "last_window_bars_sha256": "2" * 64,
                "data_chain_sha256": "4" * 64,
            },
        },
        "inputs": {
            "strategy_config_sha256": "3" * 64,
            "historical_bars_prefix_sha256": "4" * 64,
            "events": [known_event],
            "calendar": [
                {"date": "2025-01-10", "is_open": True},
                {"date": "2025-01-13", "is_open": True},
                {"date": "2025-01-14", "is_open": True},
            ],
            "rules": [rule],
            "fees": [fee],
        },
    }


def _add_event(proof: dict, *, impact: str = "2025-01-15", known_at: str = "2025-01-13T08:01:00Z") -> None:
    """私有测试辅助函数，用于新增事件。 参数：`proof`：证明场景输入；`impact`：代码标识“impact”（含义待核实）场景输入；`known_at`：已知于场景输入。 返回：None，供调用处构造夹具或断言复用。"""
    row = {
        "event_id": "new-event",
        "first_impact_date": impact,
        "known_at_utc": known_at,
        "source_sha256": "c" * 64,
        "event_type": "CASH_DIVIDEND",
        "symbol": "SH600002",
    }
    proof["inputs"]["events"].append(_seal(row, {
        **row,
        "record_date": impact,
        "gross_per_share_cny": "0.1",
    }))


def _blocker_codes(report: dict) -> set[str]:
    """私有测试辅助函数，用于阻断项代码。 参数：`report`：报告场景输入。 返回：set[str]，供调用处构造夹具或断言复用。"""
    return {row["code"] for row in report["blockers"]}


def test_future_event_first_available_after_cutoff_is_candidate_only_not_engine_resume():
    """验证未来事件首次可用之后截止候选仅不引擎续跑。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)

    report = _audit(baseline, candidate)

    assert report["candidate_rules_compatible"]
    assert not report["candidate_append_compatible"]
    assert report["checkpoint_binding_valid"] == "UNVERIFIED"
    assert report["input_files_binding_valid"] == "UNVERIFIED"
    assert report["additions"]["events"] == ["new-event"]
    assert report["can_continue"] is False
    assert report["engine_resume_supported"] is False


def test_missing_checkpoint_input_hashes_are_reported_unverified_not_treated_as_match():
    """验证缺失检查点输入哈希报告未不视为为匹配。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    del baseline["checkpoint"]["input_hashes"]

    report = _audit(baseline, deepcopy(baseline))

    assert report["candidate_rules_compatible"]
    assert report["checkpoint_binding_valid"] == "UNVERIFIED"
    assert "CHECKPOINT_BINDING_UNVERIFIED" in _blocker_codes(report)
    assert not report["candidate_append_compatible"]


def test_new_event_discovered_after_cutoff_but_affecting_history_requires_recompute():
    """验证新事件发现之后截止但影响历史要求重新计算。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, impact="2025-01-12")

    report = _audit(baseline, candidate)

    assert "LATE_EVENT_AFFECTS_HISTORY" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-12"
    assert report["requires_recompute"]


def test_correction_to_already_used_event_requires_recompute():
    """验证更正到已经使用事件要求重新计算。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["events"][0]["content_sha256"] = "d" * 64

    report = _audit(baseline, candidate)

    assert "USED_EVENT_CORRECTED" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-10"


@pytest.mark.parametrize(
    "field,value",
    [
        ("known_at_utc", "2025-01-02T00:00:00Z"),
        ("first_impact_date", "2025-01-11"),
        ("source_sha256", "9" * 64),
        ("symbol", "SH600999"),
    ],
)
def test_event_metadata_changes_cannot_hide_behind_old_content_hash(field: str, value: str):
    """验证事件元数据变化不能隐藏隐藏在旧内容哈希。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`field`：字段场景输入；`value`：值场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["events"][0][field] = value

    report = _audit(baseline, candidate)

    assert "USED_EVENT_CORRECTED" in _blocker_codes(report)
    assert "EVENT_CONTENT_HASH_INVALID" in _blocker_codes(report)
    assert not report["candidate_rules_compatible"]


def test_event_canonical_content_edit_with_stale_hash_is_rejected():
    """验证事件规范化内容修改带有过期哈希拒绝。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["events"][0]["canonical_content"]["gross_per_share_cny"] = "0.2"

    report = _audit(baseline, candidate)

    assert "EVENT_CONTENT_HASH_INVALID" in _blocker_codes(report)
    assert report["canonical_payload_digest_valid"] == "FAILED"


def test_missing_canonical_payload_is_explicitly_unverified():
    """验证缺失规范化载荷明确地未。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    del candidate["inputs"]["events"][0]["canonical_content"]

    report = _audit(baseline, candidate)

    assert "EVENT_CONTENT_HASH_UNVERIFIED" in _blocker_codes(report)
    assert report["canonical_payload_digest_valid"] == "UNVERIFIED"
    assert not report["candidate_rules_compatible"]


@pytest.mark.parametrize(
    ("family", "field", "value"),
    [
        ("events", "gross_per_share_cny", "0.2"),
        ("rules", "buy_step_shares", 200),
        ("fees", "commission_rate", "0.0003"),
    ],
)
def test_semantic_row_field_cannot_change_outside_canonical_content(family, field, value):
    """验证语义行字段不能变更范围外规范化内容。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`family`：类型场景输入；`field`：字段场景输入；`value`：值场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"][family][0][field] = value

    report = _audit(baseline, candidate)

    expected = "EVENT_CONTENT_HASH_INVALID" if family == "events" else "SCHEDULE_CONTENT_HASH_INVALID"
    assert expected in _blocker_codes(report)
    assert report["canonical_payload_digest_valid"] == "FAILED"


@pytest.mark.parametrize(
    ("known_at", "is_compatible"),
    [
        ("2025-01-13T07:59:59Z", False),  # same Shanghai date, before cutoff
        ("2025-01-13T08:00:00Z", False),  # cutoff itself is not later
        ("2025-01-13T08:00:01Z", True),   # after cutoff
    ],
)
def test_intraday_event_order_uses_explicit_utc_cutoff(known_at: str, is_compatible: bool):
    """验证盘中事件订单使用明确UTC截止。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`known_at`：已知于场景输入；`is_compatible`：兼容场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, known_at=known_at)

    report = _audit(baseline, candidate)

    assert report["candidate_rules_compatible"] is is_compatible
    assert report["candidate_append_compatible"] is False


def test_date_only_event_availability_is_unproven_and_rejected():
    """验证日期仅事件可用性未经证明与拒绝。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate, known_at="2025-01-13")

    report = _audit(baseline, candidate)

    assert "EVENT_KNOWN_AT_UNPROVEN" in _blocker_codes(report)
    assert not report["candidate_rules_compatible"]
    assert report["requires_recompute"]
    assert report["earliest_possible_affected_date"] is None


def test_baseline_event_without_known_at_provenance_cannot_be_used_for_incremental_claim():
    """验证基线事件缺少已知于来源链不能使用针对增量声明。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    del baseline["inputs"]["events"][0]["known_at_utc"]

    report = _audit(baseline, deepcopy(baseline))

    assert "BASELINE_EVENT_PROVENANCE_MISSING" in _blocker_codes(report)
    assert report["requires_recompute"]
    assert report["earliest_affected_date_precision"].startswith("UNKNOWN_")


def test_calendar_candidate_next_session_is_recomputed_and_order_independent():
    """验证日历候选下一条交易日重新计算与订单独立。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    baseline["checkpoint"]["as_of_date"] = "2025-01-10"
    baseline["checkpoint"]["next_expected_session"] = "2025-01-13"
    baseline["inputs"]["calendar"] = [
        {"date": "2025-01-10", "is_open": True},
        {"date": "2025-01-13", "is_open": True},
        {"date": "2025-01-14", "is_open": True},
    ]
    added_open = deepcopy(baseline)
    added_open["inputs"]["calendar"].append({"date": "2025-01-11", "is_open": True})
    report = _audit(baseline, added_open)
    assert "NEXT_SESSION_CHANGED" in _blocker_codes(report)
    assert report["calendar_comparison"] == {
        "original_next_open": "2025-01-13", "candidate_next_open": "2025-01-11",
    }

    added_closed = deepcopy(baseline)
    added_closed["inputs"]["calendar"].append({"date": "2025-01-11", "is_open": False})
    assert _audit(baseline, added_closed)["candidate_rules_compatible"]

    later = deepcopy(baseline)
    later["inputs"]["calendar"].append({"date": "2025-01-15", "is_open": True})
    assert _audit(baseline, later)["candidate_rules_compatible"]

    out_of_order = deepcopy(later)
    out_of_order["inputs"]["calendar"].reverse()
    assert _audit(baseline, out_of_order)["calendar_comparison"] == _audit(baseline, later)["calendar_comparison"]

    missing = deepcopy(baseline)
    missing["inputs"]["calendar"] = [row for row in missing["inputs"]["calendar"] if row["date"] != "2025-01-13"]
    assert "NEXT_SESSION_CHANGED" in _blocker_codes(_audit(baseline, missing))

    changed = deepcopy(baseline)
    next_row = next(row for row in changed["inputs"]["calendar"] if row["date"] == "2025-01-13")
    next_row["is_open"] = False
    assert "NEXT_SESSION_CHANGED" in _blocker_codes(_audit(baseline, changed))

    duplicate = deepcopy(later)
    duplicate["inputs"]["calendar"].append({"date": "2025-01-15", "is_open": True})
    with pytest.raises(ValueError, match="重复日期"):
        _audit(baseline, duplicate)


def test_future_calendar_append_and_historical_status_change_are_distinguished():
    """验证未来日历追加与历史状态变更区分。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    appended = deepcopy(baseline)
    appended["inputs"]["calendar"].append({"date": "2025-01-15", "is_open": True})
    report = _audit(baseline, appended)
    assert report["candidate_rules_compatible"]
    assert report["calendar_comparison"]["candidate_next_open"] == "2025-01-14"

    changed = deepcopy(baseline)
    changed["inputs"]["calendar"][1]["is_open"] = False
    assert "CALENDAR_HISTORY_CHANGED" in _blocker_codes(_audit(baseline, changed))


@pytest.mark.parametrize("family", ["rules", "fees"])
def test_schedule_effective_date_change_cannot_hide_behind_old_hash(family: str):
    """验证调度生效日期变更不能隐藏隐藏在旧哈希。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`family`：类型场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"][family][0]["valid_from"] = "2025-01-02"

    report = _audit(baseline, candidate)

    assert "HISTORICAL_SCHEDULE_ROW_CHANGED" in _blocker_codes(report)
    assert "SCHEDULE_CONTENT_HASH_INVALID" in _blocker_codes(report)
    assert not report["candidate_rules_compatible"]


@pytest.mark.parametrize(
    ("family", "field", "value"),
    [
        ("rules", "known_at_utc", "2025-01-02T00:00:00Z"),
        ("rules", "source_sha256", "9" * 64),
        ("fees", "known_at_utc", "2025-01-02T00:00:00Z"),
        ("fees", "source_sha256", "8" * 64),
    ],
)
def test_schedule_source_and_availability_changes_cannot_hide_behind_old_hash(family, field, value):
    """验证调度来源与可用性变化不能隐藏隐藏在旧哈希。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`family`：类型场景输入；`field`：字段场景输入；`value`：值场景输入。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"][family][0][field] = value

    report = _audit(baseline, candidate)

    assert "HISTORICAL_SCHEDULE_ROW_CHANGED" in _blocker_codes(report)
    assert "SCHEDULE_CONTENT_HASH_INVALID" in _blocker_codes(report)
    assert not report["candidate_rules_compatible"]


def test_future_rule_append_requires_known_at_and_source_evidence():
    """验证未来规则追加要求已知于与来源证据。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["rules"].append(_seal({
        "row_id": "rule-future",
        "valid_from": "2025-01-15",
        "valid_to": "2025-12-31",
        "known_at_utc": "2025-01-13T08:01:00Z",
        "source_sha256": "f" * 64,
        "symbol": "SH600001",
        "board": "MAIN",
    }, {
        "row_id": "rule-future",
        "valid_from": "2025-01-15",
        "valid_to": "2025-12-31",
        "known_at_utc": "2025-01-13T08:01:00Z",
        "source_sha256": "f" * 64,
        "symbol": "SH600001",
        "board": "MAIN",
        "buy_step_shares": 100,
    }))
    assert _audit(baseline, candidate)["candidate_rules_compatible"]

    missing_evidence = deepcopy(candidate)
    del missing_evidence["inputs"]["rules"][1]["known_at_utc"]
    assert "SCHEDULE_APPEND_EVIDENCE_MISSING" in _blocker_codes(
        _audit(baseline, missing_evidence)
    )


def test_historical_fee_edit_is_rejected():
    """验证历史费用修改拒绝。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    candidate = deepcopy(baseline)
    candidate["inputs"]["fees"][0]["content_sha256"] = "f" * 64

    report = _audit(baseline, candidate)

    assert "HISTORICAL_SCHEDULE_ROW_CHANGED" in _blocker_codes(report)
    assert report["earliest_possible_affected_date"] == "2025-01-01"


def test_repeated_increment_id_is_reported_and_invalid_proofs_are_rejected():
    """验证重复增量编号报告与无效证明拒绝。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    baseline = _proof()
    baseline["checkpoint"]["applied_increment_ids"] = [baseline["increment_id"]]
    report = _audit(baseline, deepcopy(baseline))
    assert "INCREMENT_ALREADY_APPLIED" in _blocker_codes(report)

    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)
    first = _audit(baseline, candidate)
    baseline["checkpoint"]["applied_increment_sha256s"] = [first["increment_sha256"]]
    candidate["checkpoint"]["applied_increment_sha256s"] = list(
        baseline["checkpoint"]["applied_increment_sha256s"]
    )
    candidate["increment_id"] = "different-id-for-same-content"
    report = _audit(baseline, candidate)
    assert "INCREMENT_ALREADY_APPLIED" in _blocker_codes(report)

    bad_cutoff = _proof()
    bad_cutoff["checkpoint"]["decision_cutoff_utc"] = "2025-01-13"
    with pytest.raises(ValueError, match="包含时区"):
        _audit(bad_cutoff, deepcopy(bad_cutoff))


def test_incremental_audit_cli_binds_the_proof_to_a_verified_checkpoint(tmp_path, monkeypatch):
    """验证增量审计CLI绑定证明到已检查点。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "audit_checkpoint_incremental_inputs.py"
    spec = importlib.util.spec_from_file_location("incremental_audit_cli_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checkpoint_file = tmp_path / "checkpoint.json"
    checkpoint_file.write_text("synthetic checkpoint file", encoding="utf-8")
    baseline = _proof()
    candidate = deepcopy(baseline)
    _add_event(candidate)
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    report_path = tmp_path / "report.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    verified = SimpleNamespace(
        state_sha256="1" * 64,
        as_of_date=date(2025, 1, 13),
        next_expected_session=date(2025, 1, 14),
        config_sha256="3" * 64,
        market_rules_sha256=None,
        fee_schedule_sha256=None,
        accounting_events_sha256="b" * 64,
        identity_events_sha256="c" * 64,
        corporate_actions_sha256="d" * 64,
        calendar_sha256="e" * 64,
        calendar_prefix_sha256="f" * 64,
        last_window_data_sha256="1" * 64,
        last_window_bars_sha256="2" * 64,
        data_chain_sha256="4" * 64,
    )
    monkeypatch.setattr(module, "read_backtest_checkpoint", lambda path: verified)
    monkeypatch.setattr(sys, "argv", [
        "audit_checkpoint_incremental_inputs.py", "--checkpoint", str(checkpoint_file),
        "--baseline-proof", str(baseline_path), "--candidate-proof", str(candidate_path),
        "--output", str(report_path),
    ])

    assert module.main() == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["candidate_rules_compatible"]
    assert not report["candidate_append_compatible"]
    assert report["checkpoint_binding_valid"] == "VERIFIED"
    assert report["input_files_binding_valid"] == "UNVERIFIED"
    assert report["normalized_inputs_binding_valid"] == "UNVERIFIED"
    assert report["canonical_payload_digest_valid"] == "VERIFIED"
    assert report["can_continue"] is False
    assert report["input_files"]["checkpoint_sha256"]

    baseline["checkpoint"]["as_of_date"] = "2025-01-12"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate["checkpoint"]["as_of_date"] = "2025-01-12"
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert module.main() == 0
    failed_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert failed_report["checkpoint_binding_valid"] == "FAILED"
    assert not failed_report["candidate_append_compatible"]

    baseline["checkpoint"]["as_of_date"] = "2025-01-13"
    candidate["checkpoint"]["as_of_date"] = "2025-01-13"
    baseline["checkpoint"]["input_hashes"]["config_sha256"] = "0" * 64
    candidate["checkpoint"]["input_hashes"]["config_sha256"] = "0" * 64
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    assert module.main() == 0
    hash_mismatch_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert hash_mismatch_report["checkpoint_binding_valid"] == "FAILED"
    assert any("input_hashes.config_sha256" in item for item in hash_mismatch_report["checkpoint_binding_differences"])


def test_actual_source_artifact_hashes_are_recomputed_but_semantic_link_stays_unverified(tmp_path):
    """验证实际来源产物哈希重新计算但语义关联保持未。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "audit_checkpoint_incremental_inputs.py"
    spec = importlib.util.spec_from_file_location("incremental_artifact_audit_test", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    proof = _proof()

    def write_artifact(name: str, payload: bytes) -> dict[str, str]:
        """实现测试替身test_actual_source_artifact_hashes_are_recomputed_but_semantic_link_stays_unverified的写入产物行为；用于控制被测代码与断言，不访问真实服务。 参数：`name`：名称场景输入；`payload`：载荷场景输入。 返回：dict[str, str]，供调用处构造夹具或断言复用。"""
        path = tmp_path / name
        path.write_bytes(payload)
        return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

    artifacts = {
        role: write_artifact(f"{role}.csv", role.encode("ascii"))
        for role in ("strategy_config", "historical_bars_prefix", "events", "calendar", "rules", "fees")
    }
    proof["inputs"]["artifacts"] = artifacts
    cutoff = write_artifact("cutoff-evidence.txt", b"synthetic explicit cutoff evidence")
    proof["checkpoint"]["cutoff_source_artifact"] = cutoff

    source_paths = []
    for family in ("events", "rules", "fees"):
        for row in proof["inputs"][family]:
            source = write_artifact(f"{family}-{row.get('event_id', row.get('row_id'))}.pdf", family.encode("ascii"))
            row["source_path"] = source["path"]
            row["source_sha256"] = source["sha256"]
            if family == "events":
                row["canonical_content"]["source_sha256"] = source["sha256"]
                _seal(row, row["canonical_content"])
            else:
                row["canonical_content"]["source_sha256"] = source["sha256"]
                _seal(row, row["canonical_content"])
            source_paths.append(source["path"])

    status, audit = module._audit_declared_artifacts([("baseline", proof), ("candidate", proof)])
    assert status == "VERIFIED"
    assert all(item["status"] == "VERIFIED" for item in audit)

    Path(source_paths[0]).write_bytes(b"tampered source")
    status, audit = module._audit_declared_artifacts([("baseline", proof), ("candidate", proof)])
    assert status == "FAILED"
    assert any(item["status"] == "FAILED" for item in audit)
