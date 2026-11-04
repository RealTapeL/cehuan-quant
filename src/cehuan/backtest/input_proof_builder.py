"""Rebuild checkpoint input proofs from project-normalized local inputs.

The builder is read-only with respect to backtest state. It does not merge
inputs, create checkpoints, or execute a strategy.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from cehuan.accounting.events import (
    CashDividendEvent, CodeChangeEvent, RightsOfferingEvent, ShareDistributionEvent,
)
from cehuan.backtest.checkpoint import BacktestCheckpoint
from cehuan.backtest.event_scheduler import AccountingEventSchedule
from cehuan.backtest.input_hashes import (
    CHECKPOINT_INPUT_HASH_VERSION,
    accounting_events_digest,
    canonical_payload_digest,
    chain_data_window_digest,
    compact_config_digest,
    normalized_bars_digest,
    normalized_event_table_digest,
    normalized_table_digest,
)
from cehuan.config import DEFAULT_CONFIG, load_config
from cehuan.data.accounting_event_csv import AccountingEventBundle, read_accounting_event_csv
from cehuan.data.canonical import read_bars
from cehuan.data.corporate_actions import DEFAULT_CORPORATE_ACTIONS, normalize_corporate_actions, read_corporate_actions
from cehuan.data.fee_schedule import normalize_fee_schedule
from cehuan.data.market_rules import normalize_market_rule_schedule
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.security_identity import (
    DEFAULT_SECURITY_IDENTITY_EVENTS,
    normalize_security_identity_events,
    read_security_identity_events,
)


CHECKPOINT_INPUT_FIELDS = (
    "config_sha256", "market_rules_sha256", "fee_schedule_sha256", "accounting_events_sha256",
    "identity_events_sha256", "corporate_actions_sha256", "calendar_sha256", "calendar_prefix_sha256",
    "last_window_data_sha256", "last_window_bars_sha256", "data_chain_sha256",
)
PROVENANCE_COLUMNS = (
    "input_type", "row_id", "canonical_row_sha256", "source_id", "source_file_path",
    "source_file_sha256", "known_at_utc", "known_at_evidence", "review_status",
    "reviewed_by", "reviewed_at",
)
PROVENANCE_OPTIONAL_COLUMNS = (
    "source_role", "source_url", "source_title", "evidence_location",
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).expanduser().resolve().open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(path: str | Path, relative_to: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute() and relative_to is not None:
        value = relative_to / value
    return value.resolve()


def _json_safe(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat() if value == value.normalize() else value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _read_provenance_registry(
    path: Path | None,
) -> tuple[dict[tuple[str, str, str], dict[str, str]], dict[str, Any]]:
    if path is None:
        return {}, {"status": "UNVERIFIED", "reason": "未提供来源与时点登记表", "rows": 0}
    if not path.is_file():
        return {}, {"status": "UNVERIFIED", "reason": f"登记表不存在: {path}", "rows": 0}
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        return {}, {"status": "FAILED", "reason": f"无法读取登记表: {exc}", "rows": 0}
    missing = sorted(set(PROVENANCE_COLUMNS).difference(frame.columns))
    if missing:
        return {}, {"status": "FAILED", "reason": "登记表缺字段: " + ", ".join(missing), "rows": int(len(frame))}
    for column in PROVENANCE_OPTIONAL_COLUMNS:
        if column not in frame:
            frame[column] = ""
    registry_columns = (*PROVENANCE_COLUMNS, *PROVENANCE_OPTIONAL_COLUMNS)
    frame = frame[list(registry_columns)].copy()
    for column in registry_columns:
        frame[column] = frame[column].astype("string").fillna("").str.strip()
    frame.loc[frame["source_role"].eq(""), "source_role"] = "primary"
    if frame.duplicated(["input_type", "row_id", "source_role"]).any():
        return {}, {
            "status": "FAILED", "reason": "(input_type, row_id, source_role) 必须唯一",
            "rows": int(len(frame)),
        }
    rows = {
        (str(row.input_type).upper(), str(row.row_id), str(row.source_role)): row._asdict()
        for row in frame.itertuples(index=False)
    }
    return rows, {
        "status": "VERIFIED", "reason": None, "rows": int(len(frame)),
        "source_roles": int(frame["source_role"].nunique()), "sha256": sha256_file(path),
    }


def _aware_utc(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reviewed(entry: Mapping[str, Any]) -> bool:
    if str(entry.get("review_status", "")).upper() != "REVIEWED":
        return False
    if not str(entry.get("reviewed_by", "")).strip():
        return False
    reviewed_at = str(entry.get("reviewed_at", "")).strip()
    return bool(reviewed_at and not pd.isna(pd.to_datetime(reviewed_at, errors="coerce", format="mixed")))


def _registry_match(
    registry: Mapping[tuple[str, str, str], dict[str, str]],
    input_type: str,
    row_id: str,
    canonical_sha256: str,
    expected_source_sha256: str | None,
    registry_path: Path | None,
    expected_known_at_utc: str | None = None,
    *,
    source_role: str = "primary",
    expected_source_url: str | None = None,
) -> dict[str, Any]:
    entry = registry.get((input_type.upper(), row_id, source_role))
    if entry is None:
        return {
            "source_role": source_role,
            "row_status": "UNVERIFIED", "known_at_status": "UNVERIFIED", "review_status": "UNVERIFIED",
            "issues": ["来源与时点登记表没有该事件/来源角色"],
        }
    failures: list[str] = []
    gaps: list[str] = []
    if not str(entry.get("source_id", "")).strip():
        gaps.append("source_id 不能为空")
    declared_url = str(entry.get("source_url", "")).strip()
    if expected_source_url and not declared_url:
        gaps.append("source_url 不能为空")
    elif expected_source_url and declared_url != expected_source_url:
        failures.append("登记来源 URL 与事件输入不一致")
    if entry["canonical_row_sha256"].lower() != canonical_sha256:
        failures.append("canonical_row_sha256 不匹配")
    declared_source = entry["source_file_sha256"].lower()
    if not declared_source:
        gaps.append("source_file_sha256 缺失")
    elif len(declared_source) != 64 or any(ch not in "0123456789abcdef" for ch in declared_source):
        failures.append("source_file_sha256 格式无效")
    source_path_value = entry["source_file_path"]
    source_path = _resolve(source_path_value, registry_path.parent if registry_path else None) if source_path_value else None
    observed_source = None
    if source_path is None or not source_path.is_file():
        gaps.append("来源原件缺失")
    else:
        observed_source = sha256_file(source_path)
        if declared_source and observed_source != declared_source:
            failures.append("来源原件 SHA-256 不匹配")
    if expected_source_sha256 and declared_source != expected_source_sha256.lower():
        failures.append("登记表来源散列与输入行来源散列不一致")
    row_status = "FAILED" if failures else "UNVERIFIED" if gaps else "VERIFIED"
    known_at = _aware_utc(entry.get("known_at_utc"))
    expected_known_at = _aware_utc(expected_known_at_utc)
    if expected_known_at is not None and known_at is not None and expected_known_at != known_at:
        failures.append("known_at_utc 与实际输入不一致")
        row_status = "FAILED"
    known_at_status = (
        "VERIFIED" if row_status == "VERIFIED" and known_at and entry.get("known_at_evidence") and _reviewed(entry) else
        "FAILED" if row_status == "FAILED" else "UNVERIFIED"
    )
    review_status = "VERIFIED" if row_status == "VERIFIED" and _reviewed(entry) else (
        "FAILED" if row_status == "FAILED" else "UNVERIFIED"
    )
    return {
        "source_role": source_role,
        "row_status": row_status,
        "known_at_status": known_at_status,
        "review_status": review_status,
        "known_at_utc": known_at if known_at_status == "VERIFIED" else None,
        "known_at_evidence": entry.get("known_at_evidence") or None,
        "source_id": entry.get("source_id") or None,
        "source_url": declared_url or None,
        "source_title": entry.get("source_title") or None,
        "evidence_location": entry.get("evidence_location") or None,
        "reviewed_by": entry.get("reviewed_by") or None,
        "reviewed_at": entry.get("reviewed_at") or None,
        "source_file_path": str(source_path) if source_path else None,
        "source_file_sha256": observed_source,
        "issues": [*failures, *gaps],
    }


def _artifact(path: Path | None, *, status: str | None = None, detail: str | None = None) -> dict[str, Any]:
    if path is None:
        return {"path": None, "sha256": None, "status": status or "UNVERIFIED", "detail": detail}
    if not path.is_file():
        return {"path": str(path), "sha256": None, "status": "UNVERIFIED", "detail": "文件不存在"}
    return {"path": str(path), "sha256": sha256_file(path), "status": status or "VERIFIED", "detail": detail}


def _not_applicable_artifact(reason: str) -> dict[str, Any]:
    return {"path": None, "sha256": None, "not_applicable": True, "status": "VERIFIED", "detail": reason}


def _audit_status(rows: list[Mapping[str, Any]]) -> str:
    """Aggregate audit rows without treating missing evidence as a match."""
    statuses = [str(row.get("status", "UNVERIFIED")) for row in rows]
    if not statuses or "FAILED" in statuses:
        return "FAILED" if "FAILED" in statuses else "UNVERIFIED"
    return "UNVERIFIED" if "UNVERIFIED" in statuses else "VERIFIED"


def _audit_claimed_proof_files(claimed: Any, claimed_path: Path) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(claimed, dict) or not isinstance(claimed.get("inputs"), dict):
        return "FAILED", [{"role": "claimed_proof", "status": "FAILED", "detail": "proof 缺少 inputs 对象"}]
    inputs = claimed["inputs"]
    checkpoint = claimed.get("checkpoint", {})
    artifacts = inputs.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return "FAILED", [{"role": "claimed_proof.artifacts", "status": "FAILED", "detail": "必须是对象"}]
    audits: list[dict[str, Any]] = []
    roles = (
        "strategy_config", "historical_bars_prefix", "last_window_bars", "events",
        "calendar", "rules", "fees", "identity_events", "corporate_actions",
        "history_manifest", "provenance_registry", "cutoff_evidence_record",
    )
    for role in roles:
        item = artifacts.get(role)
        if not isinstance(item, dict):
            audits.append({"role": role, "status": "UNVERIFIED", "detail": "proof 未登记输入文件"})
            continue
        if item.get("not_applicable") is True:
            hashes = checkpoint.get("input_hashes", {}) if isinstance(checkpoint, dict) else {}
            valid = (
                role == "rules" and isinstance(hashes, dict) and hashes.get("market_rules_sha256") is None
                or role == "fees" and isinstance(hashes, dict) and hashes.get("fee_schedule_sha256") is None
                or role == "events" and isinstance(hashes, dict)
                and bool(hashes.get("accounting_events_sha256"))
                and isinstance(inputs.get("events"), list) and not inputs["events"]
            )
            audits.append({
                "role": role, "status": "VERIFIED" if valid else "FAILED",
                "detail": "按 checkpoint 输入标记为未使用" if valid else "not_applicable 与 proof 内容冲突",
            })
            continue
        path_value = item.get("path")
        expected = item.get("sha256")
        if not path_value or not expected:
            audits.append({"role": role, "status": "UNVERIFIED", "detail": "文件路径或 SHA-256 缺失"})
            continue
        path = _resolve(path_value, claimed_path.parent)
        if not path.is_file():
            audits.append({"role": role, "status": "UNVERIFIED", "detail": f"文件缺失: {path}"})
            continue
        observed = sha256_file(path)
        audits.append({
            "role": role, "status": "VERIFIED" if observed == str(expected).lower() else "FAILED",
            "path": str(path), "declared_sha256": expected, "observed_sha256": observed,
        })
    for family in ("events", "rules", "fees"):
        row_family = inputs.get(family, [])
        if not isinstance(row_family, list):
            continue
        for row in row_family:
            if not isinstance(row, dict):
                continue
            row_id = row.get("event_id", row.get("row_id", "unknown"))
            path_value = row.get("source_path")
            expected = row.get("source_sha256")
            if not path_value or not expected:
                audits.append({"role": f"{family}:{row_id}", "status": "UNVERIFIED", "detail": "来源文件路径或摘要缺失"})
                continue
            path = _resolve(path_value, claimed_path.parent)
            if not path.is_file():
                audits.append({"role": f"{family}:{row_id}", "status": "UNVERIFIED", "detail": f"来源文件缺失: {path}"})
                continue
            observed = sha256_file(path)
            audits.append({
                "role": f"{family}:{row_id}", "status": "VERIFIED" if observed == str(expected).lower() else "FAILED",
                "path": str(path), "declared_sha256": expected, "observed_sha256": observed,
            })
    for family in ("identity_events", "corporate_actions"):
        rows = inputs.get(family, [])
        if not isinstance(rows, list):
            audits.append({"role": family, "status": "UNVERIFIED", "detail": "事件来源证明清单缺失"})
            continue
        for row in rows:
            if not isinstance(row, dict):
                audits.append({"role": family, "status": "FAILED", "detail": "事件来源证明行必须是对象"})
                continue
            event_id = str(row.get("event_id", "unknown"))
            sources = row.get("sources")
            if not isinstance(sources, list) or not sources:
                audits.append({
                    "role": f"{family}:{event_id}", "status": "UNVERIFIED",
                    "detail": "非空事件缺少来源文件列表",
                })
                continue
            for source in sources:
                if not isinstance(source, dict):
                    audits.append({
                        "role": f"{family}:{event_id}", "status": "FAILED",
                        "detail": "事件来源记录必须是对象",
                    })
                    continue
                role = f"{family}:{event_id}:{source.get('source_role', 'unknown')}"
                path_value = source.get("source_path")
                expected = source.get("source_sha256")
                if not path_value or not expected:
                    audits.append({"role": role, "status": "UNVERIFIED", "detail": "来源路径或 SHA-256 缺失"})
                    continue
                path = _resolve(path_value, claimed_path.parent)
                if not path.is_file():
                    audits.append({"role": role, "status": "UNVERIFIED", "detail": f"来源文件缺失: {path}"})
                    continue
                observed = sha256_file(path)
                audits.append({
                    "role": role, "status": "VERIFIED" if observed == str(expected).lower() else "FAILED",
                    "path": str(path), "declared_sha256": expected, "observed_sha256": observed,
                })
    if any(item["status"] == "FAILED" for item in audits):
        status = "FAILED"
    elif not audits or any(item["status"] == "UNVERIFIED" for item in audits):
        status = "UNVERIFIED"
    else:
        status = "VERIFIED"
    return status, audits


def _audit_canonical_rows(proof: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(proof, dict) or not isinstance(proof.get("inputs"), dict):
        return "FAILED", [{"role": "proof", "status": "FAILED", "detail": "缺少 inputs 对象"}]
    audits: list[dict[str, Any]] = []
    declared_proof_digest = proof.get("canonical_payload_sha256")
    unsigned_proof = dict(proof)
    unsigned_proof.pop("canonical_payload_sha256", None)
    observed_proof_digest = canonical_payload_digest(unsigned_proof)
    if declared_proof_digest is None:
        audits.append({"role": "proof_payload", "status": "UNVERIFIED", "detail": "proof 未登记 canonical_payload_sha256"})
    elif declared_proof_digest != observed_proof_digest:
        audits.append({
            "role": "proof_payload", "status": "FAILED", "detail": "proof canonical payload 摘要不匹配",
            "declared_sha256": declared_proof_digest, "observed_sha256": observed_proof_digest,
        })
    else:
        audits.append({"role": "proof_payload", "status": "VERIFIED", "sha256": observed_proof_digest})
    for family in ("events", "rules", "fees", "identity_events", "corporate_actions"):
        rows = proof["inputs"].get(family)
        if not isinstance(rows, list):
            audits.append({"role": family, "status": "UNVERIFIED", "detail": "规范行清单缺失"})
            continue
        for index, row in enumerate(rows):
            role = f"{family}[{index}]"
            if not isinstance(row, dict) or not isinstance(row.get("canonical_content"), dict):
                audits.append({"role": role, "status": "UNVERIFIED", "detail": "canonical_content 缺失"})
                continue
            content = row["canonical_content"]
            expected = row.get("content_sha256")
            observed = canonical_payload_digest(content)
            mismatches = [
                key for key, value in row.items()
                if key not in {"canonical_content", "content_sha256", "source_path", "sources", "source_status"}
                and content.get(key) != value
            ]
            if expected != observed or mismatches:
                audits.append({
                    "role": role, "status": "FAILED",
                    "detail": "规范内容摘要或外层字段不匹配",
                    "declared_sha256": expected, "observed_sha256": observed,
                    "different_fields": mismatches,
                })
            else:
                audits.append({"role": role, "status": "VERIFIED", "sha256": observed})
    if any(item["status"] == "FAILED" for item in audits):
        status = "FAILED"
    elif any(item["status"] == "UNVERIFIED" for item in audits):
        status = "UNVERIFIED"
    else:
        status = "VERIFIED"
    return status, audits


def _compare_artifact_declarations(
    rebuilt: Mapping[str, Any], claimed: Any,
) -> tuple[str, list[str]]:
    if not isinstance(claimed, dict) or not isinstance(claimed.get("inputs"), dict):
        return "FAILED", ["claimed proof 缺少 inputs 对象"]
    claimed_artifacts = claimed["inputs"].get("artifacts")
    rebuilt_artifacts = rebuilt.get("inputs", {}).get("artifacts")
    if not isinstance(claimed_artifacts, dict) or not isinstance(rebuilt_artifacts, dict):
        return "UNVERIFIED", ["proof 未登记完整 artifacts"]
    differences: list[str] = []
    unknown: list[str] = []
    for role, expected in rebuilt_artifacts.items():
        actual = claimed_artifacts.get(role)
        if not isinstance(actual, dict):
            unknown.append(role)
        elif expected.get("not_applicable") is True:
            if actual.get("not_applicable") is not True:
                differences.append(role)
        elif actual.get("sha256") != expected.get("sha256"):
            differences.append(role)
    if differences:
        return "FAILED", ["输入文件散列声明与重建输入不同: " + ", ".join(differences)]
    if unknown:
        return "UNVERIFIED", ["claimed proof 缺少 artifact: " + ", ".join(unknown)]
    return "VERIFIED", []


def _schedule_proof_rows(
    frame: pd.DataFrame,
    input_type: str,
    registry: Mapping[tuple[str, str, str], dict[str, str]],
    registry_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    output: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        canonical_row = _json_safe(raw)
        if input_type == "MARKET_RULE":
            row_id = f"rule:{canonical_row['symbol']}:{canonical_row['valid_from']}"
        else:
            row_id = f"fee:{canonical_row['market']}:{canonical_row['valid_from']}"
        canonical_with_id = {"row_id": row_id, **canonical_row}
        canonical_sha = canonical_payload_digest(canonical_with_id)
        source_sha = str(canonical_row.get("source_sha256", "")).lower() or None
        provenance = _registry_match(
            registry, input_type, row_id, canonical_sha, source_sha, registry_path,
        )
        proof_content = dict(canonical_with_id)
        if provenance.get("known_at_utc"):
            proof_content["known_at_utc"] = provenance["known_at_utc"]
        proof_content["source_sha256"] = source_sha
        proof_row = {
            **proof_content,
            "content_sha256": canonical_payload_digest(proof_content),
            "canonical_content": proof_content,
            "source_path": provenance.get("source_file_path"),
        }
        output.append(proof_row)
        audits.append({"input_type": input_type, "row_id": row_id, **provenance})
    return output, audits


def _accounting_event_rows(
    bundle: AccountingEventBundle | None,
    schedule: AccountingEventSchedule | None,
    registry: Mapping[tuple[str, str, str], dict[str, str]],
    registry_path: Path | None,
    source_base_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if bundle is None or schedule is None:
        return [], []
    source_by_id = {
        str(row["event_id"]): row.to_dict()
        for _, row in bundle.source_audit.iterrows()
    }
    output: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    for event in schedule.events:
        if isinstance(event, CashDividendEvent):
            event_type = "CASH_DIVIDEND"
            impact = event.record_date
            symbol = event.symbol
        elif isinstance(event, CodeChangeEvent):
            event_type = "CODE_CHANGE_1_TO_1"
            impact = event.effective_date
            symbol = None
        elif isinstance(event, ShareDistributionEvent):
            event_type = "SHARE_DISTRIBUTION"
            impact = event.record_date
            symbol = event.symbol
        elif isinstance(event, RightsOfferingEvent):
            event_type = "RIGHTS_OFFERING"
            impact = event.record_date
            symbol = event.symbol
        else:  # pragma: no cover - scheduler rejects unsupported types
            continue
        source = event.source
        canonical_content = _json_safe(asdict(event))
        canonical_content["known_at_utc"] = _aware_utc(event.known_at_utc.isoformat())
        canonical_content.update({
            "event_type": event_type,
            "first_impact_date": impact.isoformat(),
            "known_at_utc": _aware_utc(event.known_at_utc.isoformat()),
            "source_sha256": source.sha256,
            "dividend_tax_cny": (
                None if event.event_id not in schedule.dividend_tax_cny
                or schedule.dividend_tax_cny[event.event_id] is None
                else str(schedule.dividend_tax_cny[event.event_id])
            ),
            "dividend_tax_treatment": schedule.dividend_tax_treatment.get(event.event_id),
            "payment_withholding_per_share_cny": (
                None if event.event_id not in schedule.dividend_payment_withholding_per_share_cny
                or schedule.dividend_payment_withholding_per_share_cny[event.event_id] is None
                else str(schedule.dividend_payment_withholding_per_share_cny[event.event_id])
            ),
        })
        if isinstance(event, RightsOfferingEvent) and bundle is not None:
            decision = bundle.rights_offering_decisions_by_event.get(event.event_id)
            canonical_content["rights_offering_decision"] = (
                None if decision is None else _json_safe(asdict(decision))
            )
        source_row = source_by_id.get(event.event_id, {})
        archive_value = source_row.get("source_archive_file")
        archive_path = _resolve(archive_value, source_base_dir) if archive_value else None
        archive = str(archive_path) if archive_path else None
        proof_row: dict[str, Any] = {
            "event_id": event.event_id,
            "event_type": event_type,
            "first_impact_date": impact.isoformat(),
            "known_at_utc": _aware_utc(event.known_at_utc.isoformat()),
            "source_sha256": source.sha256,
            "canonical_content": canonical_content,
            "source_path": archive,
        }
        if symbol:
            proof_row["symbol"] = symbol
            canonical_content["symbol"] = symbol
        if isinstance(event, CodeChangeEvent):
            proof_row.update({
                "old_symbol": event.old_symbol,
                "new_symbol": event.new_symbol,
                "security_identity": event.security_identity,
            })
            canonical_content.update({
                "old_symbol": event.old_symbol,
                "new_symbol": event.new_symbol,
                "security_identity": event.security_identity,
            })
        proof_row["content_sha256"] = canonical_payload_digest(canonical_content)
        canonical_sha = proof_row["content_sha256"]
        provenance = _registry_match(
            registry, "ACCOUNTING_EVENT", event.event_id, canonical_sha, source.sha256, registry_path,
            _aware_utc(event.known_at_utc.isoformat()),
        )
        output.append(proof_row)
        audits.append({"input_type": "ACCOUNTING_EVENT", "row_id": event.event_id, **provenance})
    return output, audits


def _identity_and_corporate_event_rows(
    identity_events: pd.DataFrame,
    corporate_actions: pd.DataFrame,
    registry: Mapping[tuple[str, str, str], dict[str, str]],
    registry_path: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind each normalized identity/action row to its required source archives.

    A merger event has two independently checked source roles: the implementation
    result notice and the old security's last-trading notice.
    """
    identity_rows: list[dict[str, Any]] = []
    corporate_rows: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    def build_row(
        *, family: str, input_type: str, raw: Mapping[str, Any], source_specs: list[dict[str, Any]],
        output: list[dict[str, Any]],
    ) -> None:
        canonical_row = _json_safe(dict(raw))
        event_id = str(canonical_row["event_id"])
        row_id = f"{family}:{event_id}"
        event_type = str(canonical_row["event_type"])
        impact_date = str(
            canonical_row.get("effective_date")
            or canonical_row.get("record_date")
            or canonical_row.get("old_last_trade_date")
        )
        canonical_content = {
            "row_id": row_id,
            "event_id": event_id,
            "event_type": event_type,
            "first_impact_date": impact_date,
            "normalized_event": canonical_row,
        }
        canonical_sha = canonical_payload_digest(canonical_content)
        source_proofs: list[dict[str, Any]] = []
        row_statuses: list[str] = []
        known_statuses: list[str] = []
        review_statuses: list[str] = []
        for spec in source_specs:
            matched = _registry_match(
                registry,
                input_type,
                row_id,
                canonical_sha,
                spec.get("expected_sha256"),
                registry_path,
                source_role=spec["source_role"],
                expected_source_url=spec.get("source_url"),
            )
            # The declaration in the event table is itself checked against the
            # registry. These fields remain separate from machine file hashing.
            for field, expected in (("source_title", spec.get("source_title")),
                                    ("evidence_location", spec.get("evidence_location"))):
                declared = matched.get(field)
                if expected and not declared:
                    matched.setdefault("issues", []).append(f"登记 {field} 缺失")
                    if matched.get("row_status") != "FAILED":
                        matched["row_status"] = "UNVERIFIED"
                        matched["known_at_status"] = "UNVERIFIED"
                        matched["review_status"] = "UNVERIFIED"
                elif expected and declared != expected:
                    matched.setdefault("issues", []).append(f"登记 {field} 与事件输入不一致")
                    matched["row_status"] = "FAILED"
                    matched["known_at_status"] = "FAILED"
                    matched["review_status"] = "FAILED"
            source_proof = {
                "source_role": spec["source_role"],
                "source_id": matched.get("source_id"),
                "source_url": spec.get("source_url"),
                "source_title": spec.get("source_title") or matched.get("source_title"),
                "evidence_location": spec.get("evidence_location") or matched.get("evidence_location"),
                "source_path": matched.get("source_file_path"),
                "source_sha256": matched.get("source_file_sha256"),
                "declared_source_sha256": spec.get("expected_sha256"),
                "canonical_row_sha256": canonical_sha,
                "row_status": matched.get("row_status", "UNVERIFIED"),
                "known_at_status": matched.get("known_at_status", "UNVERIFIED"),
                "known_at_utc": matched.get("known_at_utc"),
                "known_at_evidence": matched.get("known_at_evidence"),
                "review_status": matched.get("review_status", "UNVERIFIED"),
                "reviewed_by": matched.get("reviewed_by"),
                "reviewed_at": matched.get("reviewed_at"),
                "issues": matched.get("issues", []),
            }
            source_proofs.append(source_proof)
            row_statuses.append(source_proof["row_status"])
            known_statuses.append(source_proof["known_at_status"])
            review_statuses.append(source_proof["review_status"])
            audits.append({
                "input_type": input_type,
                "row_id": row_id,
                **source_proof,
            })
        overall_status = (
            "FAILED" if "FAILED" in row_statuses else
            "UNVERIFIED" if not row_statuses or "UNVERIFIED" in row_statuses else "VERIFIED"
        )
        output.append({
            "row_id": row_id,
            "event_id": event_id,
            "event_type": event_type,
            "first_impact_date": impact_date,
            "canonical_content": canonical_content,
            "content_sha256": canonical_sha,
            "sources": source_proofs,
            "source_status": overall_status,
        })

    for raw in identity_events.to_dict(orient="records"):
        row = _json_safe(raw)
        build_row(
            family="security_identity",
            input_type="SECURITY_IDENTITY_EVENT",
            raw=row,
            source_specs=[{
                "source_role": "identity_notice",
                "source_url": row.get("evidence_url"),
            }],
            output=identity_rows,
        )

    for raw in corporate_actions.to_dict(orient="records"):
        row = _json_safe(raw)
        build_row(
            family="corporate_action",
            input_type="CORPORATE_ACTION",
            raw=row,
            source_specs=[
                {
                    "source_role": "implementation_notice",
                    "source_url": row.get("evidence_url"),
                    "source_title": row.get("evidence_title"),
                    "evidence_location": row.get("evidence_page_reference"),
                    "expected_sha256": row.get("evidence_sha256"),
                },
                {
                    "source_role": "old_last_trade_notice",
                    "source_url": row.get("old_last_trade_evidence_url"),
                    "source_title": row.get("old_last_trade_evidence_title"),
                    "evidence_location": row.get("old_last_trade_evidence_page_reference"),
                    "expected_sha256": row.get("old_last_trade_evidence_sha256"),
                },
            ],
            output=corporate_rows,
        )
    return identity_rows, corporate_rows, audits


def _rebuild_chain(
    manifest_path: Path | None,
    checkpoint: BacktestCheckpoint,
    current_bars_path: Path | None,
) -> tuple[str | None, dict[str, Any], list[dict[str, Any]]]:
    if manifest_path is None:
        return None, {"status": "UNVERIFIED", "reason": "未提供完整历史窗口清单", "verified_windows": 0}, []
    if not manifest_path.is_file():
        return None, {"status": "UNVERIFIED", "reason": f"历史窗口清单不存在: {manifest_path}", "verified_windows": 0}, []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, {"status": "FAILED", "reason": f"无法读取历史窗口清单: {exc}", "verified_windows": 0}, []
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("windows"), list):
        return None, {"status": "FAILED", "reason": "历史窗口清单必须是 schema_version=1 且含 windows 数组", "verified_windows": 0}, []
    chain = ""
    audit: list[dict[str, Any]] = []
    prior_end: date | None = None
    final_window: dict[str, Any] | None = None
    for index, item in enumerate(manifest["windows"]):
        if not isinstance(item, dict) or not item.get("bars_path"):
            return None, {"status": "FAILED", "reason": f"windows[{index}] 缺少 bars_path", "verified_windows": index}, audit
        bars_path = _resolve(item["bars_path"], manifest_path.parent)
        if not bars_path.is_file():
            return None, {
                "status": "UNVERIFIED", "reason": f"历史窗口文件缺失: {bars_path}",
                "verified_windows": index, "missing_window_index": index,
            }, audit
        try:
            bars = read_bars(bars_path)
            if bars.empty:
                raise ValueError("行情文件为空")
            dates = sorted(pd.to_datetime(bars["trade_date"]).dt.date.unique())
            window_start, window_end = dates[0], dates[-1]
            if prior_end is not None and window_start <= prior_end:
                raise ValueError(f"窗口与前一段重叠或乱序: {window_start} <= {prior_end}")
            mode = item.get("data_hash_mode")
            if mode not in {"raw_file", "normalized_frame"}:
                raise ValueError("data_hash_mode 必须是 raw_file 或 normalized_frame")
            window_bars_hash = normalized_bars_digest(bars)
            window_data_hash = sha256_file(bars_path) if mode == "raw_file" else window_bars_hash
            benchmark_path = _resolve(item["benchmark_path"], manifest_path.parent) if item.get("benchmark_path") else None
            if benchmark_path is not None:
                if not benchmark_path.is_file():
                    return None, {
                        "status": "UNVERIFIED", "reason": f"历史基准文件缺失: {benchmark_path}",
                        "verified_windows": index,
                    }, audit
                benchmark_hash = normalized_bars_digest(read_bars(benchmark_path))
            else:
                benchmark_hash = None
            chain = chain_data_window_digest(chain, window_data_hash, window_bars_hash, benchmark_hash)
            audit.append({
                "window_index": index,
                "bars_path": str(bars_path),
                "bars_file_sha256": sha256_file(bars_path),
                "data_hash_mode": mode,
                "window_data_sha256": window_data_hash,
                "window_bars_sha256": window_bars_hash,
                "benchmark_path": str(benchmark_path) if benchmark_path else None,
                "benchmark_sha256": benchmark_hash,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "chain_sha256": chain,
            })
            prior_end = window_end
            final_window = audit[-1]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, {"status": "FAILED", "reason": f"windows[{index}] 无法规范化: {exc}", "verified_windows": index}, audit
    if not audit:
        return None, {"status": "UNVERIFIED", "reason": "历史窗口清单没有窗口", "verified_windows": 0}, audit
    if prior_end != checkpoint.as_of_date:
        return chain, {
            "status": "FAILED", "reason": f"历史窗口末日 {prior_end} 与 checkpoint 日期 {checkpoint.as_of_date} 不一致",
            "verified_windows": len(audit),
        }, audit
    if current_bars_path and current_bars_path.is_file() and final_window:
        current_bars = read_bars(current_bars_path)
        if normalized_bars_digest(current_bars) != final_window["window_bars_sha256"]:
            return chain, {
                "status": "FAILED", "reason": "提供的当前行情与 manifest 最后窗口规范内容不同",
                "verified_windows": len(audit),
            }, audit
    if checkpoint.source_data_path and final_window:
        checkpoint_path = Path(checkpoint.source_data_path).expanduser().resolve()
        if not checkpoint_path.is_file():
            return chain, {
                "status": "UNVERIFIED", "reason": f"checkpoint 绑定的历史行情文件缺失: {checkpoint_path}",
                "verified_windows": len(audit),
            }, audit
        checkpoint_file_hash = sha256_file(checkpoint_path)
        if checkpoint_file_hash != checkpoint.last_window_data_sha256:
            return chain, {
                "status": "FAILED", "reason": "checkpoint.source_data_path 文件散列与 last_window_data_sha256 不一致",
                "verified_windows": len(audit),
            }, audit
        if checkpoint_file_hash != final_window["bars_file_sha256"]:
            return chain, {
                "status": "FAILED", "reason": "最后窗口与 checkpoint.source_data_path 字节不一致",
                "verified_windows": len(audit),
            }, audit
    if final_window and final_window["window_bars_sha256"] != checkpoint.last_window_bars_sha256:
        return chain, {
            "status": "FAILED", "reason": "最后窗口规范行情摘要与 checkpoint 不一致",
            "verified_windows": len(audit),
        }, audit
    if final_window and final_window["window_data_sha256"] != checkpoint.last_window_data_sha256:
        return chain, {
            "status": "FAILED", "reason": "最后窗口数据摘要与 checkpoint 不一致",
            "verified_windows": len(audit),
        }, audit
    if chain != checkpoint.data_chain_sha256:
        return chain, {
            "status": "FAILED", "reason": "重建的历史窗口链与 checkpoint.data_chain_sha256 不一致",
            "verified_windows": len(audit),
        }, audit
    return chain, {"status": "VERIFIED", "reason": None, "verified_windows": len(audit)}, audit


def _cutoff_evidence(path: Path | None, checkpoint: BacktestCheckpoint) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None:
        return {"status": "UNVERIFIED", "reason": "checkpoint v1 本身不含精确决策截止时点"}, {}
    if not path.is_file():
        return {"status": "UNVERIFIED", "reason": f"截止时点证据文件不存在: {path}"}, {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"status": "FAILED", "reason": f"无法读取截止时点证据: {exc}"}, {}
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        return {"status": "FAILED", "reason": "截止时点证据必须是 schema_version=1 的对象"}, {}
    differences: list[str] = []
    if str(record.get("checkpoint_state_sha256", "")).lower() != checkpoint.state_sha256:
        differences.append("证据中的 checkpoint_state_sha256 不匹配")
    instant = _aware_utc(record.get("decision_cutoff_utc"))
    if instant is None:
        differences.append("decision_cutoff_utc 缺少可靠时区时间")
    if not str(record.get("source_id", "")).strip():
        differences.append("截止时点证据 source_id 不能为空")
    source_path_value = record.get("source_file_path")
    source_path = _resolve(source_path_value, path.parent) if source_path_value else None
    expected_source_hash = str(record.get("source_file_sha256", "")).lower()
    if source_path is None or not source_path.is_file():
        differences.append("截止时点原始证据文件缺失")
    elif sha256_file(source_path) != expected_source_hash:
        differences.append("截止时点原始证据文件 SHA-256 不匹配")
    if not _reviewed(record):
        differences.append("截止时点证据尚无完整人工复核记录")
    if differences:
        return {"status": "FAILED" if "不匹配" in " ".join(differences) else "UNVERIFIED", "reason": "; ".join(differences)}, {
            "path": str(path), "sha256": sha256_file(path), "source_path": str(source_path) if source_path else None,
        }
    return {
        "status": "VERIFIED", "reason": None, "decision_cutoff_utc": instant,
        "source_id": str(record.get("source_id", "")),
    }, {
        "path": str(path), "sha256": sha256_file(path),
        "source_path": str(source_path), "source_sha256": expected_source_hash,
    }


def rebuild_checkpoint_input_proof(
    checkpoint: BacktestCheckpoint,
    *,
    config_path: str | Path = DEFAULT_CONFIG,
    bars_path: str | Path | None = None,
    calendar_path: str | Path | None = None,
    identity_events_path: str | Path | None = None,
    corporate_actions_path: str | Path | None = None,
    accounting_events_path: str | Path | None = None,
    market_rules_path: str | Path | None = None,
    fees_path: str | Path | None = None,
    history_manifest_path: str | Path | None = None,
    provenance_registry_path: str | Path | None = None,
    cutoff_evidence_path: str | Path | None = None,
    claimed_proof_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconstruct the checkpoint's normalized input proof via project adapters."""
    checkpoint.require_sealed()
    config_file = _resolve(config_path)
    bars_file = _resolve(bars_path or checkpoint.source_data_path) if (bars_path or checkpoint.source_data_path) else None
    calendar_file = _resolve(calendar_path) if calendar_path else None
    identity_file = _resolve(identity_events_path or DEFAULT_SECURITY_IDENTITY_EVENTS)
    corporate_file = _resolve(corporate_actions_path or DEFAULT_CORPORATE_ACTIONS)
    accounting_file = _resolve(accounting_events_path) if accounting_events_path else None
    rules_file = _resolve(market_rules_path) if market_rules_path else None
    fees_file = _resolve(fees_path) if fees_path else None
    manifest_file = _resolve(history_manifest_path) if history_manifest_path else None
    registry_file = _resolve(provenance_registry_path) if provenance_registry_path else None
    cutoff_file = _resolve(cutoff_evidence_path) if cutoff_evidence_path else None
    claimed_file = _resolve(claimed_proof_path) if claimed_proof_path else None

    report: dict[str, Any] = {
        "schema_version": 1,
        "report_type": "checkpoint_input_proof_rebuild",
        "hash_version": CHECKPOINT_INPUT_HASH_VERSION,
        "checkpoint_state_sha256": checkpoint.state_sha256,
        "checkpoint_as_of_date": checkpoint.as_of_date.isoformat(),
        "checkpoint_validation": "VERIFIED",
        "input_hash_results": [],
        "source_files": [],
        "provenance_rows": [],
        "issues": [],
    }
    observed: dict[str, str | None] = {field: None for field in CHECKPOINT_INPUT_FIELDS}
    file_rows: list[dict[str, Any]] = []
    normalized_components: dict[str, str] = {}
    adapters: dict[str, str] = {}
    bars = pd.DataFrame()
    calendar = pd.DataFrame()
    rules = fees = None
    identity_events = pd.DataFrame()
    corporate_actions = pd.DataFrame()
    accounting_bundle: AccountingEventBundle | None = None
    event_schedule: AccountingEventSchedule | None = None
    session_dates: list[date] = []

    def file_record(name: str, path: Path | None, required: bool = True) -> None:
        if path is None:
            file_rows.append({
                "input": name, "path": None,
                "status": "UNVERIFIED" if required else "VERIFIED",
                "reason": "必需输入路径未提供" if required else "此输入未用于 checkpoint",
                "sha256": None,
            })
            return
        if not path.is_file():
            file_rows.append({"input": name, "path": str(path), "status": "UNVERIFIED", "reason": "文件不存在", "sha256": None})
            return
        file_rows.append({
            "input": name, "path": str(path), "status": "VERIFIED", "reason": None,
            "sha256": sha256_file(path),
        })

    file_record("strategy_config", config_file)
    file_record("bars", bars_file)
    file_record("calendar", calendar_file)
    file_record("identity_events", identity_file)
    file_record("corporate_actions", corporate_file)
    empty_events_digest = accounting_events_digest(())
    accounting_events_required = checkpoint.accounting_events_sha256 != empty_events_digest
    file_record("accounting_events", accounting_file, required=accounting_events_required)
    file_record("market_rules", rules_file, required=checkpoint.market_rules_sha256 is not None)
    file_record("fees", fees_file, required=checkpoint.fee_schedule_sha256 is not None)
    file_record("history_manifest", manifest_file)
    file_record("provenance_registry", registry_file)
    file_record("cutoff_evidence", cutoff_file)
    file_record("claimed_proof", claimed_file, required=claimed_file is not None)

    try:
        config = load_config(config_file)
        observed["config_sha256"] = compact_config_digest(config)
        normalized_components["strategy_config"] = observed["config_sha256"]
        adapters["strategy_config"] = "cehuan.config.load_config + compact_config_digest"
    except (OSError, ValueError, TypeError) as exc:
        report["issues"].append({"input": "strategy_config", "status": "FAILED", "reason": str(exc)})

    if bars_file and bars_file.is_file():
        try:
            bars = read_bars(bars_file)
            if bars.empty:
                raise ValueError("行情文件为空")
            session_dates = sorted(pd.to_datetime(bars["trade_date"]).dt.date.unique())
            observed["last_window_bars_sha256"] = normalized_bars_digest(bars)
            observed["last_window_data_sha256"] = (
                sha256_file(bars_file) if checkpoint.source_data_path else observed["last_window_bars_sha256"]
            )
            normalized_components["last_window_bars"] = observed["last_window_bars_sha256"]
            adapters["bars"] = "cehuan.data.canonical.read_bars + checkpoint-v1 normalized_bars_digest"
        except (OSError, ValueError, TypeError, KeyError) as exc:
            report["issues"].append({"input": "bars", "status": "FAILED", "reason": str(exc)})
    else:
        report["issues"].append({"input": "bars", "status": "UNVERIFIED", "reason": "未取得 checkpoint 对应的行情窗口文件"})

    if calendar_file and calendar_file.is_file():
        try:
            calendar = normalize_trading_calendar(pd.read_csv(calendar_file, dtype=str, keep_default_na=False))
            observed["calendar_sha256"] = normalized_table_digest(calendar)
            prefix = calendar.loc[calendar["trade_date"] <= checkpoint.as_of_date].copy()
            observed["calendar_prefix_sha256"] = normalized_table_digest(prefix)
            normalized_components["calendar"] = observed["calendar_sha256"]
            adapters["calendar"] = "cehuan.data.calendar.normalize_trading_calendar + checkpoint-v1 normalized_table_digest"
            next_open = calendar.loc[
                calendar["is_open"].fillna(False) & (calendar["trade_date"] > checkpoint.as_of_date),
                "trade_date",
            ]
            actual_next = next_open.iloc[0] if not next_open.empty else None
            if actual_next != checkpoint.next_expected_session:
                report["issues"].append({
                    "input": "calendar", "status": "FAILED",
                    "reason": f"重建下一开市日 {actual_next} 与 checkpoint {checkpoint.next_expected_session} 不一致",
                })
            if bars is not None and not bars.empty:
                bar_dates = sorted(pd.to_datetime(bars["trade_date"]).dt.date.unique())
                open_dates = sorted(calendar.loc[calendar["is_open"].fillna(False), "trade_date"].tolist())
                expected_dates = [day for day in open_dates if bar_dates[0] <= day <= bar_dates[-1]]
                if bar_dates != expected_dates:
                    report["issues"].append({
                        "input": "calendar", "status": "FAILED",
                        "reason": "规范行情日期与交易日历的窗口日期不一致",
                    })
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            report["issues"].append({"input": "calendar", "status": "FAILED", "reason": str(exc)})

    try:
        if rules_file and rules_file.is_file():
            rules = normalize_market_rule_schedule(pd.read_csv(rules_file, dtype=str, keep_default_na=False))
            observed["market_rules_sha256"] = normalized_table_digest(rules)
            normalized_components["market_rules"] = observed["market_rules_sha256"]
            adapters["market_rules"] = "cehuan.data.market_rules.normalize_market_rule_schedule + checkpoint-v1 normalized_table_digest"
        elif checkpoint.market_rules_sha256 is None and rules_file is None:
            observed["market_rules_sha256"] = None
            normalized_components["market_rules"] = "NOT_USED"
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        report["issues"].append({"input": "market_rules", "status": "FAILED", "reason": str(exc)})
    try:
        if fees_file and fees_file.is_file():
            fees = normalize_fee_schedule(pd.read_csv(fees_file, dtype=str, keep_default_na=False))
            observed["fee_schedule_sha256"] = normalized_table_digest(fees)
            normalized_components["fees"] = observed["fee_schedule_sha256"]
            adapters["fees"] = "cehuan.data.fee_schedule.normalize_fee_schedule + checkpoint-v1 normalized_table_digest"
        elif checkpoint.fee_schedule_sha256 is None and fees_file is None:
            observed["fee_schedule_sha256"] = None
            normalized_components["fees"] = "NOT_USED"
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        report["issues"].append({"input": "fees", "status": "FAILED", "reason": str(exc)})

    if bool(rules_file) != bool(fees_file):
        report["issues"].append({"input": "schedules", "status": "FAILED", "reason": "交易规则与费率路径必须成对提供"})

    if identity_file.is_file():
        try:
            identity_events = read_security_identity_events(identity_file)
            if bars is not None and not bars.empty:
                symbols = set(bars["symbol"].astype(str).unique())
                identity_events = identity_events.loc[
                    identity_events["old_symbol"].isin(symbols) | identity_events["new_symbol"].isin(symbols)
                ].reset_index(drop=True)
            observed["identity_events_sha256"] = normalized_event_table_digest(identity_events)
            normalized_components["identity_events"] = observed["identity_events_sha256"]
            adapters["identity_events"] = "cehuan.data.security_identity.read_security_identity_events + checkpoint window symbol filter"
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            report["issues"].append({"input": "identity_events", "status": "FAILED", "reason": str(exc)})
    else:
        report["issues"].append({"input": "identity_events", "status": "UNVERIFIED", "reason": f"身份事件文件缺失: {identity_file}"})
    if corporate_file.is_file():
        try:
            corporate_actions = read_corporate_actions(corporate_file)
            observed["corporate_actions_sha256"] = normalized_event_table_digest(corporate_actions)
            normalized_components["corporate_actions"] = observed["corporate_actions_sha256"]
            adapters["corporate_actions"] = "cehuan.data.corporate_actions.read_corporate_actions"
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            report["issues"].append({"input": "corporate_actions", "status": "FAILED", "reason": str(exc)})
    else:
        report["issues"].append({"input": "corporate_actions", "status": "UNVERIFIED", "reason": f"公司行动文件缺失: {corporate_file}"})

    registry, registry_status = _read_provenance_registry(registry_file)
    report["provenance_registry"] = registry_status
    if accounting_file and accounting_file.is_file():
        try:
            accounting_bundle = read_accounting_event_csv(accounting_file)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            report["issues"].append({"input": "accounting_events", "status": "FAILED", "reason": str(exc)})
    if bars is not None and not bars.empty:
        try:
            if accounting_bundle is None:
                event_list = ()
                tax_cny: Mapping[str, Any] = {}
                tax_treatment: Mapping[str, str] = {}
                withholding: Mapping[str, Any] = {}
            else:
                event_list = accounting_bundle.events
                tax_cny = accounting_bundle.dividend_tax_cny_by_event
                tax_treatment = accounting_bundle.dividend_tax_treatment_by_event
                withholding = accounting_bundle.dividend_payment_withholding_per_share_cny_by_event
            event_schedule = AccountingEventSchedule.build(
                event_list, session_dates, tax_cny, tax_treatment, withholding,
                accounting_bundle.rights_offering_decisions_by_event if accounting_bundle else None,
            )
            observed["accounting_events_sha256"] = accounting_events_digest(
                event_list,
                event_schedule.dividend_tax_cny,
                event_schedule.dividend_tax_treatment,
                event_schedule.dividend_payment_withholding_per_share_cny,
                accounting_bundle.rights_offering_decisions_by_event if accounting_bundle else None,
            )
            normalized_components["accounting_events"] = observed["accounting_events_sha256"]
            adapters["accounting_events"] = (
                "cehuan.data.accounting_event_csv.read_accounting_event_csv + "
                "cehuan.backtest.event_scheduler.AccountingEventSchedule.build + checkpoint-v1 accounting_events_digest"
            )
        except (ValueError, TypeError) as exc:
            report["issues"].append({"input": "accounting_events", "status": "FAILED", "reason": str(exc)})

    observed["data_chain_sha256"], chain_status, chain_audit = _rebuild_chain(manifest_file, checkpoint, bars_file)
    report["historical_data_chain"] = chain_status
    report["history_windows"] = chain_audit
    if chain_status["status"] == "VERIFIED":
        normalized_components["data_chain"] = observed["data_chain_sha256"] or ""

    cutoff_status, cutoff_artifact = _cutoff_evidence(cutoff_file, checkpoint)
    report["decision_cutoff"] = cutoff_status

    provenance_audits: list[dict[str, Any]] = []
    rules_rows: list[dict[str, Any]] = []
    fees_rows: list[dict[str, Any]] = []
    if rules is not None:
        rules_rows, audits = _schedule_proof_rows(rules, "MARKET_RULE", registry, registry_file)
        provenance_audits.extend(audits)
    if fees is not None:
        fees_rows, audits = _schedule_proof_rows(fees, "FEE", registry, registry_file)
        provenance_audits.extend(audits)
    accounting_rows, audits = _accounting_event_rows(
        accounting_bundle, event_schedule, registry, registry_file,
        accounting_file.parent if accounting_file else None,
    )
    provenance_audits.extend(audits)
    identity_proof_rows, corporate_proof_rows, event_source_audits = _identity_and_corporate_event_rows(
        identity_events, corporate_actions, registry, registry_file,
    )
    provenance_audits.extend(event_source_audits)
    report["provenance_rows"] = provenance_audits

    all_review_statuses = [row.get("review_status", "UNVERIFIED") for row in provenance_audits]
    all_known_statuses = [row.get("known_at_status", "UNVERIFIED") for row in provenance_audits]
    event_adapter_reviewed = bool(
        accounting_bundle is None or accounting_bundle.source_audit.empty
        or accounting_bundle.source_audit["review_status"].astype(str).eq("REVIEWED").all()
    )
    accounting_parse_failed = any(
        row.get("input") == "accounting_events" and row.get("status") == "FAILED"
        for row in report["issues"]
    )
    def event_family_status(rows: list[dict[str, Any]]) -> str:
        statuses = [str(row.get("source_status", "UNVERIFIED")) for row in rows]
        reviews = [
            str(source.get("review_status", "UNVERIFIED"))
            for row in rows for source in row.get("sources", [])
        ]
        if "FAILED" in statuses or "FAILED" in reviews:
            return "FAILED"
        if any(status != "VERIFIED" for status in statuses) or not reviews or any(status != "VERIFIED" for status in reviews):
            return "UNVERIFIED"
        return "VERIFIED"

    identity_source_status = event_family_status(identity_proof_rows)
    corporate_source_status = event_family_status(corporate_proof_rows)
    source_review_by_family = {
        "accounting_events_rules_fees": (
            "FAILED" if "FAILED" in all_review_statuses or accounting_parse_failed else
            "UNVERIFIED" if not all_review_statuses or "UNVERIFIED" in all_review_statuses or not event_adapter_reviewed else
            "VERIFIED"
        ),
        "security_identity_events": identity_source_status,
        "corporate_actions": corporate_source_status,
    }
    source_review_status = (
        "FAILED" if "FAILED" in source_review_by_family.values() else
        "UNVERIFIED" if "UNVERIFIED" in source_review_by_family.values() else "VERIFIED"
    )
    report["source_review_by_family"] = source_review_by_family
    known_at_status = (
        "FAILED" if "FAILED" in all_known_statuses or cutoff_status["status"] == "FAILED" else
        "VERIFIED" if all_known_statuses and all(value == "VERIFIED" for value in all_known_statuses)
        and cutoff_status["status"] == "VERIFIED" else "UNVERIFIED"
    )

    input_hash_results = []
    for field in CHECKPOINT_INPUT_FIELDS:
        actual = observed.get(field)
        expected = getattr(checkpoint, field)
        if actual == expected:
            status = "VERIFIED"
            reason = None
        elif actual is None:
            status = "UNVERIFIED"
            reason = "所需输入或完整历史窗口链未提供，无法重建此摘要"
        else:
            status = "FAILED"
            reason = f"重建摘要 {actual} 与 checkpoint 摘要 {expected} 不一致"
        input_hash_results.append({"input": field, "status": status, "reconstructed_sha256": actual, "checkpoint_sha256": expected, "reason": reason})
    report["input_hash_results"] = input_hash_results
    checkpoint_binding_status = (
        "FAILED" if any(row["status"] == "FAILED" for row in input_hash_results) else
        "UNVERIFIED" if any(row["status"] == "UNVERIFIED" for row in input_hash_results) else
        "VERIFIED"
    )
    if chain_status["status"] == "FAILED":
        checkpoint_binding_status = "FAILED"

    calendar_rows = []
    if calendar is not None and not calendar.empty:
        calendar_rows = [
            {"date": row.trade_date.isoformat(), "is_open": bool(row.is_open)}
            for row in calendar.itertuples(index=False)
        ]
    event_rows = accounting_rows
    input_hashes = {field: observed.get(field) for field in CHECKPOINT_INPUT_FIELDS}
    cutoff_is_verified = cutoff_status["status"] == "VERIFIED"
    checkpoint_proof: dict[str, Any] = {
        "checkpoint_schema_version": checkpoint.schema_version,
        "state_sha256": checkpoint.state_sha256,
        "as_of_date": checkpoint.as_of_date.isoformat(),
        "next_expected_session": checkpoint.next_expected_session.isoformat(),
        "decision_cutoff_status": "VERIFIED" if cutoff_is_verified else "UNVERIFIED",
        "decision_cutoff_utc": cutoff_status.get("decision_cutoff_utc") if cutoff_is_verified else None,
        "cutoff_source": cutoff_status.get("source_id") if cutoff_is_verified else None,
        "cutoff_source_sha256": cutoff_artifact.get("source_sha256") if cutoff_is_verified else None,
        "cutoff_source_artifact": (
            {"path": cutoff_artifact["source_path"], "sha256": cutoff_artifact["source_sha256"]}
            if cutoff_is_verified else None
        ),
        "applied_increment_ids": [],
        "applied_increment_sha256s": [],
        "input_hashes": input_hashes,
    }
    no_accounting_file = accounting_file is None and not event_rows and observed.get("accounting_events_sha256") == checkpoint.accounting_events_sha256
    no_rules_file = rules_file is None and checkpoint.market_rules_sha256 is None
    no_fees_file = fees_file is None and checkpoint.fee_schedule_sha256 is None
    artifacts: dict[str, Any] = {
        "strategy_config": _artifact(config_file),
        "historical_bars_prefix": _artifact(manifest_file),
        "last_window_bars": _artifact(bars_file),
        "events": (
            _artifact(accounting_file) if accounting_file else
            _not_applicable_artifact("未使用账务事件文件；重建的空事件摘要与 checkpoint 相符")
            if no_accounting_file else _artifact(None)
        ),
        "calendar": _artifact(calendar_file),
        "rules": _artifact(rules_file) if rules_file else (
            _not_applicable_artifact("checkpoint 未绑定有效日期规则") if no_rules_file else _artifact(None)
        ),
        "fees": _artifact(fees_file) if fees_file else (
            _not_applicable_artifact("checkpoint 未绑定历史费率表") if no_fees_file else _artifact(None)
        ),
        "identity_events": _artifact(identity_file),
        "corporate_actions": _artifact(corporate_file),
        "history_manifest": _artifact(manifest_file),
        "provenance_registry": _artifact(registry_file),
        "cutoff_evidence_record": _artifact(cutoff_file),
    }
    if cutoff_is_verified:
        artifacts["decision_cutoff_source"] = {
            "path": cutoff_artifact["source_path"], "sha256": cutoff_artifact["source_sha256"],
        }
    inputs: dict[str, Any] = {
        "strategy_config_sha256": observed.get("config_sha256"),
        "historical_bars_prefix_sha256": (
            observed.get("data_chain_sha256") if chain_status["status"] == "VERIFIED" else None
        ),
        "events": event_rows,
        "identity_events": identity_proof_rows,
        "corporate_actions": corporate_proof_rows,
        "calendar": calendar_rows,
        "rules": rules_rows,
        "fees": fees_rows,
        "artifacts": artifacts,
        "normalized_input_digests": normalized_components,
        "adapter_versions": adapters,
    }
    proof = {
        "schema_version": 2,
        "increment_id": f"baseline-{checkpoint.state_sha256[:16]}",
        "checkpoint": checkpoint_proof,
        "inputs": inputs,
        "evidence_status": {
            "source_content_review_status": source_review_status,
            "known_at_evidence_status": known_at_status,
        },
    }
    proof["canonical_payload_sha256"] = canonical_payload_digest(proof)

    generated_canonical_status, generated_canonical_audit = _audit_canonical_rows(proof)
    canonical_audit = list(generated_canonical_audit)
    proof_file_audit_status, proof_file_audit = _audit_claimed_proof_files(
        proof, Path.cwd() / "input-proof-baseline.json",
    )
    proof_comparison: dict[str, Any] = {
        "status": "VERIFIED",
        "normalized_content_status": "VERIFIED",
        "checkpoint_claim_status": "VERIFIED",
        "artifact_hash_status": "VERIFIED",
        "reason": "本次 proof 由实际文件经项目适配器生成",
        "different_input_families": [],
    }
    semantic_comparison_status = "VERIFIED"
    checkpoint_claim_status = "VERIFIED"
    claimed_file_status = "VERIFIED"
    claimed_file_audit: list[dict[str, Any]] = []
    claimed_canonical_status: str | None = None
    if claimed_file is not None:
        try:
            claimed = json.loads(claimed_file.read_text(encoding="utf-8"))
            claimed_inputs = claimed.get("inputs", {}) if isinstance(claimed, dict) else {}
            compare_keys = (
                "strategy_config_sha256", "historical_bars_prefix_sha256", "events", "calendar",
                "rules", "fees", "identity_events", "corporate_actions", "normalized_input_digests",
            )
            def semantic_rows(source_inputs: Mapping[str, Any], family: str) -> Any:
                rows = source_inputs.get(family)
                if not isinstance(rows, list):
                    return rows

                identity_key = "event_id" if family in {"events", "identity_events", "corporate_actions"} else "row_id"

                def strip_paths(value: Any) -> Any:
                    if isinstance(value, dict):
                        return {key: strip_paths(item) for key, item in value.items() if key != "source_path"}
                    if isinstance(value, list):
                        return [strip_paths(item) for item in value]
                    return value

                return sorted(
                    [
                        strip_paths(row)
                        for row in rows if isinstance(row, dict)
                    ],
                    key=lambda row: str(row.get(identity_key, "")),
                )

            differences = []
            for key in compare_keys:
                row_families = {"events", "rules", "fees", "identity_events", "corporate_actions"}
                left = semantic_rows(claimed_inputs, key) if key in row_families else claimed_inputs.get(key)
                right = semantic_rows(inputs, key) if key in row_families else inputs.get(key)
                if json.dumps(left, sort_keys=True, default=str) != json.dumps(right, sort_keys=True, default=str):
                    differences.append(key)
            claimed_checkpoint = claimed.get("checkpoint", {}) if isinstance(claimed, dict) else {}
            checkpoint_differences = []
            for key in ("state_sha256", "as_of_date", "next_expected_session", "input_hashes"):
                if claimed_checkpoint.get(key) != checkpoint_proof.get(key):
                    checkpoint_differences.append(f"checkpoint.{key}")
            semantic_comparison_status = "FAILED" if differences else "VERIFIED"
            checkpoint_claim_status = "FAILED" if checkpoint_differences else "VERIFIED"
            artifact_compare_status, artifact_differences = _compare_artifact_declarations(proof, claimed)
            claimed_canonical_status, claimed_canonical_audit = _audit_canonical_rows(claimed)
            canonical_audit.extend({"proof": "claimed", **row} for row in claimed_canonical_audit)
            claimed_file_status, claimed_file_audit = _audit_claimed_proof_files(claimed, claimed_file)
            differences.extend(checkpoint_differences)
            comparison_status = (
                "FAILED" if differences or artifact_compare_status == "FAILED"
                or claimed_canonical_status == "FAILED" or claimed_file_status == "FAILED" else
                "UNVERIFIED" if artifact_compare_status == "UNVERIFIED"
                or claimed_canonical_status == "UNVERIFIED" or claimed_file_status == "UNVERIFIED" else
                "VERIFIED"
            )
            proof_comparison = {
                "status": comparison_status,
                "normalized_content_status": semantic_comparison_status,
                "checkpoint_claim_status": checkpoint_claim_status,
                "artifact_hash_status": claimed_file_status,
                "reason": (
                    "proof 规范化内容、checkpoint 或输入文件声明与适配器重建结果不一致"
                    if comparison_status == "FAILED" else
                    "claimed proof 有材料缺失，无法完成全部比对"
                    if comparison_status == "UNVERIFIED" else None
                ),
                "different_input_families": sorted(set(differences + artifact_differences)),
                "claimed_canonical_payload_status": claimed_canonical_status,
                "claimed_file_hash_status": claimed_file_status,
                "claimed_artifact_audit": claimed_file_audit,
                "claimed_proof_sha256": sha256_file(claimed_file),
            }
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            semantic_comparison_status = "FAILED"
            checkpoint_claim_status = "FAILED"
            claimed_file_status = "FAILED"
            claimed_canonical_status = "FAILED"
            proof_comparison = {
                "status": "FAILED", "normalized_content_status": "FAILED",
                "checkpoint_claim_status": "FAILED", "artifact_hash_status": "FAILED",
                "reason": f"无法读取 claimed proof: {exc}",
            }

    chain_reconstruction_status = (
        "VERIFIED" if observed.get("data_chain_sha256") is not None else chain_status["status"]
    )
    report["historical_data_chain"]["reconstruction_status"] = chain_reconstruction_status
    normalized_statuses = [row.get("status") for row in report["issues"]]
    normalized_file_roles = {
        "strategy_config", "bars", "calendar", "identity_events", "corporate_actions",
    }
    if accounting_events_required:
        normalized_file_roles.add("accounting_events")
    if checkpoint.market_rules_sha256 is not None:
        normalized_file_roles.add("market_rules")
    if checkpoint.fee_schedule_sha256 is not None:
        normalized_file_roles.add("fees")
    normalized_file_statuses = [
        row.get("status") for row in file_rows if row.get("input") in normalized_file_roles
    ]
    if (
        semantic_comparison_status == "FAILED" or "FAILED" in normalized_statuses
        or "FAILED" in normalized_file_statuses or chain_reconstruction_status == "FAILED"
        or generated_canonical_status == "FAILED"
    ):
        normalized_binding_status = "FAILED"
    elif (
        chain_reconstruction_status != "VERIFIED" or semantic_comparison_status == "UNVERIFIED"
        or "UNVERIFIED" in normalized_statuses or "UNVERIFIED" in normalized_file_statuses
        or generated_canonical_status == "UNVERIFIED"
    ):
        normalized_binding_status = "UNVERIFIED"
    else:
        normalized_binding_status = "VERIFIED"
    file_audits = file_rows + proof_file_audit + claimed_file_audit
    def is_cutoff_audit(row: Mapping[str, Any]) -> bool:
        role = str(row.get("role", ""))
        return row.get("input") == "cutoff_evidence" or any(
            value in role for value in ("cutoff_evidence", "decision_cutoff_source")
        )

    def is_source_evidence_audit(row: Mapping[str, Any]) -> bool:
        role = str(row.get("role", ""))
        return row.get("input") == "provenance_registry" or role == "provenance_registry" or any(
            role.startswith(f"{family}:")
            for family in ("events", "rules", "fees", "identity_events", "corporate_actions")
        )

    cutoff_audits = [row for row in file_audits if is_cutoff_audit(row)]
    source_evidence_audits = [row for row in file_audits if is_source_evidence_audit(row)]
    provided_input_audits = [
        row for row in file_audits
        if not is_cutoff_audit(row) and not is_source_evidence_audit(row)
    ]
    provided_input_hash_status = _audit_status(provided_input_audits)
    source_evidence_file_hash_status = _audit_status(source_evidence_audits)
    cutoff_evidence_file_hash_status = _audit_status(cutoff_audits)
    # Keep the legacy aggregate strict: required cutoff evidence remains part of
    # the overall result. The component fields make a missing evidence file
    # distinguishable from a hash mismatch in the market-data inputs.
    file_hash_status = _audit_status(file_audits)
    canonical_status = (
        "FAILED" if generated_canonical_status == "FAILED" or claimed_canonical_status == "FAILED" else
        "UNVERIFIED" if generated_canonical_status == "UNVERIFIED" or claimed_canonical_status == "UNVERIFIED" else
        "VERIFIED"
    )
    if checkpoint_claim_status == "FAILED":
        checkpoint_binding_status = "FAILED"
    report.update({
        "checkpoint_schema_version": checkpoint.schema_version,
        "checkpoint_binding_valid": checkpoint_binding_status,
        "input_file_hashes_valid": file_hash_status,
        "input_files_binding_valid": file_hash_status,
        "provided_input_artifact_hashes_valid": provided_input_hash_status,
        "source_provenance_evidence_file_hash_valid": source_evidence_file_hash_status,
        "decision_cutoff_evidence_file_hash_valid": cutoff_evidence_file_hash_status,
        "input_file_hashes_summary": {
            "provided_input_artifacts": {
                "status": provided_input_hash_status,
                "checked_rows": len(provided_input_audits),
            },
            "source_provenance_evidence_files": {
                "status": source_evidence_file_hash_status,
                "checked_rows": len(source_evidence_audits),
            },
            "decision_cutoff_evidence": {
                "status": cutoff_evidence_file_hash_status,
                "checked_rows": len(cutoff_audits),
                "reason": cutoff_status.get("reason"),
            },
            "overall_status": file_hash_status,
            "overall_gate_unchanged": True,
        },
        "canonical_payload_digest_valid": canonical_status,
        "normalized_inputs_binding_valid": normalized_binding_status,
        "can_continue": False,
        "candidate_append_compatible": False,
        "engine_resume_supported": False,
        "engine_note": "规范化输入重建不会合并输入或执行策略；当前 checkpoint 续跑仍保持严格完整哈希校验。",
        "source_content_review_status": source_review_status,
        "known_at_evidence_status": known_at_status,
        "proof_comparison": proof_comparison,
        "canonical_row_hash_audit": canonical_audit,
        "input_file_hash_audit": file_audits,
        "claimed_proof_input_file_hash_status": claimed_file_status,
        "source_files": file_rows,
        "adapter_versions": adapters,
        "normalized_input_digests": normalized_components,
        "baseline_proof_canonical_sha256": proof["canonical_payload_sha256"],
    })
    return proof, report
