#!/usr/bin/env python3
"""对单个标的执行有界、只读的 AKShare 分钟数据探测；原始响应和结果只写入私有数据目录。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cehuan.research.provider_probe import capture_akshare_minute_probe


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """执行一次有界分钟行情探测并保存请求与响应摘要；不将探测结果并入正式行情快照。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="规范代码，例如 SH600000")
    parser.add_argument("--trade-date", required=True, help="单个交易日 YYYY-MM-DD")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-attempts", type=int, default=1, choices=(1, 2))
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    private_root = (ROOT / "data/private").resolve()
    if private_root not in output_dir.parents:
        parser.error("output-dir 必须位于 data/private/ 下；原始响应不可写入 Git 管理目录")
    result = capture_akshare_minute_probe(
        symbol=args.symbol,
        trade_date=args.trade_date,
        output_dir=output_dir,
        max_attempts=args.max_attempts,
    )
    report = result["report"]
    print(json.dumps({
        "output_dir": result["output_dir"],
        "classification": report["classification"],
        "provider": report["provider"],
        "symbol": report["symbol"],
        "requested_trade_date": report["requested_trade_date"],
        "overall_status": report["overall_status"],
        "attempt_count": report["attempt_count"],
        "response_row_count_for_requested_date": report["response_row_count_for_requested_date"],
        "provider_publication_time_status": report["provider_publication_time_status"],
        "capture_layer": report["response_capture_layer"],
        "validation": result["validation"]["status"],
        "status_data": report["status_data"],
        "research_eligibility": report["research_eligibility"],
        "broker_connected": report["broker_connected"],
        "orders_generated": report["orders_generated"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
