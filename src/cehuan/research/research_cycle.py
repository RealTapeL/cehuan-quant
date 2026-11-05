"""Orchestrate a synthetic research-agent → experiment → review cycle."""

from __future__ import annotations

import copy
import hashlib
import json
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.research.controlled_experiments import run_candidate_experiment
from cehuan.research.research_agent import (
    generate_validated_hypothesis,
    resolve_research_model,
    validate_strategy_research_input,
)
from cehuan.research.synthetic_cycle import (
    run_synthetic_cycle,
    synthetic_cycle_bars,
    synthetic_cycle_config,
)


DEFAULT_AGENT_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "research_agent.toml"
RESEARCH_AGENT_INPUT_EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "strategy-research-agent-input.synthetic.json"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _read_settings(path: str | Path | None) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_AGENT_CONFIG
    with config_path.open("rb") as handle:
        settings = tomllib.load(handle)
    agent = settings.get("agent")
    if not isinstance(agent, dict):
        raise ValueError("research agent TOML 必须包含 [agent]")
    return agent


def _build_input(
    *,
    research_id: str,
    objective: str,
    bars: pd.DataFrame,
    config: dict[str, Any],
    bars_digest: str,
    review_summary: dict[str, Any] | None = None,
    prior_experiments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    template = json.loads(RESEARCH_AGENT_INPUT_EXAMPLE.read_text(encoding="utf-8"))
    first_day = pd.to_datetime(bars["trade_date"]).min().date().isoformat()
    last_day = pd.to_datetime(bars["trade_date"]).max().date().isoformat()
    template.update({
        "research_id": research_id,
        "objective": objective,
        "scope": {
            "market": "CN_A_SHARE",
            "start_date": first_day,
            "end_date": last_day,
            "universe_id": "synthetic-cycle-universe-v1",
        },
        "data_trust": {
            "input_file_integrity": {"status": "SYNTHETIC", "sha256": bars_digest, "scope": "当轮生成的合成行情文件"},
            "canonical_data_quality": {"status": "SYNTHETIC_FIXTURE", "scope": "只用于软件流程验证"},
            "checkpoint_binding": {"status": "NOT_USED", "scope": "本研究周期不恢复历史 checkpoint"},
            "historical_availability_and_vendor_vintage": {"status": "NOT_APPLICABLE_SYNTHETIC", "scope": "无真实供应商数据"},
            "market_rule_coverage": {"status": "SYNTHETIC_ASSUMPTION", "fallback_rows": int(len(bars)), "mode": "synthetic_fixture"},
            "fee_coverage": {"status": "SYNTHETIC_CONFIG_ASSUMPTION", "mode": "project_default_config_for_workflow_test"},
            "corporate_action_coverage": {"status": "SYNTHETIC_EMPTY_EVENT_SET", "typed_events_consumed": 0, "scope": "本轮没有公司行动"},
            "formal_baseline": {"status": "NOT_READY", "interpretation": "未完成真实历史规则、费率和公司行动完整覆盖审计"},
            "overall": "SYNTHETIC_ONLY_NOT_MARKET_EVIDENCE",
        },
        "frozen_strategy": {
            "strategy_id": "momentum-technical-baseline-synthetic-v1",
            "config_sha256": _canonical_digest(config),
            "parameters": copy.deepcopy(config["strategy"]),
        },
        "prior_experiments": prior_experiments or [],
        "review_summary": review_summary,
        "known_data_gaps": [
            "全部行情、成交、持仓及交易状态均为合成示例",
            "合成结果不评价真实 A 股策略收益",
            "正式基线保持 NOT_READY",
            "未覆盖真实历史市场规则、个人费用和全市场公司行动",
        ],
        "allowed_actions": ["PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"],
        "forbidden_actions": [
            "MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL",
            "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT",
        ],
    })
    return validate_strategy_research_input(template)


def _review_summary(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    reconciliation = pd.read_csv(run_dir / "reconciliation.csv")
    facts: list[dict[str, Any]] = []
    for row in reconciliation.to_dict(orient="records"):
        facts.append({
            key: (None if pd.isna(value) else value.item() if hasattr(value, "item") else value)
            for key, value in {
                "symbol": row.get("symbol"), "side": row.get("side"),
                "planned_quantity": row.get("planned_quantity"), "filled_quantity": row.get("filled_quantity"),
                "fill_ratio": row.get("fill_ratio"), "reconciliation_status": row.get("reconciliation_status"),
                "order_status": row.get("order_status"),
            }.items()
        })
    position_status = manifest.get("position_snapshot_status", "UNKNOWN")
    position_facts = None
    positions_file = run_dir / "position_reconciliation.csv"
    if positions_file.is_file():
        positions = pd.read_csv(positions_file)
        position_facts = [
            {key: (None if pd.isna(value) else value.item() if hasattr(value, "item") else value)
             for key, value in row.items()}
            for row in positions.to_dict(orient="records")
        ]
    return {
        "evidence_scope": "synthetic_cycle_artifacts_only",
        "reconciliation_facts": facts,
        "position_snapshot_status": position_status,
        "position_reconciliation_facts": position_facts,
        "interpretation_constraints": [
            "没有成交行不得推断撤单、拒绝或过期",
            "没有持仓快照时不得推断实际持仓或可卖数量为零",
            "这些数据是合成输入，不是华泰账户记录",
        ],
    }


def _research_report(
    *,
    research_id: str,
    objective: str,
    initial_hypothesis: dict[str, Any],
    next_hypothesis: dict[str, Any] | None,
    baseline_metrics: dict[str, Any],
    candidate_registration: dict[str, Any],
    review_summary: dict[str, Any],
    model_audits: list[dict[str, Any]],
    next_hypothesis_error: str | None,
) -> str:
    comparison = candidate_registration.get("metric_comparison", {})
    lines = [
        "# 策环研究周期报告",
        "",
        "> **SYNTHETIC ONLY。** 本报告验证 Agent、回测和复盘程序的连接，不代表 A 股策略收益或真实账户结果。",
        "",
        f"- 研究周期 ID：`{research_id}`",
        f"- 用户研究目标：{objective}",
        "- 正式基线：`NOT_READY`",
        "- 券商连接：否；真实委托：否",
        "",
        "## Agent 初始假设",
        "",
        f"- 问题观察：{initial_hypothesis['problem_observed']}",
        f"- 假设理由：{initial_hypothesis['rationale']}",
        f"- 拟议变化：`{initial_hypothesis['proposed_changes']}`",
        f"- 状态：`{initial_hypothesis['status']}`；需要人工审核：是",
        "",
        "## 程序计算事实",
        "",
        f"- 合成基线净值收益字段：`{baseline_metrics.get('total_return', '未提供')}`",
        f"- 候选合成净值收益字段：`{comparison.get('candidate_total_return', '未运行')}`",
        f"- 基线/候选合成最大回撤字段：`{comparison.get('baseline_max_drawdown', '未提供')}` / `{comparison.get('candidate_max_drawdown', '未运行')}`",
        f"- 合成基线交易行数：`{baseline_metrics.get('trade_count', '见 baseline_trades.csv')}`",
        f"- 候选实验状态：`{candidate_registration.get('status')}`",
        f"- 候选参数登记：`{candidate_registration.get('changed_parameters')}`",
        f"- 候选与基线使用相同输入：`{candidate_registration.get('same_data_and_initial_conditions')}`",
        f"- 成交复盘事实：`{review_summary.get('reconciliation_facts')}`",
        f"- 持仓快照状态：`{review_summary.get('position_snapshot_status')}`",
        "",
        "## Agent 下一轮假设",
        "",
    ]
    if next_hypothesis:
        lines += [
            f"- 问题观察：{next_hypothesis['problem_observed']}",
            f"- 建议：`{next_hypothesis['proposed_changes']}`",
            f"- 验证计划：{next_hypothesis['validation_plan']}",
            f"- 状态：`{next_hypothesis['status']}`；需要人工审核：是",
        ]
    else:
        lines.append(f"下一轮假设未生成：{next_hypothesis_error or '未知原因'}")
    lines += [
        "",
        "## 观察与限制",
        "",
        "- 上述行情、规则、基线、候选、计划、成交和持仓均为合成或项目默认假设。",
        "- 程序计算事实来自保存的 CSV/JSON；Agent 文字是研究建议，不是数据事实。",
        "- 当前只实现有限配置路径映射；其他规则会记录为 `PENDING_IMPLEMENTATION`，不会静默忽略。",
        "- 本周期没有修改默认策略配置，也不自动采用候选参数。",
        "- 费率、市场规则和公司行动没有全市场历史验证；正式基线仍为 `NOT_READY`。",
        "",
        "## 模型调用审计",
        "",
        f"- 提供方/模型：`{[(item.get('provider'), item.get('model')) for item in model_audits]}`",
        f"- 实际调用次数：{sum(int(item.get('attempt_count', 0)) for item in model_audits)}；假设请求数：{len(model_audits)}；每次尝试数：`{[item.get('attempt_count') for item in model_audits]}`",
        f"- token 用量：`{[item.get('response_usage') for item in model_audits]}`；费用未估算（无本地费率表）。",
        "",
        "## 下一步",
        "",
        "人工审阅假设与合成实验定义。真实历史数据进入研究前，继续完成点时可得性、交易规则、费用和公司行动覆盖；不得直接把本报告中的合成结果转为实盘策略。",
        "",
    ]
    return "\n".join(lines)


def run_research_cycle(
    *,
    output_root: str | Path,
    objective: str,
    now: datetime | None = None,
    scenario: str = "partial_fill",
    agent_config_path: str | Path | None = None,
    model_mode: str | None = None,
) -> Path:
    """Run initial hypothesis, paired synthetic experiment, synthetic review, then next hypothesis."""
    objective = objective.strip()
    if not objective or len(objective) > 2000:
        raise ValueError("研究目标必须为 1 到 2000 个字符")
    local_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    settings = _read_settings(agent_config_path)
    max_hypotheses = int(settings.get("max_hypotheses_per_cycle", 2))
    max_experiments = int(settings.get("max_experiments_per_cycle", 1))
    max_retries = int(settings.get("max_model_retries", 1))
    if max_hypotheses != 2 or max_experiments != 1:
        raise ValueError("MVP 限额为每周期最多 2 条假设、1 个候选实验")
    if max_retries > 1 or max_retries < 0:
        raise ValueError("MVP 单次模型调用最多重试 1 次")
    model, model_settings_audit = resolve_research_model(settings, model_mode)

    research_id = f"research-{local_now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    bars = synthetic_cycle_bars(local_now)
    bars_digest = _sha256_bytes(bars.to_csv(index=False).encode("utf-8"))
    config = synthetic_cycle_config()
    initial_input = _build_input(
        research_id=research_id, objective=objective, bars=bars,
        config=config, bars_digest=bars_digest,
    )
    initial_experiment_id = f"{research_id}-initial-candidate"
    try:
        initial_hypothesis, initial_audit = generate_validated_hypothesis(
            model,
            input_payload=initial_input,
            phase="initial",
            input_data_digest=bars_digest,
            experiment_id=initial_experiment_id,
            max_retries=max_retries,
        )
    except Exception as exc:
        failed_dir = Path(output_root).expanduser().resolve() / research_id
        failed_dir.mkdir(parents=True, exist_ok=False)
        failed_audit = getattr(exc, "audit", None)
        (failed_dir / "agent_input_rejected.json").write_text(
            json.dumps(initial_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        failure = {
            "schema": "cehuan.research_agent_rejection.v1",
            "research_cycle_id": research_id,
            "provider": getattr(model, "provider", "unknown"),
            "model": getattr(model, "model", "unknown"),
            "selected_mode": model_settings_audit["selected_mode"],
            "attempt_limit": max_retries + 1,
            "attempt_count": failed_audit.get("attempt_count", max_retries + 1) if failed_audit else 0,
            "model_audit": failed_audit,
            "rejection_reason": str(exc)[:500],
            "input_data_sha256": bars_digest,
            "orders_generated": False,
            "broker_connected": False,
        }
        (failed_dir / "agent_output_rejected.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        raise ValueError(f"初始研究假设被拒绝；审计记录：{failed_dir}；{exc}") from None
    model_audits = [initial_audit]
    candidate_box: dict[str, Any] = {}

    def before_plan(run_dir: Path, actual_bars: pd.DataFrame, base_config: dict[str, Any], base_result: dict[str, Any]) -> dict[str, str]:
        actual_digest = _sha256_bytes((run_dir / "synthetic_bars.csv").read_bytes())
        if actual_digest != bars_digest:
            raise ValueError("Agent 假设使用的合成行情与回测实际行情不同")
        if base_config != config:
            raise ValueError("synthetic-cycle 基线配置与 Agent 输入冻结配置不同")
        (run_dir / "agent_input_initial.json").write_text(
            json.dumps(initial_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        (run_dir / "hypothesis_initial.json").write_text(
            json.dumps(initial_hypothesis, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        (run_dir / "model_audit_initial.json").write_text(
            json.dumps(initial_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        candidate_box["artifacts"] = run_candidate_experiment(
            run_dir=run_dir,
            bars=actual_bars,
            baseline_config=base_config,
            baseline_backtest=base_result,
            hypothesis=initial_hypothesis,
        )
        candidate_box["registration"] = json.loads((run_dir / "candidate_experiment.json").read_text(encoding="utf-8"))
        return {
            "agent_input_initial": "agent_input_initial.json",
            "hypothesis_initial": "hypothesis_initial.json",
            "model_audit_initial": "model_audit_initial.json",
            **candidate_box["artifacts"],
        }

    run_dir = run_synthetic_cycle(
        output_root, local_now, scenario=scenario,
        cycle_run_id=research_id,
        before_plan_callback=before_plan,
    )
    review_summary = _review_summary(run_dir)
    review_digest = _canonical_digest({
        "bars_sha256": bars_digest,
        "review_summary": review_summary,
        "candidate_experiment_sha256": hashlib.sha256((run_dir / "candidate_experiment.json").read_bytes()).hexdigest(),
    })
    next_input = _build_input(
        research_id=research_id,
        objective=objective,
        bars=bars,
        config=config,
        bars_digest=review_digest,
        review_summary=review_summary,
        prior_experiments=[{
            "experiment_id": initial_hypothesis["experiment_id"],
            "result_type": candidate_box["registration"]["status"],
            "summary": "程序运行的合成基线/候选对照；不是市场研究证据。",
            "input_digest": bars_digest,
        }],
    )
    (run_dir / "agent_input_next.json").write_text(
        json.dumps(next_input, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    next_hypothesis = None
    next_audit = None
    next_error = None
    if max_hypotheses >= 2:
        try:
            next_hypothesis, next_audit = generate_validated_hypothesis(
                model,
                input_payload=next_input,
                phase="next",
                input_data_digest=review_digest,
                experiment_id=f"{research_id}-next-round",
                max_retries=max_retries,
            )
            model_audits.append(next_audit)
            (run_dir / "hypothesis_next.json").write_text(
                json.dumps(next_hypothesis, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
            (run_dir / "model_audit_next.json").write_text(
                json.dumps(next_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            )
        except Exception as exc:
            next_error = str(exc)
            failed_audit = getattr(exc, "audit", None)
            if failed_audit:
                model_audits.append(failed_audit)
            (run_dir / "hypothesis_next_rejected.json").write_text(
                json.dumps({
                    "status": "REJECTED", "reason": next_error[:500], "model_audit": failed_audit,
                }, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    baseline_metrics = json.loads((run_dir / "baseline_metrics.json").read_text(encoding="utf-8"))
    candidate_registration = candidate_box["registration"]
    report = _research_report(
        research_id=research_id,
        objective=objective,
        initial_hypothesis=initial_hypothesis,
        next_hypothesis=next_hypothesis,
        baseline_metrics=baseline_metrics,
        candidate_registration=candidate_registration,
        review_summary=review_summary,
        model_audits=model_audits,
        next_hypothesis_error=next_error,
    )
    (run_dir / "research_report.md").write_text(report, encoding="utf-8")
    # Finalize the synthetic-cycle manifest before hashing artifacts. Otherwise
    # the research manifest would record a stale hash for manifest.json.
    cycle_manifest_path = run_dir / "manifest.json"
    cycle_manifest = json.loads(cycle_manifest_path.read_text(encoding="utf-8"))
    cycle_manifest["research_cycle_manifest"] = "research_cycle_manifest.json"
    cycle_manifest["research_report"] = "research_report.md"
    cycle_manifest["initial_hypothesis"] = "hypothesis_initial.json"
    cycle_manifest["next_hypothesis"] = "hypothesis_next.json" if next_hypothesis else "hypothesis_next_rejected.json"
    cycle_manifest["candidate_experiment"] = "candidate_experiment.json"
    cycle_manifest["research_cycle_id"] = research_id
    cycle_manifest_path.write_text(json.dumps(cycle_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    artifacts = sorted(path for path in run_dir.iterdir() if path.is_file() and path.name != "research_cycle_manifest.json")
    manifest = {
        "schema": "cehuan.research_cycle_manifest.v1",
        "research_cycle_id": research_id,
        "run_id": research_id,
        "classification": "SYNTHETIC_ONLY_NOT_MARKET_RESEARCH",
        "objective": objective,
        "data_sha256": bars_digest,
        "formal_baseline": "NOT_READY",
        "model": {
            **model_settings_audit,
            "provider": getattr(model, "provider", "unknown"),
            "call_count": sum(int(item.get("attempt_count", 0)) for item in model_audits),
            "hypothesis_request_count": len(model_audits),
            "max_hypotheses": max_hypotheses,
            "max_experiments": max_experiments,
            "max_retries_per_call": max_retries,
            "calls": model_audits,
            "api_cost_estimate": None,
        },
        "candidate_experiment_status": candidate_registration["status"],
        "position_snapshot_status": review_summary["position_snapshot_status"],
        "broker_connected": False,
        "account_data_used": False,
        "orders_generated": False,
        "active_strategy_modified": False,
        "artifacts": {path.name: _sha256_bytes(path.read_bytes()) for path in artifacts},
        "next_hypothesis_status": "GENERATED" if next_hypothesis else "REJECTED_OR_SKIPPED",
    }
    (run_dir / "research_cycle_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    return run_dir
