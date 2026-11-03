from __future__ import annotations

import argparse
import json
from pathlib import Path

from cehuan.data.accounting_sample_audit import build_accounting_sample_review_pack


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成本机私有的公司行动公告逐字段人工审阅包")
    parser.add_argument(
        "--samples",
        default="docs/evidence/corporate-action-sample-audit.csv",
        help="公司行动事件样本清单",
    )
    parser.add_argument(
        "--fields",
        default="docs/evidence/corporate-action-sample-field-review.csv",
        help="逐字段公告值/提取值/页码审核表",
    )
    parser.add_argument(
        "--output",
        default="outputs/corporate-action-review-pack",
        help="输出目录；目录已存在时拒绝覆盖。请放在 gitignore 覆盖的位置",
    )
    args = parser.parse_args()

    def input_path(value: str) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    output = input_path(args.output)
    result = build_accounting_sample_review_pack(
        input_path(args.samples), input_path(args.fields), output,
    )
    print(json.dumps({**result, "output_dir": str(output.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
