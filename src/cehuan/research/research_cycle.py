"""研究周期编排器。模块串接结构化假设、受控候选实验、合成成交复盘和下一轮假设，并把程序事实绑定到文件哈希；默认示例使用合成输入，不修改当前有效策略。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.research.controlled_experiments import build_experiment_capabilities, run_candidate_experiment
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
    """计算内存字节的 SHA-256。参数：value 为待摘要内容。返回：小写十六进制摘要。"""
    return hashlib.sha256(value).hexdigest()


def _canonical_digest(value: object) -> str:
    """将 JSON 兼容对象按稳定序列化口径生成摘要。参数：value 为待绑定对象。返回：SHA-256 字符串；不可序列化值由 JSON 编码器拒绝。"""
    return _sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _read_settings(path: str | Path | None) -> dict[str, Any]:
    """读取可选研究 Agent 设置文件。参数：path 为配置路径，None 时使用项目默认设置。返回：设置映射；缺失或 TOML 格式错误时抛出读取/解析异常。"""
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
    """构造并验证研究 Agent 的单轮输入。参数：research_id/objective 标识研究，bars/config 为合成或受限数据与冻结配置，bars_digest 绑定行情，review_summary/prior_experiments 为可选既有事实。返回：符合输入 schema 的字典。异常：内容未通过输入校验时阻断。"""
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
        "experiment_capabilities": build_experiment_capabilities(config),
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
    """从已完成的研究目录汇总程序复盘事实。参数：run_dir 为本轮目录。返回：复盘状态和可供下一轮模型使用的精简摘要；缺失产物保持未知/失败，不伪造数据。"""
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


def _artifact_reference(run_dir: Path, role: str, filename: str) -> dict[str, str]:
    """生成研究产物的路径和摘要引用。参数：run_dir 为周期目录，role 描述文件用途，filename 为相对文件名。返回：可审计的产物引用；文件缺失时标记不可用。"""
    path = run_dir / filename
    if not path.is_file():
        return {"role": role, "path": filename, "status": "MISSING"}
    return {
        "role": role,
        "path": filename,
        "status": "PRESENT",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _finite_metric(metrics: dict[str, Any] | None, key: str) -> float | int | None:
    """从指标对象取出有限数值。参数：metrics 为可选指标映射，key 为指标名。返回：有限数值或 None，避免把缺失值当作零。"""
    if not metrics or key not in metrics:
        return None
    value = metrics[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return value


def _build_next_round_review_summary(
    run_dir: Path,
    *,
    reconciliation_facts: dict[str, Any],
    initial_hypothesis: dict[str, Any],
) -> dict[str, Any]:
    """从已保存实验、复盘和假设构造下一轮绑定输入。参数：run_dir 为本轮产物，reconciliation_facts 为程序对账事实，initial_hypothesis 为初始假设。返回：分开保存程序事实与文字建议的摘要及来源引用；不运行实验。"""
    registration_path = run_dir / "candidate_experiment.json"
    registration = json.loads(registration_path.read_text(encoding="utf-8"))
    baseline_path = run_dir / "baseline_metrics.json"
    candidate_path = run_dir / "candidate_metrics.json"
    status = str(registration.get("status", "UNKNOWN"))
    baseline_metrics = None
    candidate_metrics = None
    metrics_status = "BASELINE_METRICS_MISSING"
    comparison: dict[str, Any] | None = None
    if baseline_path.is_file():
        baseline_sha256 = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        if registration.get("baseline_metrics_sha256") != baseline_sha256:
            metrics_status = "BASELINE_METRIC_ARTIFACT_HASH_MISMATCH"
        else:
            baseline_metrics = json.loads(baseline_path.read_text(encoding="utf-8"))
            metrics_status = "EXPERIMENT_NOT_COMPLETED" if status != "COMPLETED_SYNTHETIC_ONLY" else "CANDIDATE_METRICS_MISSING"
    if status == "COMPLETED_SYNTHETIC_ONLY" and baseline_metrics is not None:
        if not candidate_path.is_file():
            metrics_status = "CANDIDATE_METRICS_MISSING"
        else:
            candidate_sha256 = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            if registration.get("candidate_metrics_sha256") != candidate_sha256:
                metrics_status = "CANDIDATE_METRIC_ARTIFACT_HASH_MISMATCH"
            else:
                candidate_metrics = json.loads(candidate_path.read_text(encoding="utf-8"))
                metrics_status = "AVAILABLE_FROM_HASH_BOUND_PERSISTED_METRICS"
    if metrics_status == "AVAILABLE_FROM_HASH_BOUND_PERSISTED_METRICS":
        comparison = {}
        for key in ("total_return", "max_drawdown", "volatility", "turnover", "win_rate", "trade_count"):
            baseline_value = _finite_metric(baseline_metrics, key)
            candidate_value = _finite_metric(candidate_metrics, key)
            if baseline_value is not None and candidate_value is not None:
                comparison[key] = {
                    "baseline": baseline_value,
                    "candidate": candidate_value,
                    "candidate_minus_baseline": float(candidate_value) - float(baseline_value),
                }
        if not comparison:
            metrics_status = "NO_COMPARABLE_NUMERIC_METRICS"
            comparison = None

    source_artifacts = [
        _artifact_reference(run_dir, "candidate_experiment_registration", "candidate_experiment.json"),
        _artifact_reference(run_dir, "baseline_metrics", "baseline_metrics.json"),
        _artifact_reference(run_dir, "candidate_metrics", "candidate_metrics.json"),
        _artifact_reference(run_dir, "trade_reconciliation", "reconciliation.csv"),
        _artifact_reference(run_dir, "position_reconciliation", "position_reconciliation.csv"),
        _artifact_reference(run_dir, "synthetic_cycle_manifest", "manifest.json"),
        _artifact_reference(run_dir, "initial_agent_hypothesis", "hypothesis_initial.json"),
    ]
    return {
        "schema": "cehuan.research_next_round_context.v1",
        "evidence_scope": "synthetic_cycle_artifacts_only",
        "program_facts": {
            "experiment": {
                "experiment_id": registration.get("experiment_id"),
                "hypothesis_id": registration.get("hypothesis_id"),
                "status": status,
                "changed_parameters": registration.get("changed_parameters", []),
                "pending_implementation": registration.get("pending_implementation", []),
                "same_data_and_initial_conditions": registration.get("same_data_and_initial_conditions"),
            },
            "metrics_status": metrics_status,
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "metric_comparison": comparison,
            "reconciliation": reconciliation_facts,
            "source_artifacts": source_artifacts,
            "interpretation_constraints": [
                "此处 program_facts 只反映所列程序产物；不代表真实 A 股收益或账户事实。",
                "候选未完成或指标产物缺失时，null/缺失表示不可用；不得推测或补写为程序计算事实。",
                "无成交行、无持仓快照或原因未知时，不得推断撤单、拒绝、过期或零持仓。",
                "正式基线为 NOT_READY；历史时点、规则、费用和公司行动仍有限制。",
            ],
        },
        "agent_authored_context": {
            "classification": "AGENT_TEXT_NOT_PROGRAM_FACT",
            "initial_hypothesis_id": initial_hypothesis.get("hypothesis_id"),
            "status": initial_hypothesis.get("status"),
            "problem_observed": initial_hypothesis.get("problem_observed"),
            "rationale": initial_hypothesis.get("rationale"),
            "proposed_changes": initial_hypothesis.get("proposed_changes"),
        },
    }


def _research_report(
    *,
    research_id: str,
    objective: str,
    initial_hypothesis: dict[str, Any],
    next_hypothesis: dict[str, Any] | None,
    program_facts: dict[str, Any],
    review_summary: dict[str, Any],
    model_audits: list[dict[str, Any]],
    next_hypothesis_error: str | None,
) -> str:
    """生成周期级中文研究报告。参数：研究 ID/目标、初始及下一轮假设、程序事实、复盘摘要、模型审计和下一轮错误共同构成报告内容。返回：Markdown 文本，明确合成边界和未完成状态。"""
    comparison = program_facts.get("metric_comparison") or {}
    experiment_facts = program_facts.get("experiment", {})
    baseline_metrics = program_facts.get("baseline_metrics") or {}
    candidate_metrics = program_facts.get("candidate_metrics") or {}
    total_return_comparison = comparison.get("total_return", {})
    drawdown_comparison = comparison.get("max_drawdown", {})
    lines = [
        "# 策环研究周期报告",
        "",
        "> **SYNTHETIC ONLY。** 本报告验证 Agent、回测和复盘程序的连接，不代表 A 股策略收益或真实账户结果。",
        "",
        f"- 研究周期 ID：`{research_id}`",
        "",
        "## 研究目标",
        "",
        objective,
        "",
        "## 执行边界",
        "",
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
        f"- 候选实验状态：`{experiment_facts.get('status', 'UNKNOWN')}`；指标状态：`{program_facts.get('metrics_status', 'UNKNOWN')}`",
        f"- 合成基线净值收益字段：`{baseline_metrics.get('total_return', '未提供')}`",
        f"- 候选合成净值收益字段：`{total_return_comparison.get('candidate', '无完整候选指标')}`",
        f"- 基线/候选合成最大回撤字段：`{drawdown_comparison.get('baseline', baseline_metrics.get('max_drawdown', '未提供'))}` / `{drawdown_comparison.get('candidate', candidate_metrics.get('max_drawdown', '无完整候选指标'))}`",
        f"- 合成基线交易行数：`{baseline_metrics.get('trade_count', '见 baseline_trades.csv')}`",
        f"- 候选参数登记：`{experiment_facts.get('changed_parameters')}`",
        f"- 候选与基线使用相同输入：`{experiment_facts.get('same_data_and_initial_conditions')}`",
        "",
        "## 成交与持仓复盘事实",
        "",
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
    """执行一个隔离研究周期：生成假设、运行配对候选实验、复盘合成成交并提出下一轮假设。参数：output_root 为输出根目录，objective 为研究目标，now 固定运行时间，scenario 选择合成成交情景，agent_config_path/model_mode 控制模型。返回：周期 ID、状态和产物路径。副作用：写入唯一运行目录，真实 LLM 模式可能计费；不连接券商。异常：本地校验或流程门控失败时保存失败状态并返回/抛出对应结果。"""
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
        rejected_input_path = failed_dir / "agent_input_rejected.json"
        rejected_output_path = failed_dir / "agent_output_rejected.json"
        rejected_input_path.write_text(
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
        rejected_output_path.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        rejection_manifest = {
            "schema": "cehuan.research_cycle_rejection_manifest.v1",
            "research_cycle_id": research_id,
            "classification": "SYNTHETIC_ONLY_NOT_MARKET_RESEARCH",
            "failure_status": "INITIAL_HYPOTHESIS_REJECTED",
            "model": {
                **model_settings_audit,
                "provider": getattr(model, "provider", "unknown"),
                "attempt_count": failure["attempt_count"],
                "attempts": failed_audit.get("attempts", []) if failed_audit else [],
                "usage": failed_audit.get("response_usage") if failed_audit else None,
                "api_cost_estimate": None,
            },
            "formal_baseline": "NOT_READY",
            "account_data_used": False,
            "broker_connected": False,
            "orders_generated": False,
            "artifacts": {
                rejected_input_path.name: _sha256_bytes(rejected_input_path.read_bytes()),
                rejected_output_path.name: _sha256_bytes(rejected_output_path.read_bytes()),
            },
        }
        (failed_dir / "rejection_manifest.json").write_text(
            json.dumps(rejection_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raise ValueError(f"初始研究假设被拒绝；审计记录：{failed_dir}；{exc}") from None
    model_audits = [initial_audit]
    candidate_box: dict[str, Any] = {}

    def before_plan(run_dir: Path, actual_bars: pd.DataFrame, base_config: dict[str, Any], base_result: dict[str, Any]) -> dict[str, str]:
        """在创建合成交易计划前调用的可选研究钩子。参数：run_dir 为周期目录，actual_bars 为本次行情，base_config/base_result 为冻结配置与基线结果。返回：该周期需要记录的文件引用或哈希映射；副作用由调用方约束在隔离目录内。"""
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
        try:
            candidate_box["artifacts"] = run_candidate_experiment(
                run_dir=run_dir,
                bars=actual_bars,
                baseline_config=base_config,
                baseline_backtest=base_result,
                hypothesis=initial_hypothesis,
            )
        except Exception as exc:
            candidate_box["artifacts"] = {"candidate_experiment": "candidate_experiment.json"}
            failed_registration = {
                "schema": "cehuan.controlled_synthetic_experiment.v1",
                "experiment_id": initial_hypothesis["experiment_id"],
                "hypothesis_id": initial_hypothesis["hypothesis_id"],
                "result_type": "synthetic_candidate_comparison_not_market_research",
                "status": "FAILED",
                "baseline_metrics_sha256": hashlib.sha256(
                    (run_dir / "baseline_metrics.json").read_bytes()
                ).hexdigest(),
                "failure_type": type(exc).__name__,
                "failure_summary": str(exc)[:500],
                "broker_connected": False,
                "orders_generated": False,
            }
            (run_dir / "candidate_experiment.json").write_text(
                json.dumps(failed_registration, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
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
    reconciliation_facts = _review_summary(run_dir)
    review_summary = _build_next_round_review_summary(
        run_dir,
        reconciliation_facts=reconciliation_facts,
        initial_hypothesis=initial_hypothesis,
    )
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
            "summary": (
                "程序已完成合成候选与基线对照；数值详见带来源散列的 review_summary.program_facts。"
                if candidate_box["registration"]["status"] == "COMPLETED_SYNTHETIC_ONLY"
                else f"程序记录候选实验状态为 {candidate_box['registration']['status']}；没有可引用的完整基线/候选比较事实。"
            ),
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

    report = _research_report(
        research_id=research_id,
        objective=objective,
        initial_hypothesis=initial_hypothesis,
        next_hypothesis=next_hypothesis,
        program_facts=review_summary["program_facts"],
        review_summary=reconciliation_facts,
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
        "candidate_experiment_status": candidate_box["registration"]["status"],
        "position_snapshot_status": reconciliation_facts["position_snapshot_status"],
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
