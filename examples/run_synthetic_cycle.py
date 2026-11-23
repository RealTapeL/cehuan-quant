"""命令行运行合成交易计划到复盘闭环的示例入口。

调用研究模块生成虚构计划、成交与复盘产物；不访问真实行情或券商，
输出目录由命令行参数指定。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from cehuan.research.synthetic_cycle import run_synthetic_cycle


def main() -> int:
    """解析演练参数并运行指定的合成复盘场景。

    参数：无；参数从命令行读取。返回：进程退出码 0。副作用是创建新的
    合成研究产物目录；具体场景由 ``--scenario`` 限定。
    """
    parser = argparse.ArgumentParser(description="Run a fully synthetic plan-to-review cycle; no broker calls")
    parser.add_argument("--output", default="outputs/synthetic-cycle", help="root directory for synthetic artifacts")
    parser.add_argument(
        "--scenario",
        choices=("partial_fill", "no_fill", "duplicate_import", "partial_fill_cancelled", "modified_plan", "missing_positions"),
        default="partial_fill",
        help="要演示的复盘异常路径",
    )
    args = parser.parse_args()
    output = Path(args.output).expanduser()
    if not output.is_absolute():
        output = Path(__file__).resolve().parents[1] / output
    run_dir = run_synthetic_cycle(output, scenario=args.scenario)
    print(f"SYNTHETIC ONLY; no broker connection or order was made.\nArtifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
