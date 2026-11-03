from __future__ import annotations

import argparse
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from cehuan.backtest.checkpoint import BacktestCheckpoint, read_backtest_checkpoint
from cehuan.backtest.incremental_inputs import audit_incremental_inputs, validate_incremental_proof


REPO_ROOT = Path(__file__).resolve().parents[1]

CHECKPOINT_HASH_FIELDS = tuple(
    item.name for item in fields(BacktestCheckpoint)
    if item.name.endswith("_sha256") and item.name != "state_sha256"
)
INPUT_ARTIFACT_ROLES = (
    "strategy_config", "historical_bars_prefix", "events", "calendar", "rules", "fees",
)


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 输入 {path}: {exc}") from exc


def _checkpoint_binding_status(proof_checkpoint: dict[str, Any], checkpoint: Any) -> tuple[str, list[str]]:
    differences: list[str] = []
    for field, actual in (
        ("state_sha256", checkpoint.state_sha256),
        ("as_of_date", checkpoint.as_of_date.isoformat()),
        ("next_expected_session", checkpoint.next_expected_session.isoformat()),
    ):
        if proof_checkpoint.get(field) != actual:
            differences.append(f"{field}: proof={proof_checkpoint.get(field)!r}, checkpoint={actual!r}")
    declared_hashes = proof_checkpoint.get("input_hashes")
    if not isinstance(declared_hashes, dict):
        return "FAILED" if differences else "UNVERIFIED", differences + ["checkpoint.input_hashes 未提供"]
    for field in CHECKPOINT_HASH_FIELDS:
        declared = declared_hashes.get(field, "__MISSING__")
        actual = getattr(checkpoint, field, None)
        if declared != actual:
            differences.append(f"input_hashes.{field}: proof={declared!r}, checkpoint={actual!r}")
    return ("FAILED", differences) if differences else ("VERIFIED", [])


def _verify_artifact(path_value: Any, expected_sha: Any, role: str) -> dict[str, str]:
    if path_value in (None, ""):
        return {"role": role, "status": "UNVERIFIED", "detail": "未提供实际文件路径"}
    if not isinstance(path_value, str):
        return {"role": role, "status": "FAILED", "detail": "实际文件路径必须是字符串"}
    if expected_sha in (None, ""):
        return {"role": role, "status": "UNVERIFIED", "detail": "未提供 64 位 SHA-256"}
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha.strip()):
        return {"role": role, "status": "FAILED", "detail": "SHA-256 格式无效"}
    if not path_value.strip():
        return {"role": role, "status": "UNVERIFIED", "detail": "未提供实际文件路径"}
    try:
        path = _path(path_value)
        if not path.is_file():
            return {"role": role, "status": "FAILED", "detail": f"文件不存在: {path}"}
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        return {"role": role, "status": "FAILED", "detail": f"无法读取登记文件: {exc}"}
    expected = expected_sha.strip().lower()
    return {
        "role": role,
        "path": str(path),
        "declared_sha256": expected,
        "observed_sha256": observed,
        "status": "VERIFIED" if observed == expected else "FAILED",
    }


def _audit_declared_artifacts(proofs: list[tuple[str, dict[str, Any]]]) -> tuple[str, list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    for side, proof in proofs:
        checkpoint = proof.get("checkpoint", {})
        inputs = proof.get("inputs", {})
        artifacts = inputs.get("artifacts", {}) if isinstance(inputs, dict) else {}
        if not isinstance(artifacts, dict):
            rows.append({"role": f"{side}:inputs.artifacts", "status": "FAILED", "detail": "必须是对象"})
            continue
        for role in INPUT_ARTIFACT_ROLES:
            item = artifacts.get(role)
            if not isinstance(item, dict):
                rows.append({"role": f"{side}:{role}", "status": "UNVERIFIED", "detail": "proof 未登记实际输入文件"})
                continue
            item_audit = _verify_artifact(item.get("path"), item.get("sha256"), f"{side}:{role}")
            rows.append(item_audit)
        cutoff = checkpoint.get("cutoff_source_artifact") if isinstance(checkpoint, dict) else None
        if isinstance(cutoff, dict):
            rows.append(_verify_artifact(cutoff.get("path"), cutoff.get("sha256"), f"{side}:decision_cutoff_source"))
        else:
            rows.append({
                "role": f"{side}:decision_cutoff_source", "status": "UNVERIFIED",
                "detail": "未登记截止时点证据原件路径",
            })
        for family in ("events", "rules", "fees"):
            family_rows = inputs.get(family, []) if isinstance(inputs, dict) else []
            for row in family_rows if isinstance(family_rows, list) else []:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("event_id", row.get("row_id", "unknown")))
                rows.append(_verify_artifact(
                    row.get("source_path"), row.get("source_sha256"), f"{side}:{family}:{row_id}"
                ))
    if any(item["status"] == "FAILED" for item in rows):
        return "FAILED", rows
    if rows and all(item["status"] == "VERIFIED" for item in rows):
        return "VERIFIED", rows
    return "UNVERIFIED", rows


def _blocked_report(proof_schema_status: str, detail: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "report_type": "checkpoint_incremental_input_preflight_only",
        "proof_schema_valid": proof_schema_status,
        "checkpoint_binding_valid": "UNVERIFIED",
        "input_files_binding_valid": "UNVERIFIED",
        "canonical_payload_digest_valid": "UNVERIFIED",
        "normalized_inputs_binding_valid": "UNVERIFIED",
        "canonical_row_hash_audit": [],
        "source_artifact_binding_status": "UNVERIFIED",
        "candidate_rules_compatible": False,
        "candidate_append_compatible": False,
        "can_continue": False,
        "engine_resume_supported": False,
        "engine_note": "当前 run_backtest 仍要求完整输入哈希一致；本报告不会放行续跑。",
        "additions": {"events": [], "calendar": [], "rules": [], "fees": []},
        "changes": [],
        "blockers": [{"code": "PROOF_VALIDATION_FAILED", "detail": detail}],
        "earliest_possible_affected_date": None,
        "earliest_affected_date_precision": "UNKNOWN_PROOF_INVALID",
        "requires_recompute": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 checkpoint 增量输入的只读兼容性预检报告；不会放行回测续跑",
    )
    parser.add_argument("--checkpoint", required=True, help="已经封存并校验的 checkpoint v1 JSON")
    parser.add_argument("--baseline-proof", required=True, help="保存 checkpoint 历史输入前缀及截止时点证据的 JSON")
    parser.add_argument("--candidate-proof", required=True, help="候选新增输入及其来源证明 JSON")
    parser.add_argument("--output", required=True, help="报告 JSON 输出路径")
    args = parser.parse_args()

    checkpoint_path = _path(args.checkpoint)
    baseline_path = _path(args.baseline_proof)
    candidate_path = _path(args.candidate_proof)
    output_path = _path(args.output)
    checkpoint = read_backtest_checkpoint(checkpoint_path)
    baseline = _read_json(baseline_path)
    candidate = _read_json(candidate_path)
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("baseline/candidate proof 顶层必须是 JSON 对象")
    proof_checkpoint = baseline.get("checkpoint", {})
    checkpoint_status, checkpoint_differences = (
        _checkpoint_binding_status(proof_checkpoint, checkpoint)
        if isinstance(proof_checkpoint, dict)
        else ("FAILED", ["baseline proof 缺少 checkpoint 对象"])
    )
    artifact_status, artifact_audit = _audit_declared_artifacts([
        ("baseline", baseline), ("candidate", candidate),
    ])
    proof_validation_errors: list[str] = []
    for label, proof in (("baseline", baseline), ("candidate", candidate)):
        try:
            validate_incremental_proof(proof, label)
        except ValueError as exc:
            proof_validation_errors.append(f"{label}: {exc}")
    if proof_validation_errors:
        report = _blocked_report("FAILED", "; ".join(proof_validation_errors))
    else:
        try:
            report = audit_incremental_inputs(baseline, candidate)
            report["proof_schema_valid"] = "VERIFIED"
        except ValueError as exc:
            report = _blocked_report("VERIFIED", str(exc))
            report["checkpoint_binding_valid"] = "FAILED"
            report["checkpoint_binding_differences"] = [str(exc)]
    proof_checkpoint_status = report.get("checkpoint_binding_valid", "UNVERIFIED")
    if proof_checkpoint_status == "UNVERIFIED" and not report.get("checkpoint_binding_differences"):
        # The pure comparator intentionally cannot verify this layer. The CLI
        # replaces that placeholder with its comparison against the sealed file.
        proof_checkpoint_status = "VERIFIED"
    combined_checkpoint_status = (
        "FAILED" if checkpoint_status == "FAILED" or proof_checkpoint_status == "FAILED" else
        "UNVERIFIED" if checkpoint_status == "UNVERIFIED" or proof_checkpoint_status == "UNVERIFIED" else
        "VERIFIED"
    )
    report["checkpoint_binding_valid"] = combined_checkpoint_status
    report["checkpoint_binding_differences"] = list(report.get("checkpoint_binding_differences", []))
    report["checkpoint_binding_differences"].extend(checkpoint_differences)
    report["input_file_hashes_valid"] = (
        "FAILED" if any(item["status"] == "FAILED" for item in artifact_audit)
        else "VERIFIED" if all(item["status"] == "VERIFIED" for item in artifact_audit)
        else "UNVERIFIED"
    )
    report["input_file_hash_audit"] = artifact_audit
    report["input_files_binding_valid"] = artifact_status
    report["normalized_inputs_binding_valid"] = "UNVERIFIED"
    report["candidate_append_compatible"] = (
        report["candidate_rules_compatible"]
        and combined_checkpoint_status == "VERIFIED"
        and artifact_status == "VERIFIED"
        and report["normalized_inputs_binding_valid"] == "VERIFIED"
    )
    report["blockers"] = [
        item for item in report["blockers"]
        if item["code"] not in {"CHECKPOINT_BINDING_UNVERIFIED", "INPUT_FILES_BINDING_UNVERIFIED"}
    ]
    if combined_checkpoint_status != "VERIFIED":
        report["blockers"].append({
            "code": "CHECKPOINT_BINDING_" + combined_checkpoint_status,
            "detail": "; ".join(report["checkpoint_binding_differences"]) or "无法核实 checkpoint 绑定",
        })
    if artifact_status != "VERIFIED":
        report["blockers"].append({
            "code": "INPUT_FILES_BINDING_" + artifact_status,
            "detail": "proof 登记的实际输入文件字节散列未全部核实",
        })
    if report["normalized_inputs_binding_valid"] != "VERIFIED":
        report["blockers"].append({
            "code": "NORMALIZED_INPUTS_BINDING_UNVERIFIED",
            "detail": "文件字节散列通过仍不足以证明规范化行由这些文件经项目适配器生成",
        })
    report["input_files"] = {
        "checkpoint_sha256": hashlib.sha256(checkpoint_path.read_bytes()).hexdigest(),
        "baseline_proof_sha256": hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        "candidate_proof_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output_path.write_text(serialized, encoding="utf-8")
    print(json.dumps({
        "candidate_append_compatible": report["candidate_append_compatible"],
        "checkpoint_binding_valid": report["checkpoint_binding_valid"],
        "input_file_hashes_valid": report["input_file_hashes_valid"],
        "input_files_binding_valid": report["input_files_binding_valid"],
        "can_continue": report["can_continue"],
        "engine_resume_supported": report["engine_resume_supported"],
        "blocker_count": len(report["blockers"]),
        "report": str(output_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
