"""Freshness-gated daily research brief built from sealed paper rankings."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.config import load_config
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.research.controlled_experiments import build_experiment_capabilities
from cehuan.research.paper_ranking_review import build_paper_ranking_facts
from cehuan.research.real_summary_research import _proposal_review
from cehuan.research.research_cycle import _read_settings
from cehuan.research.research_agent import (
    ModelReply,
    ResearchHypothesisRejected,
    ResearchModel,
    generate_validated_hypothesis,
    resolve_research_model,
)
from cehuan.research.real_market_summary import REPO_ROOT, sha256_file


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_AGENT_CONFIG = REPO_ROOT / "configs" / "research_agent.toml"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "default.toml"
REAL_CLASSIFICATION = "REAL_MARKET_DAILY_RESEARCH_BRIEF"


def _repo_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def _local_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _write_json(value: Any, path: Path) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def latest_completed_session(calendar: pd.DataFrame, now: datetime | None = None) -> str:
    """Return the latest calendar-open day whose close has elapsed locally."""
    local_now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    normalized = normalize_trading_calendar(calendar)
    cutoff = local_now.date()
    if local_now.time().replace(tzinfo=None) < time(15, 5):
        from datetime import timedelta

        cutoff -= timedelta(days=1)
    completed = normalized.loc[
        normalized["is_open"].fillna(False)
        & normalized["trade_date"].map(lambda day: day <= cutoff)
    ]
    if completed.empty:
        raise ValueError("交易日历中没有可确认的已完成交易日")
    return completed["trade_date"].max().isoformat()


def _load_calendar(path: Path, now: datetime | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"交易日历不存在：{path}")
    raw = pd.read_csv(path, low_memory=False)
    normalized = normalize_trading_calendar(raw)
    source_values = sorted(set(normalized["source"].astype(str)))
    retrieved_values = sorted(set(normalized["retrieved_at_utc"].dropna().astype(str)))
    info: dict[str, Any] = {
        "path": str(path),
        "sha256": sha256_file(path),
        "source": source_values,
        "retrieved_at_utc": retrieved_values,
        "calendar_start": normalized["trade_date"].min().isoformat() if not normalized.empty else None,
        "calendar_end": normalized["trade_date"].max().isoformat() if not normalized.empty else None,
        "provider_publication_time": "UNVERIFIED",
    }
    capture_path = path.parent / "capture-manifest.json"
    if capture_path.is_file():
        capture = json.loads(capture_path.read_text(encoding="utf-8"))
        recorded = capture.get("normalized_sha256")
        info["capture_manifest"] = {"path": str(capture_path), "sha256": sha256_file(capture_path)}
        info["capture_status"] = capture.get("status", "UNKNOWN")
        info["capture_finished_at_local"] = capture.get("capture_finished_at_asia_shanghai")
        info["provider_publication_time"] = capture.get("provider_publication_evidence", "UNVERIFIED")
        info["normalized_hash_binding"] = "VERIFIED" if recorded == info["sha256"] else "FAILED"
        if recorded != info["sha256"]:
            raise ValueError("交易日历与同目录 capture-manifest 的规范文件 SHA-256 不匹配")
    else:
        info["capture_manifest"] = None
        info["capture_status"] = "NOT_PROVIDED"
        info["normalized_hash_binding"] = "UNVERIFIED"
    info["latest_completed_session"] = latest_completed_session(normalized, now)
    return normalized, info


def _scan_rank_manifests(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    for manifest_path in root.rglob("paper-ranking-manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("record_type") != "PAPER_RANKING_RUN" or manifest.get("classification") != "FORWARD_PAPER":
            continue
        source_capture_path = (manifest.get("input_paths") or {}).get("source_capture_manifest")
        source_capture = None
        if source_capture_path:
            try:
                source_capture = json.loads(Path(source_capture_path).expanduser().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                source_capture = None
        results.append({
            "path": str(manifest_path.parent),
            "manifest_path": str(manifest_path),
            "manifest_sha256": sha256_file(manifest_path),
            "signal_date": manifest.get("signal_date"),
            "status": manifest.get("status", "UNKNOWN"),
            "finished_at_utc": manifest.get("finished_at_utc") or manifest.get("ranking_generated_at_utc"),
            "source_capture_manifest": source_capture_path,
            "source_capture_sha256": sha256_file(Path(source_capture_path).expanduser()) if source_capture_path and Path(source_capture_path).expanduser().is_file() else None,
            "source_capture_started_local": _local_timestamp((source_capture or {}).get("capture_started_at_utc")),
            "source_capture_finished_local": _local_timestamp((source_capture or {}).get("capture_finished_at_utc") or (source_capture or {}).get("captured_at_utc")),
            "blocking_reasons": manifest.get("blocking_reasons") or manifest.get("reasons") or [],
        })
    return results


def _scan_incomplete_ranking_statuses(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    results: list[dict[str, Any]] = []
    for status_path in root.rglob("ranking-status.json"):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status.get("status") != "NEEDS_DATA" or not status.get("signal_date"):
            continue
        capture_path = status_path.parent / "capture-manifest.json"
        try:
            capture = json.loads(capture_path.read_text(encoding="utf-8")) if capture_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            capture = {}
        bars_path = status_path.parent / "normalized" / "bars.csv"
        bars_coverage: dict[str, Any] = {"path": str(bars_path) if bars_path.is_file() else None}
        if bars_path.is_file():
            try:
                bars = pd.read_csv(bars_path, usecols=lambda column: column in {"trade_date", "symbol"}, dtype=str)
                bars_coverage.update({
                    "sha256": sha256_file(bars_path),
                    "row_count": int(len(bars)),
                    "latest_trade_date": str(bars["trade_date"].max()) if "trade_date" in bars and not bars.empty else None,
                    "signal_date_row_count": int(bars["trade_date"].astype(str).eq(str(status["signal_date"])).sum()) if "trade_date" in bars else None,
                })
            except (OSError, ValueError, pd.errors.ParserError):
                bars_coverage["read_status"] = "UNVERIFIED"
        else:
            bars_coverage["read_status"] = "NOT_PRESENT"
        results.append({
            "path": str(status_path.parent),
            "status_path": str(status_path),
            "sha256": sha256_file(status_path),
            "signal_date": status.get("signal_date"),
            "status": status.get("status"),
            "blocking_reasons": status.get("blocking_reasons") or [],
            "rankings_generated": status.get("rankings_generated"),
            "capture_manifest_path": str(capture_path) if capture_path.is_file() else None,
            "capture_manifest_sha256": sha256_file(capture_path) if capture_path.is_file() else None,
            "capture_started_at_local": capture.get("created_at_asia_shanghai"),
            "capture_finished_at_local": _local_timestamp(capture.get("capture_finished_at_utc")),
            "bars_coverage": bars_coverage,
        })
    return sorted(results, key=lambda item: (str(item["signal_date"]), str(item["path"])))


def _make_agent_input(
    facts: Mapping[str, Any], *, run_id: str, config_path: Path, summary_path: Path,
) -> dict[str, Any]:
    config = load_config(config_path)
    strategy = config.get("strategy", {})
    summary_facts = {
        "signal_date": facts["signal_date"],
        "scope": facts["scope"],
        "window_gate": facts["window_gate"],
        "ranking": facts["ranking"],
        "data_capture": facts["data_capture"],
        "limitations": facts["limitations"],
        "execution_boundaries": facts["execution_boundaries"],
    }
    evidence_statuses = {
        "DATA_CAPTURE": "RECORDED",
        "LIMITATIONS": "UNVERIFIED",
    }
    evidence_catalog = [
        {
            "fact_id": fact_id,
            "statement": item["claim"],
            "artifact_sha256": item["sha256"],
            "evidence_status": evidence_statuses.get(fact_id, "RECONSTRUCTED"),
        }
        for fact_id, item in sorted(facts["evidence_catalog"].items())
    ]
    summary_doc = {
        "schema": "cehuan.daily_research_summary.v1",
        "summary_id": run_id,
        "facts": summary_facts,
        "evidence_catalog": evidence_catalog,
    }
    summary_digest = hashlib.sha256(
        (json.dumps(summary_doc, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    ).hexdigest()
    capability = build_experiment_capabilities(config)
    return {
        "schema_version": 1,
        "research_id": run_id,
        "objective": "基于最近已完成交易日的程序事实提出一条待研究、需人工审核的结构化假设；不得形成策略结论或交易指令。",
        "scope": {
            "market": "CN_A_SHARE",
            "start_date": facts["signal_date"],
            "end_date": facts["signal_date"],
            "universe_id": "small-scope-" + hashlib.sha256(",".join(facts["scope"]["symbols"]).encode()).hexdigest()[:16],
        },
        "data_trust": {
            "input_file_integrity": {"status": "RECONSTRUCTED", "scope": "排名运行清单及输入文件哈希复核"},
            "canonical_data_quality": {"status": "RECONSTRUCTED", "scope": "冻结排名和 21 日窗口审计"},
            "checkpoint_binding": {"status": "NOT_USED", "scope": "每日纸面候选简报不使用回测 checkpoint"},
            "historical_availability_and_vendor_vintage": {"status": "UNVERIFIED", "scope": "供应商发布时间未核实"},
            "market_rule_coverage": {"status": "FALLBACK_USED", "scope": "未完成全市场有效日期规则核验"},
            "fee_coverage": {"status": "UNVERIFIED", "scope": "不计算收益或费用"},
            "corporate_action_coverage": {"status": "INCOMPLETE_UNVERIFIED", "scope": "不完整；无记录不等于无事件"},
            "formal_baseline": {"status": "NOT_READY"},
            "overall": "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST",
        },
        "frozen_strategy": {
            "strategy_id": str(strategy.get("name", "UNKNOWN")),
            "config_sha256": sha256_file(config_path),
            "parameters": {
                key: strategy.get(key)
                for key in ("lookback_sessions", "rebalance_sessions", "max_positions", "max_position_weight", "max_total_exposure", "min_momentum")
            },
        },
        "prior_experiments": [],
        "known_data_gaps": list(facts["limitations"]),
        "allowed_actions": ["PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"],
        "forbidden_actions": [
            "MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL",
            "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT",
        ],
        "experiment_capabilities": capability,
        "market_research_summary": {
            "schema": "cehuan.real_market_research_summary_input.v1",
            "summary_id": run_id,
            "summary_sha256": summary_digest,
            "classification": "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST",
            "facts": summary_facts,
            "evidence_catalog": evidence_catalog,
        },
        "_summary_document": summary_doc,
        "_summary_path": str(summary_path),
    }


class _DailyBriefStubModel:
    """Deterministic, visibly non-LLM adapter used for local flow validation."""

    provider = "stub"
    model = "cehuan-daily-brief-stub-v1"

    def generate(self, *, phase: str, prompt: str, context: Mapping[str, Any]) -> ModelReply:
        payload = context["research_input"]
        summary = payload["market_research_summary"]
        evidence = summary["evidence_catalog"][0]
        capabilities = payload["experiment_capabilities"]["supported_parameters"][0]
        current = capabilities["current_value"]
        minimum, maximum = capabilities["minimum_inclusive"], capabilities["maximum_inclusive"]
        next_value = current + 1 if current < maximum else current - 1
        proposal = {
            "schema_version": 1,
            "hypothesis_id": f"{payload['research_id']}-{phase}-hypothesis",
            "experiment_id": context["experiment_id"],
            "input_data_digest": context["input_data_digest"],
            "problem_observed": "Stub 仅验证每日简报的结构化输入与证据绑定，不是市场研究结论。",
            "proposed_changes": [{"config_path": capabilities["config_path"], "current_value": current, "proposed_value": next_value, "reason": "合成流程验证；没有应用。"}],
            "rationale": "仅用于检查格式、哈希和安全边界。",
            "evidence": [{
                "description": evidence["statement"],
                "artifact_sha256": evidence["artifact_sha256"],
                "evidence_status": evidence["evidence_status"],
                "source_fact_id": evidence["fact_id"],
            }],
            "required_data": ["新鲜且冻结的交易日数据", "逐证券状态与来源时点证据"],
            "known_limitations": ["Stub 输出不代表真实模型解释。", "正式基线 NOT_READY。"],
            "validation_plan": "不执行任何实验；仅验证本地简报格式。",
            "status": "NEEDS_DATA",
            "human_review_required": True,
            "may_modify_active_strategy": False,
            "may_generate_orders": False,
        }
        return ModelReply(json.dumps(proposal, ensure_ascii=False), self.provider, self.model)


def _report_markdown(
    *, status: str, target_date: str, run_time: str, calendar_info: Mapping[str, Any],
    facts: Mapping[str, Any] | None, hypothesis: Mapping[str, Any] | None,
    proposal_review: Mapping[str, Any] | None, model_audit: Mapping[str, Any],
    blocking_reasons: list[str], latest_ranking: Mapping[str, Any] | None,
) -> str:
    lines = [
        f"# {target_date} 策环每日人工研究简报",
        "",
        f"- 简报状态：`{status}`；最近已完成交易日：`{target_date}`；本机生成时间：`{run_time}`（Asia/Shanghai）。",
        f"- 交易日历来源：`{', '.join(calendar_info.get('source', [])) or 'UNKNOWN'}`；日历字段抓取时间（UTC）：`{', '.join(calendar_info.get('retrieved_at_utc', [])) or 'UNKNOWN'}`；抓取清单记录的本机完成时间：`{calendar_info.get('capture_finished_at_local') or 'UNKNOWN'}`；供应商发布时间：`{calendar_info.get('provider_publication_time', 'UNVERIFIED')}`。",
        "- 性质：研究候选简报，不是个人交易建议，不生成可执行股数或委托。正式基线 `NOT_READY`。",
        "",
        "## 程序核实的事实",
        "",
    ]
    if facts is None:
        lines.extend([
            "- 目前没有与最近已完成交易日匹配、且状态为 CAPTURED 的封存 FORWARD_PAPER 排名；因此本简报没有排名事实，也没有请求模型。",
            f"- 最近发现的已封存排名：`{latest_ranking.get('signal_date') if latest_ranking else 'NONE'}`，状态 `{latest_ranking.get('status') if latest_ranking else 'NONE'}`；运行目录 `{latest_ranking.get('path') if latest_ranking else 'NONE'}`；排名生成时间 UTC `{latest_ranking.get('finished_at_utc') if latest_ranking else 'UNKNOWN'}`；行情本机抓取区间 `{latest_ranking.get('source_capture_started_local') if latest_ranking else 'UNKNOWN'}` 至 `{latest_ranking.get('source_capture_finished_local') if latest_ranking else 'UNKNOWN'}`。该旧结果不用于代表当前交易日。",
            "- 当前日期尚未在已登记、带来源清单的输入链中核验到当天行情、对应点时成员、逐日上市/ST/停牌状态、完整 21 个开市日规范价格窗口及通过门控的排名；未登记的原始文件不据此推断为存在或不存在。",
        ])
    else:
        ranking = facts["ranking"]
        lines.extend([
            f"- 排名数据日期：`{facts['signal_date']}`；研究池 `{facts['scope']['label']}`，共 {facts['scope']['symbol_count']} 只；原候选 {ranking['original_count']} 条，H1 候选 {ranking['h1_count']} 条。",
            f"- 原 Top 5：{', '.join(row['symbol'] for row in ranking['original_top5']) or '无'}。",
            f"- H1 Top 5：{', '.join(row['symbol'] for row in ranking['h1_top5']) or '无'}。",
            f"- H1 窗口检查通过 {facts['window_gate']['passed_symbol_count']}/{facts['scope']['symbol_count']}；直接差异：{ranking['direct_reason']}",
            f"- 行情来源：{', '.join(facts['data_capture']['price_sources']) or 'UNKNOWN'}；状态来源：{facts['data_capture']['status_source']}；本机行情抓取区间：{facts['data_capture']['local_capture_interval']}；供应商发布时间：{facts['data_capture']['provider_publication_time']}。",
        ])
    lines.extend(["", "## 模型研究假设（仅建议，不是策略结论）", ""])
    if hypothesis is None:
        lines.append(f"- 未生成假设：`{model_audit.get('status', status)}`。本次模型请求数 {model_audit.get('call_count', 0)}；费用状态 `{model_audit.get('cost_status', 'UNKNOWN')}`。")
    else:
        lines.extend([
            f"- 模型路径：`{model_audit.get('selected_mode')}`；provider/model：`{model_audit.get('provider') or 'UNKNOWN'}` / `{model_audit.get('model') or 'UNKNOWN'}`；调用次数 {model_audit.get('call_count', 0)}；usage `{json.dumps(model_audit.get('usage'), ensure_ascii=False, sort_keys=True) if model_audit.get('usage') else 'UNKNOWN'}`；费用 `{model_audit.get('cost_status', 'UNKNOWN_NO_RELIABLE_RATE')}`。",
            "- 本地产生的 Stub 内容仅用于流程测试，不是 LLM 输出。" if model_audit.get("selected_mode") == "stub" else "- 以下文字是模型建议，不能视为程序事实或已验证策略结论。",
            f"- 状态：`{hypothesis['status']}`；待研究问题：{hypothesis['problem_observed']}",
            f"- 假设理由：{hypothesis['rationale']}",
            f"- 拟研究的单变量：{json.dumps(hypothesis['proposed_changes'], ensure_ascii=False)}；程序审核：`{proposal_review.get('status') if proposal_review else 'NOT_REVIEWED'}`。没有应用到有效策略。",
            f"- 验证计划：{hypothesis['validation_plan']}",
            f"- 所引证据：{json.dumps(hypothesis['evidence'], ensure_ascii=False)}",
        ])
    lines.extend([
        "",
        "## 未核实项",
        "",
        *(f"- {reason}" for reason in (blocking_reasons if facts is None else facts["limitations"])),
        "",
        "## 需要人工检查的风险",
        "",
        "- 确认数据日期与本机抓取时间；检查 ST/停牌/上市状态是否有来源及时间证据；核对窗口缺日、复权口径和代码身份；不得把 H1 排名差异解释成收益或可成交性改善。",
        "- 不计算收益、NAV 或回撤；不修改当前策略；不连接华泰；不生成订单。",
        "",
        "## 输入与产物哈希",
        "",
        f"- 交易日历：`{calendar_info['path']}`；SHA-256 `{calendar_info['sha256']}`。",
    ])
    if calendar_info.get("capture_manifest"):
        lines.append(
            f"- 交易日历抓取清单：`{calendar_info['capture_manifest']['path']}`；SHA-256 `{calendar_info['capture_manifest']['sha256']}`。"
        )
    if facts is not None and isinstance(facts.get("sources"), Mapping):
        lines.extend([
            f"- 排名运行清单：`{facts['sources']['ranking_run_manifest']['path']}`；SHA-256 `{facts['sources']['ranking_run_manifest']['sha256']}`。",
            f"- 抓取清单：`{facts['sources']['input_capture_manifest']['path']}`；SHA-256 `{facts['sources']['input_capture_manifest']['sha256']}`。",
            *[
                f"- `{name}`：SHA-256 `{digest}`。"
                for name, digest in sorted(facts["sources"]["files"].items())
            ],
        ])
    return "\n".join(lines) + "\n"


def run_daily_research_brief(
    *, calendar_path: str | Path, ranking_root: str | Path = "data/private/experiments",
    output_root: str | Path = "outputs/daily-research-briefs", model_mode: str,
    agent_config_path: str | Path | None = None, config_path: str | Path = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> Path:
    """Build a freshness-gated brief from the newest completed session and sealed rank."""
    local_now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    started = local_now.isoformat(timespec="seconds")
    calendar_file = _repo_path(calendar_path)
    rank_root = _repo_path(ranking_root)
    root = _repo_path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    if model_mode not in {"stub", "llm"}:
        raise ValueError("model_mode 必须显式为 stub 或 llm")
    _, calendar_info = _load_calendar(calendar_file, local_now)
    target_date = str(calendar_info["latest_completed_session"])
    manifests = _scan_rank_manifests(rank_root)
    incomplete_statuses = _scan_incomplete_ranking_statuses(rank_root)
    latest_captured = sorted(
        (item for item in manifests if item["status"] in {"CAPTURED", "CAPTURED_WITH_EXCLUSIONS"}),
        key=lambda item: (str(item.get("signal_date") or ""), str(item.get("finished_at_utc") or "")),
    )
    latest_ranking = latest_captured[-1] if latest_captured else None
    matching = [
        item for item in manifests
        if item.get("signal_date") == target_date and item["status"] in {"CAPTURED", "CAPTURED_WITH_EXCLUSIONS"}
    ]
    matching.sort(key=lambda item: str(item.get("finished_at_utc") or ""))
    facts: dict[str, Any] | None = None
    blocking_reasons: list[str] = []
    model_audit: dict[str, Any] = {
        "requested_mode": model_mode,
        "selected_mode": None,
        "provider": None,
        "model": None,
        "call_count": 0,
        "retry_count": 0,
        "usage": None,
        "cost_status": "UNKNOWN_NO_RELIABLE_RATE",
        "api_key_logged": False,
        "account_data_sent": False,
        "broker_data_sent": False,
    }
    hypothesis: dict[str, Any] | None = None
    proposal_review: dict[str, Any] | None = None
    status = "NEEDS_DATA"
    if not matching:
        same_day = [item for item in manifests if item.get("signal_date") == target_date]
        blocking_reasons.append(f"最近已完成交易日 {target_date} 没有 CAPTURED 的 FORWARD_PAPER 排名。")
        if same_day:
            blocking_reasons.extend(
                f"{item['status']} 产物 {item['path']}：{item['blocking_reasons'] or ['清单未记录具体原因']}"
                for item in same_day
            )
        blocking_reasons.extend([
            "需要新鲜的研究日行情、与固定研究池匹配的点时成员、逐日上市/ST/停牌状态、完整 21 个开市日的价格窗口及绑定这些输入的 capture manifest。",
            "供应商发布时间必须单独记录；没有证据时保持 UNVERIFIED。",
        ])
        if incomplete_statuses:
            latest_incomplete = incomplete_statuses[-1]
            reason_labels = {
                "signal-date membership does not confirm all ten frozen securities": "信号日名单没有确认冻结的全部 10 只证券",
                "10 security-session rows have unknown listing/ST/suspension state": "10 个证券-交易日的上市/ST/停牌状态未知",
            }
            reasons = [reason_labels.get(str(reason), str(reason)) for reason in latest_incomplete["blocking_reasons"]]
            bars_coverage = latest_incomplete.get("bars_coverage", {})
            bars_summary = (
                f"该次尝试的规范行情 {bars_coverage.get('row_count')} 行，最新日期 {bars_coverage.get('latest_trade_date')}，"
                f"信号日行情行数 {bars_coverage.get('signal_date_row_count')}，行情文件 SHA-256 {bars_coverage.get('sha256')}。"
                if bars_coverage.get("sha256") else "该次尝试未提供可核验的规范行情文件。"
            )
            blocking_reasons.append(
                "最近一次有界纸面采集尝试："
                f"{latest_incomplete['signal_date']} {latest_incomplete['status']}；"
                f"本机采集时间 {latest_incomplete['capture_started_at_local'] or 'UNKNOWN'} 至 {latest_incomplete['capture_finished_at_local'] or 'UNKNOWN'}；"
                f"原因：{'；'.join(reasons) if reasons else '未记录具体原因'}；"
                f"{bars_summary}产物 {latest_incomplete['path']}，状态文件 SHA-256 {latest_incomplete['sha256']}，"
                f"采集清单 SHA-256 {latest_incomplete['capture_manifest_sha256'] or 'UNKNOWN'}。"
            )
    else:
        selected = matching[-1]
        try:
            facts = build_paper_ranking_facts(ranking_dir=selected["path"])
            if facts["signal_date"] != target_date:
                raise ValueError("排名日期与最近已完成交易日不一致")
            generated_local = datetime.fromisoformat(facts["ranking_generated_at_local"])
            capture_start_local = facts["data_capture"]["local_capture_interval"][0]
            capture_end_local = facts["data_capture"]["local_capture_interval"][1]
            if generated_local.date().isoformat() != target_date:
                raise ValueError("排名生成发生在信号日之后；历史回填不能用作当日前瞻简报")
            if not capture_start_local or not capture_end_local or any(
                datetime.fromisoformat(value).date().isoformat() != target_date
                for value in (capture_start_local, capture_end_local)
            ):
                raise ValueError("行情抓取时间未能证明来自信号日当天；历史回填不能用作当日前瞻简报")
        except Exception as exc:
            blocking_reasons.append(f"当日排名存在但无法通过封存输入重建：{type(exc).__name__}: {str(exc)[:200]}；没有发起模型请求。")
            facts = None
        if facts is not None:
            run_id = f"daily-brief-{target_date.replace('-', '')}-{uuid.uuid4().hex[:8]}"
            config_file = _repo_path(config_path)
            summary_path = root / run_id / "daily-research-summary.json"
            agent_input = _make_agent_input(facts, run_id=run_id, config_path=config_file, summary_path=summary_path)
            run_dir = root / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            _write_json(agent_input.pop("_summary_document"), summary_path)
            agent_input.pop("_summary_path", None)
            input_path = run_dir / "agent-input.json"
            _write_json(agent_input, input_path)
            input_digest = sha256_file(input_path)
            _write_json(facts, run_dir / "program-facts.json")
            model_audit["selected_mode"] = model_mode
            model_audit["input_sha256"] = input_digest
            model: ResearchModel | None = None
            try:
                settings = _read_settings(agent_config_path or DEFAULT_AGENT_CONFIG)
                if model_mode == "stub":
                    model = _DailyBriefStubModel()
                else:
                    try:
                        model, resolved = resolve_research_model(settings, "llm")
                    except ValueError:
                        model_audit["failure_category"] = "MISSING_OR_INVALID_LLM_CONFIGURATION"
                        model_audit["api_key_logged"] = False
                        model_audit["account_data_sent"] = False
                        model_audit["status"] = "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"
                        model = None
                    if model is not None:
                        model_audit.update({
                            "provider": resolved.get("provider"),
                            "model": resolved.get("model"),
                        })
                if model is None:
                    status = "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"
                else:
                    # No automatic retry in the daily brief: one run means at most one request.
                    hypothesis, hypothesis_audit = generate_validated_hypothesis(
                        model,
                        input_payload=agent_input,
                        phase="initial",
                        input_data_digest=input_digest,
                        experiment_id=f"{run_id}-human-review-only",
                        max_retries=0,
                    )
                    model_audit.update({
                        "provider": hypothesis_audit.get("provider"),
                        "model": hypothesis_audit.get("model"),
                        "call_count": hypothesis_audit.get("attempt_count", 0),
                        "usage": hypothesis_audit.get("response_usage"),
                        "prompt_sha256": hypothesis_audit.get("prompt_sha256"),
                        "status": "HYPOTHESIS_VALIDATED",
                    })
                    if model_mode == "llm":
                        proposal_review = _proposal_review(hypothesis, load_config(config_file))
                        status = "COMPLETED_REAL_LLM_NEEDS_DATA"
                    else:
                        proposal_review = {"status": "STUB_FLOW_ONLY_NOT_LLM", "items": []}
                        status = "COMPLETED_STUB_NOT_LLM"
                    _write_json(hypothesis, run_dir / "research-hypothesis.json")
                    _write_json(proposal_review, run_dir / "proposal-review.json")
                    model_audit["research_summary_sha256"] = sha256_file(summary_path)
            except ResearchHypothesisRejected as exc:
                attempt_audit = exc.audit
                model_audit.update({
                    "status": "MODEL_REVIEW_REJECTED",
                    "failure_category": "STRUCTURED_OUTPUT_VALIDATION_FAILED",
                    "call_count": attempt_audit.get("attempt_count", 0),
                    "usage": attempt_audit.get("response_usage"),
                    "prompt_sha256": attempt_audit.get("prompt_sha256"),
                })
                status = "MODEL_REVIEW_REJECTED"
            except Exception as exc:
                model_audit["status"] = "MODEL_REQUEST_FAILED"
                model_audit["failure_type"] = type(exc).__name__
                model_audit["failure_category"] = "CONFIGURATION_OR_REQUEST_FAILED"
                status = "MODEL_REQUEST_FAILED"
            if model_audit.get("call_count", 0) == 0:
                model_audit["call_count"] = 0
            model_audit["cost_status"] = "UNKNOWN_NO_RELIABLE_RATE"
            _write_json(model_audit, run_dir / "model-audit.json")
            facts_output_path = run_dir / "program-facts.json"
            output_paths = [summary_path, input_path, facts_output_path, run_dir / "model-audit.json"]
            if (run_dir / "research-hypothesis.json").exists():
                output_paths.extend([run_dir / "research-hypothesis.json", run_dir / "proposal-review.json"])
            report_path = run_dir / "daily-brief.md"
            report_path.write_text(_report_markdown(
                status=status,
                target_date=target_date,
                run_time=started,
                calendar_info=calendar_info,
                facts=facts,
                hypothesis=hypothesis,
                proposal_review=proposal_review,
                model_audit=model_audit,
                blocking_reasons=blocking_reasons,
                latest_ranking=selected,
            ), encoding="utf-8")
            output_paths.append(report_path)
            manifest = {
                "schema": "cehuan.daily_research_brief.v1",
                "run_id": run_id,
                "status": status,
                "classification": REAL_CLASSIFICATION if model_mode == "llm" else "STUB_PATH_VALIDATION_NOT_LLM",
                "signal_date": target_date,
                "generated_at_local": started,
                "calendar": calendar_info,
                "ranking_dir": selected["path"],
                "ranking_manifest_sha256": selected["manifest_sha256"],
                "model": model_audit,
                "files": {path.name: sha256_file(path) for path in output_paths},
                "formal_baseline": "NOT_READY",
                "returns_computed": False,
                "strategy_modified": False,
                "broker_connected": False,
                "orders_generated": False,
            }
            _write_json(manifest, run_dir / "brief-manifest.json")
            return run_dir

    run_id = f"daily-brief-{target_date.replace('-', '')}-{uuid.uuid4().hex[:8]}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "model-audit.json").write_text(json.dumps({"status": "NOT_CALLED_NO_FRESH_RANKING", **model_audit}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = run_dir / "daily-brief.md"
    report_path.write_text(_report_markdown(
        status=status,
        target_date=target_date,
        run_time=started,
        calendar_info=calendar_info,
        facts=None,
        hypothesis=None,
        proposal_review=None,
        model_audit={"status": status, **model_audit},
        blocking_reasons=blocking_reasons,
        latest_ranking=latest_ranking,
    ), encoding="utf-8")
    manifest = {
        "schema": "cehuan.daily_research_brief.v1",
        "run_id": run_id,
        "status": status,
        "classification": REAL_CLASSIFICATION,
        "signal_date": target_date,
        "generated_at_local": started,
        "calendar": calendar_info,
        "latest_available_ranking": latest_ranking,
        "latest_available_ranking_manifest_sha256": latest_ranking.get("manifest_sha256") if latest_ranking else None,
        "latest_incomplete_ranking_status": incomplete_statuses[-1] if incomplete_statuses else None,
        "matching_rank_count": len(matching),
        "blocking_reasons": blocking_reasons,
        "model": model_audit,
        "files": {
            "model-audit.json": sha256_file(run_dir / "model-audit.json"),
            "daily-brief.md": sha256_file(report_path),
        },
        "formal_baseline": "NOT_READY",
        "returns_computed": False,
        "strategy_modified": False,
        "broker_connected": False,
        "orders_generated": False,
    }
    _write_json(manifest, run_dir / "brief-manifest.json")
    return run_dir
