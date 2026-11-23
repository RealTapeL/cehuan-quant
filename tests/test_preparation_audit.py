"""策环测试模块：验证审计输入目录、来源登记、哈希核验及原子产物写入。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/audit_preparation_inputs.py"
_SPEC = importlib.util.spec_from_file_location("preparation_audit_script", _SCRIPT_PATH)
_AUDIT = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(_AUDIT)
atomic_write_text = _AUDIT.atomic_write_text
create_run_directory = _AUDIT.create_run_directory
verify_source_registers = _AUDIT.verify_source_registers


def _write_json(path, value):
    """私有测试辅助函数，用于写入JSON。 参数：`path`：路径场景输入；`value`：值场景输入。 返回值供调用处构造夹具或断言复用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_csv(path, rows):
    """私有测试辅助函数，用于写入CSV。 参数：`path`：路径场景输入；`rows`：行场景输入。 返回值供调用处构造夹具或断言复用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["event_key"]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _audit_fixture(
    root, *, swap_event_hashes=False, duplicate_public_key=False, mismatch_unknown_source=False
):
    """私有测试辅助函数，用于审计夹具。 参数：`root`：代码标识“root”（含义待核实）场景输入；`swap_event_hashes`：换股事件哈希场景输入；`duplicate_public_key`：重复代码标识“public”（含义待核实）密钥场景输入；`mismatch_unknown_source`：不符未知来源场景输入。 返回值供调用处构造夹具或断言复用。"""
    evidence = root / "data/private/evidence/preparation-20261029"
    evidence.mkdir(parents=True)
    a_path = evidence / "source-a.pdf"
    b_path = evidence / "source-b.pdf"
    a_path.write_bytes(b"source-a")
    b_path.write_bytes(b"source-b")
    a_sha = hashlib.sha256(a_path.read_bytes()).hexdigest()
    b_sha = hashlib.sha256(b_path.read_bytes()).hexdigest()
    sources = [
        {
            "event_key": "event-a",
            "symbol": "SH600001",
            "event_type": "TEST_EVENT",
            "url": "https://example.invalid/a.pdf",
            "archive_file": "data/private/evidence/preparation-20261029/source-a.pdf",
            "sha256": a_sha,
        },
        {
            "event_key": "event-b",
            "symbol": "SH600002",
            "event_type": "TEST_EVENT",
            "url": "https://example.invalid/b.pdf",
            "archive_file": "data/private/evidence/preparation-20261029/source-b.pdf",
            "sha256": b_sha,
        },
    ]
    _write_json(evidence / "event-source-archive-index.json", {"sources": sources})
    public_events = [
        {
            "event_key": source["event_key"],
            "symbol": source["symbol"],
            "event_type": source["event_type"],
            "official_source_url": source["url"],
            "archived_file_sha256": source["sha256"],
            "event_date_in_key": "",
            "expected_dates": "",
            "observed_dates": "",
        }
        for source in sources
    ]
    if swap_event_hashes:
        public_events[0]["archived_file_sha256"], public_events[1]["archived_file_sha256"] = (
            public_events[1]["archived_file_sha256"], public_events[0]["archived_file_sha256"]
        )
    if duplicate_public_key:
        public_events[1]["event_key"] = public_events[0]["event_key"]
    _write_csv(root / "docs/evidence/event-source-index.csv", public_events)

    unknown_path = evidence / "unknown-types-remaining/originals/fund.pdf"
    unknown_path.parent.mkdir(parents=True)
    unknown_path.write_bytes(b"unknown-source")
    unknown_sha = hashlib.sha256(unknown_path.read_bytes()).hexdigest()
    unknown_symbol = {
        "symbol": "SH510110",
        "instrument_name": "Synthetic ETF",
        "member_days": "10",
        "first_seen_date": "2023-01-03",
        "last_seen_date": "2023-01-16",
        "classification": "EXPLAINED_NON_STOCK_FUND",
        "source_pdf_page": "1",
        "source_title_excerpt": "Synthetic source row",
        "basis": "synthetic fixture",
    }
    _write_json(evidence / "unknown-types-remaining/source-index.json", {
        "sources": [{
            "source_id": unknown_sha[:16],
            "url": "https://example.invalid/fund.pdf",
            "sha256": unknown_sha,
            "archive_file": "data/private/evidence/preparation-20261029/unknown-types-remaining/originals/fund.pdf",
            "symbols": [unknown_symbol],
        }],
    })
    public_unknown = {
        **unknown_symbol,
        "official_source_url": "https://example.invalid/fund.pdf",
        "archived_source_sha256": unknown_sha,
    }
    if mismatch_unknown_source:
        public_unknown["archived_source_sha256"] = a_sha
    _write_csv(root / "docs/evidence/unknown-type-remaining-43-audit.csv", [public_unknown])
    _write_csv(root / "docs/evidence/market-fee-evidence.csv", [])


def test_source_register_matches_each_event_key_to_its_own_source(tmp_path):
    """验证来源登记匹配每项事件密钥到其自身来源。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    _audit_fixture(tmp_path, swap_event_hashes=True)

    result = verify_source_registers(tmp_path)

    assert not result["ok"]
    assert result["event_key_source_mismatches"] == [
        "event-a: sha256",
        "event-b: sha256",
    ]


def test_source_register_rejects_duplicate_event_keys(tmp_path):
    """验证来源登记拒绝重复事件键。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    _audit_fixture(tmp_path, duplicate_public_key=True)

    result = verify_source_registers(tmp_path)

    assert not result["ok"]
    assert result["duplicate_public_event_keys"] == ["event-a"]


def test_source_register_matches_each_unknown_symbol_to_its_source_row(tmp_path):
    """验证来源登记匹配每项未知证券代码到其来源行。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    _audit_fixture(tmp_path, mismatch_unknown_source=True)

    result = verify_source_registers(tmp_path)

    assert not result["ok"]
    assert result["unknown_symbol_source_mismatches"] == [
        "SH510110: archived_source_sha256",
    ]


def test_new_run_directory_refuses_to_overwrite_existing_report(tmp_path):
    """验证新运行目录拒绝到覆盖已有报告。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    destination = tmp_path / "run-20261102"
    create_run_directory(destination)
    (destination / "run-manifest.json").write_text("original", encoding="utf-8")

    with pytest.raises(FileExistsError):
        create_run_directory(destination)

    assert (destination / "run-manifest.json").read_text(encoding="utf-8") == "original"


def test_run_directory_rejects_path_like_ids(tmp_path):
    """验证运行目录拒绝路径例如编号。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    with pytest.raises(ValueError, match="run-id"):
        create_run_directory(tmp_path / "../escape")


def test_atomic_write_publishes_complete_file(tmp_path):
    """验证原子写入发布完整文件。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    target = tmp_path / "report.json"

    atomic_write_text(target, '{"complete": true}\n')

    assert target.read_text(encoding="utf-8") == '{"complete": true}\n'
    assert sorted(path.name for path in tmp_path.iterdir()) == ["report.json"]
