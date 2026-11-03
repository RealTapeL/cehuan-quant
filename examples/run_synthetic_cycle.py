from __future__ import annotations

import argparse
from pathlib import Path

from cehuan.research.synthetic_cycle import run_synthetic_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a fully synthetic plan-to-review cycle; no broker calls")
    parser.add_argument("--output", default="outputs/synthetic-cycle", help="root directory for synthetic artifacts")
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = Path(__file__).resolve().parents[1] / output
    run_dir = run_synthetic_cycle(output)
    print(f"SYNTHETIC ONLY; no broker connection or order was made.\nArtifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
