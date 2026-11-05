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
from cehuan.research.candidate_artifacts import (
    classify_candidate_inventory, inspect_original_run_candidate_anchor, reconstruct_candidate_pool,
)
from cehuan.research.hypothesis_input_coverage import compute_hypothesis_input_coverage
from cehuan.research.schedule_coverage import audit_schedule_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BARS = ROOT / "data/private/snapshots/bs-bars-universe-20241008-20250110-v1/bars.csv"
DEFAULT_BARS_MANIFEST = DEFAULT_BARS.parent / "manifest.json"
DEFAULT_UNIVERSE_DIR = ROOT / "data/private/universes/bs-universe-20241001-20250110-v1"
DEFAULT_CANDIDATES = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/audit-timing-b60a7f0-20261104-final2/candidate-pool-by-signal.csv"
DEFAULT_AUDIT_DIR = DEFAULT_CANDIDATES.parent
DEFAULT_CONFIG = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/run-committed-b60a7f0/config.json"
DEFAULT_FROZEN_BARS = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/inputs/bars.csv"
DEFAULT_ORIGINAL_RUN_MANIFEST = ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/manifest.json"


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
    lines.extend(["", "### 原候选池 H1 重排诊断", "", "| 信号日 | 原候选数 | H1 保留 | 排除/未核实 | 原前 N 保留 | 新进入前 N | 排名状态 |", "|---|---:|---:|---:|---:|---:|---|"])
    for item in report["h1"]["candidate_rank_diagnostics_by_signal"]:
        lines.append(
            f"| {item['signal_date']} | {item['candidate_rows']} | {item['h1_retained_candidate_rows']} | "
            f"{item['h1_excluded_or_unverified_candidate_rows']} | {item['original_top_n_retained']} | "
            f"{item['new_top_n_members_after_rerank'] if item['new_top_n_members_after_rerank'] is not None else '—'} | `{item['rank_comparison_status']}` |"
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
        f"- 候选证据：文件清单匹配 `{report['candidate_file_integrity']['status']}`；冻结输入事后重建 `{report['candidate_reconstruction']['status']}`；原运行时锚点 `{report['candidate_original_run_time_anchor']['status']}`。事后证据不会被升级为运行时证据。",
        f"- 市场规则表：`{report['market_rules']['status']}`；历史费率表：`{report['fees']['status']}`。`NOT_PROVIDED` 只表示本次命令未提供，不表示整台机器或项目中不存在。",
        "- 配置回退只作为未核实假设。H1 只在已重建原候选池内部过滤和重排，不计算收益、不评价策略优劣，也不引入原候选池之外的新证券。",
        "- 68 个开市日已用于开发诊断，不是独立留出；数据范围也不代表全 A 股历史完整性。", "",
        "## 发布状态", "", "- Formal baseline: `NOT_READY`", "- Incremental continuation: `can_continue=false`",
        "- Broker connection: `false`", "- Orders generated: `false`", "",
    ])
    return "\n".join(lines)


def _h1_candidate_changes_markdown(frame: pd.DataFrame, report: dict[str, Any]) -> str:
    detail = frame.loc[frame.scope.eq("ORIGINAL_CANDIDATE_POOL")].copy()
    impacted = detail.loc[
        ~detail.h1_status.eq("ELIGIBLE_FOR_H1_DIAGNOSTIC")
        | detail.top_n_change.isin({"NEW_TOP_N_MEMBER_AFTER_RERANK", "DROPPED_FROM_TOP_N_AFTER_FILTER"})
    ].copy()
    lines = [
        "# H1 原候选池变化说明", "",
        "**口径：** 仅对冻结输入事后重建的原候选池做窗口过滤与排名重算；不是历史点时可得性证明，不计算收益，不是独立样本外结果。", "",
        "| 信号日 | 证券 | 原排名 | H1 状态 | 排除原因 | H1 新排名 | Top-N 变化 |", "|---|---|---:|---|---|---:|---|",
    ]
    for row in impacted.sort_values(["signal_date", "rank", "symbol"], kind="stable").itertuples(index=False):
        h1_rank = "—" if pd.isna(row.h1_rank) else int(row.h1_rank)
        reasons = "—" if pd.isna(row.reasons) or not str(row.reasons).strip() else str(row.reasons)
        reasons = reasons.replace("|", "\\|")
        change = str(row.top_n_change).replace("|", "\\|")
        lines.append(
            f"| {row.signal_date} | `{row.symbol}` | {int(row.rank)} | `{row.h1_status}` | {reasons} | {h1_rank} | `{change}` |"
        )
    lines.extend([
        "", "## 每日汇总", "", "| 信号日 | 原候选 | H1 保留 | 排除/未知 | 原 Top-N 被排除 | 新进入 Top-N |", "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["h1"]["candidate_rank_diagnostics_by_signal"]:
        lines.append(
            f"| {row['signal_date']} | {row['candidate_rows']} | {row['h1_retained_candidate_rows']} | "
            f"{row['h1_excluded_or_unverified_candidate_rows']} | {row['original_top_n_excluded']} | "
            f"{row['new_top_n_members_after_rerank']} |"
        )
    lines.extend([
        "", f"完整逐候选结果见同目录 `h1-candidate-rank-diagnostic.csv`。原候选重建状态：`{report['candidate_reconstruction']['status']}`；",
        f"原运行时排名锚点：`{report['candidate_original_run_time_anchor']['status']}`；历史状态可得性：`UNVERIFIED`。", "",
    ])
    return "\n".join(lines)


def build_report(
    *, bars_path: Path, bars_manifest_path: Path, universe_dir: Path,
    candidates_path: Path, audit_dir: Path | None, config_path: Path,
    candidate_inventory_path: Path | None = None,
    frozen_bars_path: Path | None = None, original_run_manifest_path: Path | None = None,
    market_rules_path: Path | None = None, fee_schedule_path: Path | None = None,
    output_dir: Path,
) -> dict[str, Any]:
    bars_path = bars_path.resolve()
    bars_manifest_path = bars_manifest_path.resolve()
    universe_dir = universe_dir.resolve()
    candidates_path = candidates_path.resolve()
    audit_dir = audit_dir.resolve() if audit_dir is not None else None
    config_path = config_path.resolve()
    universe_path = universe_dir / "securities_by_date.csv"
    calendar_path = universe_dir / "trading_calendar.csv"
    universe_manifest_path = universe_dir / "manifest.json"
    if candidate_inventory_path is None and audit_dir is not None:
        candidate_inventory_path = audit_dir / "artifact-inventory.csv"
    candidate_inventory_path = candidate_inventory_path.resolve() if candidate_inventory_path is not None else None
    frozen_bars_path = frozen_bars_path.resolve() if frozen_bars_path is not None else None
    original_run_manifest_path = original_run_manifest_path.resolve() if original_run_manifest_path is not None else None
    market_rules_path = market_rules_path.resolve() if market_rules_path is not None else None
    fee_schedule_path = fee_schedule_path.resolve() if fee_schedule_path is not None else None
    input_paths = [
        bars_path, bars_manifest_path, universe_path, calendar_path, universe_manifest_path,
        candidates_path, config_path,
    ]
    optional_paths = [candidate_inventory_path, frozen_bars_path, original_run_manifest_path, market_rules_path, fee_schedule_path]
    input_paths.extend(path for path in optional_paths if path is not None)
    missing = [str(path) for path in (bars_path, bars_manifest_path, universe_path, calendar_path, universe_manifest_path, candidates_path, config_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少预检输入: " + ", ".join(missing))
    before = {str(path): (sha256_file(path) if path.is_file() else None) for path in input_paths}
    bars_manifest = _json(bars_manifest_path)
    universe_manifest = _json(universe_manifest_path)
    expected_bars = bars_manifest.get("file_sha256", {}).get("bars.csv")
    expected_universe = universe_manifest.get("files_sha256", {}).get("securities_by_date.csv")
    expected_calendar = universe_manifest.get("files_sha256", {}).get("trading_calendar.csv")
    input_records = [
        _input_record(bars_path, "canonical_bars", expected_bars),
        _input_record(universe_path, "daily_universe_members", expected_universe),
        _input_record(calendar_path, "trading_calendar", expected_calendar),
        _input_record(candidates_path, "reconstructed_candidate_pool"),
        _input_record(config_path, "committed_run_config"),
        _input_record(bars_manifest_path, "bars_snapshot_manifest"),
        _input_record(universe_manifest_path, "universe_snapshot_manifest"),
    ]
    if candidate_inventory_path is not None:
        input_records.append(_input_record(candidate_inventory_path, "candidate_artifact_inventory"))
    if original_run_manifest_path is not None:
        input_records.append(_input_record(original_run_manifest_path, "experiment_manifest_claim_only"))
        if original_run_manifest_path.is_file():
            original_manifest = _json(original_run_manifest_path)
            frozen_bar_expected = original_manifest.get("input_sources", {}).get("bars.csv", {}).get("sha256")
            if frozen_bars_path is not None:
                input_records.append(_input_record(frozen_bars_path, "original_run_frozen_bars", frozen_bar_expected))
    elif frozen_bars_path is not None:
        input_records.append(_input_record(frozen_bars_path, "candidate_reconstruction_bars"))
    if frozen_bars_path is not None and not any(row["role"] in {"original_run_frozen_bars", "candidate_reconstruction_bars"} for row in input_records):
        input_records.append(_input_record(frozen_bars_path, "candidate_reconstruction_bars"))
    candidate_integrity = classify_candidate_inventory(candidates_path, candidate_inventory_path)
    if candidate_inventory_path is not None and not candidate_inventory_path.is_file():
        candidate_integrity["status"] = "MISSING"

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
    report["candidate_file_integrity"] = candidate_integrity
    reconstruction_status: dict[str, Any] = {"status": "NOT_PROVIDED", "reason": "Frozen candidate-generation bars were not provided."}
    if frozen_bars_path is not None:
        reconstruction_status, _ = reconstruct_candidate_pool(frozen_bars_path, config_path, candidates)
    report["candidate_reconstruction"] = reconstruction_status
    report["candidate_original_run_time_anchor"] = inspect_original_run_candidate_anchor(original_run_manifest_path, candidates_path)
    report["snapshot_manifest_scope"] = {
        "bars_snapshot_id": bars_manifest.get("snapshot_id"),
        "universe_snapshot_id": universe_manifest.get("snapshot_id"),
        "bars_source": bars_manifest.get("provider"),
        "universe_source": universe_manifest.get("provider"),
        "universe_data_scope_warning": universe_manifest.get("data_scope_warning"),
    }
    rule_pairs = {
        (str(row.symbol), str(day))
        for row in h2_detail.itertuples(index=False)
        for day in (row.decision_session_date, row.execution_session_date)
    }
    fee_dates = {
        str(day) for row in h2_detail.itertuples(index=False)
        for day in (row.decision_session_date, row.execution_session_date)
    }
    rules_status, _, rules_records = audit_schedule_file(
        path=market_rules_path, label="market_rules", required_pairs=rule_pairs, required_dates=fee_dates,
    )
    fees_status, _, fees_records = audit_schedule_file(
        path=fee_schedule_path, label="fee_schedule", required_pairs=rule_pairs, required_dates=fee_dates,
    )
    input_records.extend(rules_records + fees_records)
    report["market_rules"] = rules_status
    report["fees"] = {
        **fees_status,
        "config_costs": config.get("costs", {}),
        "config_status": "AVAILABLE_BUT_UNVERIFIED_ASSUMPTION_NOT_PERSONAL_BROKER_COSTS",
    }
    report["rules_fees_and_status"]["historical_fee_schedule"] = fees_status["status"]
    report["rules_fees_and_status"]["market_rule_schedule"] = rules_status["status"]
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

    report["research_task_registration"] = {
        "registration_id": "H1-H2-INPUT-COVERAGE-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "hypothesis_ids": ["H1_SUSPENSION_AND_WINDOW_COVERAGE", "H2_DELAYED_REPLACEMENT_AFTER_OPEN_OBSERVATION"],
        "input_scope": report["scope"],
        "candidate_evidence": {
            "inventory_status": candidate_integrity["status"],
            "frozen_input_reconstruction_status": reconstruction_status.get("status"),
            "original_run_time_anchor": report["candidate_original_run_time_anchor"]["status"],
        },
        "input_files": [
            {"role": row["role"], "path": row["path"], "sha256": row.get("sha256"), "status": row["status"]}
            for row in input_records
        ],
        "timing_evidence": {
            "historical_vendor_publish_time": "UNVERIFIED",
            "adapter_available_at_semantics": report["rules_fees_and_status"]["adapter_available_at_semantics"],
        },
        "coverage_status": {
            "h1": "DEVELOPMENT_DIAGNOSTIC_ONLY_HISTORICAL_AVAILABILITY_UNVERIFIED",
            "h2": "NEEDS_DATA",
            "market_rules": rules_status["status"],
            "fees": fees_status["status"],
        },
        "stop_conditions": report["experiment_registration"]["stopping_conditions"],
        "human_review_status": "PENDING",
        "active_strategy_modified": False,
        "broker_connected": False,
        "orders_generated": False,
        "formal_baseline": "NOT_READY",
        "can_continue": False,
    }
    registration_output = report["research_task_registration"]

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
        h1_detail.loc[h1_detail.scope.eq("ORIGINAL_CANDIDATE_POOL")].to_csv(
            staging / "h1-candidate-rank-diagnostic.csv", index=False,
        )
        (staging / "h1-candidate-changes.md").write_text(
            _h1_candidate_changes_markdown(h1_detail, report), encoding="utf-8",
        )
        h2_detail.to_csv(staging / "h2-candidate-input-coverage.csv", index=False)
        # Read every input hash again before publication to detect concurrent replacement.
        after = {str(path): (sha256_file(path) if path.is_file() else None) for path in input_paths}
        if before != after:
            raise RuntimeError("预检期间源文件发生变化，拒绝发布报告")
        report["input_hashes_stable_during_run"] = True
        (staging / "research-task-registration.json").write_text(json.dumps(registration_output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["output_sha256"] = {
            name: sha256_file(staging / name)
            for name in ("h1-window-coverage.csv", "h1-candidate-rank-diagnostic.csv", "h1-candidate-changes.md", "h2-candidate-input-coverage.csv", "research-task-registration.json")
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
    parser.add_argument("--candidate-inventory", type=Path, help="候选文件清单；匹配只证明本次提供的清单内容，不证明原运行时封存")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--frozen-bars", type=Path, default=DEFAULT_FROZEN_BARS, help="生成候选排名时绑定的历史行情，用于事后重建")
    parser.add_argument("--original-run-manifest", type=Path, default=DEFAULT_ORIGINAL_RUN_MANIFEST, help="仅检查原实验清单；无候选哈希绑定时仍为 UNVERIFIED")
    parser.add_argument("--market-rules", type=Path, help="可选：有效日期市场规则 CSV")
    parser.add_argument("--fee-schedule", type=Path, help="可选：有效日期历史费率 CSV")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_report(
        bars_path=args.bars, bars_manifest_path=args.bars_manifest,
        universe_dir=args.universe_dir, candidates_path=args.candidates,
        audit_dir=(args.candidate_audit_dir if args.candidate_inventory is None else None), config_path=args.config,
        candidate_inventory_path=args.candidate_inventory,
        frozen_bars_path=args.frozen_bars,
        original_run_manifest_path=args.original_run_manifest,
        market_rules_path=args.market_rules,
        fee_schedule_path=args.fee_schedule,
        output_dir=args.output_dir,
    )
    report = result["report"]
    print(json.dumps({
        "output_dir": result["output_dir"],
        "report_type": report["report_type"],
        "signal_dates": report["scope"]["signal_dates"],
        "h1_windows": len(report["h1"]["summary_by_signal_and_scope"]),
        "h2_candidate_rows": sum(row["candidate_rows"] for row in report["h2"]["summary_by_signal"]),
        "candidate_inventory_status": report["candidate_file_integrity"]["status"],
        "candidate_reconstruction_status": report["candidate_reconstruction"]["status"],
        "candidate_original_run_time_anchor": report["candidate_original_run_time_anchor"]["status"],
        "h1_candidate_rank_diagnostics": report["h1"]["candidate_rank_diagnostics_by_signal"],
        "market_rules_status": report["market_rules"]["status"],
        "fees_status": report["fees"]["status"],
        "formal_baseline": "NOT_READY",
        "can_continue": False,
        "strategy_returns_computed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
