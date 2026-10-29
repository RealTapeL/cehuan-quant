from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cehuan.data.canonical import normalize_bar_frame
from cehuan.data.corporate_actions import normalize_corporate_actions
from cehuan.data.quality import check_data_quality
from cehuan.data.security_identity import (
    audit_out_date_boundary_evidence,
    normalize_security_identity_events,
    sha256_file,
)
from cehuan.data.universe_snapshot import filter_bars_to_point_in_time_universe


READINESS_SCHEMA = "cehuan.baseline_readiness.v1"
# These file-context keys also name the keyword arguments accepted by
# readiness_input_hashes; keep the formal caller's required set centralized.
READINESS_CONTEXT_KEYS = (
    "data",
    "point_in_time_quality",
    "universe",
    "identity_events",
    "out_date_evidence",
    "unknown_type_review",
    "unknown_type_summary",
    "unknown_type_bars",
    "corporate_actions",
    "corporate_action_coverage",
    "market_rules",
    "fees",
    "benchmark",
    "config",
)
ACTION_COVERAGE_COLUMNS = [
    "symbol", "valid_from", "valid_to", "source", "source_sha256",
    "review_status", "reviewed_by", "reviewed_at",
]
FEE_COLUMNS = [
    "valid_from", "valid_to", "market", "commission_rate", "minimum_commission_cny",
    "stamp_duty_sell_rate", "transfer_fee_rate", "source", "source_sha256",
    "review_status", "reviewed_by", "reviewed_at",
]
RULE_COLUMNS = [
    "symbol", "board", "valid_from", "valid_to", "is_listed", "is_suspended", "is_st",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares", "source", "source_sha256",
    "review_status", "reviewed_by", "reviewed_at",
]


def _git_state() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip())
        return commit, not dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None


def universe_input_hashes(universe: str | Path) -> dict[str, str]:
    root = Path(universe).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"股票池快照目录不存在：{root}")
    hashes = {}
    for name in ("manifest.json", "securities_by_date.csv", "reconciliation.csv"):
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"股票池快照缺少 {name}")
        hashes[f"universe/{name}"] = sha256_file(path)
    return hashes


def readiness_input_hashes(
    *,
    data: str | Path,
    point_in_time_quality: str | Path,
    universe: str | Path,
    identity_events: str | Path,
    out_date_evidence: str | Path,
    unknown_type_review: str | Path,
    unknown_type_summary: str | Path,
    unknown_type_bars: str | Path,
    corporate_actions: str | Path,
    corporate_action_coverage: str | Path,
    market_rules: str | Path,
    fees: str | Path,
    benchmark: str | Path,
    config: str | Path,
) -> dict[str, str]:
    files = {
        "data": data,
        "point_in_time_quality": point_in_time_quality,
        "identity_events": identity_events,
        "out_date_evidence": out_date_evidence,
        "unknown_type_review": unknown_type_review,
        "unknown_type_summary": unknown_type_summary,
        "unknown_type_bars": unknown_type_bars,
        "corporate_actions": corporate_actions,
        "corporate_action_coverage": corporate_action_coverage,
        "market_rules": market_rules,
        "fees": fees,
        "benchmark": benchmark,
        "config": config,
    }
    hashes = {key: sha256_file(value) for key, value in files.items()}
    hashes.update(universe_input_hashes(universe))
    return hashes


def _read_dated_csv(path: str | Path, required: list[str], label: str) -> pd.DataFrame:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"{label}文件不存在：{source}")
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    missing = set(required).difference(frame.columns)
    if missing:
        raise ValueError(f"{label}文件缺少字段：" + ", ".join(sorted(missing)))
    return frame


def _dated_rows(frame: pd.DataFrame, label: str) -> tuple[pd.Series, pd.Series]:
    start = pd.to_datetime(frame["valid_from"], errors="coerce")
    end_text = frame["valid_to"].replace("", pd.NA)
    end = pd.to_datetime(end_text, errors="coerce")
    if start.isna().any() or (end_text.notna() & end.isna()).any():
        raise ValueError(f"{label} valid_from/valid_to 日期无效")
    if (end.notna() & end.lt(start)).any():
        raise ValueError(f"{label} valid_to 早于 valid_from")
    return start.dt.normalize(), end.dt.normalize()


def _audit_market_rules(bars: pd.DataFrame, rules_path: str | Path, start: str, end: str) -> dict[str, Any]:
    rules = _read_dated_csv(rules_path, RULE_COLUMNS, "有效日期交易规则")
    rules["symbol"] = rules["symbol"].astype(str).str.strip().str.upper()
    rules["board"] = rules["board"].astype(str).str.strip()
    rules["source"] = rules["source"].astype(str).str.strip()
    rules["valid_from"], rules["valid_to"] = _dated_rows(rules, "有效日期交易规则")
    for column in ("is_listed", "is_suspended", "is_st", "has_price_limit"):
        rules[column] = rules[column].astype(str).str.strip().str.lower().map(
            {"1": True, "true": True, "0": False, "false": False}
        )
    numeric = [
        "price_limit_pct", "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
    ]
    for column in numeric:
        rules[column] = pd.to_numeric(rules[column], errors="coerce")
    for column in ("source", "source_sha256", "review_status", "reviewed_by", "reviewed_at"):
        rules[column] = rules[column].astype(str).str.strip()
    rule_errors = []
    duplicate = rules.duplicated(["symbol", "valid_from"], keep=False)
    if duplicate.any():
        rule_errors.append(f"有效日期交易规则存在重复证券/起始日期 {int(duplicate.sum())} 行")
    for symbol, group in rules.sort_values("valid_from").groupby("symbol", sort=False):
        previous_end = None
        first = True
        for row in group.itertuples(index=False):
            if not first and (pd.isna(previous_end) or row.valid_from <= previous_end):
                rule_errors.append(f"{symbol} 的有效日期交易规则区间重叠")
                break
            previous_end = row.valid_to
            first = False

    selected = bars.loc[
        pd.to_datetime(bars["trade_date"]).between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both")
    ].copy()
    expected_rows = int(len(selected))
    matched_rows = complete_rows = 0
    missing_rows = []
    by_symbol = {symbol: group for symbol, group in rules.groupby("symbol", sort=False)}
    for symbol, group in selected.groupby("symbol", sort=False, observed=True):
        symbol = str(symbol)
        candidates = by_symbol.get(symbol)
        if candidates is None:
            missing_rows.append((symbol, len(group)))
            continue
        dates = pd.to_datetime(group["trade_date"]).dt.normalize()
        hit_count = np.zeros(len(group), dtype=np.int8)
        hit_pos = np.full(len(group), -1, dtype=np.int32)
        for pos, rule in enumerate(candidates.itertuples(index=False)):
            match = dates.ge(rule.valid_from)
            if pd.notna(rule.valid_to):
                match &= dates.le(rule.valid_to)
            indices = np.flatnonzero(match.to_numpy())
            hit_count[indices] += 1
            hit_pos[indices] = pos
        matched = hit_count == 1
        matched_rows += int(matched.sum())
        if not matched.all():
            missing_rows.append((symbol, int((~matched).sum())))
            continue
        for local_pos, rule_pos in enumerate(hit_pos):
            rule = candidates.iloc[int(rule_pos)]
            complete = (
                bool(rule["board"])
                and bool(rule["source"])
                and len(rule["source_sha256"]) == 64
                and all(ch in "0123456789abcdefABCDEF" for ch in rule["source_sha256"])
                and rule["review_status"] == "REVIEWED"
                and bool(rule["reviewed_by"])
                and bool(rule["reviewed_at"])
                and pd.notna(rule["is_listed"])
                and pd.notna(rule["is_suspended"])
                and pd.notna(rule["is_st"])
                and pd.notna(rule["has_price_limit"])
                and pd.notna(rule["buy_min_shares"])
                and pd.notna(rule["buy_step_shares"])
                and pd.notna(rule["sell_min_shares"])
                and pd.notna(rule["sell_step_shares"])
                and all(float(rule[column]).is_integer() for column in (
                    "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
                ))
                and all(float(rule[column]) > 0 for column in (
                    "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
                ))
                and (
                    (not bool(rule["has_price_limit"]) and pd.isna(rule["price_limit_pct"]))
                    or (
                        bool(rule["has_price_limit"])
                        and pd.notna(rule["price_limit_pct"])
                        and 0 < float(rule["price_limit_pct"]) <= 1
                    )
                )
                and pd.notna(rule["valid_from"])
            )
            if complete:
                complete_rows += 1
            else:
                missing_rows.append((symbol, 1))
    return {
        "complete": not rule_errors and matched_rows == expected_rows and complete_rows == expected_rows,
        "evaluation_rows": expected_rows,
        "matched_rows": matched_rows,
        "complete_rows": complete_rows,
        "unmatched_or_incomplete_symbol_rows": int(sum(count for _, count in missing_rows)),
        "sample_symbols": [symbol for symbol, _ in missing_rows[:25]],
        "errors": rule_errors,
    }


def _audit_action_coverage(
    bars: pd.DataFrame,
    coverage_path: str | Path,
    actions: pd.DataFrame,
    start: str,
    end: str,
) -> dict[str, Any]:
    coverage = _read_dated_csv(coverage_path, ACTION_COVERAGE_COLUMNS, "公司行动覆盖证明")
    coverage["symbol"] = coverage["symbol"].astype(str).str.strip().str.upper()
    coverage["valid_from"], coverage["valid_to"] = _dated_rows(coverage, "公司行动覆盖证明")
    selected = bars.loc[
        pd.to_datetime(bars["trade_date"]).between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both")
    ]
    spans = selected.groupby("symbol", sort=False, observed=True)["trade_date"].agg(["min", "max"])
    errors = []
    covered_symbols = 0
    for symbol, span in spans.iterrows():
        candidates = coverage.loc[coverage["symbol"].eq(str(symbol))]
        eligible = candidates.loc[
            candidates["valid_from"].le(pd.Timestamp(span["min"]))
            & (candidates["valid_to"].isna() | candidates["valid_to"].ge(pd.Timestamp(span["max"])))
        ]
        if len(eligible) != 1:
            errors.append(f"{symbol} 缺少唯一、覆盖其评价区间的公司行动覆盖证明")
            continue
        row = eligible.iloc[0]
        if not (
            row["source"].strip()
            and row["source_sha256"].strip()
            and row["review_status"] == "REVIEWED_COMPLETE"
            and row["reviewed_by"].strip()
            and row["reviewed_at"].strip()
        ):
            errors.append(f"{symbol} 公司行动覆盖证明未完成来源复核")
            continue
        covered_symbols += 1
    active_actions = actions.loc[
        actions["old_last_trade_date"].le(end)
        & actions["new_shares_tradable_date"].ge(start)
    ] if not actions.empty else actions
    unresolved_actions = active_actions.loc[
        active_actions["allocation_status"].ne("CONFIRMED_ACCOUNT_ALLOCATION")
    ] if not active_actions.empty else active_actions
    for action in unresolved_actions.to_dict(orient="records"):
        errors.append(
            f"{action['old_symbol']}->{action['new_symbol']} 在评价窗口内有未确认最终获配股数的公司行动"
        )
    return {
        "complete": not errors and covered_symbols == int(spans.shape[0]),
        "evaluation_symbols": int(spans.shape[0]),
        "covered_symbols": covered_symbols,
        "active_action_count": int(len(active_actions)),
        "unresolved_allocation_count": int(len(unresolved_actions)),
        "errors": errors[:100],
    }


def _audit_corporate_action_evidence(actions: pd.DataFrame) -> dict[str, Any]:
    missing = []
    for row in actions.to_dict(orient="records"):
        for label, url, title, page, digest in (
            (
                "换股实施公告", row["evidence_url"], row["evidence_title"],
                row["evidence_page_reference"], row["evidence_sha256"],
            ),
            (
                "旧股最后交易日公告", row["old_last_trade_evidence_url"],
                row["old_last_trade_evidence_title"], row["old_last_trade_evidence_page_reference"],
                row["old_last_trade_evidence_sha256"],
            ),
        ):
            digest = str(digest).strip()
            if not (
                str(url).startswith("https://")
                and str(title).strip()
                and str(page).strip()
                and len(digest) == 64
                and all(character in "0123456789abcdefABCDEF" for character in digest)
                and str(row["reviewed_by"]).strip()
                and str(row["reviewed_at"]).strip()
            ):
                missing.append(f"{row['event_id']} 缺少可复核的{label}文件 SHA-256 或复核字段")
    return {"complete": not missing, "event_count": int(len(actions)), "errors": missing}


def _audit_fee_schedule(fees_path: str | Path, bars: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    fees = _read_dated_csv(fees_path, FEE_COLUMNS, "历史费率")
    fees["valid_from"], fees["valid_to"] = _dated_rows(fees, "历史费率")
    for column in ("commission_rate", "minimum_commission_cny", "stamp_duty_sell_rate", "transfer_fee_rate"):
        fees[column] = pd.to_numeric(fees[column], errors="coerce")
    numeric_columns = ["commission_rate", "minimum_commission_cny", "stamp_duty_sell_rate", "transfer_fee_rate"]
    invalid_rate = fees[numeric_columns].isna().any(axis=1) | fees[numeric_columns].lt(0).any(axis=1)
    if invalid_rate.any():
        return {"complete": False, "errors": ["历史费率包含缺失或负数费率"]}
    dates = pd.to_datetime(bars.loc[
        pd.to_datetime(bars["trade_date"]).between(pd.Timestamp(start), pd.Timestamp(end), inclusive="both"),
        "trade_date",
    ]).drop_duplicates().dt.normalize()
    errors = []
    fee_intervals = fees.loc[fees["market"].eq("A_SHARE")].sort_values("valid_from")
    previous_end = None
    first = True
    for row in fee_intervals.itertuples(index=False):
        if not first and (pd.isna(previous_end) or row.valid_from <= previous_end):
            errors.append("A_SHARE 历史费率有效期重叠")
            break
        previous_end = row.valid_to
        first = False
    for day in dates:
        matches = fees.loc[
            fees["valid_from"].le(day)
            & (fees["valid_to"].isna() | fees["valid_to"].ge(day))
            & fees["market"].eq("A_SHARE")
        ]
        if len(matches) != 1:
            errors.append(f"{day:%Y-%m-%d} 没有唯一有效的 A 股历史费率")
    review_ok = fees["source"].str.strip().ne("") & fees["source_sha256"].str.strip().ne("")
    review_ok &= fees["source_sha256"].map(
        lambda value: len(str(value)) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in str(value))
    )
    review_ok &= fees["review_status"].eq("REVIEWED")
    review_ok &= fees["reviewed_by"].str.strip().ne("") & fees["reviewed_at"].str.strip().ne("")
    if not review_ok.all():
        errors.append("历史费率缺少来源散列或人工复核元数据")
    return {"complete": not errors, "evaluation_session_count": int(len(dates)), "errors": errors}


def build_baseline_readiness(
    *,
    data: pd.DataFrame,
    input_hashes: dict[str, str],
    universe: str | Path,
    identity_events_path: str | Path,
    out_date_evidence_path: str | Path,
    unknown_type_review_path: str | Path,
    unknown_type_summary_path: str | Path,
    unknown_type_bars_path: str | Path,
    point_in_time_quality_path: str | Path,
    corporate_actions_path: str | Path,
    corporate_action_coverage_path: str | Path,
    market_rules_path: str | Path,
    fees_path: str | Path,
    benchmark: pd.DataFrame,
    evaluation_start: str,
    evaluation_end: str,
) -> dict[str, Any]:
    """Generate an input-bound readiness decision; never accept a user bool."""
    bars = normalize_bar_frame(data)
    start = pd.Timestamp(evaluation_start).strftime("%Y-%m-%d")
    end = pd.Timestamp(evaluation_end).strftime("%Y-%m-%d")
    if end < start:
        raise ValueError("评价区间结束日期早于开始日期")
    selected = bars.loc[pd.to_datetime(bars["trade_date"]).between(start, end, inclusive="both")]
    blockers: list[str] = []
    checks: dict[str, Any] = {}
    if selected.empty:
        blockers.append("EVALUATION_WINDOW_HAS_NO_DATA")

    quality_path = Path(point_in_time_quality_path).expanduser().resolve()
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    if quality.get("schema_version") != 1:
        blockers.append("POINT_IN_TIME_QUALITY_SCHEMA_UNSUPPORTED")
    if not quality.get("healthy") or not quality.get("raw_bar_quality", {}).get("healthy") or not quality.get("universe_reconciliation_quality", {}).get("healthy"):
        blockers.append("POINT_IN_TIME_QUALITY_NOT_HEALTHY")
    if quality.get("data_sha256") != input_hashes.get("data"):
        blockers.append("POINT_IN_TIME_DATA_HASH_MISMATCH")
    manifest_path = Path(universe).expanduser().resolve() / "manifest.json"
    universe_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_universe_hash = input_hashes.get("universe/securities_by_date.csv")
    reported_membership_hash = quality.get("universe_reconciliation_quality", {}).get("source_artifact_sha256")
    universe_hash_matches = expected_universe_hash == reported_membership_hash
    if not universe_hash_matches:
        blockers.append("POINT_IN_TIME_UNIVERSE_HASH_MISMATCH")
    identity_hash_matches = quality.get("security_identity_events_sha256") == input_hashes.get("identity_events")
    if not identity_hash_matches:
        blockers.append("POINT_IN_TIME_IDENTITY_EVENTS_HASH_MISMATCH")
    snapshot_id_matches = quality.get("universe_snapshot_id") == universe_manifest.get("snapshot_id")
    if not snapshot_id_matches:
        blockers.append("POINT_IN_TIME_UNIVERSE_SNAPSHOT_ID_MISMATCH")
    checks["point_in_time_quality"] = {
        "healthy": bool(quality.get("healthy")),
        "schema_version": quality.get("schema_version"),
        "data_sha256_matches": quality.get("data_sha256") == input_hashes.get("data"),
        "universe_sha256_matches": universe_hash_matches,
        "identity_events_sha256_matches": identity_hash_matches,
        "universe_snapshot_id_matches": snapshot_id_matches,
        "universe_snapshot_id": quality.get("universe_snapshot_id"),
    }

    identity_path = Path(identity_events_path).expanduser().resolve()
    if not identity_path.is_file():
        blockers.append("IDENTITY_EVENT_INPUT_MISSING")
        identities = pd.DataFrame()
    else:
        identities = normalize_security_identity_events(pd.read_csv(identity_path, dtype=str, keep_default_na=False))
    checks["identity_events"] = {"count": int(len(identities)), "sha256": input_hashes.get("identity_events")}

    raw_quality = check_data_quality(bars)
    membership_path = Path(universe).expanduser().resolve() / "securities_by_date.csv"
    membership = pd.read_csv(membership_path, dtype=str, keep_default_na=False)
    filtered, membership_summary, _ = filter_bars_to_point_in_time_universe(
        bars, membership, security_identity_events=identities,
    )
    supplied_keys = set(zip(bars["trade_date"].astype(str), bars["symbol"].astype(str)))
    recomputed_keys = set(zip(filtered["trade_date"].astype(str), filtered["symbol"].astype(str)))
    first_membership_date = pd.to_datetime(membership["trade_date"], errors="coerce").min()
    bar_days = pd.to_datetime(bars["trade_date"])
    warmup_keys = set(zip(
        bars.loc[bar_days.lt(first_membership_date), "trade_date"].astype(str),
        bars.loc[bar_days.lt(first_membership_date), "symbol"].astype(str),
    ))
    unmatched_non_warmup = supplied_keys.difference(recomputed_keys).difference(warmup_keys)
    point_in_time_recomputed = bool(
        raw_quality.get("healthy")
        and membership_summary["reconciliation_quality"].get("healthy")
        and not unmatched_non_warmup
    )
    if not point_in_time_recomputed:
        blockers.append("POINT_IN_TIME_QUALITY_RECOMPUTATION_FAILED")
    out_date_quality, _ = audit_out_date_boundary_evidence(
        Path(universe).expanduser().resolve() / "reconciliation.csv",
        out_date_evidence_path,
    )
    if not out_date_quality.get("healthy"):
        blockers.append("OUT_DATE_EVIDENCE_REVIEW_INCOMPLETE")
    checks["point_in_time_recomputation"] = {
        "raw_bar_quality_healthy": bool(raw_quality.get("healthy")),
        "universe_reconciliation_healthy": bool(membership_summary["reconciliation_quality"].get("healthy")),
        "unmatched_non_warmup_bar_rows": len(unmatched_non_warmup),
        "pre_snapshot_warmup_bar_rows": len(warmup_keys),
        "complete": point_in_time_recomputed,
    }
    checks["out_date_evidence"] = out_date_quality

    unknown_summary = json.loads(Path(unknown_type_summary_path).expanduser().resolve().read_text(encoding="utf-8"))
    unknown_review_path = Path(unknown_type_review_path).expanduser().resolve()
    unknown_bars_path = Path(unknown_type_bars_path).expanduser().resolve()
    classified_unknown_rows = int(membership["pool_status"].astype(str).eq("UNKNOWN_TYPE").sum())
    unknown_review = pd.read_csv(unknown_review_path, dtype=str, keep_default_na=False)
    unknown_partition_balances = bool(
        unknown_summary.get("classification_partition_balances") is True
        and int(unknown_summary.get("unknown_rows", -1)) == classified_unknown_rows
        and int(unknown_summary.get("unknown_rows", -1)) == (
            int(unknown_summary.get("explained_non_stock_rows", 0))
            + int(unknown_summary.get("added_as_stock_rows", 0))
            + int(unknown_summary.get("unresolved_rows", 0))
        )
        and int(unknown_review["unknown_member_rows"].astype(int).sum()) == classified_unknown_rows
    )
    unknown_hashes_match = bool(
        unknown_summary.get("universe_membership_sha256") == input_hashes.get("universe/securities_by_date.csv")
        and unknown_summary.get("bars_sha256") == input_hashes.get("unknown_type_bars")
        and unknown_summary.get("review_csv_sha256") == input_hashes.get("unknown_type_review")
    )
    unknown_types_complete = bool(
        unknown_partition_balances
        and unknown_hashes_match
        and int(unknown_summary.get("unresolved_rows", -1)) == 0
        and int(unknown_summary.get("added_as_stock_rows", -1)) == 0
    )
    if not unknown_partition_balances:
        blockers.append("UNKNOWN_TYPE_AUDIT_PARTITION_INVALID")
    if not unknown_hashes_match:
        blockers.append("UNKNOWN_TYPE_AUDIT_INPUT_HASH_MISMATCH")
    if int(unknown_summary.get("unresolved_rows", -1)) != 0:
        blockers.append("UNKNOWN_TYPE_CLASSIFICATION_INCOMPLETE")
    if int(unknown_summary.get("added_as_stock_rows", -1)) != 0:
        blockers.append("UNKNOWN_TYPE_STOCKS_NOT_INCLUDED_IN_UNIVERSE_SNAPSHOT")
    checks["unknown_types"] = {
        "unknown_rows": classified_unknown_rows,
        "unknown_symbols": int(unknown_summary.get("unknown_symbols", -1)),
        "explained_non_stock_rows": int(unknown_summary.get("explained_non_stock_rows", -1)),
        "added_as_stock_rows": int(unknown_summary.get("added_as_stock_rows", -1)),
        "unresolved_rows": int(unknown_summary.get("unresolved_rows", -1)),
        "classification_partition_balances": unknown_partition_balances,
        "input_hashes_match": unknown_hashes_match,
        "complete_for_current_universe": unknown_types_complete,
        "beijing_market_included": False,
        "research_scope": "classified Shanghai/Shenzhen type=1 members only; not full A-share historical coverage",
    }

    actions = normalize_corporate_actions(
        pd.read_csv(corporate_actions_path, dtype=str, keep_default_na=False)
        if Path(corporate_actions_path).is_file() else pd.DataFrame()
    )
    if not Path(corporate_actions_path).is_file():
        blockers.append("CORPORATE_ACTIONS_INPUT_MISSING")
    action_evidence = _audit_corporate_action_evidence(actions)
    if not action_evidence["complete"]:
        blockers.append("CORPORATE_ACTION_EVIDENCE_INCOMPLETE")
    action_coverage = _audit_action_coverage(
        selected, corporate_action_coverage_path, actions, start, end,
    )
    checks["corporate_actions"] = {
        "event_count": int(len(actions)),
        "evidence": action_evidence,
        "coverage": action_coverage,
    }
    if not action_coverage["complete"]:
        blockers.append("CORPORATE_ACTION_COVERAGE_OR_ACCOUNTING_INCOMPLETE")

    rules = _audit_market_rules(selected, market_rules_path, start, end)
    checks["market_rules"] = rules
    if not rules["complete"]:
        blockers.append("EFFECTIVE_DATED_MARKET_RULES_INCOMPLETE")

    fees = _audit_fee_schedule(fees_path, selected, start, end)
    checks["fees"] = fees
    if not fees["complete"]:
        blockers.append("HISTORICAL_FEES_INCOMPLETE_OR_UNREVIEWED")

    normalized_benchmark = normalize_bar_frame(benchmark)
    benchmark_symbol = str(normalized_benchmark["symbol"].iloc[0]) if not normalized_benchmark.empty else ""
    benchmark_dates = set(normalized_benchmark.loc[
        normalized_benchmark["symbol"].astype(str).eq(benchmark_symbol), "trade_date"
    ].astype(str))
    evaluation_dates = set(selected["trade_date"].astype(str).unique())
    benchmark_complete = bool(evaluation_dates) and evaluation_dates.issubset(benchmark_dates)
    checks["benchmark"] = {
        "symbol": benchmark_symbol,
        "evaluation_session_count": len(evaluation_dates),
        "covered_session_count": len(evaluation_dates & benchmark_dates),
        "complete": benchmark_complete,
    }
    if not benchmark_complete:
        blockers.append("BENCHMARK_COVERAGE_INCOMPLETE")

    # Current simulation code still uses fallback/configured costs and rules,
    # and does not post complete corporate actions to the portfolio ledger.
    checks["execution_engine"] = {
        "effective_dated_market_rules_consumed": False,
        "historical_fee_schedule_consumed": False,
        "corporate_action_ledger_complete": False,
        "complete": False,
    }
    blockers.append("EXECUTION_ENGINE_DOES_NOT_YET_CONSUME_AUDITED_RULES_FEES_AND_ACTION_LEDGER")

    code_commit, clean_tree = _git_state()
    if not code_commit:
        blockers.append("AUDIT_CODE_COMMIT_UNKNOWN")
    if clean_tree is not True:
        blockers.append("AUDIT_CODE_WORKTREE_NOT_CLEAN")
    report = {
        "schema": READINESS_SCHEMA,
        "schema_version": 1,
        "audit_program_git_commit": code_commit,
        "audit_program_worktree_clean": clean_tree,
        "evaluation_window": {"start": start, "end": end},
        "universe_snapshot_id": universe_manifest.get("snapshot_id"),
        "input_hashes": {key: value for key, value in input_hashes.items() if not key.endswith("_path")},
        "checks": checks,
        "blockers": sorted(set(blockers)),
        "ready": not blockers,
        "decision": "READY" if not blockers else "NOT_READY",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    canonical = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    report["report_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return report


def require_baseline_ready(
    report: dict[str, Any],
    expected_input_hashes: dict[str, str],
    evaluation_start: str,
    evaluation_end: str,
    current_git_commit: str | None = None,
) -> None:
    """Reject missing, stale, hand-shaped, or input-mismatched readiness reports."""
    if report.get("schema") != READINESS_SCHEMA or report.get("schema_version") != 1:
        raise ValueError("正式基线就绪报告缺失或 schema 版本不支持")
    report_copy = dict(report)
    supplied_digest = report_copy.pop("report_sha256", None)
    canonical = json.dumps(report_copy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if supplied_digest != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
        raise ValueError("正式基线就绪报告自身散列校验失败")
    if report.get("ready") is not True or report.get("decision") != "READY" or report.get("blockers"):
        raise ValueError("正式基线未就绪：" + "; ".join(report.get("blockers", ["READINESS_NOT_READY"])))
    checks = report.get("checks", {})
    if not (
        checks.get("point_in_time_quality", {}).get("healthy") is True
        and checks.get("point_in_time_quality", {}).get("data_sha256_matches") is True
        and checks.get("point_in_time_quality", {}).get("universe_sha256_matches") is True
        and checks.get("point_in_time_quality", {}).get("identity_events_sha256_matches") is True
        and checks.get("point_in_time_recomputation", {}).get("complete") is True
        and checks.get("out_date_evidence", {}).get("healthy") is True
        and checks.get("unknown_types", {}).get("complete_for_current_universe") is True
        and checks.get("corporate_actions", {}).get("coverage", {}).get("complete") is True
        and checks.get("corporate_actions", {}).get("evidence", {}).get("complete") is True
        and checks.get("market_rules", {}).get("complete") is True
        and checks.get("fees", {}).get("complete") is True
        and checks.get("benchmark", {}).get("complete") is True
        and checks.get("execution_engine", {}).get("complete") is True
    ):
        raise ValueError("正式基线就绪报告的底层审计项不完整")
    if report.get("input_hashes") != expected_input_hashes:
        raise ValueError("正式基线就绪报告与当前数据、股票池、身份事件、公司行动、规则、费率或配置散列不一致")
    window = report.get("evaluation_window", {})
    if window != {
        "start": pd.Timestamp(evaluation_start).strftime("%Y-%m-%d"),
        "end": pd.Timestamp(evaluation_end).strftime("%Y-%m-%d"),
    }:
        raise ValueError("正式基线就绪报告的评价窗口与本次运行不一致")
    if current_git_commit and report.get("audit_program_git_commit") != current_git_commit:
        raise ValueError("正式基线就绪报告由不同 Git 提交生成，必须重新运行 readiness audit")
    if report.get("audit_program_worktree_clean") is not True:
        raise ValueError("正式基线就绪报告不是从干净的 Git 工作区生成")
    _, current_clean = _git_state()
    if current_clean is not True:
        raise ValueError("当前 Git 工作区有未提交改动；正式基线就绪报告必须从干净提交复核")


def require_report_matches_recomputed(saved: dict[str, Any], recomputed: dict[str, Any]) -> None:
    """Ensure the persisted report is exactly the decision current code recomputes."""
    compared_fields = (
        "schema", "schema_version", "audit_program_git_commit", "audit_program_worktree_clean",
        "evaluation_window", "universe_snapshot_id", "input_hashes", "checks", "blockers",
        "ready", "decision",
    )
    mismatches = [field for field in compared_fields if saved.get(field) != recomputed.get(field)]
    if mismatches:
        raise ValueError(
            "正式基线就绪报告与当前输入重新审计结果不一致：" + ", ".join(mismatches)
        )
