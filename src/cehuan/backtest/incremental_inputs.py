"""Report-only preflight for proposed checkpoint input increments.

This module does not alter or relax ``run_backtest`` checkpoint validation.
It evaluates a versioned proof document and returns an auditable compatibility
report for synthetic and future integration work.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any, Mapping


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value.strip()):
        raise ValueError(f"{field} 必须是 64 位十六进制 SHA-256")
    return value.strip().lower()


def _day(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD")
    return parsed


def _instant(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是带时区的 ISO 时间；仅有日期不能用于点时判断")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} 必须是带时区的 ISO 时间；仅有日期不能用于点时判断") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区；仅有日期或本地时间不能用于点时判断")
    return parsed.astimezone(timezone.utc)


def _unique_rows(value: Any, key: str, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label}[{index}] 必须是对象")
        row_key = str(row.get(key, "")).strip()
        if not row_key:
            raise ValueError(f"{label}[{index}].{key} 不能为空")
        if row_key in result:
            raise ValueError(f"{label} 存在重复 {key}={row_key}")
        result[row_key] = dict(row)
    return result


def _validate_proof(proof: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(proof, Mapping) or proof.get("schema_version") != 1:
        raise ValueError(f"{label} 必须是 schema_version=1 的对象")
    checkpoint = proof.get("checkpoint")
    inputs = proof.get("inputs")
    if not isinstance(checkpoint, Mapping) or not isinstance(inputs, Mapping):
        raise ValueError(f"{label} 必须包含 checkpoint 与 inputs 对象")
    _sha(checkpoint.get("state_sha256"), f"{label}.checkpoint.state_sha256")
    as_of = _day(checkpoint.get("as_of_date"), f"{label}.checkpoint.as_of_date")
    next_session = _day(checkpoint.get("next_expected_session"), f"{label}.checkpoint.next_expected_session")
    if next_session <= as_of:
        raise ValueError(f"{label}.checkpoint.next_expected_session 必须晚于 as_of_date")
    _instant(checkpoint.get("decision_cutoff_utc"), f"{label}.checkpoint.decision_cutoff_utc")
    if not str(checkpoint.get("cutoff_source", "")).strip():
        raise ValueError(f"{label}.checkpoint.cutoff_source 不能为空")
    _sha(checkpoint.get("cutoff_source_sha256"), f"{label}.checkpoint.cutoff_source_sha256")
    if "applied_increment_ids" not in checkpoint or "applied_increment_sha256s" not in checkpoint:
        raise ValueError(f"{label}.checkpoint 必须记录已应用增量 ID 与内容摘要")
    increment_ids = checkpoint["applied_increment_ids"]
    if not isinstance(increment_ids, list) or any(not isinstance(item, str) or not item.strip() for item in increment_ids):
        raise ValueError(f"{label}.checkpoint.applied_increment_ids 必须是非空字符串数组")
    increment_hashes = checkpoint["applied_increment_sha256s"]
    if not isinstance(increment_hashes, list):
        raise ValueError(f"{label}.checkpoint.applied_increment_sha256s 必须是数组")
    for index, item in enumerate(increment_hashes):
        _sha(item, f"{label}.checkpoint.applied_increment_sha256s[{index}]")
    for field in ("strategy_config_sha256", "historical_bars_prefix_sha256"):
        _sha(inputs.get(field), f"{label}.inputs.{field}")
    events = _unique_rows(inputs.get("events"), "event_id", f"{label}.inputs.events")
    for event_id, row in events.items():
        _day(row.get("first_impact_date"), f"{label}.inputs.events[{event_id}].first_impact_date")
        _sha(row.get("content_sha256"), f"{label}.inputs.events[{event_id}].content_sha256")
        _sha(row.get("source_sha256"), f"{label}.inputs.events[{event_id}].source_sha256")
    rules = _unique_rows(inputs.get("rules"), "row_id", f"{label}.inputs.rules")
    fees = _unique_rows(inputs.get("fees"), "row_id", f"{label}.inputs.fees")
    for row_id, row in [*rules.items(), *fees.items()]:
        _day(row.get("valid_from"), f"{label}.inputs.schedule[{row_id}].valid_from")
        _sha(row.get("content_sha256"), f"{label}.inputs.schedule[{row_id}].content_sha256")
    calendar = inputs.get("calendar")
    if not isinstance(calendar, list):
        raise ValueError(f"{label}.inputs.calendar 必须是数组")
    seen_days: set[str] = set()
    for index, row in enumerate(calendar):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label}.inputs.calendar[{index}] 必须是对象")
        day = _day(row.get("date"), f"{label}.inputs.calendar[{index}].date")
        if day.isoformat() in seen_days:
            raise ValueError(f"{label}.inputs.calendar 存在重复日期 {day}")
        if not isinstance(row.get("is_open"), bool):
            raise ValueError(f"{label}.inputs.calendar[{index}].is_open 必须是布尔值")
        seen_days.add(day.isoformat())
    return dict(checkpoint), dict(inputs)


def _row_fingerprint(row: Mapping[str, Any], field: str, label: str) -> str:
    return _sha(row.get(field), f"{label}.{field}")


def _increment_digest(additions: Mapping[str, Any]) -> str:
    payload = {"additions": additions}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def audit_incremental_inputs(baseline_proof: Mapping[str, Any], candidate_proof: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two proofs without making an engine continuation executable.

    A clean report means only that the candidate input set satisfies this
    preflight contract. The current backtest engine still requires exact full
    input hashes and cannot consume candidate additions.
    """
    base_cp, base = _validate_proof(baseline_proof, "baseline")
    candidate_cp, current = _validate_proof(candidate_proof, "candidate")
    for field in (
        "state_sha256", "as_of_date", "next_expected_session", "decision_cutoff_utc",
        "cutoff_source", "cutoff_source_sha256", "applied_increment_ids", "applied_increment_sha256s",
    ):
        if base_cp[field] != candidate_cp[field]:
            raise ValueError(f"baseline 与 candidate checkpoint.{field} 不一致")
    as_of = _day(base_cp["as_of_date"], "checkpoint.as_of_date")
    cutoff = _instant(base_cp["decision_cutoff_utc"], "checkpoint.decision_cutoff_utc")
    blockers: list[dict[str, str]] = []
    changes: list[dict[str, str]] = []
    additions: dict[str, list[str]] = {"events": [], "calendar": [], "rules": [], "fees": []}
    addition_content: dict[str, list[dict[str, Any]]] = {
        "events": [], "calendar": [], "rules": [], "fees": [],
    }
    affected_dates: list[date] = []

    def block(code: str, detail: str, affected: date | None = None) -> None:
        blockers.append({"code": code, "detail": detail})
        if affected is not None:
            affected_dates.append(affected)

    for field, label in (("strategy_config_sha256", "策略配置"), ("historical_bars_prefix_sha256", "已使用行情前缀")):
        if base[field] != current[field]:
            block("HISTORICAL_INPUT_CHANGED", f"{label}摘要发生变化")

    # Existing events are immutable. Only an event with a new ID can be a candidate append.
    base_events = _unique_rows(base["events"], "event_id", "baseline.events")
    current_events = _unique_rows(current["events"], "event_id", "candidate.events")
    for event_id, row in base_events.items():
        try:
            _instant(row.get("known_at_utc"), f"baseline.events[{event_id}].known_at_utc")
        except ValueError as exc:
            block("BASELINE_EVENT_PROVENANCE_MISSING", f"已使用事件 {event_id} 缺少可得时点证明: {exc}")
    for event_id in sorted(base_events.keys() - current_events.keys()):
        row = base_events[event_id]
        impact = _day(row.get("first_impact_date"), f"baseline.events[{event_id}].first_impact_date")
        changes.append({"input": "event", "event_id": event_id, "change": "removed"})
        block("USED_EVENT_REMOVED", f"已使用事件 {event_id} 被删除", impact)
    for event_id in sorted(base_events.keys() & current_events.keys()):
        left, right = base_events[event_id], current_events[event_id]
        old_hash = _row_fingerprint(left, "content_sha256", f"baseline.events[{event_id}]")
        new_hash = _row_fingerprint(right, "content_sha256", f"candidate.events[{event_id}]")
        if old_hash != new_hash:
            impact_values = [_day(left.get("first_impact_date"), f"baseline.events[{event_id}].first_impact_date"),
                             _day(right.get("first_impact_date"), f"candidate.events[{event_id}].first_impact_date")]
            earliest = min(impact_values)
            changes.append({"input": "event", "event_id": event_id, "change": "content_changed"})
            block("USED_EVENT_CORRECTED", f"已使用事件 {event_id} 内容被更正", earliest)
    new_event_rows = [current_events[key] for key in sorted(current_events.keys() - base_events.keys())]
    for row in new_event_rows:
        event_id = str(row["event_id"])
        try:
            known_at = _instant(row.get("known_at_utc"), f"candidate.events[{event_id}].known_at_utc")
        except ValueError as exc:
            block("EVENT_KNOWN_AT_UNPROVEN", f"事件 {event_id}: {exc}")
            continue
        impact = _day(row.get("first_impact_date"), f"candidate.events[{event_id}].first_impact_date")
        try:
            _sha(row.get("source_sha256"), f"candidate.events[{event_id}].source_sha256")
            _sha(row.get("content_sha256"), f"candidate.events[{event_id}].content_sha256")
        except ValueError as exc:
            block("EVENT_EVIDENCE_INVALID", f"事件 {event_id}: {exc}", impact)
            continue
        if known_at <= cutoff:
            known_day = known_at.astimezone(timezone(timedelta(hours=8))).date()
            block("EVENT_NOT_PROVEN_NEW", f"事件 {event_id} 的可得时点不晚于决策截止时点", known_day)
        elif impact <= as_of:
            block("LATE_EVENT_AFFECTS_HISTORY", f"新发现事件 {event_id} 的首次影响日 {impact} 不晚于 checkpoint；必须回溯重算", impact)
        else:
            additions["events"].append(event_id)
            addition_content["events"].append({"event_id": event_id, "content_sha256": row["content_sha256"].lower()})

    # Preserve all calendar history and the originally recorded next open session.
    def calendar_map(rows: list[dict[str, Any]], label: str) -> dict[date, bool]:
        return {_day(row["date"], f"{label}.date"): row["is_open"] for row in rows}

    base_calendar = calendar_map(base["calendar"], "baseline.calendar")
    current_calendar = calendar_map(current["calendar"], "candidate.calendar")
    expected = _day(base_cp["next_expected_session"], "checkpoint.next_expected_session")
    if base_calendar.get(expected) is not True or current_calendar.get(expected) is not True:
        block("NEXT_SESSION_NOT_PRESERVED", f"checkpoint 记录的下一开市日 {expected} 缺失或未标为开市", expected)
    for day, is_open in base_calendar.items():
        if day not in current_calendar:
            block("CALENDAR_ROW_REMOVED", f"候选日历删除已有日期 {day}", day)
        elif current_calendar[day] != is_open:
            block("CALENDAR_HISTORY_CHANGED" if day <= expected else "CALENDAR_EXISTING_ROW_CHANGED",
                  f"已有日期 {day} 的开市状态被修改", day)
    for day in sorted(current_calendar.keys() - base_calendar.keys()):
        if day <= as_of:
            block("CALENDAR_HISTORICAL_ROW_ADDED", f"候选日历在 checkpoint 当日或之前新增日期 {day}", day)
        else:
            additions["calendar"].append(day.isoformat())
            addition_content["calendar"].append({"date": day.isoformat(), "is_open": current_calendar[day]})

    # Existing rule/fee rows are immutable. New rows require explicit future validity,
    # exact known-at time and source hash; current project schedules do not yet carry
    # these fields, so their unproven additions are rejected.
    for field, label in (("rules", "交易规则"), ("fees", "费率")):
        left_rows = _unique_rows(base[field], "row_id", f"baseline.{field}")
        right_rows = _unique_rows(current[field], "row_id", f"candidate.{field}")
        for row_id in sorted(left_rows.keys() - right_rows.keys()):
            row = left_rows[row_id]
            valid_from = _day(row.get("valid_from"), f"baseline.{field}[{row_id}].valid_from")
            changes.append({"input": field[:-1], "row_id": row_id, "change": "removed"})
            block("USED_SCHEDULE_ROW_REMOVED", f"已使用{label}行 {row_id} 被删除", valid_from)
        for row_id in sorted(left_rows.keys() & right_rows.keys()):
            old_hash = _row_fingerprint(left_rows[row_id], "content_sha256", f"baseline.{field}[{row_id}]")
            new_hash = _row_fingerprint(right_rows[row_id], "content_sha256", f"candidate.{field}[{row_id}]")
            if old_hash != new_hash:
                valid_from = min(
                    _day(left_rows[row_id].get("valid_from"), f"baseline.{field}[{row_id}].valid_from"),
                    _day(right_rows[row_id].get("valid_from"), f"candidate.{field}[{row_id}].valid_from"),
                )
                changes.append({"input": field[:-1], "row_id": row_id, "change": "content_changed"})
                block("HISTORICAL_SCHEDULE_ROW_CHANGED", f"已使用{label}行 {row_id} 发生变化", valid_from)
        for row_id in sorted(right_rows.keys() - left_rows.keys()):
            row = right_rows[row_id]
            valid_from = _day(row.get("valid_from"), f"candidate.{field}[{row_id}].valid_from")
            if valid_from <= as_of:
                block("HISTORICAL_SCHEDULE_ROW_ADDED", f"新增{label}行 {row_id} 的 valid_from={valid_from} 不晚于 checkpoint", valid_from)
                continue
            try:
                known_at = _instant(row.get("known_at_utc"), f"candidate.{field}[{row_id}].known_at_utc")
                _sha(row.get("source_sha256"), f"candidate.{field}[{row_id}].source_sha256")
                _sha(row.get("content_sha256"), f"candidate.{field}[{row_id}].content_sha256")
            except ValueError as exc:
                block("SCHEDULE_APPEND_EVIDENCE_MISSING", f"新增{label}行 {row_id} 缺少可得时间或来源证据: {exc}", valid_from)
                continue
            if known_at <= cutoff:
                block("SCHEDULE_APPEND_NOT_PROVEN_NEW", f"新增{label}行 {row_id} 的可得时点不晚于决策截止时点", valid_from)
            else:
                additions[field].append(row_id)
                addition_content[field].append({"row_id": row_id, "content_sha256": row["content_sha256"].lower()})

    increment_id = str(candidate_proof.get("increment_id", "")).strip()
    increment_sha = _increment_digest(addition_content)
    if not increment_id:
        block("INCREMENT_ID_MISSING", "candidate.increment_id 不能为空")
    elif increment_id in set(base_cp.get("applied_increment_ids", [])):
        block("INCREMENT_ALREADY_APPLIED", f"增量 {increment_id} 已记录为应用过")
    if increment_sha in {_sha(item, "checkpoint.applied_increment_sha256s") for item in base_cp.get("applied_increment_sha256s", [])}:
        block("INCREMENT_ALREADY_APPLIED", f"内容摘要 {increment_sha} 已记录为应用过")

    if not increment_id:
        increment_sha = None
    earliest = min(affected_dates).isoformat() if affected_dates else None
    candidate_compatible = not blockers
    historical_blocker_codes = {
        "HISTORICAL_INPUT_CHANGED", "USED_EVENT_REMOVED", "USED_EVENT_CORRECTED",
        "LATE_EVENT_AFFECTS_HISTORY", "CALENDAR_ROW_REMOVED", "CALENDAR_HISTORY_CHANGED",
        "CALENDAR_HISTORICAL_ROW_ADDED", "USED_SCHEDULE_ROW_REMOVED",
        "HISTORICAL_SCHEDULE_ROW_CHANGED", "HISTORICAL_SCHEDULE_ROW_ADDED",
        "BASELINE_EVENT_PROVENANCE_MISSING", "EVENT_KNOWN_AT_UNPROVEN", "EVENT_NOT_PROVEN_NEW",
    }
    requires_recompute = any(item["code"] in historical_blocker_codes for item in blockers)
    return {
        "schema_version": 1,
        "report_type": "checkpoint_incremental_input_preflight_only",
        "candidate_append_compatible": candidate_compatible,
        "can_continue": False,
        "engine_resume_supported": False,
        "engine_note": "当前 run_backtest 仍要求完整输入哈希一致；本报告不会放行续跑。",
        "checkpoint": {
            "state_sha256": str(base_cp["state_sha256"]).lower(),
            "as_of_date": as_of.isoformat(),
            "next_expected_session": expected.isoformat(),
            "decision_cutoff_utc": _instant(base_cp["decision_cutoff_utc"], "decision_cutoff_utc").isoformat(),
            "cutoff_source": str(base_cp["cutoff_source"]),
            "cutoff_source_sha256": str(base_cp["cutoff_source_sha256"]).lower(),
        },
        "increment_id": increment_id or None,
        "increment_sha256": increment_sha,
        "additions": additions,
        "addition_content": addition_content,
        "changes": changes,
        "blockers": blockers,
        "earliest_possible_affected_date": earliest,
        "earliest_affected_date_precision": (
            "EXACT_FROM_CHANGED_EVENT_OR_SCHEDULE" if earliest else
            "UNKNOWN_HASH_BOUND_INPUT_REQUIRES_FULL_WINDOW_RECALCULATION" if requires_recompute else
            "NO_HISTORICAL_CHANGE_IDENTIFIED"
        ),
        "requires_recompute": requires_recompute,
    }
