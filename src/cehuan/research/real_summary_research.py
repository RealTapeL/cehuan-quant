"""Run one bounded research-agent request over a sanitized real-market summary."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from cehuan.research.controlled_experiments import (
    SUPPORTED_HYPOTHESIS_PATHS,
    _apply_one_change,
    build_experiment_capabilities,
)
from cehuan.research.real_market_summary import (
    REPO_ROOT,
    build_real_market_summary,
    sha256_file,
    write_real_market_summary,
)
from cehuan.research.research_agent import (
    ResearchHypothesisRejected,
    generate_validated_hypothesis,
    resolve_research_model,
    validate_strategy_research_input,
)
from cehuan.research.research_cycle import _read_settings


DEFAULT_OBJECTIVE = (
    "围绕 2024-10-08 至 2025-01-10 真实行情摘要中的动量排名与停牌观察，"
    "提出一条可以在补齐数据和规则后验证的研究假设。"
)
REAL_SUMMARY_CLASSIFICATION = "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST"


def _write_json(value: Any, path: Path) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _record_usage(provider_audit: dict[str, Any], model_audit: dict[str, Any]) -> None:
    attempts = model_audit.get("attempts", [])
    usage_rows = [item.get("usage") for item in attempts if isinstance(item, dict) and isinstance(item.get("usage"), dict)]
    provider_audit["usage_by_attempt"] = usage_rows
    if usage_rows:
        keys = set.intersection(*(set(row) for row in usage_rows))
        provider_audit["usage_total"] = {key: sum(int(row[key]) for row in usage_rows) for key in sorted(keys)}
    else:
        provider_audit["usage_total"] = None


def _summary_for_agent(summary: dict[str, Any], summary_digest: str) -> dict[str, Any]:
    """Whitelist facts for the model; never include local source paths or raw rows."""
    evidence_catalog = [
        {
            "fact_id": item["fact_id"],
            "statement": item["statement"],
            "artifact_sha256": item["artifact_sha256"],
            "evidence_status": item["evidence_status"],
        }
        for item in summary["evidence_catalog"]
    ]
    return {
        "schema": "cehuan.real_market_research_summary_input.v1",
        "summary_id": summary["summary_id"],
        "summary_sha256": summary_digest,
        "classification": REAL_SUMMARY_CLASSIFICATION,
        "facts": {
            "scope": summary["scope"],
            "dataset": summary["dataset"],
            "raw_quality_audit": summary["raw_quality_audit"],
            "point_in_time_audit": summary["point_in_time_audit"],
            "strategy": summary["strategy"],
            "signal_observations": summary["signal_observations"],
            "configured_fallbacks_not_historically_verified": summary["configured_fallbacks_not_historically_verified"],
            "known_limitations": summary["known_limitations"],
            "prior_research_state": summary["prior_research_state"],
        },
        "evidence_catalog": evidence_catalog,
    }


def _build_agent_input(summary: dict[str, Any], summary_digest: str, config: dict[str, Any], research_id: str) -> dict[str, Any]:
    scope = summary["scope"]
    strategy = config.get("strategy", {})
    capabilities = build_experiment_capabilities(config)
    trust = {
        "input_file_integrity": {
            "status": "HASHES_RECOMPUTED_AND_BOUND_TO_EXISTING_MANIFESTS",
            "sha256": summary_digest,
            "scope": "摘要输入文件与本地原始/审计文件哈希重新计算；不证明来源语义正确。",
        },
        "canonical_data_quality": {
            "status": "STRUCTURAL_AUDIT_HEALTHY_WITH_SCOPE_LIMITS",
            "scope": "现有快照和已分类沪深股票池内部结构审计；供应商完备性未独立验证。",
        },
        "checkpoint_binding": {"status": "NOT_USED", "scope": "本研究只使用摘要，不续跑 checkpoint。"},
        "historical_availability_and_vendor_vintage": {
            "status": "UNVERIFIED",
            "scope": "BaoStock 回取时间、适配器 available_at 和 2024 年历史决策时点可得性未核实。",
        },
        "market_rule_coverage": {
            "status": "FALLBACK_USED",
            "fallback_rows": int(summary["configured_fallbacks_not_historically_verified"].get("rows_using_market_rule_fallback") or 0),
            "mode": "配置回退，不是已验证的历史板块/涨跌停/交易单位规则。",
        },
        "fee_coverage": {
            "status": "UNVERIFIED",
            "mode": "配置费用，仅为研究假设。",
            "personal_brokerage_rate": "NOT_PROVIDED",
        },
        "corporate_action_coverage": {
            "status": "INCOMPLETE_UNVERIFIED",
            "typed_events_consumed": 0,
            "scope": "事件登记覆盖不完整；无记录不等于无公司行动。",
        },
        "formal_baseline": {"status": "NOT_READY", "interpretation": "不运行正式收益比较。"},
        "overall": REAL_SUMMARY_CLASSIFICATION,
    }
    gaps = list(summary["known_limitations"])
    return validate_strategy_research_input({
        "schema_version": 1,
        "research_id": research_id,
        "objective": DEFAULT_OBJECTIVE,
        "scope": {
            "market": "CN_A_SHARE",
            "start_date": scope["start_date"],
            "end_date": scope["end_date"],
            "universe_id": str(scope.get("universe_snapshot_id") or "UNVERIFIED_LOCAL_SHANGHAI_SHENZHEN_UNIVERSE"),
        },
        "data_trust": trust,
        "frozen_strategy": {
            "strategy_id": strategy.get("name", "UNKNOWN"),
            "config_sha256": summary["strategy"]["config_sha256"],
            "parameters": {
                key: strategy.get(key)
                for key in (
                    "lookback_sessions", "rebalance_sessions", "max_positions",
                    "max_position_weight", "max_total_exposure", "min_momentum",
                )
            },
        },
        "prior_experiments": [],
        "known_data_gaps": gaps,
        "allowed_actions": ["PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"],
        "forbidden_actions": [
            "MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL",
            "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT",
        ],
        "experiment_capabilities": capabilities,
        "market_research_summary": _summary_for_agent(summary, summary_digest),
    })


def _proposal_review(hypothesis: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    changes = hypothesis["proposed_changes"]
    if len(changes) != 1:
        return {
            "status": "REJECTED_CHANGE_COUNT",
            "items": [{"status": "REJECTED", "reason": "本阶段只接受一项可检验改动。"}],
        }
    change = changes[0]
    path = change["config_path"]
    if path not in SUPPORTED_HYPOTHESIS_PATHS:
        return {
            "status": "PENDING_IMPLEMENTATION",
            "items": [{
                "config_path": path,
                "status": "PENDING_IMPLEMENTATION",
                "reason": "该参数/规则不在受控实验器支持清单；没有被应用或执行。",
            }],
        }
    _, reason = _apply_one_change(config, change)
    if reason:
        return {
            "status": "REJECTED_SUPPORTED_PARAMETER_CHANGE",
            "items": [{"config_path": path, "status": "REJECTED", "reason": reason}],
        }
    return {
        "status": "VALIDATED_PROPOSAL_NOT_EXECUTED",
        "items": [{
            "config_path": path,
            "current_value": change.get("current_value"),
            "proposed_value": change.get("proposed_value"),
            "status": "VALIDATED_PROPOSAL_ONLY",
            "reason": "通过受控参数范围校验；没有改动有效策略，也没有运行回测。",
        }],
    }


def _report(summary: dict[str, Any], hypothesis: dict[str, Any] | None, proposal: dict[str, Any] | None,
            model_status: str, model_audit: dict[str, Any] | None) -> str:
    data = summary["dataset"]
    lines = [
        "# 策环真实行情摘要研究报告",
        "",
        f"- 分类：`{summary['classification']}`",
        f"- 摘要 ID：`{summary['summary_id']}`",
        f"- 模型状态：`{model_status}`",
        f"- 正式基线：`{summary['prior_research_state']['formal_baseline']}`",
        "",
        "## 研究目标",
        "",
        DEFAULT_OBJECTIVE,
        "",
        "## 数据与程序事实",
        "",
        f"- 来源：{summary['scope'].get('provider')}，本地 BaoStock 规范日线；区间 {summary['scope']['start_date']} 至 {summary['scope']['end_date']}。",
        f"- 数据规模：{data['date_count']} 个交易日、{data['symbol_count']} 只证券、{data['row_count']} 行；OHLC 无效 {data['invalid_ohlc_rows']} 行，重复证券日 {data['duplicate_symbol_date_rows']} 行。",
        f"- 快照质量记录：回取时间 {summary['raw_quality_audit']['retrieved_at_utc']}；审计记录 {len(summary['raw_quality_audit']['warnings'] or [])} 条警告，其中包括单日原始收盘价变动超过 50% 的一项待核查警告。",
        f"- 停牌标记 {data['is_suspended_true_rows']} 行；ST 标记 {data['is_st_true_rows']} 行。名称 `*ST` 前缀与标记冲突 {data['display_name_st_prefix_but_is_st_false_rows']} 行，但名称的历史时点来源未核实。",
        f"- 行情使用未复权 OHLC 执行价格字段，信号配置使用后复权收盘价；供应商 vintage 和历史发布时间未核实。",
        f"- 已分类沪深股票池内部对账：成员日 {summary['point_in_time_audit']['daily_stock_member_rows']}，有行情 {summary['point_in_time_audit']['members_with_bars']}，缺失 {summary['point_in_time_audit']['members_without_bars']}；这不是供应商完整性证明，不含北交所。",
        f"- 冻结基线参数：{json.dumps(summary['strategy']['parameters'], ensure_ascii=False, sort_keys=True)}。",
        f"- 回退：{summary['configured_fallbacks_not_historically_verified']['rows_using_market_rule_fallback']} 行使用市场规则回退；费率为配置值，不是已核验历史费率或个人华泰费率。",
        f"- 行情中市场规则字段缺失计数：{json.dumps(data['market_rule_missing_rows_by_field'], ensure_ascii=False, sort_keys=True)}。",
        f"- 回退配置值：市场 {json.dumps(summary['configured_fallbacks_not_historically_verified']['market'], ensure_ascii=False, sort_keys=True)}；费用 {json.dumps(summary['configured_fallbacks_not_historically_verified']['costs'], ensure_ascii=False, sort_keys=True)}。",
        "- 信号日候选排名是从冻结行情和配置事后重建，不证明它曾在原始运行时封存。",
        "",
        "### 已有信号观察（事后重建）",
        "",
    ]
    for day in summary["signal_observations"]["dates"]:
        top = ", ".join(
            f"{item['rank']}. {item['symbol']}（动量 {item['momentum']:.4f}）"
            for item in day["top_candidates"]
        )
        lines.append(f"- {day['signal_date']}：{day['eligible_ranked_candidate_count']} 个排名候选；前三名：{top}。")
    focus = data["focus_suspension_candidate_window"]
    lines.extend([
        f"- {focus['symbol']}：截至 {focus['signal_date']} 的重建观察窗 {focus['actual_rows']} 行，其中停牌标记 {focus['suspended_rows']} 行，重建动量 {focus['reconstructed_20_session_momentum']:.4f}；该观察需要先核实停牌与价格序列口径。",
        "",
        "## 数据限制",
        "",
        *[f"- {item}" for item in summary["known_limitations"]],
        "",
        "## Agent 初始假设",
        "",
    ])
    if hypothesis is None:
        lines.extend([
            "真实模型尚未返回有效假设；没有使用 stub 代替。",
            "",
            f"运行状态：`{model_status}`。",
        ])
    else:
        lines.extend([
            f"- 状态：`{hypothesis['status']}`。",
            f"- 观察问题：{hypothesis['problem_observed']}",
            f"- 理由：{hypothesis['rationale']}",
            f"- 验证计划：{hypothesis['validation_plan']}",
            f"- 建议所需数据：{'；'.join(hypothesis['required_data']) or '未额外列出'}",
            f"- Agent 声明限制：{'；'.join(hypothesis['known_limitations']) or '未额外列出；策环数据限制仍然适用'}",
            f"- 受控参数检查：`{proposal['status'] if proposal else 'NOT_CHECKED'}`。",
            "",
            "### 引用证据",
            "",
        ])
        catalog = {item["fact_id"]: item for item in summary["evidence_catalog"]}
        for evidence in hypothesis["evidence"]:
            fact = catalog[evidence["source_fact_id"]]
            lines.append(
                f"- `{evidence['source_fact_id']}` [{evidence['evidence_status']}]："
                f"{fact['statement']}（来源 SHA-256 `{fact['artifact_sha256']}`）"
            )
        if proposal:
            lines.extend(["", "### 参数或规则处理", ""])
            for item in proposal["items"]:
                lines.append(f"- `{item.get('config_path', 'UNKNOWN')}`：{item['status']}。{item['reason']}")
    usage = (model_audit or {}).get("response_usage")
    calls = (model_audit or {}).get("attempt_count", 0)
    lines.extend([
        "",
        "## 调用与执行边界",
        "",
        f"- 模型调用次数：{calls}；供应商返回 token usage：`{json.dumps(usage, ensure_ascii=False) if usage is not None else 'UNKNOWN/NOT_RETURNED'}`；费用：未知（没有可靠费率）。",
        "- 本次未运行真实行情回测，未计算策略收益，未修改有效策略，未连接券商，未生成委托。",
        "- 本研究使用真实行情的聚合摘要，不代表经验证的策略收益或交易建议。",
        "",
        "## 输入文件与哈希",
        "",
        *[f"- `{item['source_id']}`：`{item['path']}`；SHA-256 `{item['sha256']}`。" for item in summary["sources"]],
        "",
        "## 策环准备的下一项数据诊断（程序建议，非模型事实）",
        "",
        "先核实 SH603268 观察窗口内停牌状态及复权价格口径，再冻结一个同区间对照：分别按既有停牌处理与严格剔除停牌观察窗口计算信号排名；在历史时点可得性、规则、费用和公司行动门槛未解决前，只作为数据/信号诊断，不报告正式收益。",
        "",
    ])
    return "\n".join(lines)


def _load_frozen_summary(
    path: str | Path,
    *,
    repository_root: Path = REPO_ROOT,
    verify_reconstruction: bool = True,
    summary_rebuilder: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repository_root = repository_root.resolve()
    summary_path = Path(path).expanduser()
    summary_path = (summary_path if summary_path.is_absolute() else repository_root / summary_path).resolve()
    ignored_root = (repository_root / "outputs").resolve()
    try:
        summary_path.relative_to(ignored_root)
    except ValueError:
        raise ValueError("真实摘要只能从 Git 忽略的 outputs/ 目录加载") from None
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("schema") != "cehuan.real_market_research_summary.v1":
        raise ValueError("摘要文件 schema 无效")
    if summary.get("classification") != REAL_SUMMARY_CLASSIFICATION:
        raise ValueError("摘要分类不是真实行情研究摘要")
    sources = summary.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("冻结摘要缺少原始来源清单")
    required_source_ids = {
        "bars", "snapshot_manifest", "quality", "universe", "universe_audit",
        "point_in_time_quality", "strategy_config", "trade_audit", "candidate_ranks",
    }
    if {item.get("source_id") for item in sources if isinstance(item, dict)} != required_source_ids:
        raise ValueError("冻结摘要来源清单不完整或含未识别项")
    for source in sources:
        source_path = (repository_root / source["path"]).resolve()
        try:
            source_path.relative_to(repository_root)
        except ValueError:
            raise ValueError("冻结摘要来源路径越出项目目录") from None
        if not source_path.is_file() or sha256_file(source_path) != source.get("sha256"):
            raise ValueError(f"冻结摘要来源文件缺失或哈希变化：{source.get('source_id')}")
    source_hashes = {item["sha256"] for item in sources}
    for fact in summary.get("evidence_catalog", []):
        if fact.get("artifact_sha256") not in source_hashes:
            raise ValueError("冻结摘要 evidence_catalog 未绑定到来源文件")
    safety = summary.get("safety", {})
    if any(safety.get(key) is not False for key in (
        "account_data_included", "broker_fill_files_included",
        "raw_market_bars_included_in_agent_input", "orders_generated",
    )):
        raise ValueError("摘要安全标记不满足脱敏研究输入边界，拒绝发送")
    if verify_reconstruction:
        rebuilder = summary_rebuilder or build_real_market_summary
        rebuilt = rebuilder(summary.get("summary_id", ""))
        if summary != rebuilt:
            raise ValueError("冻结摘要与当前来源文件经策环适配器重建的内容不一致；拒绝发送，需重新生成摘要")
    return summary


def run_real_summary_research(*, output_root: str | Path = "outputs/real-market-research",
                              agent_config_path: str | Path | None = None,
                              summary_path: str | Path | None = None,
                              max_retries: int | None = None) -> Path:
    """Build a private fact pack and make one explicit LLM-mode request if configured."""
    root = Path(output_root).expanduser()
    root = (root if root.is_absolute() else REPO_ROOT / root).resolve()
    ignored_root = (REPO_ROOT / "outputs").resolve()
    try:
        root.relative_to(ignored_root)
    except ValueError:
        raise ValueError("真实摘要研究产物只能写入 Git 忽略的 outputs/ 目录") from None

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    run_id = f"real-research-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    summary_id = f"summary-{run_id.removeprefix('real-research-')}"
    if summary_path is None:
        summary = build_real_market_summary(summary_id=summary_id)
        summary_json, summary_markdown = write_real_market_summary(summary, root / run_id)
    else:
        frozen_path = Path(summary_path).expanduser()
        frozen_path = (frozen_path if frozen_path.is_absolute() else REPO_ROOT / frozen_path).resolve()
        summary = _load_frozen_summary(frozen_path)
        run_dir = root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        summary_json = run_dir / "market_summary.json"
        shutil.copyfile(frozen_path, summary_json)
        frozen_markdown = frozen_path.with_name("market_summary.md")
        summary_markdown = run_dir / "market_summary.md"
        if frozen_markdown.is_file():
            shutil.copyfile(frozen_markdown, summary_markdown)
        else:
            summary_markdown.write_text("# 真实行情研究摘要\n\n摘要已从冻结 JSON 载入。\n", encoding="utf-8")
    summary_digest = sha256_file(summary_json)
    config_path = REPO_ROOT / "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/run-committed-b60a7f0/config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    agent_input = _build_agent_input(summary, summary_digest, config, run_id)
    run_dir = summary_json.parent
    input_path = run_dir / "agent_input.json"
    _write_json(agent_input, input_path)
    input_digest = sha256_file(input_path)

    settings = _read_settings(agent_config_path)
    retry_limit = int(settings.get("max_model_retries", 1)) if max_retries is None else max_retries
    if retry_limit < 0 or retry_limit > 1:
        raise ValueError("本次真实摘要研究最多允许 1 次受控校验重试")

    model_audit: dict[str, Any] | None = None
    hypothesis: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    model_status = "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"
    provider_audit: dict[str, Any] = {
        "requested_mode": "llm",
        "selected_mode": "llm",
        "provider": None,
        "model": os.environ.get("CEHUAN_LLM_MODEL", "") or settings.get("model") or None,
        "api_key_configured": bool(os.environ.get("CEHUAN_LLM_API_KEY")),
        "base_url_configured": bool(os.environ.get("CEHUAN_LLM_BASE_URL") or settings.get("base_url")),
        "call_count": 0,
        "usage": None,
        "cost_status": "UNKNOWN_NO_RELIABLE_RATE",
    }
    model = None
    try:
        model, selected = resolve_research_model(settings, "llm")
    except ValueError as exc:
        # Missing env settings are recorded without attempting a stub or revealing any secret.
        provider_audit["configuration_error"] = str(exc)
        model_status = "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"

    if model is not None:
        provider_audit.update(selected)
        provider_audit.update({"provider": model.provider, "model": model.model})
        try:
            hypothesis, model_audit = generate_validated_hypothesis(
                model,
                input_payload=agent_input,
                phase="initial",
                input_data_digest=input_digest,
                experiment_id=f"{run_id}-proposed-validation",
                max_retries=retry_limit,
            )
            provider_audit["call_count"] = int(model_audit["attempt_count"])
            provider_audit["usage"] = model_audit.get("response_usage")
            _record_usage(provider_audit, model_audit)
            proposal = _proposal_review(hypothesis, config)
            if proposal["status"] == "REJECTED_CHANGE_COUNT":
                provider_audit["hypothesis_status"] = "REJECTED_CHANGE_COUNT"
                model_status = "MODEL_OUTPUT_REJECTED_PARAMETER_VALIDATION"
            elif proposal["status"] == "REJECTED_SUPPORTED_PARAMETER_CHANGE":
                provider_audit["hypothesis_status"] = "REJECTED_SUPPORTED_PARAMETER_CHANGE"
                model_status = "MODEL_OUTPUT_REJECTED_PARAMETER_VALIDATION"
            elif proposal["status"] == "PENDING_IMPLEMENTATION":
                provider_audit["hypothesis_status"] = proposal["status"]
                model_status = "COMPLETED_REAL_LLM_PENDING_IMPLEMENTATION"
            else:
                provider_audit["hypothesis_status"] = "VALIDATED_STRUCTURED_OUTPUT"
                model_status = "COMPLETED_REAL_LLM_NEEDS_DATA"
            _write_json(hypothesis, run_dir / "agent_hypothesis.json")
            _write_json(proposal, run_dir / "proposal_review.json")
        except ResearchHypothesisRejected as exc:
            model_audit = exc.audit
            provider_audit["call_count"] = int(model_audit.get("attempt_count", 0))
            provider_audit["usage"] = model_audit.get("response_usage")
            _record_usage(provider_audit, model_audit)
            if model_audit.get("response_classification") == "MODEL_REQUEST_FAILED":
                provider_audit["hypothesis_status"] = "REQUEST_FAILED"
                model_status = "MODEL_REQUEST_FAILED"
            else:
                provider_audit["hypothesis_status"] = "REJECTED_LOCAL_VALIDATION"
                model_status = "MODEL_OUTPUT_REJECTED"
            _write_json({
                "status": model_status,
                "reason": str(exc),
                "model_audit": model_audit,
            }, run_dir / "agent_rejection.json")
        except Exception as exc:
            # Avoid persisting a provider exception body or URL; it could echo request context.
            model_status = "MODEL_REQUEST_FAILED"
            provider_audit["hypothesis_status"] = "REQUEST_FAILED"
            provider_audit["failure_type"] = type(exc).__name__
            provider_audit["failure_detail"] = "请求未完成或研究输出无法校验；敏感响应内容未保存。"

    model_audit_path = run_dir / "model_audit.json"
    _write_json({"provider": provider_audit, "request_audit": model_audit}, model_audit_path)
    report_path = run_dir / "research_report.md"
    report_path.write_text(_report(summary, hypothesis, proposal, model_status, model_audit), encoding="utf-8")
    manifest = {
        "schema": "cehuan.real_summary_research_run.v1",
        "research_id": run_id,
        "classification": REAL_SUMMARY_CLASSIFICATION,
        "status": model_status,
        "files": {
            path.name: sha256_file(path)
            for path in (summary_json, summary_markdown, input_path, model_audit_path, report_path)
        },
        "summary_source_files": summary["sources"],
        "model": provider_audit,
        "agent_input_sha256": input_digest,
        "backtest_run": False,
        "formal_baseline": "NOT_READY",
        "active_strategy_modified": False,
        "broker_connected": False,
        "orders_generated": False,
    }
    if hypothesis is not None:
        manifest["files"]["agent_hypothesis.json"] = sha256_file(run_dir / "agent_hypothesis.json")
        manifest["files"]["proposal_review.json"] = sha256_file(run_dir / "proposal_review.json")
    if model_status == "MODEL_OUTPUT_REJECTED":
        manifest["files"]["agent_rejection.json"] = sha256_file(run_dir / "agent_rejection.json")
    _write_json(manifest, run_dir / "research_manifest.json")
    return run_dir
