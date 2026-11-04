from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

from cehuan.research.strategy_hypothesis import validate_strategy_hypothesis


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_object(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _git_context() -> dict[str, Any]:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    changes = subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.splitlines()
    return {"commit": commit, "worktree_clean": not bool(changes), "uncommitted_paths": [line[3:] for line in changes]}


def _read_and_verify_inventory(audit_dir: Path) -> dict[str, str]:
    inventory_path = audit_dir / "audit-artifact-inventory.csv"
    if not inventory_path.is_file():
        raise FileNotFoundError(f"来源审计包缺少清单：{inventory_path}")
    hashes: dict[str, str] = {}
    with inventory_path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        if not {"relative_path", "sha256"}.issubset(rows.fieldnames or set()):
            raise ValueError("审计清单必须包含 relative_path 和 sha256")
        for row in rows:
            relative = PurePosixPath(str(row["relative_path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"审计清单含不安全路径：{relative}")
            key = relative.as_posix()
            if key in hashes:
                raise ValueError(f"审计清单路径重复：{key}")
            expected = str(row["sha256"]).lower()
            if not _SHA256.fullmatch(expected):
                raise ValueError(f"审计清单哈希格式无效：{key}")
            path = audit_dir.joinpath(*relative.parts)
            if not path.is_file():
                raise FileNotFoundError(f"审计清单引用文件不存在：{path}")
            actual = _sha256(path)
            if actual != expected:
                raise ValueError(f"来源审计包哈希不匹配：{key}; expected={expected}; actual={actual}")
            hashes[key] = actual
    return hashes


def _publish_new_directory(staging: Path, output_dir: Path) -> None:
    """Publish once, using an exclusive sibling lock and refusing replacement."""
    lock_path = output_dir.parent / f".{output_dir.name}.publish-lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(f"另一个进程正在发布该研究包，或遗留发布锁存在：{lock_path}") from exc
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        if output_dir.exists():
            raise FileExistsError(f"研究假设输出目录已存在，拒绝覆盖：{output_dir}")
        os.rename(staging, output_dir)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        lock_path.unlink(missing_ok=True)


def _legacy_package_review(
    audit_dir: Path,
    summary: dict[str, Any],
    config: dict[str, Any],
    actual_config_sha256: str,
) -> dict[str, Any]:
    paths = {
        "input": audit_dir / "research-agent-input.json",
        "hypothesis_suspension": audit_dir / "research-agent-hypothesis-suspension.json",
        "hypothesis_zero_target": audit_dir / "research-agent-hypothesis-zero-target.json",
    }
    if not all(path.is_file() for path in paths.values()):
        return {"status": "UNVERIFIED", "reason": "旧版假设包的输入或两份假设文件缺失。"}

    old_input = _read_json(paths["input"])
    old_h1 = _read_json(paths["hypothesis_suspension"])
    old_h2 = _read_json(paths["hypothesis_zero_target"])
    registered = summary.get("input_hashes", {}).get("config.json")
    actual_digest = actual_config_sha256
    old_digest = old_input.get("frozen_strategy", {}).get("config_sha256")
    actual_parameters = config.get("strategy", {})
    old_parameters = old_input.get("frozen_strategy", {}).get("parameters")
    frozen_inputs = summary.get("input_hashes", {})
    old_expected_input_digest = _digest_object(frozen_inputs)
    checks = {
        "actual_config_matches_audit_summary": "MATCHED" if actual_digest == registered else "MISMATCHED",
        "legacy_config_hash_matches_actual_file": "MATCHED" if actual_digest == old_digest else "MISMATCHED",
        "legacy_parameters_match_actual_config": "MATCHED" if old_parameters == actual_parameters else "MISMATCHED",
        "legacy_hypothesis_digests_match_legacy_input": (
            "MATCHED" if old_h1.get("input_data_digest") == old_h2.get("input_data_digest") == old_expected_input_digest else "MISMATCHED"
        ),
        "legacy_h1_evidence_matches_window_file": (
            "MATCHED" if any(e.get("artifact_sha256") == _sha256(audit_dir / "suspension-candidate-window.csv") for e in old_h1.get("evidence", [])) else "MISMATCHED"
        ),
        "legacy_h2_evidence_matches_zero_target_file": (
            "MATCHED" if any(e.get("artifact_sha256") == _sha256(audit_dir / "zero-target-review.csv") for e in old_h2.get("evidence", [])) else "MISMATCHED"
        ),
    }
    return {
        "status": "MATCHED" if all(status == "MATCHED" for status in checks.values()) else "MISMATCHED",
        "checks": checks,
        "legacy_config_sha256": old_digest,
        "actual_config_sha256": actual_digest,
        "legacy_parameters_match": old_parameters == actual_parameters,
        "legacy_hypothesis_input_digest": old_h1.get("input_data_digest"),
        "expected_legacy_input_digest_from_audit_hashes": old_expected_input_digest,
    }


def _build_payloads(
    *,
    audit_dir: Path,
    summary: dict[str, Any],
    config: dict[str, Any],
    actual_config_sha256: str,
    evidence_hashes: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    frozen_inputs = summary.get("input_hashes", {})
    input_digest = _digest_object({
        "audit_summary_sha256": evidence_hashes["trade-audit-summary.json"],
        "audit_input_hashes": frozen_inputs,
        "config_sha256": actual_config_sha256,
        "evidence_sha256": evidence_hashes,
    })
    diagnostic = summary.get("suspension_candidate_diagnostic") or {}
    suspension_path = audit_dir / "suspension-candidate-window.csv"
    zero_path = audit_dir / "zero-target-review.csv"
    neighbors_path = audit_dir / "suspension-candidate-rank-neighbors.csv"
    suspension = pd.read_csv(suspension_path, keep_default_na=False)
    zero_targets = pd.read_csv(zero_path, keep_default_na=False)
    neighbors = pd.read_csv(neighbors_path, keep_default_na=False)
    symbol = str(diagnostic.get("symbol", ""))
    candidate_rows = neighbors.loc[neighbors.symbol.astype(str).eq(symbol)]
    if len(candidate_rows) != 1:
        raise ValueError(f"停牌候选执行链必须唯一，当前匹配 {len(candidate_rows)} 行")
    candidate = candidate_rows.iloc[0]
    if zero_targets.empty:
        raise ValueError("未找到舍入为零的目标，无法生成第二条假设")
    zero = zero_targets.iloc[0]

    research_trust = summary.get("research_trust", {})
    data_trust = {
        "input_file_integrity": {
            "status": research_trust.get("input_file_integrity", "UNVERIFIED"),
            "sha256": input_digest,
            "scope": "审计摘要、摘要登记的运行输入、配置文件及本假设包实际引用证据的联合摘要。",
        },
        "canonical_data_quality": {
            "status": research_trust.get("canonical_data_quality", "UNVERIFIED"),
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
        "formal_baseline": {"status": "NOT_READY", "interpretation": "仅为研究诊断输入。"},
        "overall": "DEVELOPMENT_DIAGNOSTIC_ONLY_NEEDS_DATA",
    }
    input_payload = {
        "schema_version": 1,
        "research_id": f"{summary.get('experiment_id', 'unknown')}-suspension-zero-target-review",
        "objective": "检验停牌窗口筛选和零股目标补位时点是否产生可复现、风险受控的候选规则差异。",
        "scope": {
            "market": "CN_A_SHARE",
            "start_date": summary["window"]["start"],
            "end_date": summary["window"]["end"],
            "universe_id": "PRIVATE_FROZEN_SHSZ_UNIVERSE_SNAPSHOT",
        },
        "data_trust": data_trust,
        "frozen_strategy": {
            "strategy_id": f"{summary.get('experiment_id', 'unknown')}-frozen",
            "config_sha256": actual_config_sha256,
            "parameters": config.get("strategy", {}),
        },
        "prior_experiments": [{
            "experiment_id": summary.get("experiment_id", "UNAVAILABLE"),
            "result_type": "RESEARCH_BACKTEST_DEVELOPMENT_DIAGNOSTIC",
            "summary": "仅用于审计链路与合成研究原型；不构成独立留出或策略有效性结论。",
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
            "该短窗口已用于开发问题发现，不能称为独立样本外验证。",
            "历史供应商发布时点与行情 vintage 未验证；公告日期不自动转换为精确 known_at。",
            "历史市场规则使用配置回退，华泰个人费率不可用。",
            "公司行动覆盖不完整，冻结运行没有消费 typed accounting events。",
            "trades.csv 是成交记录；完整模拟委托日志不存在，订单状态未知。",
        ],
        "allowed_actions": ["PROPOSE_HYPOTHESIS", "EXPLAIN_EVIDENCE", "DESIGN_VALIDATION"],
        "forbidden_actions": ["MODIFY_ACTIVE_STRATEGY", "EXECUTE_BACKTEST_WITHOUT_APPROVAL", "GENERATE_BROKER_ORDER", "CONNECT_BROKER_ACCOUNT"],
    }
    evidence_suspension = {
        "description": "来源审计包中的停牌候选观察窗口；供应商 vintage 和历史时点可得性仍未验证。",
        "artifact_sha256": evidence_hashes["suspension-candidate-window.csv"],
        "evidence_status": "RECONSTRUCTED",
    }
    evidence_zero = {
        "description": "来源审计包中的零股目标和下一候选复核表；交易单位为配置回退，不是券商订单。",
        "artifact_sha256": evidence_hashes["zero-target-review.csv"],
        "evidence_status": "RECONSTRUCTED",
    }
    state_counts = diagnostic.get("window_state_counts_including_base") or {}
    count_total = state_counts.get("total_rows", len(suspension))
    count_halted = state_counts.get("suspended_rows", 0)
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
            "此窗口只证明状态观测，不证明供应商点时可得性或策略优劣。"
        ),
        "proposed_changes": [{
            "config_path": "research.candidate_window.suspension_policy",
            "current_value": "SIGNAL_DAY_ELIGIBILITY_ONLY_ROW_SHIFT_LOOKBACK",
            "proposed_value": "REQUIRE_COMPLETE_21_SESSION_WINDOW_KNOWN_NOT_SUSPENDED",
            "reason": "研究原型要求窗口与 21 个开市日匹配，停牌或未知状态时排除新买候选。",
        }],
        "rationale": "检验过滤窗口内停牌与未知状态是否改变候选、排名和执行诊断；不预设过滤后表现更好。",
        "evidence": [evidence_suspension],
        "required_data": ["历史证券状态", "逐证券行情与交易日历", "历史规则与费用", "行情时点来源", "独立验证区间"],
        "known_limitations": [
            "当前窗口只用于开发诊断，不能作为独立留出。",
            "规范行情 data_source 标签不证明原始供应商响应未回填或未修订。",
            "状态未知与行情缺失必须分别保留，不得静默归为非停牌。",
            "正式基线 NOT_READY，假设状态为 NEEDS_DATA。",
        ],
        "validation_plan": "在合成数据上先验证 21 个开市日精确匹配、重复/缺失/未知/停牌原因和候选重排。之后仅在同一冻结输入上比较 H1 对照与候选集；候选规则唯一差异是完整窗口且停牌状态均已知非停牌。完成来源、规则、费用和可得时点门槛后，另用冻结后的时间区间或前瞻纸面跟踪评价收益与风险。",
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
            f"按配置回退最小数量 {int(zero['buy_minimum_shares_from_config_fallback'])} 股舍入为 0。"
            f"当前顺序先选前 N 名再舍入，下一名 rank {zero.get('next_unselected_candidate_rank', 'UNKNOWN')} "
            f"{zero.get('next_unselected_candidate_symbol', 'UNKNOWN')} 未自动替补。"
        ),
        "proposed_changes": [{
            "config_path": "research.zero_share_target_policy",
            "current_value": "SELECT_TOP_N_THEN_ROUND_KEEP_CASH",
            "proposed_value": "OBSERVE_OPEN_THEN_SCHEDULE_REPLACEMENT_NEXT_SESSION_OPEN",
            "reason": "开盘价可得后才决定替补时，不得假定能以同一开盘价成交；候补选择后冻结数量并延后执行。",
        }],
        "rationale": "用有明确时间边界的候补执行流程检验目标槽补位，同时控制风险预算、现金和费用；候补若在下一开市日受阻则不使用该日新价格重新挑选。",
        "evidence": [evidence_zero],
        "required_data": ["完整候选排名", "决策日和下一开市日行情及可得时点", "历史交易单位和市场规则", "费用", "账户现金和持仓状态", "独立验证区间"],
        "known_limitations": [
            "本原型把开盘后决策的替补推迟至下一开市日开盘，含有显式执行延迟。",
            "合成模拟填充不证明真实开盘可成交；涨跌停和队列流动性仍需模型。",
            "交易单位及费用必须按日期核验，当前私有诊断使用配置回退。",
            "当前窗口用于开发诊断；正式基线 NOT_READY。",
        ],
        "validation_plan": "在合成数据上固定信号排名、决策日开盘报价、单槽目标金额、组合风险预算、剩余风险、现金和费用。先观察决策日开盘价后选出候补，冻结证券与数量，只在显式较晚的下一开市日开盘模拟执行；若该日停牌、状态未知、价格变化导致风险预算超限或现金不足，则记录阻断并保留现金，不再使用该日价格改选候补。与当前零股保留现金对照比较目标槽、现金、费用和阻断状态。",
        "status": "NEEDS_DATA",
        "human_review_required": True,
        "may_modify_active_strategy": False,
        "may_generate_orders": False,
    }
    return input_payload, validate_strategy_hypothesis(h1), validate_strategy_hypothesis(h2)


def build(audit_dir: Path, config_path: Path, output_dir: Path) -> dict[str, Any]:
    """Create a new research package while treating the source audit as immutable."""
    audit_dir = audit_dir.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if output_dir.exists():
        raise FileExistsError(f"研究假设输出目录已存在，拒绝覆盖：{output_dir}")
    if output_dir == audit_dir or output_dir.is_relative_to(audit_dir) or audit_dir.is_relative_to(output_dir):
        raise ValueError("研究假设输出目录必须与来源审计包分离")
    summary_path = audit_dir / "trade-audit-summary.json"
    required_names = [
        "trade-audit-summary.json", "suspension-candidate-window.csv",
        "suspension-candidate-rank-neighbors.csv", "zero-target-review.csv",
    ]
    missing = [str(audit_dir / name) for name in required_names if not (audit_dir / name).is_file()]
    if missing or not config_path.is_file():
        if not config_path.is_file():
            missing.append(str(config_path))
        raise FileNotFoundError("生成研究假设缺少输入：" + ", ".join(missing))

    inventory_hashes = _read_and_verify_inventory(audit_dir)
    for name in required_names:
        if name not in inventory_hashes:
            raise ValueError(f"来源清单没有绑定必要输入：{name}")
    summary = _read_json(summary_path)
    if summary.get("schema_version") != 2:
        raise ValueError("研究假设生成器要求成交审计 summary schema_version=2")
    registered_config_sha256 = summary.get("input_hashes", {}).get("config.json")
    if not isinstance(registered_config_sha256, str) or not _SHA256.fullmatch(registered_config_sha256.lower()):
        raise ValueError("审计摘要缺少有效的 config.json SHA-256；拒绝生成配置未绑定的假设包")
    actual_config_sha256 = _sha256(config_path)
    if actual_config_sha256 != registered_config_sha256.lower():
        raise ValueError(
            "实际配置文件 SHA-256 与审计摘要登记值不一致；拒绝将该配置参数绑定到原审计："
            f"registered={registered_config_sha256}; actual={actual_config_sha256}"
        )
    config = _read_json(config_path)
    if not isinstance(config.get("strategy"), dict):
        raise ValueError("实际配置缺少 strategy 对象")

    git = _git_context()
    generator_path = Path(__file__).resolve()
    evidence_names = [*required_names, "audit-artifact-inventory.csv"]
    source_hashes_before = {name: _sha256(audit_dir / name) for name in evidence_names}
    source_hashes_before["config.json"] = actual_config_sha256
    legacy_review = _legacy_package_review(audit_dir, summary, config, actual_config_sha256)
    reference = {
        "schema_version": 1,
        "source_audit_directory_alias": audit_dir.name,
        "source_audit_directory": str(audit_dir),
        "source_summary_sha256": source_hashes_before["trade-audit-summary.json"],
        "source_inventory_sha256": source_hashes_before["audit-artifact-inventory.csv"],
        "source_evidence_sha256": {name: source_hashes_before[name] for name in required_names},
        "actual_config_file_alias": config_path.name,
        "actual_config_sha256": actual_config_sha256,
        "summary_registered_config_sha256": registered_config_sha256,
        "config_binding_status": "MATCHED",
        "legacy_hypothesis_package_review": legacy_review,
        "source_inventory_file_count_verified": len(inventory_hashes),
        "generator_git": git,
        "generator_sha256": _sha256(generator_path),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_audit_mutated": False,
    }
    input_payload, h1, h2 = _build_payloads(
        audit_dir=audit_dir,
        summary=summary,
        config=config,
        actual_config_sha256=actual_config_sha256,
        evidence_hashes={**source_hashes_before, **inventory_hashes},
    )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.tmp-", dir=output_dir.parent))
    try:
        _write_json(staging / "source-audit-reference.json", reference)
        _write_json(staging / "research-agent-input.json", input_payload)
        _write_json(staging / "hypothesis-suspension.json", h1)
        _write_json(staging / "hypothesis-zero-target.json", h2)
        summary_lines = [
            "# 研究假设包摘要",
            "",
            f"- 来源审计：`{audit_dir.name}`；摘要 SHA-256 `{reference['source_summary_sha256']}`。",
            f"- 来源清单 SHA-256：`{reference['source_inventory_sha256']}`；清单内 {len(inventory_hashes)} 项已逐项核验。",
            f"- 实际配置哈希与审计登记：`{actual_config_sha256}`；状态 `{reference['config_binding_status']}`。",
            f"- 旧版假设包本机复核：`{legacy_review['status']}`。",
            f"- H1/H2：`{h1['status']}` / `{h2['status']}`；使用现有 schema；未调用 LLM/RD-Agent。",
            "- 来源审计目录只读；本包是独立新输出。假设不会修改有效策略或生成委托。",
            "- 本包只生成待审核研究假设，没有重跑回测；正式基线仍 `NOT_READY`，独立验证仍未完成。",
            "",
            "## 文件",
            "",
            "- `source-audit-reference.json`: 来源审计和配置哈希绑定。",
            "- `research-agent-input.json`: 按现有 Agent 输入契约构造的研究输入。",
            "- `hypothesis-suspension.json`: H1 待审核研究假设。",
            "- `hypothesis-zero-target.json`: H2 含延后执行边界的待审核假设。",
        ]
        (staging / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

        inventory_rows: list[dict[str, Any]] = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            inventory_rows.append({
                "relative_path": path.relative_to(staging).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
        with (staging / "hypothesis-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["relative_path", "size_bytes", "sha256"])
            writer.writeheader()
            writer.writerows(inventory_rows)

        inventory_hashes_after = _read_and_verify_inventory(audit_dir)
        if inventory_hashes_after != inventory_hashes:
            raise RuntimeError("生成期间来源审计包发生变化，未发布研究假设包")
        for name, before in source_hashes_before.items():
            current_path = config_path if name == "config.json" else audit_dir / name
            if _sha256(current_path) != before:
                raise RuntimeError(f"生成期间来源文件发生变化，未发布研究假设包：{name}")
        _publish_new_directory(staging, output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "output_dir": str(output_dir),
        "input": input_payload,
        "hypotheses": [h1, h2],
        "source_reference": reference,
        "legacy_review": legacy_review,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a schema-validated research package without modifying its source audit.")
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.audit_dir, args.config, args.output_dir)
    print(json.dumps({
        "output_dir": result["output_dir"],
        "hypothesis_ids": [item["hypothesis_id"] for item in result["hypotheses"]],
        "statuses": [item["status"] for item in result["hypotheses"]],
        "legacy_review": result["legacy_review"]["status"],
        "source_audit_mutated": result["source_reference"]["source_audit_mutated"],
        "orders_generated": False,
        "active_strategy_modified": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
