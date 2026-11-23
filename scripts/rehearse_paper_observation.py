#!/usr/bin/env python3
"""用合成输入演练前瞻纸面观察记录的状态流转；不请求数据供应商，也不连接券商。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cehuan.research.paper_observation import build_synthetic_paper_observation


def main() -> int:
    """构造并验证合成前瞻观察样例；不访问供应商、真实证券数据或券商。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_synthetic_paper_observation(args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
