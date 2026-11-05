from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
from typing import Any

from cehuan.research.observation_parsers import (
    AKSHARE_MINUTE_PARSER,
    PARSER_VERSION,
    SYNTHETIC_QUOTE_PARSER,
    SYNTHETIC_STATUS_PARSER,
    canonical_payload_sha256,
    parse_observation_payload,
)

PAPER_OBSERVATION_SCHEMA_VERSION = 2
SUPPORTED_PAPER_OBSERVATION_SCHEMA_VERSIONS = {1, 2}
OBSERVATION_OUTCOMES = {"AVAILABLE", "MISSING", "ERROR", "PARSE_FAILED"}
OBSERVATION_PHASES = {"DECISION", "EXECUTION"}
OBSERVATION_TYPES = {"QUOTE", "STATUS"}


def validate_paper_observation_file(path: str | Path) -> dict[str, Any]:
    record_path = Path(path).resolve()
    document = json.loads(record_path.read_text(encoding="utf-8"))
    return validate_paper_observation_document(document, base_dir=record_path.parent)


def validate_paper_observation_document(
    document: dict[str, Any], *, base_dir: str | Path,
) -> dict[str, Any]:
    version = document.get("schema_version")
    if version not in SUPPORTED_PAPER_OBSERVATION_SCHEMA_VERSIONS:
        raise ValueError("paper observation schema_version 不支持")
    if version == 1:
        return _validate_legacy_v1(document, Path(base_dir))
    return _validate_v2(document, Path(base_dir))


def _validate_legacy_v1(document: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Read old synthetic records without pretending their payload was rebuilt."""
    if document.get("record_type") != "PAPER_OBSERVATION" or not str(document.get("observation_id", "")).strip():
        raise ValueError("记录必须包含 PAPER_OBSERVATION 类型和 observation_id")
    classification = document.get("classification")
    if classification not in {"SYNTHETIC", "FORWARD_PAPER"}:
        raise ValueError("classification 必须是 SYNTHETIC 或 FORWARD_PAPER")
    signal = document.get("signal")
    if not isinstance(signal, dict):
        raise ValueError("signal 必须为对象")
    _aware_timestamp(signal.get("signal_date") + "T15:00:00+08:00" if isinstance(signal.get("signal_date"), str) else None, "signal.signal_date")
    _aware_timestamp(signal.get("generated_at_utc"), "signal.generated_at_utc")
    _validate_file_binding(signal.get("ranking_artifact"), base_dir, "signal.ranking_artifact")
    config_hash = str(signal.get("config_sha256", "")).lower()
    if len(config_hash) != 64 or any(character not in "0123456789abcdef" for character in config_hash):
        raise ValueError("signal.config_sha256 必须是 64 位十六进制")
    entries = document.get("observations")
    if not isinstance(entries, list) or not entries:
        raise ValueError("observations 必须是非空数组")
    attempts_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"observations[{index}] 必须为对象")
        symbol = str(entry.get("symbol", "")).strip().upper()
        phase = entry.get("phase")
        kind = entry.get("observation_type")
        outcome = entry.get("outcome")
        if not symbol or phase not in OBSERVATION_PHASES or kind not in OBSERVATION_TYPES or outcome not in OBSERVATION_OUTCOMES:
            raise ValueError(f"observations[{index}] 的 symbol/phase/type/outcome 无效")
        attempt_id = str(entry.get("attempt_id", "")).strip()
        if not attempt_id or attempt_id in seen_ids:
            raise ValueError(f"observations[{index}] attempt_id 缺失或重复")
        seen_ids.add(attempt_id)
        attempt_no = entry.get("attempt_no")
        if not isinstance(attempt_no, int) or attempt_no < 1:
            raise ValueError(f"observations[{index}] attempt_no 必须为正整数")
        captured = _aware_timestamp(entry.get("captured_at_utc"), f"observations[{index}].captured_at_utc")
        source = entry.get("source")
        if not isinstance(source, dict) or not str(source.get("name", "")).strip():
            raise ValueError(f"observations[{index}].source.name 缺失")
        for timestamp_field in ("event_time_utc", "provider_published_at_utc"):
            raw_time = source.get(timestamp_field)
            if raw_time not in (None, ""):
                _aware_timestamp(raw_time, f"observations[{index}].source.{timestamp_field}")
        raw_response = entry.get("raw_response")
        _validate_file_binding(raw_response, base_dir, f"observations[{index}].raw_response")
        payload = entry.get("payload")
        if outcome == "AVAILABLE":
            if not isinstance(payload, dict):
                raise ValueError(f"observations[{index}] AVAILABLE 必须包含 payload")
            _validate_payload_v1(kind, payload, index)
        elif payload is not None:
            raise ValueError(f"observations[{index}] 的 {outcome} payload 必须为 null")
        key = (symbol, phase, kind)
        attempts_by_key.setdefault(key, []).append({**entry, "_captured_parsed": captured})

    result_rows: list[dict[str, Any]] = []
    for key, attempts in sorted(attempts_by_key.items()):
        attempts.sort(key=lambda row: row["attempt_no"])
        numbers = [row["attempt_no"] for row in attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"{key} 的 attempt_no 必须从 1 连续递增")
        captured_times = [row["_captured_parsed"] for row in attempts]
        if captured_times != sorted(captured_times):
            raise ValueError(f"{key} 的 captured_at_utc 必须随重试单调递增")
        final = attempts[-1]
        failed_before_recovery = any(row["outcome"] in {"MISSING", "ERROR"} for row in attempts[:-1])
        result_rows.append({
            "symbol": key[0],
            "phase": key[1],
            "observation_type": key[2],
            "attempt_count": len(attempts),
            "final_outcome": final["outcome"],
            "recovered_after_failure": bool(failed_before_recovery and final["outcome"] == "AVAILABLE"),
            "provider_publish_time_status": "RECORDED" if final["source"].get("provider_published_at_utc") else "UNVERIFIED",
            "source_event_time_status": "RECORDED" if final["source"].get("event_time_utc") else "UNVERIFIED",
            "latest_capture_time_utc": final["captured_at_utc"],
            "payload_rebuilt_from_raw": "UNVERIFIED_LEGACY_SCHEMA_HAS_NO_PARSER_BINDING",
        })
    return {
        "status": "VALID_LEGACY_UNVERIFIED_PAYLOAD_BINDING",
        "classification": classification,
        "observation_id": document["observation_id"],
        "schema_valid": "VERIFIED",
        "raw_file_hash_valid": "VERIFIED",
        "payload_rebuilt_from_raw": "UNVERIFIED_LEGACY_SCHEMA_HAS_NO_PARSER_BINDING",
        "payload_matches_record": "UNVERIFIED",
        "time_evidence_status": "UNVERIFIED_LEGACY_CAPTURE_TIME_ONLY",
        "observation_keys": len(result_rows),
        "attempt_count": len(entries),
        "final_available_count": sum(row["final_outcome"] == "AVAILABLE" for row in result_rows),
        "final_missing_count": sum(row["final_outcome"] == "MISSING" for row in result_rows),
        "final_error_count": sum(row["final_outcome"] == "ERROR" for row in result_rows),
        "recovered_after_failure_count": sum(row["recovered_after_failure"] for row in result_rows),
        "provider_publication_timestamp_recorded_count": sum(row["provider_publish_time_status"] == "RECORDED" for row in result_rows),
        "historical_point_in_time_claim": "NOT_ESTABLISHED_BY_LOCAL_CAPTURE_OR_RECORDED_VENDOR_TIMESTAMP_ALONE",
        "orders_or_broker_calls": False,
        "keys": result_rows,
    }


def _validate_v2(document: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    if document.get("record_type") != "PAPER_OBSERVATION" or not str(document.get("observation_id", "")).strip():
        raise ValueError("记录必须包含 PAPER_OBSERVATION 类型和 observation_id")
    classification = document.get("classification")
    if classification not in {"SYNTHETIC", "FORWARD_PAPER"}:
        raise ValueError("classification 必须是 SYNTHETIC 或 FORWARD_PAPER")
    if document.get("trusted_external_timestamp", "UNVERIFIED") != "UNVERIFIED":
        raise ValueError("本地未签名纸面记录不能声称 trusted_external_timestamp 已验证")
    signal = document.get("signal")
    schedule = document.get("observation_schedule")
    if not isinstance(signal, dict) or not isinstance(schedule, dict):
        raise ValueError("schema v2 必须包含 signal 和 observation_schedule 对象")
    signal_date = signal.get("signal_date")
    _aware_timestamp(f"{signal_date}T15:00:00+08:00" if isinstance(signal_date, str) else None, "signal.signal_date")
    generated = _aware_timestamp(signal.get("generated_at_utc"), "signal.generated_at_utc")
    decision_at = _aware_timestamp(schedule.get("decision_at_utc"), "observation_schedule.decision_at_utc")
    execution_at = _aware_timestamp(schedule.get("execution_at_utc"), "observation_schedule.execution_at_utc")
    if generated > decision_at:
        raise ValueError("排名生成时间晚于补位决策时点")
    if execution_at <= decision_at:
        raise ValueError("执行观察时点必须晚于补位决策时点")
    _validate_file_binding(signal.get("ranking_artifact"), base_dir, "signal.ranking_artifact")
    config_hash = str(signal.get("config_sha256", "")).lower()
    if len(config_hash) != 64 or any(character not in "0123456789abcdef" for character in config_hash):
        raise ValueError("signal.config_sha256 必须是 64 位十六进制")

    entries = document.get("observations")
    if not isinstance(entries, list) or not entries:
        raise ValueError("observations 必须是非空数组")
    attempts_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    rebuilt_count = 0
    payload_match_count = 0
    late_count = 0
    timeline_rows: list[dict[str, Any]] = []
    key_payloads: dict[tuple[str, str, str], dict[str, Any] | None] = {}
    payload_by_attempt: dict[str, dict[str, Any] | None] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"observations[{index}] 必须为对象")
        symbol = str(entry.get("symbol", "")).strip().upper()
        phase, kind, outcome = entry.get("phase"), entry.get("observation_type"), entry.get("outcome")
        if not symbol or phase not in OBSERVATION_PHASES or kind not in OBSERVATION_TYPES or outcome not in OBSERVATION_OUTCOMES:
            raise ValueError(f"observations[{index}] 的 symbol/phase/type/outcome 无效")
        attempt_id = str(entry.get("attempt_id", "")).strip()
        if not attempt_id or attempt_id in seen_ids:
            raise ValueError(f"observations[{index}] attempt_id 缺失或重复")
        seen_ids.add(attempt_id)
        attempt_no = entry.get("attempt_no")
        if not isinstance(attempt_no, int) or isinstance(attempt_no, bool) or attempt_no < 1:
            raise ValueError(f"observations[{index}].attempt_no 必须为正整数")

        request_started = _aware_timestamp(entry.get("request_started_at_utc"), f"observations[{index}].request_started_at_utc")
        response_received = _aware_timestamp(entry.get("response_received_at_utc"), f"observations[{index}].response_received_at_utc")
        planned = _aware_timestamp(entry.get("planned_observation_at_utc"), f"observations[{index}].planned_observation_at_utc")
        if request_started > response_received:
            raise ValueError(f"observations[{index}] 请求开始时间晚于响应/错误记录时间")
        if entry.get("captured_at_utc") not in (None, ""):
            legacy_capture = _aware_timestamp(entry["captured_at_utc"], f"observations[{index}].captured_at_utc")
            if legacy_capture != response_received:
                raise ValueError(f"observations[{index}] captured_at_utc 与 response_received_at_utc 不一致")

        source = entry.get("source")
        if not isinstance(source, dict) or not str(source.get("name", "")).strip():
            raise ValueError(f"observations[{index}].source.name 缺失")
        parser_name, parser_version = source.get("parser_name"), str(source.get("parser_version", ""))
        if not parser_name or not parser_version:
            raise ValueError(f"observations[{index}] 缺少 parser_name/parser_version")
        event_raw = source.get("event_time_utc")
        event_time = _aware_timestamp(event_raw, f"observations[{index}].source.event_time_utc") if event_raw not in (None, "") else None
        published_raw = source.get("provider_published_at_utc")
        published = _aware_timestamp(published_raw, f"observations[{index}].source.provider_published_at_utc") if published_raw not in (None, "") else None
        if event_time is not None and event_time > response_received:
            raise ValueError(f"observations[{index}] 市场事件时间晚于本机接收时间")
        if published is not None and published > response_received:
            raise ValueError(f"observations[{index}] 供应商发布时间晚于本机接收时间")

        if phase == "DECISION" and planned > decision_at:
            raise ValueError(f"observations[{index}] 决策阶段 planned_observation 晚于 decision_at")
        if phase == "EXECUTION" and planned < execution_at:
            raise ValueError(f"observations[{index}] 执行阶段 planned_observation 早于 execution_at")
        if phase == "EXECUTION" and event_time is not None and event_time <= decision_at:
            raise ValueError(f"observations[{index}] 执行阶段事件时间不晚于决策时点")

        raw_binding = entry.get("raw_response")
        raw_path = _validate_file_binding(raw_binding, base_dir, f"observations[{index}].raw_response")
        if not isinstance(raw_binding, dict) or raw_binding.get("format") != "JSON":
            raise ValueError(f"observations[{index}].raw_response.format 当前必须为 JSON")
        capture_layer = raw_binding.get("capture_layer")
        if capture_layer not in {"SDK_SERIALIZED_RETURN", "HTTP_BODY", "SYNTHETIC_RESPONSE", "LOCAL_ERROR_RECORD"}:
            raise ValueError(f"observations[{index}].raw_response.capture_layer 无效")

        payload = entry.get("payload")
        if outcome == "AVAILABLE":
            if not isinstance(payload, dict):
                raise ValueError(f"observations[{index}] AVAILABLE 必须包含规范化 payload")
            _validate_payload(kind, payload, index)
            rebuilt = parse_observation_payload(
                raw_path.read_bytes(), parser_name=str(parser_name), parser_version=parser_version,
                observation_type=kind, symbol=symbol, expected_event_time_utc=event_raw,
                source_metadata=source.get("parser_metadata") if isinstance(source.get("parser_metadata"), dict) else {},
            )
            if rebuilt is None:
                raise ValueError(f"observations[{index}] 原始响应中没有对应事件，但 outcome=AVAILABLE")
            rebuilt_count += 1
            if rebuilt != payload:
                raise ValueError(f"observations[{index}] payload 与原始响应重新解析结果不一致")
            payload_digest = str(entry.get("normalized_payload_sha256", "")).lower()
            if payload_digest != canonical_payload_sha256(rebuilt):
                raise ValueError(f"observations[{index}] normalized_payload_sha256 不匹配")
            payload_match_count += 1
            payload_by_attempt[attempt_id] = rebuilt
        else:
            if payload is not None:
                raise ValueError(f"observations[{index}] {outcome} 的 payload 必须为 null")
            if outcome == "ERROR" and not str(entry.get("error_code", "")).strip():
                raise ValueError(f"observations[{index}] ERROR 必须保留 error_code")
            if outcome == "PARSE_FAILED" and not str(entry.get("parse_error", "")).strip():
                raise ValueError(f"observations[{index}] PARSE_FAILED 必须保留 parse_error")
            if outcome == "ERROR" and capture_layer != "LOCAL_ERROR_RECORD":
                raise ValueError(f"observations[{index}] ERROR 必须标记 LOCAL_ERROR_RECORD")
            if outcome == "PARSE_FAILED" and capture_layer == "LOCAL_ERROR_RECORD":
                raise ValueError(f"observations[{index}] PARSE_FAILED 应归档业务响应而非本地错误记录")
            if outcome == "PARSE_FAILED":
                try:
                    parse_observation_payload(
                        raw_path.read_bytes(), parser_name=str(parser_name), parser_version=parser_version,
                        observation_type=kind, symbol=symbol, expected_event_time_utc=event_raw,
                        source_metadata=source.get("parser_metadata") if isinstance(source.get("parser_metadata"), dict) else {},
                    )
                except Exception:
                    pass
                else:
                    raise ValueError(f"observations[{index}] 标记 PARSE_FAILED，但指定解析器可正常重建")
            payload_by_attempt[attempt_id] = None

        late = response_received > planned
        if late:
            late_count += 1
        timeline_rows.append({
            "symbol": symbol, "phase": phase, "observation_type": kind, "attempt_id": attempt_id,
            "request_started_at_utc": entry["request_started_at_utc"],
            "response_received_at_utc": entry["response_received_at_utc"],
            "planned_observation_at_utc": entry["planned_observation_at_utc"],
            "decision_timing_status": "LATE_FOR_PLANNED_DECISION" if late and phase == "DECISION" else (
                "ON_TIME_OR_EARLY" if not late and phase == "DECISION" else "LATE_FOR_PLANNED_EXECUTION" if late else "ON_TIME_OR_EARLY"
            ),
            "provider_publish_time_status": "RECORDED_SOURCE_UNVERIFIED" if published is not None else "UNVERIFIED",
            "event_time_status": "RECORDED" if event_time is not None else "UNVERIFIED",
            "raw_file_hash_valid": "VERIFIED",
            "payload_rebuilt_from_raw": "VERIFIED" if outcome == "AVAILABLE" else "NOT_APPLICABLE",
        })
        key = (symbol, phase, kind)
        attempts_by_key.setdefault(key, []).append({
            **entry, "_request_parsed": request_started, "_received_parsed": response_received,
        })

    result_rows = []
    for key, attempts in sorted(attempts_by_key.items()):
        attempts.sort(key=lambda row: row["attempt_no"])
        numbers = [row["attempt_no"] for row in attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError(f"{key} 的 attempt_no 必须从 1 连续递增")
        for previous, current in zip(attempts, attempts[1:]):
            if current["_request_parsed"] < previous["_received_parsed"]:
                raise ValueError(f"{key} 重试请求开始时间早于上次响应/错误记录")
        final = attempts[-1]
        key_payloads[key] = payload_by_attempt[final["attempt_id"]]
        failed_before_recovery = any(row["outcome"] in {"MISSING", "ERROR", "PARSE_FAILED"} for row in attempts[:-1])
        result_rows.append({
            "symbol": key[0], "phase": key[1], "observation_type": key[2],
            "attempt_count": len(attempts), "final_outcome": final["outcome"],
            "recovered_after_failure": bool(failed_before_recovery and final["outcome"] == "AVAILABLE"),
            "payload_rebuilt_from_raw": "VERIFIED" if final["outcome"] == "AVAILABLE" else "NOT_APPLICABLE",
            "provider_publish_time_status": "RECORDED_SOURCE_UNVERIFIED" if final["source"].get("provider_published_at_utc") else "UNVERIFIED",
            "latest_response_received_at_utc": final["response_received_at_utc"],
        })

    eligibility = _eligibility_summary(
        result_rows, key_payloads, document.get("eligibility_inputs", {}), decision_at,
    )
    status = "VALID_WITH_DATA_GAPS" if any(row["final_outcome"] != "AVAILABLE" for row in result_rows) else "VALID"
    if late_count:
        status = "VALID_WITH_LATE_OBSERVATIONS"
    return {
        "status": status, "classification": classification, "observation_id": document["observation_id"],
        "schema_valid": "VERIFIED", "raw_file_hash_valid": "VERIFIED",
        "payload_rebuilt_from_raw": "VERIFIED" if rebuilt_count else "NOT_APPLICABLE",
        "payload_matches_record": "VERIFIED" if payload_match_count == rebuilt_count and rebuilt_count else "NOT_APPLICABLE",
        "time_evidence_status": "LOCAL_REQUEST_AND_RECEIVE_TIMES_RECORDED_PROVIDER_TIME_SEPARATE",
        "historical_point_in_time_status": "NOT_ESTABLISHED_BY_CAPTURE_ALONE",
        "observation_keys": len(result_rows), "attempt_count": len(entries),
        "final_available_count": sum(row["final_outcome"] == "AVAILABLE" for row in result_rows),
        "final_missing_count": sum(row["final_outcome"] == "MISSING" for row in result_rows),
        "final_error_count": sum(row["final_outcome"] == "ERROR" for row in result_rows),
        "final_parse_failed_count": sum(row["final_outcome"] == "PARSE_FAILED" for row in result_rows),
        "recovered_after_failure_count": sum(row["recovered_after_failure"] for row in result_rows),
        "late_observation_count": late_count, "timeline": timeline_rows,
        "eligibility": eligibility, "keys": result_rows,
        "execution_status": "NO_BROKER_FILL_EVIDENCE",
        "orders_or_broker_calls": False,
        "trusted_external_timestamp": "UNVERIFIED",
    }


def derive_research_eligibility(
    *, quote_available: bool | None, is_listed: bool | None, is_suspended: bool | None,
    is_st: bool | None, exclude_st: bool, market_rules_complete: Any,
    price_within_limits: Any, account_cash_sufficient: Any,
    decision_observations_timely: bool | None = True,
) -> dict[str, Any]:
    """Derive a research-only eligibility state; never interpret it as a fill guarantee."""
    supplied_inputs: dict[str, dict[str, Any]] = {}

    def unpack(label: str, raw: Any) -> bool | None:
        if isinstance(raw, dict):
            value = raw.get("value")
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{label}.value 必须是 bool 或 None")
            sha = str(raw.get("source_sha256", "")).lower()
            structurally_bound = (
                bool(str(raw.get("source_id", "")).strip())
                and len(sha) == 64
                and all(char in "0123456789abcdef" for char in sha)
                and bool(str(raw.get("known_at_utc", "")).strip())
                and bool(str(raw.get("valid_from", "")).strip())
                and bool(str(raw.get("review_status", "")).strip())
                and bool(str(raw.get("reviewed_by", "")).strip())
                and bool(str(raw.get("reviewed_at", "")).strip())
            )
            supplied_inputs[label] = {
                "value": value,
                "evidence_status": "STRUCTURALLY_BOUND_SOURCE_UNVERIFIED" if structurally_bound else "INCOMPLETE_EVIDENCE",
                "source_id": raw.get("source_id"),
                "source_sha256": sha or None,
                "known_at_utc": raw.get("known_at_utc"),
                "valid_from": raw.get("valid_from"),
                "valid_to": raw.get("valid_to"),
                "review_status": raw.get("review_status"),
            }
            return value
        if raw is not None and not isinstance(raw, bool):
            raise ValueError(f"{label} 必须是 bool、结构化证据对象或 None")
        supplied_inputs[label] = {
            "value": raw,
            "evidence_status": "SUPPLIED_ASSUMPTION" if raw is not None else "MISSING",
            "source_id": None,
            "source_sha256": None,
            "known_at_utc": None,
            "valid_from": None,
            "valid_to": None,
            "review_status": None,
        }
        return raw

    market_rules_value = unpack("market_rules_complete", market_rules_complete)
    price_limit_value = unpack("price_within_limits", price_within_limits)
    account_cash_value = unpack("account_cash_sufficient", account_cash_sufficient)
    for label, value in (
        ("quote_available", quote_available), ("is_listed", is_listed),
        ("is_suspended", is_suspended), ("is_st", is_st),
    ):
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{label} 必须是 bool 或 None")
    if not isinstance(exclude_st, bool):
        raise ValueError("exclude_st 必须是布尔值")
    blocked: list[str] = []
    unknown: list[str] = []
    if is_listed is False:
        blocked.append("NOT_LISTED")
    elif is_listed is None:
        unknown.append("LISTING_STATUS_UNKNOWN")
    if is_suspended is True:
        blocked.append("SUSPENDED")
    elif is_suspended is None:
        unknown.append("SUSPENSION_STATUS_UNKNOWN")
    if exclude_st and is_st is True:
        blocked.append("ST_EXCLUDED_BY_RESEARCH_CONFIG")
    elif exclude_st and is_st is None:
        unknown.append("ST_STATUS_UNKNOWN")
    if quote_available is False or quote_available is None:
        unknown.append("QUOTE_UNAVAILABLE_OR_UNKNOWN")
    if decision_observations_timely is not True:
        unknown.append("DECISION_OBSERVATION_NOT_AVAILABLE_BY_DECISION")
    if market_rules_value is not True:
        unknown.append("MARKET_RULES_INCOMPLETE_OR_UNKNOWN")
    if price_limit_value is False:
        blocked.append("PRICE_OUTSIDE_RULE_LIMITS")
    elif price_limit_value is None:
        unknown.append("PRICE_LIMIT_CHECK_INCOMPLETE_OR_FAILED")
    if account_cash_value is False:
        blocked.append("INSUFFICIENT_ACCOUNT_CASH")
    elif account_cash_value is None:
        unknown.append("ACCOUNT_CASH_CHECK_INCOMPLETE_OR_FAILED")
    state = "BLOCKED" if blocked else "UNKNOWN" if unknown else "ELIGIBLE"
    return {
        "state": state, "blocked_reasons": blocked, "unknown_reasons": unknown,
        "eligibility_inputs": supplied_inputs,
        "evidence_quality": (
            "STRUCTURALLY_BOUND_SOURCE_UNVERIFIED"
            if supplied_inputs and all(row["evidence_status"] == "STRUCTURALLY_BOUND_SOURCE_UNVERIFIED" for row in supplied_inputs.values())
            else "SUPPLIED_ASSUMPTION_OR_INCOMPLETE_EVIDENCE"
        ),
        "meaning": "Research eligibility under supplied inputs only; not a broker acceptance or fill guarantee.",
    }


def _eligibility_summary(
    result_rows: list[dict[str, Any]], payloads: dict[tuple[str, str, str], dict[str, Any] | None],
    inputs: Any, decision_at: datetime,
) -> list[dict[str, Any]]:
    config = inputs if isinstance(inputs, dict) else {}
    output = []
    keys = sorted({(row["symbol"], row["phase"]) for row in result_rows if row["phase"] == "DECISION"})
    for symbol, phase in keys:
        quote = payloads.get((symbol, phase, "QUOTE"))
        state = payloads.get((symbol, phase, "STATUS"))
        final_rows = {
            row["observation_type"]: row for row in result_rows
            if row["symbol"] == symbol and row["phase"] == phase
        }
        available_observations = [
            (payload, final_rows.get(kind))
            for kind, payload in (("QUOTE", quote), ("STATUS", state))
            if payload is not None
        ]
        decision_observations_timely = all(
            row is not None
            and _aware_timestamp(row.get("latest_response_received_at_utc"), "latest_response_received_at_utc") <= decision_at
            and _aware_timestamp(payload.get("event_time_utc"), "payload.event_time_utc") <= decision_at
            for payload, row in available_observations
        ) if available_observations else True
        result = derive_research_eligibility(
            quote_available=quote is not None,
            is_listed=state.get("is_listed") if state else None,
            is_suspended=state.get("is_suspended") if state else None,
            is_st=state.get("is_st") if state else None,
            exclude_st=config.get("exclude_st", True),
            market_rules_complete=config.get("market_rules_complete"),
            price_within_limits=config.get("price_within_limits"),
            account_cash_sufficient=config.get("account_cash_sufficient"),
            decision_observations_timely=decision_observations_timely,
        )
        output.append({"symbol": symbol, "phase": phase, **result})
    return output


def build_synthetic_paper_observation(output_dir: str | Path) -> dict[str, Any]:
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"拒绝覆盖已有纸面记录演练目录: {target}")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        ranking = b'{"synthetic":true,"signal_date":"2026-11-04","symbols":["SH600001"]}\n'
        (staging / "ranking.json").write_bytes(ranking)
        payloads = {
            "decision-quote-timeout.json": b'{"error_code":"SYNTHETIC_TIMEOUT"}\n',
            "decision-quote-retry.json": b'{"symbol":"SH600001","event_time_utc":"2026-11-05T01:31:00Z","open_price_cny":10.25,"price_semantics":"OBSERVED_OPEN_PRICE"}\n',
            "decision-status-missing.json": b'[]\n',
            "execution-quote.json": b'{"symbol":"SH600001","event_time_utc":"2026-11-06T01:31:00Z","open_price_cny":10.40,"price_semantics":"OBSERVED_OPEN_PRICE"}\n',
            "execution-status.json": b'{"symbol":"SH600001","event_time_utc":"2026-11-06T01:31:00Z","is_listed":true,"is_suspended":false,"is_st":false}\n',
        }
        for name, content in payloads.items():
            (raw_dir / name).write_bytes(content)
        now_entries = [
            _synthetic_attempt("quote-decision", 1, "DECISION", "QUOTE", "ERROR", "market_data_stub", SYNTHETIC_QUOTE_PARSER, "2026-11-05T01:31:00Z", None, "2026-11-05T01:31:00Z", "2026-11-05T01:31:00.500Z", "2026-11-05T01:31:00Z", "raw/decision-quote-timeout.json", None, staging),
            _synthetic_attempt("quote-decision", 2, "DECISION", "QUOTE", "AVAILABLE", "market_data_stub", SYNTHETIC_QUOTE_PARSER, "2026-11-05T01:31:00Z", "2026-11-05T01:31:01Z", "2026-11-05T01:31:01Z", "2026-11-05T01:31:02Z", "2026-11-05T01:31:00Z", "raw/decision-quote-retry.json", {"symbol": "SH600001", "event_time_utc": "2026-11-05T01:31:00Z", "open_price_cny": 10.25, "price_semantics": "OBSERVED_OPEN_PRICE"}, staging),
            _synthetic_attempt("status-decision", 1, "DECISION", "STATUS", "MISSING", "status_stub", SYNTHETIC_STATUS_PARSER, "2026-11-05T01:31:00Z", None, "2026-11-05T01:31:01Z", "2026-11-05T01:31:03Z", "2026-11-05T01:31:00Z", "raw/decision-status-missing.json", None, staging),
            _synthetic_attempt("quote-execution", 1, "EXECUTION", "QUOTE", "AVAILABLE", "market_data_stub", SYNTHETIC_QUOTE_PARSER, "2026-11-06T01:31:00Z", "2026-11-06T01:31:01Z", "2026-11-06T01:31:01Z", "2026-11-06T01:31:02Z", "2026-11-06T01:31:00Z", "raw/execution-quote.json", {"symbol": "SH600001", "event_time_utc": "2026-11-06T01:31:00Z", "open_price_cny": 10.40, "price_semantics": "OBSERVED_OPEN_PRICE"}, staging),
            _synthetic_attempt("status-execution", 1, "EXECUTION", "STATUS", "AVAILABLE", "status_stub", SYNTHETIC_STATUS_PARSER, "2026-11-06T01:31:00Z", "2026-11-06T01:31:01Z", "2026-11-06T01:31:01Z", "2026-11-06T01:31:02Z", "2026-11-06T01:31:00Z", "raw/execution-status.json", {"symbol": "SH600001", "event_time_utc": "2026-11-06T01:31:00Z", "is_listed": True, "is_suspended": False, "is_st": False}, staging),
        ]
        document = {
            "schema_version": PAPER_OBSERVATION_SCHEMA_VERSION,
            "record_type": "PAPER_OBSERVATION",
            "classification": "SYNTHETIC",
            "observation_id": "synthetic-paper-rehearsal-20261105-001",
            "trusted_external_timestamp": "UNVERIFIED",
            "manifest_signature": "UNSIGNED",
            "signal": {
                "signal_date": "2026-11-04",
                "generated_at_utc": "2026-11-04T08:00:00Z",
                "ranking_artifact": {"path": "ranking.json", "sha256": _sha256(staging / "ranking.json")},
                "code_revision": "synthetic",
                "config_sha256": "0" * 64,
            },
            "observation_schedule": {
                "decision_at_utc": "2026-11-05T01:31:00Z",
                "execution_at_utc": "2026-11-06T01:31:00Z",
            },
            "eligibility_inputs": {
                "exclude_st": True,
                "market_rules_complete": None,
                "price_within_limits": None,
                "account_cash_sufficient": None,
            },
            "observations": now_entries,
            "limitations": [
                "Synthetic input only; no live provider was queried.",
                "Local capture time does not prove market executability or a historical publication time.",
                "can_buy is not stored as a provider fact; research eligibility remains UNKNOWN without rules and account inputs.",
                "No broker connection, account access, or order generation.",
            ],
        }
        record_path = staging / "paper-observation.json"
        record_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validation = validate_paper_observation_document(document, base_dir=staging)
        (staging / "validation-report.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.exists():
            raise FileExistsError(f"发布前目标已存在，拒绝覆盖: {target}")
        staging.rename(target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"output_dir": str(target), "validation": validation}


def _synthetic_attempt(
    prefix: str, attempt: int, phase: str, kind: str, outcome: str, source_name: str,
    parser_name: str, event_time: str | None, published: str | None,
    requested: str, received: str, planned: str, raw_path: str,
    payload: dict[str, Any] | None, base_dir: Path,
) -> dict[str, Any]:
    entry = {
        "symbol": "SH600001",
        "phase": phase,
        "observation_type": kind,
        "attempt_no": attempt,
        "attempt_id": f"{prefix}-{attempt}",
        "outcome": outcome,
        "source": {
            "name": source_name,
            "event_time_utc": event_time,
            "provider_published_at_utc": published,
            "parser_name": parser_name,
            "parser_version": PARSER_VERSION,
        },
        "request_started_at_utc": requested,
        "response_received_at_utc": received,
        "planned_observation_at_utc": planned,
        "raw_response": {
            "path": raw_path, "sha256": _sha256(base_dir / raw_path),
            "format": "JSON",
            "capture_layer": "LOCAL_ERROR_RECORD" if outcome == "ERROR" else "SYNTHETIC_RESPONSE",
        },
        "payload": payload,
        "error_code": "SYNTHETIC_TIMEOUT" if outcome == "ERROR" else None,
    }
    if outcome == "AVAILABLE" and payload is not None:
        entry["normalized_payload_sha256"] = canonical_payload_sha256(payload)
    return entry


def _validate_payload(kind: str, payload: dict[str, Any], index: int) -> None:
    if kind == "QUOTE":
        try:
            price = float(payload["open_price_cny"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"observations[{index}] quote 缺少数值 open_price_cny") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"observations[{index}] open_price_cny 必须为正数")
        if not str(payload.get("symbol", "")).strip() or not str(payload.get("event_time_utc", "")).strip():
            raise ValueError(f"observations[{index}] quote 缺少证券代码或事件时间")
        _aware_timestamp(payload["event_time_utc"], f"observations[{index}].payload.event_time_utc")
        if not str(payload.get("price_semantics", "")).strip():
            raise ValueError(f"observations[{index}] quote 缺少 price_semantics")
    else:
        required = ("is_listed", "is_suspended", "is_st")
        if any(not isinstance(payload.get(field), bool) for field in required):
            raise ValueError(f"observations[{index}] status 必须明确提供 {', '.join(required)} 布尔值")
        if "can_buy" in payload:
            raise ValueError(f"observations[{index}] can_buy 不是供应商状态字段，应由规则与账户输入推导")
        if payload.get("symbol") is None or payload.get("event_time_utc") is None:
            raise ValueError(f"observations[{index}] status 缺少证券代码或事件时间")
        _aware_timestamp(payload["event_time_utc"], f"observations[{index}].payload.event_time_utc")


def _validate_payload_v1(kind: str, payload: dict[str, Any], index: int) -> None:
    if kind == "QUOTE":
        try:
            price = float(payload["open_price_cny"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"observations[{index}] quote 缺少数值 open_price_cny") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"observations[{index}] open_price_cny 必须为正数")
    else:
        required = ("is_listed", "is_suspended", "is_st", "can_buy")
        if any(not isinstance(payload.get(field), bool) for field in required):
            raise ValueError(f"observations[{index}] status 必须明确提供 {', '.join(required)} 布尔值")


def _validate_file_binding(value: Any, base_dir: Path, label: str) -> Path:
    if not isinstance(value, dict) or not str(value.get("path", "")).strip():
        raise ValueError(f"{label} 必须包含相对路径和 SHA-256")
    relative = Path(value["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{label}.path 必须留在记录目录内")
    path = (base_dir / relative).resolve()
    if base_dir.resolve() not in path.parents:
        raise ValueError(f"{label}.path 超出记录目录")
    expected = str(value.get("sha256", "")).lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError(f"{label}.sha256 必须是 64 位十六进制")
    if not path.is_file() or _sha256(path) != expected:
        raise ValueError(f"{label} 文件缺失或 SHA-256 不匹配")
    return path


def _aware_timestamp(value: Any, label: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{label} 缺失")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} 必须带时区")
    return parsed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
