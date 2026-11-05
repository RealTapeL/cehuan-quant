"""Map a small allowlist of hypotheses to reproducible, non-active candidate runs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.backtest.engine import run_backtest


SUPPORTED_HYPOTHESIS_PATHS = {
    "strategy.rebalance_sessions": {"kind": "integer", "minimum": 1, "maximum": 30},
    "strategy.lookback_sessions": {"kind": "integer", "minimum": 2, "maximum": 30},
    "strategy.min_momentum": {"kind": "number", "minimum": -0.5, "maximum": 0.5},
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_write(value: object, path: Path) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _apply_one_change(baseline_config: dict[str, Any], change: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path = str(change["config_path"])
    contract = SUPPORTED_HYPOTHESIS_PATHS.get(path)
    if contract is None:
        return None, f"{path} 尚未进入受控实验映射清单，状态为 PENDING_IMPLEMENTATION"
    section, key = path.split(".", 1)
    actual_current = baseline_config.get(section, {}).get(key)
    if change.get("current_value") != actual_current:
        return None, f"{path} current_value 与冻结基线不一致；拒绝套用旧假设"
    value = change.get("proposed_value")
    if contract["kind"] == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, f"{path} proposed_value 必须是整数"
    elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None, f"{path} proposed_value 必须是有限数值"
    if not contract["minimum"] <= float(value) <= contract["maximum"]:
        return None, f"{path} proposed_value 超出受控范围"
    candidate = copy.deepcopy(baseline_config)
    candidate[section][key] = value
    return candidate, None


def run_candidate_experiment(
    *,
    run_dir: Path,
    bars: pd.DataFrame,
    baseline_config: dict[str, Any],
    baseline_backtest: dict[str, Any],
    hypothesis: dict[str, Any],
) -> dict[str, str]:
    """Run at most one one-factor candidate against the already-run synthetic baseline."""
    run_dir = run_dir.expanduser().resolve()
    bars_path = run_dir / "synthetic_bars.csv"
    baseline_metrics_path = run_dir / "baseline_metrics.json"
    if not bars_path.is_file() or not baseline_metrics_path.is_file():
        raise FileNotFoundError("候选实验需要 synthetic-cycle 已写出的基线行情和基线指标")
    bars_sha256 = _sha256(bars_path)
    frame_digest = hashlib.sha256(bars.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()
    if bars_sha256 != frame_digest:
        raise ValueError("候选实验行情与 synthetic-cycle 冻结行情字节不一致")

    registration: dict[str, Any] = {
        "schema": "cehuan.controlled_synthetic_experiment.v1",
        "experiment_id": hypothesis["experiment_id"],
        "hypothesis_id": hypothesis["hypothesis_id"],
        "result_type": "synthetic_candidate_comparison_not_market_research",
        "status": "NOT_RUN",
        "input_bars_sha256": bars_sha256,
        "baseline_config_sha256": _sha256(run_dir / "baseline_config.json"),
        "same_data_and_initial_conditions": True,
        "same_backtest_engine": "cehuan.backtest.engine.run_backtest",
        "same_accounting_mode": "ledger_v2",
        "changed_parameters": [],
        "pending_implementation": [],
        "broker_connected": False,
        "orders_generated": False,
    }
    changes = hypothesis["proposed_changes"]
    if hypothesis["status"] != "PROPOSED":
        registration["status"] = "BLOCKED_BY_HYPOTHESIS_STATUS"
        registration["pending_implementation"] = [f"hypothesis status is {hypothesis['status']}; candidate not executed"]
        _json_write(registration, run_dir / "candidate_experiment.json")
        return {"candidate_experiment": "candidate_experiment.json"}
    if len(changes) != 1:
        registration["status"] = "REJECTED_MULTI_CHANGE_NOT_CONTROLLED"
        registration["pending_implementation"] = ["one-factor-at-a-time contract requires exactly one proposed change"]
        _json_write(registration, run_dir / "candidate_experiment.json")
        return {"candidate_experiment": "candidate_experiment.json"}
    candidate_config, reason = _apply_one_change(baseline_config, changes[0])
    if reason:
        registration["status"] = "NEEDS_IMPLEMENTATION_OR_REVIEW"
        registration["pending_implementation"] = [reason]
        registration["changed_parameters"] = [{
            "config_path": changes[0]["config_path"],
            "proposed_value": changes[0]["proposed_value"],
            "status": "PENDING_IMPLEMENTATION",
        }]
        _json_write(registration, run_dir / "candidate_experiment.json")
        return {"candidate_experiment": "candidate_experiment.json"}

    assert candidate_config is not None
    _json_write(candidate_config, run_dir / "candidate_config.json")
    candidate = run_backtest(
        bars,
        100_000.0,
        candidate_config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
    )
    candidate_metrics = dict(candidate["metrics"])
    candidate_metrics.update({
        "experiment_id": hypothesis["experiment_id"],
        "hypothesis_id": hypothesis["hypothesis_id"],
        "result_type": "synthetic_candidate_comparison_not_market_research",
    })
    _json_write(candidate_metrics, run_dir / "candidate_metrics.json")
    candidate["equity_curve"].to_csv(run_dir / "candidate_equity_curve.csv", index=False)
    candidate["trades"].to_csv(run_dir / "candidate_trades.csv", index=False)
    registration.update({
        "status": "COMPLETED_SYNTHETIC_ONLY",
        "changed_parameters": [{
            "config_path": changes[0]["config_path"],
            "baseline_value": changes[0].get("current_value"),
            "candidate_value": changes[0]["proposed_value"],
            "status": "APPLIED_TO_CANDIDATE_CONFIG_ONLY",
        }],
        "candidate_config_sha256": _sha256(run_dir / "candidate_config.json"),
        "candidate_metrics_sha256": _sha256(run_dir / "candidate_metrics.json"),
        "candidate_trades_sha256": _sha256(run_dir / "candidate_trades.csv"),
        "baseline_metrics_sha256": _sha256(baseline_metrics_path),
        "baseline_trade_count": int(len(baseline_backtest["trades"])),
        "candidate_trade_count": int(len(candidate["trades"])),
        "metric_comparison": {
            "baseline_total_return": baseline_backtest["metrics"].get("total_return"),
            "candidate_total_return": candidate["metrics"].get("total_return"),
            "baseline_max_drawdown": baseline_backtest["metrics"].get("max_drawdown"),
            "candidate_max_drawdown": candidate["metrics"].get("max_drawdown"),
            "interpretation": "程序计算结果仅描述此合成输入；不构成策略收益或优劣结论。",
        },
    })
    _json_write(registration, run_dir / "candidate_experiment.json")
    return {
        "candidate_config": "candidate_config.json",
        "candidate_metrics": "candidate_metrics.json",
        "candidate_equity_curve": "candidate_equity_curve.csv",
        "candidate_trades": "candidate_trades.csv",
        "candidate_experiment": "candidate_experiment.json",
    }
