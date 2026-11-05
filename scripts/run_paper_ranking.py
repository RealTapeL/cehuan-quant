#!/usr/bin/env python3
"""Generate a sealed ranking-only paper artifact; never creates a broker order."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cehuan.research.forward_ranking import create_paper_ranking_artifact


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/default.toml")
    parser.add_argument("--signal-date", required=True)
    parser.add_argument("--classification", choices=("SYNTHETIC", "FORWARD_PAPER"), default="SYNTHETIC")
    parser.add_argument("--source-capture-manifest", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    private_root = (ROOT / "data/private").resolve()
    if private_root not in output_dir.parents:
        parser.error("output-dir 必须位于 data/private/ 下")
    result = create_paper_ranking_artifact(
        bars_path=args.data,
        config_path=args.config,
        signal_date=args.signal_date,
        output_dir=output_dir,
        classification=args.classification,
        source_capture_manifest_path=args.source_capture_manifest,
    )
    manifest = result["manifest"]
    print(json.dumps({
        "output_dir": result["output_dir"],
        "classification": manifest["classification"],
        "status": manifest["status"],
        "signal_date": manifest["signal_date"],
        "ranking_generated_at_utc": manifest["ranking_generated_at_utc"],
        "candidate_count": manifest.get("candidate_count", 0),
        "ranking_file_sha256": manifest.get("ranking_file_sha256"),
        "data_trust": manifest["data_trust"],
        "blocking_reasons": manifest.get("blocking_reasons", []),
        "orders_generated": manifest["orders_generated"],
        "formal_baseline": manifest["formal_baseline"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
