from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from cehuan.backtest.checkpoint import read_backtest_checkpoint
from cehuan.backtest.input_proof_builder import rebuild_checkpoint_input_proof


REPO_ROOT = Path(__file__).resolve().parents[1]


def _path(value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _atomic_json(path: Path, value: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="使用策环现有适配器重建 checkpoint 输入证明；只读，不运行回测或改写 checkpoint",
    )
    parser.add_argument("--checkpoint", required=True, help="已封存的 backtest checkpoint v1 JSON")
    parser.add_argument("--config", help="实际使用的 TOML 配置；默认 configs/default.toml")
    parser.add_argument("--bars", help="checkpoint 最后窗口的规范行情 CSV；缺省取 checkpoint.source_data_path")
    parser.add_argument("--calendar", required=True, help="实际使用的完整交易日历 CSV")
    parser.add_argument("--identity-events", help="证券身份事件 CSV；默认使用配置文件指定的路径")
    parser.add_argument("--corporate-actions", help="公司行动 CSV；默认 configs/corporate_actions.csv")
    parser.add_argument("--accounting-events", help="ledger_v2 事件 CSV；没有事件文件时重建为空事件摘要")
    parser.add_argument("--market-rules", help="有效日期市场规则 CSV；须与 --fees 成对提供")
    parser.add_argument("--fees", help="有效日期费率 CSV；须与 --market-rules 成对提供")
    parser.add_argument("--history-manifest", help="按执行顺序列出 checkpoint 所有输入窗口的 schema_version=1 JSON")
    parser.add_argument("--provenance-registry", help="来源、known_at 与人工复核登记 CSV")
    parser.add_argument("--cutoff-evidence", help="绑定 checkpoint state SHA 的决策截止时点证据 JSON")
    parser.add_argument("--claimed-proof", help="可选：核对已有 proof 是否与实际适配器重建结果一致")
    parser.add_argument("--output-dir", required=True, help="私有输出目录")
    args = parser.parse_args()

    checkpoint_path = _path(args.checkpoint)
    output_dir = _path(args.output_dir)
    assert checkpoint_path is not None and output_dir is not None
    checkpoint = read_backtest_checkpoint(checkpoint_path)
    proof, report = rebuild_checkpoint_input_proof(
        checkpoint,
        config_path=_path(args.config) or (REPO_ROOT / "configs/default.toml"),
        bars_path=_path(args.bars),
        calendar_path=_path(args.calendar),
        identity_events_path=_path(args.identity_events),
        corporate_actions_path=_path(args.corporate_actions),
        accounting_events_path=_path(args.accounting_events),
        market_rules_path=_path(args.market_rules),
        fees_path=_path(args.fees),
        history_manifest_path=_path(args.history_manifest),
        provenance_registry_path=_path(args.provenance_registry),
        cutoff_evidence_path=_path(args.cutoff_evidence),
        claimed_proof_path=_path(args.claimed_proof),
    )
    proof_path = output_dir / "input-proof-baseline.json"
    report_path = output_dir / "input-proof-rebuild-report.json"
    if proof_path.exists() or report_path.exists():
        raise FileExistsError(
            f"重建输出已存在，避免覆盖审计产物：{proof_path} 或 {report_path}；请指定新的 --output-dir"
        )
    proof_bytes = _atomic_json(proof_path, proof)
    report["baseline_proof_file_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
    report["baseline_proof_path"] = str(proof_path)
    report["checkpoint_file_sha256"] = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    _atomic_json(report_path, report)
    print(json.dumps({
        "proof": str(proof_path),
        "report": str(report_path),
        "input_file_hashes_valid": report["input_file_hashes_valid"],
        "canonical_payload_digest_valid": report["canonical_payload_digest_valid"],
        "normalized_inputs_binding_valid": report["normalized_inputs_binding_valid"],
        "checkpoint_binding_valid": report["checkpoint_binding_valid"],
        "historical_data_chain": report["historical_data_chain"]["status"],
        "source_content_review_status": report["source_content_review_status"],
        "known_at_evidence_status": report["known_at_evidence_status"],
        "can_continue": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
