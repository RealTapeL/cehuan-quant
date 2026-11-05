#!/usr/bin/env python3
"""Create and validate a synthetic forward-paper observation; no provider or broker calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cehuan.research.paper_observation import build_synthetic_paper_observation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_synthetic_paper_observation(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
