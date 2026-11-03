from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from cehuan.backtest.checkpoint import read_backtest_checkpoint
from cehuan.backtest.incremental_inputs import audit_incremental_inputs


REPO_ROOT = Path(__file__).resolve().parents[1]


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 输入 {path}: {exc}") from exc


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
    if not isinstance(proof_checkpoint, dict):
        raise ValueError("baseline proof 缺少 checkpoint 对象")
    binding = {
        "state_sha256": checkpoint.state_sha256,
        "as_of_date": checkpoint.as_of_date.isoformat(),
        "next_expected_session": checkpoint.next_expected_session.isoformat(),
    }
    for field, expected in binding.items():
        if proof_checkpoint.get(field) != expected:
            raise ValueError(f"baseline proof 与实际 checkpoint 的 {field} 不匹配")

    report = audit_incremental_inputs(baseline, candidate)
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
        "can_continue": report["can_continue"],
        "engine_resume_supported": report["engine_resume_supported"],
        "blocker_count": len(report["blockers"]),
        "report": str(output_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
