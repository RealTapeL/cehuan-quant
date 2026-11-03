from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import uuid

import pandas as pd

from cehuan.accounting.events import ActionSource, CashDividendEvent, ShareDistributionEvent
from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config


def _write_json(payload: object, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def run(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / f"synthetic-action-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(exist_ok=False)

    # These are generated fixture sessions and prices. The July 4/5 dates anchor
    # the fixture to the public notice, but this is not the exchange calendar.
    dates = pd.bdate_range("2024-06-03", "2024-07-05")
    rows: list[dict[str, object]] = []
    for index, timestamp in enumerate(dates):
        close = 10.0 + 0.025 * index
        if timestamp.date().isoformat() == "2024-07-05":
            previous_close = 10.0 + 0.025 * (index - 1)
            # A synthetic ex-right reference-price adjustment prevents a fake
            # price discontinuity from masquerading as action-generated NAV.
            close = (previous_close - 0.10) / (1.0 + 0.30)
        rows.append({
            "trade_date": timestamp.date().isoformat(),
            "symbol": "SH600101",
            "name": "SYNTHETIC SH600101",
            "open": close - 0.01,
            "high": close + 0.08,
            "low": close - 0.08,
            "close": close,
            "adjusted_close": close,
            "volume_shares": 1_000_000,
            "amount_cny": close * 1_000_000,
            "is_listed": True,
            "is_suspended": False,
            "is_st": False,
            "has_price_limit": True,
            "price_limit_pct": 0.10,
            "buy_min_shares": 100,
            "buy_step_shares": 100,
            "sell_min_shares": 100,
            "sell_step_shares": 100,
            "board": "MAIN",
            "market_rule_source": "synthetic fixture assumption",
            "market_rule_valid_from": "2024-06-03",
            "market_rule_valid_to": "",
            "data_source": "synthetic fixture; not AKShare/BaoStock",
            "retrieved_at_utc": "2026-11-03T00:00:00+00:00",
            "available_at": f"{timestamp.date().isoformat()}T07:05:00+00:00",
        })
    bars = pd.DataFrame(rows)
    bars.to_csv(run_dir / "synthetic_bars.csv", index=False)
    bars_sha256 = hashlib.sha256((run_dir / "synthetic_bars.csv").read_bytes()).hexdigest()

    source_bytes = (
        "SYNTHETIC ONLY. Rewritten fixture fields loosely exercise a same-day dividend and share distribution.\n"
        "No broker statement, actual lot allocation, tax result, or market price is represented.\n"
    ).encode("utf-8")
    source_path = run_dir / "synthetic_notice.txt"
    source_path.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    source = ActionSource(
        source_id="synthetic-fixture-notice",
        url="https://example.invalid/synthetic-notice",
        sha256=source_sha256,
        title="Synthetic combined dividend and share-distribution fixture",
        page_reference="synthetic fixture lines 1-2",
        reviewed_by="synthetic fixture; not human announcement sign-off",
        reviewed_at="2026-11-03",
    )
    event_time = "2024-06-28T06:00:00Z"
    dividend = CashDividendEvent(
        event_id="synthetic-dividend-2024-07",
        symbol="SH600101",
        record_date="2024-07-04",
        ex_date="2024-07-05",
        payable_date="2024-07-05",
        gross_per_share_cny=Decimal("0.10"),
        known_at_utc=event_time,
        source=source,
    )
    distribution = ShareDistributionEvent(
        event_id="synthetic-transfer-2024-07",
        symbol="SH600101",
        record_date="2024-07-04",
        effective_date="2024-07-05",
        registration_date="2024-07-05",
        tradable_date="2024-07-05",
        shares_per_old_share=Decimal("0.30"),
        known_at_utc=event_time,
        source=source,
    )
    config = load_config()
    config["strategy"].update({
        "lookback_sessions": 3,
        "rebalance_sessions": 3,
        "max_positions": 1,
        "max_position_weight": 0.20,
        "max_total_exposure": 0.20,
        "min_momentum": 0.0,
    })
    config["backtest"]["rolling_window_sessions"] = 8
    result = run_backtest(
        bars,
        100_000,
        config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
        accounting_events=(dividend, distribution),
        dividend_tax_treatment_by_event={dividend.event_id: "DEFERRED_BY_HOLDING_PERIOD"},
        dividend_payment_withholding_per_share_cny_by_event={dividend.event_id: Decimal("0")},
    )
    result["equity_curve"].to_csv(run_dir / "equity_curve.csv", index=False)
    result["trades"].to_csv(run_dir / "trades.csv", index=False)
    result["accounting_journal"].to_csv(run_dir / "accounting_journal.csv", index=False)
    pd.DataFrame([
        asdict(record) for record in result["accounting_snapshot"].dividend_tax_records
    ]).to_csv(run_dir / "dividend_tax_lot_records.csv", index=False)
    _write_json(asdict(result["accounting_snapshot"]), run_dir / "accounting_snapshot.json")
    _write_json(result["metrics"], run_dir / "diagnostic_metrics.json")
    _write_json({
        "result_type": "synthetic_ledger_v2_research_diagnostic",
        "synthetic_only": True,
        "exchange_calendar": "not used; generated business-day fixture sessions",
        "private_market_data_used": False,
        "broker_data_used": False,
        "broker_order_sent": False,
        "real_notice_rows_used_as_events": False,
        "event_fields_rewritten_for_fixture": {
            "cash_dividend_cny_per_share": "0.10",
            "share_distribution_per_old_share": "0.30",
            "record_date": "2024-07-04",
            "effective_and_payment_date": "2024-07-05",
            "payment_withholding_per_share_assumption": "0; policy fixture, not account evidence",
        },
        "source_fixture_sha256": source_sha256,
        "bars_sha256": bars_sha256,
        "artifact_files": sorted([path.name for path in run_dir.iterdir()] + ["manifest.json"]),
    }, run_dir / "manifest.json")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a synthetic same-day dividend/share-distribution ledger diagnostic; no broker calls"
    )
    parser.add_argument("--output", default="outputs/synthetic-action-diagnostic")
    args = parser.parse_args()
    output_root = Path(args.output).expanduser()
    if not output_root.is_absolute():
        output_root = Path(__file__).resolve().parents[1] / output_root
    run_dir = run(output_root)
    print(f"SYNTHETIC ONLY; no private market data, account evidence, or broker orders used.\nArtifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
