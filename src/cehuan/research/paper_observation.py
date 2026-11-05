from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any


PAPER_OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_OUTCOMES = {"AVAILABLE", "MISSING", "ERROR"}
OBSERVATION_PHASES = {"DECISION", "EXECUTION"}
OBSERVATION_TYPES = {"QUOTE", "STATUS"}


def validate_paper_observation_file(path: str | Path) -> dict[str, Any]:
    record_path = Path(path).resolve()
    document = json.loads(record_path.read_text(encoding="utf-8"))
    return validate_paper_observation_document(document, base_dir=record_path.parent)


def validate_paper_observation_document(
    document: dict[str, Any], *, base_dir: str | Path,
) -> dict[str, Any]:
    if document.get("schema_version") != PAPER_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("paper observation schema_version 不支持")
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
    _validate_file_binding(signal.get("ranking_artifact"), Path(base_dir), "signal.ranking_artifact")
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
        _validate_file_binding(raw_response, Path(base_dir), f"observations[{index}].raw_response")
        payload = entry.get("payload")
        if outcome == "AVAILABLE":
            if not isinstance(payload, dict):
                raise ValueError(f"observations[{index}] AVAILABLE 必须包含 payload")
            _validate_payload(kind, payload, index)
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
        })
    return {
        "status": "VALID",
        "classification": classification,
        "observation_id": document["observation_id"],
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
            "decision-quote-timeout.txt": b"synthetic timeout: no response\n",
            "decision-quote-retry.json": b'{"symbol":"SH600001","time":"2026-11-05T09:31:00+08:00","price":10.25}\n',
            "decision-status-missing.json": b'{"status":"not returned in synthetic rehearsal"}\n',
            "execution-quote.json": b'{"symbol":"SH600001","time":"2026-11-06T09:31:00+08:00","price":10.40}\n',
            "execution-status.json": b'{"symbol":"SH600001","is_listed":true,"is_suspended":false,"is_st":false,"can_buy":true}\n',
        }
        for name, content in payloads.items():
            (raw_dir / name).write_bytes(content)
        now_entries = [
            _synthetic_attempt("quote-decision", 1, "DECISION", "QUOTE", "ERROR", "market_data_stub", "2026-11-05T09:31:00+08:00", None, "2026-11-05T01:31:00Z", "raw/decision-quote-timeout.txt", None, staging),
            _synthetic_attempt("quote-decision", 2, "DECISION", "QUOTE", "AVAILABLE", "market_data_stub", "2026-11-05T09:31:00+08:00", "2026-11-05T01:31:01Z", "2026-11-05T01:31:02Z", "raw/decision-quote-retry.json", {"open_price_cny": 10.25}, staging),
            _synthetic_attempt("status-decision", 1, "DECISION", "STATUS", "MISSING", "status_stub", None, None, "2026-11-05T01:31:03Z", "raw/decision-status-missing.json", None, staging),
            _synthetic_attempt("quote-execution", 1, "EXECUTION", "QUOTE", "AVAILABLE", "market_data_stub", "2026-11-06T09:31:00+08:00", "2026-11-06T01:31:01Z", "2026-11-06T01:31:02Z", "raw/execution-quote.json", {"open_price_cny": 10.40}, staging),
            _synthetic_attempt("status-execution", 1, "EXECUTION", "STATUS", "AVAILABLE", "status_stub", "2026-11-06T09:31:00+08:00", "2026-11-06T01:31:01Z", "2026-11-06T01:31:02Z", "raw/execution-status.json", {"is_listed": True, "is_suspended": False, "is_st": False, "can_buy": True}, staging),
        ]
        document = {
            "schema_version": PAPER_OBSERVATION_SCHEMA_VERSION,
            "record_type": "PAPER_OBSERVATION",
            "classification": "SYNTHETIC",
            "observation_id": "synthetic-paper-rehearsal-20261105-001",
            "signal": {
                "signal_date": "2026-11-04",
                "generated_at_utc": "2026-11-04T08:00:00Z",
                "ranking_artifact": {"path": "ranking.json", "sha256": _sha256(staging / "ranking.json")},
                "code_revision": "synthetic",
                "config_sha256": "0" * 64,
            },
            "observations": now_entries,
            "limitations": [
                "Synthetic input only; no live provider was queried.",
                "Local capture time does not prove market executability or a historical publication time.",
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
    event_time: str | None, published: str | None, captured: str, raw_path: str,
    payload: dict[str, Any] | None, base_dir: Path,
) -> dict[str, Any]:
    return {
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
        },
        "captured_at_utc": captured,
        "raw_response": {"path": raw_path, "sha256": _sha256(base_dir / raw_path)},
        "payload": payload,
        "error_code": "SYNTHETIC_TIMEOUT" if outcome == "ERROR" else None,
    }


def _validate_payload(kind: str, payload: dict[str, Any], index: int) -> None:
    if kind == "QUOTE":
        try:
            price = float(payload["open_price_cny"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"observations[{index}] quote 缺少数值 open_price_cny") from exc
        if price <= 0:
            raise ValueError(f"observations[{index}] open_price_cny 必须为正数")
    else:
        required = ("is_listed", "is_suspended", "is_st", "can_buy")
        if any(not isinstance(payload.get(field), bool) for field in required):
            raise ValueError(f"observations[{index}] status 必须明确提供 {', '.join(required)} 布尔值")


def _validate_file_binding(value: Any, base_dir: Path, label: str) -> None:
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
