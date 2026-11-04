from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _inventory(output_dir: Path) -> None:
    import csv

    rows: list[dict[str, Any]] = []
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file() and item.name != "audit-artifact-inventory.csv"):
        rows.append({
            "relative_path": path.relative_to(output_dir).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    with (output_dir / "audit-artifact-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relative_path", "size_bytes", "sha256"])
        writer.writeheader()
        writer.writerows(rows)


def build(audit_dir: Path, config_path: Path) -> dict[str, Any]:
    audit_dir = audit_dir.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    summary_path = audit_dir / "trade-audit-summary.json"
    suspension_path = audit_dir / "suspension-candidate-window.csv"
    target_path = audit_dir / "zero-target-review.csv"
    required = [summary_path, suspension_path, target_path, config_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("生成研究假设缺少输入：" + ", ".join(missing))

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("schema_version") != 2:
        raise ValueError("研究假设生成器要求成交审计 summary schema_version=2")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    suspension = pd.read_csv(suspension_path, keep_default_na=False)
    zero_targets = pd.read_csv(target_path, keep_default_na=False)
    diagnostic = summary.get("suspension_candidate_diagnostic") or {}
    neighbors_path = audit_dir / "suspension-candidate-rank-neighbors.csv"
    if not neighbors_path.is_file():
        raise FileNotFoundError(f"缺少候选排名与执行链：{neighbors_path}")
    neighbors = pd.read_csv(neighbors_path, keep_default_na=False)
    symbol = str(diagnostic.get("symbol", ""))
    candidate = neighbors.loc[neighbors.symbol.astype(str).eq(symbol)]
    if len(candidate) != 1:
        raise ValueError(f"停牌候选链必须唯一，当前匹配 {len(candidate)} 行")
    candidate = candidate.iloc[0]
    if zero_targets.empty:
        raise ValueError("未找到舍入为零的目标，无法生成第二条假设")
    zero = zero_targets.iloc[0]

    frozen_inputs = summary.get("input_hashes", {})
    input_digest = hashlib.sha256(
        json.dumps(frozen_inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    config_digest = frozen_inputs.get("config.json") or _sha256(config_path)
    data_trust = {
        "input_file_integrity": {
            "status": summary.get("research_trust", {}).get("input_file_integrity", "UNVERIFIED"),
            "sha256": input_digest,
            "scope": "审计摘要中的冻结运行输入哈希；候选池历史库存哈希缺失时按摘要说明保留限制。",
        },
        "canonical_data_quality": {
            "status": summary.get("research_trust", {}).get("canonical_data_quality", "UNVERIFIED"),
            "scope": "仅限已分类沪深行情快照的结构检查，不等于供应商历史完整性证明。",
        },
        "checkpoint_binding": {
            "status": "UNVERIFIED",
            "scope": "本假设包绑定审计输入哈希；不声称通过独立 checkpoint 续跑证明。",
        },
        "historical_availability_and_vendor_vintage": {
            "status": "UNVERIFIED",
            "scope": "适配器时间不是历史供应商发布时间证明；原始 BaoStock 响应版本未随规范行情归档。",
        },
        "market_rule_coverage": {
            "status": "FALLBACK_USED",
            "fallback_rows": int(summary.get("market_rule_fallback_rows_in_window", -1)),
            "mode": "CONFIG_FALLBACK_NOT_HISTORICAL_RULE_VERIFICATION",
        },
        "fee_coverage": {
            "status": "UNVERIFIED_FALLBACK_USED",
            "mode": "FLAT_CONFIG_ASSUMPTION",
            "personal_brokerage_rate": "UNAVAILABLE",
        },
        "corporate_action_coverage": {
            "status": "UNVERIFIED_INCOMPLETE_EVENT_COVERAGE",
            "typed_events_consumed": len(summary.get("record_date_entitlement_audit", {}).get("typed_accounting_events_in_frozen_run", [])),
            "scope": "空事件注册表或有限查询不证明无公司行动。",
        },
        "formal_baseline": {"status": "NOT_READY", "interpretation": "仅为开发诊断输入。"},
        "overall": "DEVELOPMENT_DIAGNOSTIC_ONLY_NEEDS_DATA",
    }
    input_payload = {
        "schema_version": 1,
        "research_id": f"{summary.get('experiment_id', 'unknown')}-suspension-zero-target-review",
        "objective": "检验停牌窗口筛选和零股目标现金处理是否产生可复现、风险受控的候选规则差异。",
        "scope": {
            "market": "CN_A_SHARE",
            "start_date": summary["window"]["start"],
            "end_date": summary["window"]["end"],
            "universe_id": "PRIVATE_FROZEN_SHSZ_UNIVERSE_SNAPSHOT",
        },
        "data_trust": data_trust,
        "frozen_strategy": {
            "strategy_id": f"{summary.get('experiment_id', 'unknown')}-frozen",
            "config_sha256": config_digest,
            "parameters": config.get("strategy", {}),
        },
        "prior_experiments": [{
            "experiment_id": summary.get("experiment_id", "UNAVAILABLE"),
            "result_type": "RESEARCH_BACKTEST_DEVELOPMENT_DIAGNOSTIC",
            "summary": "仅用于审计链路与假设生成；不构成独立留出或策略有效性结论。",
            "input_digest": input_digest,
        }],
        "review_summary": {
            "fill_record_count": summary.get("fill_record_count"),
            "order_log_status": summary.get("order_log_status"),
            "suspension_window": diagnostic.get("window_state_counts_including_base"),
            "suspension_return_observations": diagnostic.get("return_observation_state_counts_excluding_base"),
            "suspension_window_alignment": (diagnostic.get("session_window_audit") or {}).get("window_alignment_status"),
            "zero_target_count": summary.get("selected_targets_reconstructed_to_zero_shares"),
            "zero_target_reviews": summary.get("zero_target_reviews", []),
        },
        "known_data_gaps": [
            "该 68 交易日窗口已用于开发问题发现，不能称为独立样本外验证。",
            "历史供应商发布时点与行情 vintage 未验证；项目没有将公告日期转换为精确 known_at。",
            "窗口全市场历史规则使用配置回退，个人华泰费率不可用。",
            "公司行动覆盖不完整，冻结运行没有消费 typed accounting events。",
            "trades.csv 是成交记录；完整委托日志不存在，订单状态未知。",
        ],
        "allowed_actions": ["PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"],
        "forbidden_actions": ["MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL", "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT"],
    }

    evidence_suspension = {
        "description": "从冻结规范行情与排名重建的停牌候选观察窗；供应商 vintage 和历史时点可得性仍未验证。",
        "artifact_sha256": _sha256(suspension_path),
        "evidence_status": "RECONSTRUCTED",
    }
    evidence_zero = {
        "description": "从冻结候选排名、配置回退手数、开盘参考价及模拟成交重建的零股目标行；不是券商订单。",
        "artifact_sha256": _sha256(target_path),
        "evidence_status": "RECONSTRUCTED",
    }
    count_total = (diagnostic.get("window_state_counts_including_base") or {}).get("total_rows", len(suspension))
    count_halted = (diagnostic.get("window_state_counts_including_base") or {}).get("suspended_rows", 0)
    rank = int(candidate["rank"])
    momentum = float(candidate["momentum"])
    h1 = {
        "schema_version": 1,
        "hypothesis_id": "HYP-SUSPENSION-WINDOW-FILTER-001",
        "experiment_id": summary.get("experiment_id", "UNAVAILABLE"),
        "input_data_digest": input_digest,
        "problem_observed": (
            f"审计候选 {symbol} 在信号日排名第 {rank}，动量重建为 {momentum:.12g}；"
            f"其 {count_total} 行（含基准行）窗口中记录 {count_halted} 行停牌。"
            "此窗口只证明状态观测，不证明未来可得性或策略优劣。"
        ),
        "proposed_changes": [{
            "config_path": "research.candidate_window.suspension_policy",
            "current_value": "SIGNAL_DAY_ELIGIBILITY_ONLY_ROW_SHIFT_LOOKBACK",
            "proposed_value": "REQUIRE_COMPLETE_21_SESSION_WINDOW_KNOWN_NOT_SUSPENDED",
            "reason": "仅作为待实现研究变体；要求窗口与开市日历匹配，停牌或未知状态时不纳入新买候选。",
        }],
        "rationale": "检验过滤窗口内停牌与未知状态是否改变候选、目标和模拟执行诊断；不预设过滤后表现更好。",
        "evidence": [evidence_suspension],
        "required_data": [
            "历史证券状态与停复牌记录及来源版本",
            "逐证券行情与交易日历对齐报告",
            "经核实的历史交易单位、涨跌停规则与费用",
            "公告/行情的首次可得时间证据",
            "冻结后的独立时间区间或前瞻纸面记录",
        ],
        "known_limitations": [
            "当前样本用于开发诊断，不能作为独立留出。",
            "规范行情的 data_source 标签不证明原始供应商响应未回填或未修订。",
            "停牌状态未知必须保留为未知；缺行情不能自动归类为停牌。",
            "正式基线 NOT_READY，假设状态为 NEEDS_DATA。",
        ],
        "validation_plan": "先对照组与候选组在同一冻结输入上逐日比较候选数、排名、目标及受阻状态；唯一规则差异为 21 行窗口停牌状态要求。若窗口缺日、重复、未知状态或输入可信度门槛失败，停止收益比较。之后冻结所有输入与指标，再使用另一个未查看的时间区间或前瞻纸面跟踪；同时报告收益、回撤、换手、费用及数据缺口。",
        "status": "NEEDS_DATA",
        "human_review_required": True,
        "may_modify_active_strategy": False,
        "may_generate_orders": False,
    }
    h2 = {
        "schema_version": 1,
        "hypothesis_id": "HYP-ZERO-TARGET-CASH-ALLOCATION-001",
        "experiment_id": summary.get("experiment_id", "UNAVAILABLE"),
        "input_data_digest": input_digest,
        "problem_observed": (
            f"一次入选目标（{zero['symbol']}，排名 {int(zero['rank'])}）目标金额 ¥{float(zero['target_amount_cny']):.2f}，"
            f"参考开盘价 ¥{float(zero['execution_open_price_cny']):.2f}，理论数量 {int(zero['raw_target_shares_before_rounding'])} 股；"
            f"按配置回退最小数量 {int(zero['buy_minimum_shares_from_config_fallback'])} 股舍入后为 0，生命周期为 {zero['target_lifecycle_status']}。"
            f"排名前 N 先选后舍入；记录的下一未入选候选为 rank {zero.get('next_unselected_candidate_rank', 'UNKNOWN')} "
            f"{zero.get('next_unselected_candidate_symbol', 'UNKNOWN')}，当前未应用替补。"
        ),
        "proposed_changes": [{
            "config_path": "research.zero_share_target_policy",
            "current_value": "SELECT_TOP_N_THEN_ROUND_KEEP_CASH",
            "proposed_value": "AFTER_ZERO_TARGET_SCAN_NEXT_RANK_WITHIN_SAME_RISK_BUDGET",
            "reason": "比较排名后舍入导致的现金留存与后续候选补位；该配置路径仅是研究设计，不属于当前运行配置。",
        }],
        "rationale": "检验零股目标后的候选补位是否改善目标槽利用，同时控制总仓位、单股权重、风险、换手和费用。",
        "evidence": [evidence_zero],
        "required_data": [
            "与实验一致的完整候选池和排名",
            "按日期适用的证券交易单位与板块规则",
            "执行参考价格及其可得时点",
            "历史及个人费用口径",
            "现金、持仓和公司行动账本状态",
            "冻结后的独立时间区间或前瞻纸面记录",
        ],
        "known_limitations": [
            "交易单位来自配置回退，不是已核实历史交易规则。",
            "当前审计的目标金额及开盘价是模拟重建值，不是个人账户资金或订单。",
            "现有实现按排名先选前 N 个，再逐一舍入；没有用第 N+1 名替代零股目标。",
            "当前短窗口已用于开发诊断，不能作为独立留出；正式基线 NOT_READY。",
        ],
        "validation_plan": "在完全相同的信号、候选池、开盘参考价、风险预算、交易规则和费用下比较当前先选后舍入/保留现金与零目标后按排名补位两种预先冻结规则。比较零目标数、有效目标数、现金比例、组合暴露、换手、费用、未成交模拟目标、收益和回撤；不得基于当前窗口表现选择规则。冻结候选规则后再使用另一个未查看的时间区间或前瞻纸面跟踪。",
        "status": "NEEDS_DATA",
        "human_review_required": True,
        "may_modify_active_strategy": False,
        "may_generate_orders": False,
    }
    hypotheses = [validate_strategy_hypothesis(h1), validate_strategy_hypothesis(h2)]
    _write_json(audit_dir / "research-agent-input.json", input_payload)
    _write_json(audit_dir / "research-agent-hypothesis-suspension.json", hypotheses[0])
    _write_json(audit_dir / "research-agent-hypothesis-zero-target.json", hypotheses[1])
    summary["research_agent_artifacts"] = {
        "input": "research-agent-input.json",
        "hypotheses": [
            {"path": "research-agent-hypothesis-suspension.json", "id": h1["hypothesis_id"], "status": h1["status"]},
            {"path": "research-agent-hypothesis-zero-target.json", "id": h2["hypothesis_id"], "status": h2["status"]},
        ],
        "generation": "DETERMINISTIC_FROM_AUDIT_ARTIFACTS_NO_LLM_OR_RD_AGENT_CALLED",
        "active_strategy_modified": False,
        "orders_generated": False,
    }
    _write_json(summary_path, summary)
    _inventory(audit_dir)
    return {"input": input_payload, "hypotheses": hypotheses}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build schema-validated, read-only research hypotheses from a private audit.")
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.audit_dir, args.config)
    print(json.dumps({
        "research_id": result["input"]["research_id"],
        "hypothesis_ids": [item["hypothesis_id"] for item in result["hypotheses"]],
        "statuses": [item["status"] for item in result["hypotheses"]],
        "orders_generated": False,
        "active_strategy_modified": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
