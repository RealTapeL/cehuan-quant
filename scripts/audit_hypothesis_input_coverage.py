#!/usr/bin/env python3
"""Read-only H1/H2 coverage preflight for a private historical snapshot."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

import pandas as pd

from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.canonical import read_bars
from cehuan.research.hypothesis_input_coverage import compute_hypothesis_input_coverage


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BARS = ROOT / "data/private/snapshots/bs-bars-universe-20241008-20250110-v1/bars.csv"
DEFAULT_BARS_MANIFEST = DEFAULT_BARS.parent / "manifest.json"
DEFAULT_UNIVERSE_DIR = ROOT / "data/private/universes/bs-universe-20241001-20250110-v1"
DEFAULT_CANDIDATES = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/audit-timing-b60a7f0-20261104-final2/candidate-pool-by-signal.csv"
DEFAULT_AUDIT_DIR = DEFAULT_CANDIDATES.parent
DEFAULT_CONFIG = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/run-committed-b60a7f0/config.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须为对象: {path}")
    return value


def _git_context() -> dict[str, Any]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return {"head": head, "working_tree_dirty": bool(status.strip())}


def _input_record(path: Path, role: str, expected_sha256: str | None = None) -> dict[str, Any]:
    observed = sha256_file(path) if path.is_file() else None
    status = "MISSING" if observed is None else "AVAILABLE_BUT_UNVERIFIED"
    if expected_sha256:
        status = "VERIFIED" if observed == expected_sha256 else "FAILED"
    return {
        "path": str(path.resolve()), "role": role, "sha256": observed,
        "expected_sha256": expected_sha256, "status": status,
    }


def _markdown(report: dict[str, Any], input_records: list[dict[str, Any]]) -> str:
    lines = [
        "# H1/H2 真实快照输入覆盖预检", "",
        f"- 类型：`{report['report_type']}`", f"- 生成时间：`{report['created_at_utc']}`",
        f"- Git HEAD：`{report['git']['head']}`（工作区 dirty：`{report['git']['working_tree_dirty']}`）",
        f"- 数据范围：{report['scope']['data_scope']}",
        "- 不计算策略收益、不产生交易计划；正式基线 `NOT_READY`，`can_continue=false`。", "",
        "## 输入文件", "", "| 输入 | 状态 | SHA-256 |", "|---|---|---|",
    ]
    for item in input_records:
        lines.append(f"| `{item['role']}` | `{item['status']}` | `{item.get('sha256') or 'MISSING'}` |")
    lines.extend(["", "## H1 窗口覆盖", "", "| 信号日 | 范围 | 股票/候选数 | 日历/行情/价格有效且非停牌 | 排除或未核实 | 未知类型成员行 |", "|---|---|---:|---:|---:|---:|"])
    for item in report["h1"]["summary_by_signal_and_scope"]:
        lines.append(
            f"| {item['signal_date']} | {item['scope']} | {item['universe_or_candidate_symbols']} | "
            f"{item['fully_covered_valid_price_non_suspended_windows']} | {item['excluded_or_unverified_windows']} | "
            f"{item['unknown_type_member_rows_excluded_from_stock_pool'] if item['unknown_type_member_rows_excluded_from_stock_pool'] is not None else '—'} |"
        )
    lines.extend(["", "## H2 输入覆盖", "", "| 信号日 | 候选行 | 决策日 09:31 有报价 | 决策报价按记录时间可得 | 执行日有报价 | 执行报价按记录时间可得 | 规则完整行 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for item in report["h2"]["summary_by_signal"]:
        lines.append(
            f"| {item['signal_date']} | {item['candidate_rows']} | {item['decision_open_rows']} | "
            f"{item['decision_rows_recorded_available_by_0931']} | {item['execution_open_rows']} | "
            f"{item['execution_rows_recorded_available_by_0931']} | "
            f"{item['decision_market_rule_complete_rows']}/{item['candidate_rows']} |"
        )
    lines.extend([
        "", "## 解释与限制", "",
        "- `available_at` 是适配器按交易日 15:00（Asia/Shanghai）写入的时间假设，不是历史供应商发布时间证据。",
        "- `is_listed`、`is_suspended`、`is_st` 虽在行情行中有值，但没有独立状态来源与状态可得时间绑定；按点时使用仍为 `UNVERIFIED`。",
        "- H2 候选文件来自运行后的重建审计产物；本次记录其当前文件散列，但缺少原运行时散列锚点。",
        "- 市场规则与历史费用缺失时，配置回退只可标为 `AVAILABLE_BUT_UNVERIFIED`。本报告不评价收益或策略优劣。",
        "- 68 个开市日已用于开发诊断，不是独立留出；数据范围也不代表全 A 股历史完整性。", "",
        "## 发布状态", "", "- Formal baseline: `NOT_READY`", "- Incremental continuation: `can_continue=false`",
        "- Broker connection: `false`", "- Orders generated: `false`", "",
    ])
    return "\n".join(lines)


def build_report(
    *, bars_path: Path, bars_manifest_path: Path, universe_dir: Path,
    candidates_path: Path, audit_dir: Path, config_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    bars_path = bars_path.resolve()
    bars_manifest_path = bars_manifest_path.resolve()
    universe_dir = universe_dir.resolve()
    candidates_path = candidates_path.resolve()
    audit_dir = audit_dir.resolve()
    config_path = config_path.resolve()
    universe_path = universe_dir / "securities_by_date.csv"
    calendar_path = universe_dir / "trading_calendar.csv"
    universe_manifest_path = universe_dir / "manifest.json"
    candidate_inventory_path = audit_dir / "artifact-inventory.csv"
    input_paths = [
        bars_path, bars_manifest_path, universe_path, calendar_path, universe_manifest_path,
        candidates_path, candidate_inventory_path, config_path,
    ]
    missing = [str(path) for path in input_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少预检输入: " + ", ".join(missing))
    before = {str(path): sha256_file(path) for path in input_paths}
    bars_manifest = _json(bars_manifest_path)
    universe_manifest = _json(universe_manifest_path)
    expected_bars = bars_manifest.get("file_sha256", {}).get("bars.csv")
    expected_universe = universe_manifest.get("files_sha256", {}).get("securities_by_date.csv")
    expected_calendar = universe_manifest.get("files_sha256", {}).get("trading_calendar.csv")
    inventory = pd.read_csv(candidate_inventory_path, dtype=str)
    config_inventory_rows = inventory.loc[
        inventory.relative_path.astype(str).str.endswith("run-committed-b60a7f0/config.json")
    ] if "relative_path" in inventory.columns else pd.DataFrame()
    expected_config = config_inventory_rows.iloc[0].get("sha256") if len(config_inventory_rows) == 1 else None
    input_records = [
        _input_record(bars_path, "canonical_bars", expected_bars),
        _input_record(universe_path, "daily_universe_members", expected_universe),
        _input_record(calendar_path, "trading_calendar", expected_calendar),
        _input_record(candidates_path, "reconstructed_candidate_pool"),
        _input_record(candidate_inventory_path, "candidate_artifact_inventory"),
        _input_record(config_path, "committed_run_config", expected_config),
        _input_record(bars_manifest_path, "bars_snapshot_manifest"),
        _input_record(universe_manifest_path, "universe_snapshot_manifest"),
    ]
    candidate_inventory_rows = inventory.loc[
        inventory.get("relative_path", pd.Series(dtype=str)).astype(str).str.endswith("candidate-pool-by-signal.csv")
    ] if "relative_path" in inventory.columns else pd.DataFrame()
    candidate_original_anchor = "UNVERIFIED_RECONSTRUCTED_AFTER_RUN"
    if not candidate_inventory_rows.empty:
        candidate_original_anchor = "VERIFIED" if candidate_inventory_rows.iloc[0].get("sha256") == before[str(candidates_path)] else "FAILED"
        next(row for row in input_records if row["role"] == "reconstructed_candidate_pool")["expected_sha256"] = candidate_inventory_rows.iloc[0].get("sha256")
        next(row for row in input_records if row["role"] == "reconstructed_candidate_pool")["status"] = candidate_original_anchor

    config = _json(config_path)
    bars = read_bars(bars_path, compact=True)
    members = pd.read_csv(universe_path, dtype=str)
    calendar = normalize_trading_calendar(pd.read_csv(calendar_path, dtype=str))
    candidates = pd.read_csv(candidates_path, dtype=str)
    report, h1_detail, h2_detail = compute_hypothesis_input_coverage(
        bars, members, calendar, candidates, config=config,
    )
    report["git"] = _git_context()
    report["source_file_hashes"] = input_records
    report["candidate_original_run_hash_anchor"] = candidate_original_anchor
    report["snapshot_manifest_scope"] = {
        "bars_snapshot_id": bars_manifest.get("snapshot_id"),
        "universe_snapshot_id": universe_manifest.get("snapshot_id"),
        "bars_source": bars_manifest.get("provider"),
        "universe_source": universe_manifest.get("provider"),
        "universe_data_scope_warning": universe_manifest.get("data_scope_warning"),
    }
    report["fees"] = {
        "historical_fee_schedule_rows": 0,
        "status": "MISSING",
        "config_costs": config.get("costs", {}),
        "config_status": "AVAILABLE_BUT_UNVERIFIED_ASSUMPTION_NOT_PERSONAL_BROKER_COSTS",
    }
    report["corporate_actions"] = {
        "status": "NOT_AUDITED_AS_PART_OF_THIS_COVERAGE_PREFLIGHT",
        "reason": "This report measures H1/H2 input availability only; no completeness claim is made for the event registry.",
    }
    report["experiment_registration"] = {
        "development_window_already_observed": True,
        "independent_holdout": False,
        "h1_candidate_rule": "Exactly 21 open sessions ending on signal day, one unique bar with positive close/adjusted_close per session, known and non-suspended status.",
        "h2_candidate_rule": "Decision on next open session; replacement symbol and shares frozen; scheduled synthetic execution on following open session.",
        "h2_comparison_warning": "Whole policy jointly changes replacement selection and execution delay; matched-horizon control retains cash.",
        "metrics": ["H1 eligible/excluded counts and causes", "H2 quote/status/rule/timing coverage"],
        "stopping_conditions": ["unknown point-in-time status", "missing market rule or historical fee inputs", "unknown historical availability timestamps"],
        "strategy_returns_computed": False,
        "human_review_required": True,
        "formal_baseline": "NOT_READY",
        "can_continue": False,
    }

    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir.parent / f".{output_dir.name}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"已有同名预检任务正在写入: {lock_path}") from exc
    os.close(lock_fd)
    staging: Path | None = None
    try:
        if output_dir.exists():
            raise FileExistsError(f"拒绝覆盖已有预检目录: {output_dir}")
        staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        h1_detail.to_csv(staging / "h1-window-coverage.csv", index=False)
        h2_detail.to_csv(staging / "h2-candidate-input-coverage.csv", index=False)
        # Read every input hash again before publication to detect concurrent replacement.
        after = {str(path): sha256_file(path) for path in input_paths}
        if before != after:
            raise RuntimeError("预检期间源文件发生变化，拒绝发布报告")
        report["input_hashes_stable_during_run"] = True
        report["output_sha256"] = {
            name: sha256_file(staging / name)
            for name in ("h1-window-coverage.csv", "h2-candidate-input-coverage.csv")
        }
        report["summary_markdown"] = "summary.md"
        (staging / "coverage-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (staging / "summary.md").write_text(_markdown(report, input_records), encoding="utf-8")
        if output_dir.exists():
            raise FileExistsError(f"发布前目标目录已出现，拒绝覆盖: {output_dir}")
        os.rename(staging, output_dir)
    except Exception:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        lock_path.unlink(missing_ok=True)
    return {"output_dir": str(output_dir), "report": report}


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only H1/H2 input coverage preflight; computes no returns or orders.")
    parser.add_argument("--bars", type=Path, default=DEFAULT_BARS)
    parser.add_argument("--bars-manifest", type=Path, default=DEFAULT_BARS_MANIFEST)
    parser.add_argument("--universe-dir", type=Path, default=DEFAULT_UNIVERSE_DIR)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--candidate-audit-dir", type=Path, default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_report(
        bars_path=args.bars, bars_manifest_path=args.bars_manifest,
        universe_dir=args.universe_dir, candidates_path=args.candidates,
        audit_dir=args.candidate_audit_dir, config_path=args.config,
        output_dir=args.output_dir,
    )
    report = result["report"]
    print(json.dumps({
        "output_dir": result["output_dir"],
        "report_type": report["report_type"],
        "signal_dates": report["scope"]["signal_dates"],
        "h1_windows": len(report["h1"]["summary_by_signal_and_scope"]),
        "h2_candidate_rows": sum(row["candidate_rows"] for row in report["h2"]["summary_by_signal"]),
        "candidate_original_run_hash_anchor": report["candidate_original_run_hash_anchor"],
        "formal_baseline": "NOT_READY",
        "can_continue": False,
        "strategy_returns_computed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
