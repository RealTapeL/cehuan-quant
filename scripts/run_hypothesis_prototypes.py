from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.research.hypothesis_prototypes import (
    build_h2_open_based_replacement_plan,
    compare_h1_candidate_pool,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _git_context() -> dict[str, Any]:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    paths = subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.splitlines()
    return {"commit": commit, "worktree_clean": not bool(paths), "uncommitted_paths": [line[3:] for line in paths]}


def _publish_new_directory(staging: Path, output_dir: Path) -> None:
    lock_path = output_dir.parent / f".{output_dir.name}.publish-lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"另一个进程正在发布该实验，或遗留发布锁存在：{lock_path}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        if output_dir.exists():
            raise FileExistsError(f"实验输出在构建期间出现，拒绝覆盖：{output_dir}")
        os.rename(staging, output_dir)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        lock_path.unlink(missing_ok=True)


def _synthetic_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    dates = pd.bdate_range("2025-01-06", periods=21).strftime("%Y-%m-%d").tolist()
    signal_date = dates[-1]
    calendar = pd.DataFrame({"trade_date": dates, "is_open": True})
    symbols = ["SYN_CLEAR", "SYN_HALTED", "SYN_UNKNOWN", "SYN_MISSING", "SYN_DUPLICATE"]
    bar_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        for index, day in enumerate(dates):
            if symbol == "SYN_MISSING" and index == 4:
                continue
            suspended: object = False
            if symbol == "SYN_HALTED" and index == 7:
                suspended = True
            if symbol == "SYN_UNKNOWN" and index == 7:
                suspended = ""
            bar_rows.append({"symbol": symbol, "trade_date": day, "is_suspended": suspended})
            if symbol == "SYN_DUPLICATE" and index == 7:
                bar_rows.append({"symbol": symbol, "trade_date": day, "is_suspended": False})
    bars = pd.DataFrame(bar_rows)
    baseline = pd.DataFrame([
        {"symbol": symbol, "signal_date": signal_date, "momentum": momentum, "rank": rank}
        for rank, (symbol, momentum) in enumerate([
            ("SYN_HALTED", 0.25), ("SYN_CLEAR", 0.20), ("SYN_UNKNOWN", 0.15),
            ("SYN_MISSING", 0.10), ("SYN_DUPLICATE", 0.05),
        ], start=1)
    ])

    quote_columns = ["symbol", "trade_date", "open_price_cny", "available_at_utc", "is_suspended", "is_listed", "can_buy"]
    decision_quotes = pd.DataFrame([
        {"symbol": "SYN_PRIMARY", "trade_date": "2025-02-05", "open_price_cny": 12.0, "available_at_utc": "2025-02-05T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
        {"symbol": "SYN_REPLACEMENT", "trade_date": "2025-02-05", "open_price_cny": 4.0, "available_at_utc": "2025-02-05T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
        {"symbol": "SYN_FALLBACK", "trade_date": "2025-02-05", "open_price_cny": 3.0, "available_at_utc": "2025-02-05T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
    ], columns=quote_columns)
    execution_quotes = pd.DataFrame([
        {"symbol": "SYN_REPLACEMENT", "trade_date": "2025-02-06", "open_price_cny": 4.5, "available_at_utc": "2025-02-06T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
        {"symbol": "SYN_FALLBACK", "trade_date": "2025-02-06", "open_price_cny": 3.2, "available_at_utc": "2025-02-06T01:30:00Z", "is_suspended": False, "is_listed": True, "can_buy": True},
    ], columns=quote_columns)
    payload = {
        "data_status": "FULLY_SYNTHETIC_NO_REAL_MARKET_OR_ACCOUNT_DATA",
        "h1": {"signal_date": signal_date, "lookback_sessions": 20, "synthetic_symbols": symbols},
        "h2": {
            "signal_date": "2025-02-04",
            "ranking_available_at_utc": "2025-02-04T07:10:00Z",
            "ranked_candidates": [
                {"symbol": "SYN_PRIMARY", "rank": 1, "momentum": 0.2},
                {"symbol": "SYN_REPLACEMENT", "rank": 2, "momentum": 0.1},
                {"symbol": "SYN_FALLBACK", "rank": 3, "momentum": 0.05},
            ],
            "decision_session_date": "2025-02-05",
            "decision_at_utc": "2025-02-05T01:31:00Z",
            "execution_session_date": "2025-02-06",
            "execution_at_utc": "2025-02-06T01:31:00Z",
            "budget": {
                "max_positions": 1,
                "target_amount_per_slot_cny": 500.0,
                "portfolio_risk_budget_cny": 1000.0,
                "reserved_risk_cny": 0.0,
                "available_cash_cny": 500.0,
                "buy_min_shares": 100,
                "buy_step_shares": 100,
                "commission_rate": 0.0003,
                "minimum_commission_cny": 5.0,
            },
        },
    }
    return bars, calendar, baseline, {"input": payload, "decision_quotes": decision_quotes, "execution_quotes": execution_quotes}


def build_run(output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"实验输出已存在，拒绝覆盖：{output_dir}")
    bars, calendar, baseline, h2_data = _synthetic_inputs()
    h1 = compare_h1_candidate_pool(baseline, bars, calendar, lookback_sessions=20)
    candidate = h2_data["input"]["h2"]["ranked_candidates"]
    budget = h2_data["input"]["h2"]["budget"]
    h2 = build_h2_open_based_replacement_plan(
        pd.DataFrame(candidate),
        h2_data["decision_quotes"],
        h2_data["execution_quotes"],
        signal_date=h2_data["input"]["h2"]["signal_date"],
        ranking_available_at_utc=h2_data["input"]["h2"]["ranking_available_at_utc"],
        decision_session_date=h2_data["input"]["h2"]["decision_session_date"],
        execution_session_date=h2_data["input"]["h2"]["execution_session_date"],
        decision_at_utc=h2_data["input"]["h2"]["decision_at_utc"],
        execution_at_utc=h2_data["input"]["h2"]["execution_at_utc"],
        **budget,
    )
    now = datetime.now(timezone.utc)
    run_id = f"hypothesis-prototypes-synthetic-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        _write_json(staging / "synthetic-input.json", h2_data["input"])
        bars.to_csv(staging / "h1-synthetic-bars.csv", index=False)
        calendar.to_csv(staging / "h1-synthetic-calendar.csv", index=False)
        baseline.to_csv(staging / "h1-baseline-candidates.csv", index=False)
        h2_data["decision_quotes"].to_csv(staging / "h2-decision-open-quotes.csv", index=False)
        h2_data["execution_quotes"].to_csv(staging / "h2-next-session-open-quotes.csv", index=False)
        h1.to_csv(staging / "h1-candidate-comparison.csv", index=False)
        _write_json(staging / "h2-delayed-replacement-plan.json", h2)
        h1_status = Counter(h1["h1_status"].astype(str))
        control = {
            "policy": "SELECT_TOP_N_THEN_ROUND_KEEP_CASH",
            "primary_target": "SYN_PRIMARY",
            "primary_open_price_cny": 12.0,
            "target_amount_cny": budget["target_amount_per_slot_cny"],
            "raw_shares": int(budget["target_amount_per_slot_cny"] // 12.0),
            "rounded_shares": 0,
            "replacement_selected": False,
            "simulated_fill": False,
            "cash_retained_before_other_portfolio_activity_cny": budget["available_cash_cny"],
        }
        candidate_result = h2["execution_results"][0] if h2["execution_results"] else None
        _write_json(staging / "h2-control-candidate-diff.json", {
            "only_registered_rule_difference": "zero-share target keeps cash (control) vs. after observing the decision-session open, select a replacement and schedule it for the next session open (candidate)",
            "control": control,
            "candidate": {
                "replacement_orders": h2["replacement_orders"],
                "execution_result": candidate_result,
                "same_decision_session_open_fill_allowed": False,
                "selection_uses_execution_quote": False,
            },
        })
        source_paths = [Path(__file__).resolve(), Path("src/cehuan/research/hypothesis_prototypes.py").resolve()]
        input_paths = [
            staging / "synthetic-input.json", staging / "h1-synthetic-bars.csv",
            staging / "h1-synthetic-calendar.csv", staging / "h1-baseline-candidates.csv",
            staging / "h2-decision-open-quotes.csv", staging / "h2-next-session-open-quotes.csv",
        ]
        outputs = [p for p in staging.iterdir() if p.is_file()]
        manifest = {
            "schema_version": 1,
            "experiment_id": run_id,
            "run_type": "SYNTHETIC_PROGRAM_BEHAVIOR_DIAGNOSTIC_NOT_STRATEGY_BACKTEST",
            "created_at_utc": now.isoformat(),
            "git": _git_context(),
            "hypotheses": ["HYP-SUSPENSION-WINDOW-FILTER-001", "HYP-ZERO-TARGET-CASH-ALLOCATION-001"],
            "input_data_status": "SYNTHETIC_ONLY",
            "input_sha256": {path.name: _sha256(path) for path in input_paths},
            "code_sha256": {path.name: _sha256(path) for path in source_paths},
            "registered_design": {
                "control_and_candidate_share_same_synthetic_inputs": True,
                "h1_only_rule_difference": "candidate eligibility requires exactly 21 open sessions with one unique bar per session and known non-suspended status",
                "h2_only_rule_difference": "control keeps zero-share slot as cash; candidate observes next-session decision open, freezes replacement quantity, and only schedules next-session-after-decision open",
                "h2_ranking_available_at_utc": h2_data["input"]["h2"]["ranking_available_at_utc"],
                "h2_decision_at_utc": h2_data["input"]["h2"]["decision_at_utc"],
                "h2_execution_at_utc": h2_data["input"]["h2"]["execution_at_utc"],
                "budget_fee_lot_and_existing_position_assumptions": budget,
                "metrics": ["H1 eligible/excluded counts and reasons", "H2 selected replacement, frozen shares, simulated fill/block, fees, remaining cash"],
                "stopping_conditions": ["unknown or unavailable statuses", "insufficient data", "budget/cash/fee failure", "no valid replacement"],
                "human_review_required": True,
                "may_modify_active_strategy": False,
                "may_generate_orders": False,
            },
            "results": {
                "h1_status_counts": dict(h1_status),
                "h1_control_candidate_rows": int(len(h1)),
                "h1_eligible_rows": int(h1["h1_status"].eq("ELIGIBLE").sum()),
                "h1_excluded_rows": int(h1["h1_status"].eq("EXCLUDED").sum()),
                "h2_selected_orders": len(h2["replacement_orders"]),
                "h2_simulated_fills": sum(item["status"] == "SIMULATED_FILL_NEXT_SESSION_OPEN" for item in h2["execution_results"]),
                "result_scope": "No return, Sharpe, drawdown or strategy-performance claim; synthetic program behavior only.",
            },
            "release_status": {"formal_baseline": "NOT_READY", "can_continue": False, "independent_validation": "NOT_PERFORMED"},
            "safety": {"broker_connected": False, "orders_generated": False, "active_strategy_modified": False},
            "output_sha256": {path.name: _sha256(path) for path in outputs},
        }
        _write_json(staging / "experiment-manifest.json", manifest)
        _publish_new_directory(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"output_dir": str(output_dir), "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic H1/H2 synthetic research prototypes; no market data or orders.")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_run(args.output_dir)
    print(json.dumps({
        "output_dir": result["output_dir"],
        "experiment_id": result["manifest"]["experiment_id"],
        "h1_status_counts": result["manifest"]["results"]["h1_status_counts"],
        "h2_simulated_fills": result["manifest"]["results"]["h2_simulated_fills"],
        "formal_baseline": "NOT_READY",
        "can_continue": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
