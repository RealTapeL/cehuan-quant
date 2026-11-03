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

from cehuan.accounting.events import ActionSource, CodeChangeEvent
from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config


def _write_json(payload: object, path: Path) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def run(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / f"synthetic-code-change-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    run_dir.mkdir(exist_ok=False)

    dates = pd.bdate_range("2025-01-02", periods=24)
    effective_date = dates[13].date()
    rows: list[dict[str, object]] = []
    for index, timestamp in enumerate(dates):
        day = timestamp.date()
        prior_old = 10.0 + 0.18 * max(0, index - 1)
        close_old = 10.0 + 0.18 * index
        symbol = "SZ300114" if day < effective_date else "SZ302132"
        name = "SYNTHETIC OLD CODE" if day < effective_date else "SYNTHETIC NEW CODE"
        rows.append({
            "trade_date": day.isoformat(), "symbol": symbol, "name": name,
            "open": prior_old, "high": max(prior_old, close_old) + 0.05,
            "low": min(prior_old, close_old) - 0.05, "close": close_old,
            "adjusted_close": close_old, "volume_shares": 1_000_000,
            "amount_cny": close_old * 1_000_000, "is_listed": True,
            "is_suspended": False, "is_st": False, "has_price_limit": True,
            "price_limit_pct": 0.10, "buy_min_shares": 100, "buy_step_shares": 100,
            "sell_min_shares": 100, "sell_step_shares": 100, "board": "MAIN",
            "market_rule_source": "synthetic fixture assumption",
            "market_rule_valid_from": "2025-01-02", "market_rule_valid_to": "",
            "data_source": "synthetic fixture; not AKShare/BaoStock",
            "retrieved_at_utc": "2026-11-03T00:00:00+00:00",
            "available_at": f"{day.isoformat()}T07:05:00+00:00",
        })
        # A weaker fixed alternative keeps a one-position momentum strategy focused
        # on the changing identity in this deliberately synthetic fixture.
        close_other = 30.0 - 0.03 * index
        previous_other = 30.0 - 0.03 * max(0, index - 1)
        rows.append({
            "trade_date": day.isoformat(), "symbol": "SZ000001", "name": "SYNTHETIC ALTERNATIVE",
            "open": previous_other, "high": max(previous_other, close_other) + 0.05,
            "low": min(previous_other, close_other) - 0.05, "close": close_other,
            "adjusted_close": close_other, "volume_shares": 1_000_000,
            "amount_cny": close_other * 1_000_000, "is_listed": True,
            "is_suspended": False, "is_st": False, "has_price_limit": True,
            "price_limit_pct": 0.10, "buy_min_shares": 100, "buy_step_shares": 100,
            "sell_min_shares": 100, "sell_step_shares": 100, "board": "MAIN",
            "market_rule_source": "synthetic fixture assumption",
            "market_rule_valid_from": "2025-01-02", "market_rule_valid_to": "",
            "data_source": "synthetic fixture; not AKShare/BaoStock",
            "retrieved_at_utc": "2026-11-03T00:00:00+00:00",
            "available_at": f"{day.isoformat()}T07:05:00+00:00",
        })

    bars = pd.DataFrame(rows)
    bars_path = run_dir / "synthetic_bars.csv"
    bars.to_csv(bars_path, index=False)
    bars_sha256 = hashlib.sha256(bars_path.read_bytes()).hexdigest()
    notice = (
        "SYNTHETIC ONLY. Fixture exercises a 1:1 symbol migration without representing a real issuer notice.\n"
        "No real security identity, account position, broker result, or market price is represented.\n"
    ).encode("utf-8")
    source_path = run_dir / "synthetic_notice.txt"
    source_path.write_bytes(notice)
    source_sha256 = hashlib.sha256(notice).hexdigest()
    source = ActionSource(
        source_id="synthetic-code-change-fixture",
        url="https://example.invalid/synthetic-code-change",
        sha256=source_sha256,
        title="Synthetic 1:1 code change fixture; not a real announcement",
        page_reference="synthetic fixture lines 1-2",
        reviewed_by="synthetic fixture; no human announcement sign-off",
        reviewed_at="2026-11-03",
    )
    event = CodeChangeEvent(
        event_id="synthetic-code-change-2025-01",
        old_symbol="SZ300114",
        new_symbol="SZ302132",
        effective_date=effective_date,
        security_identity="SYNTHETIC-SAME-ISSUER",
        share_ratio=Decimal("1"),
        known_at_utc="2024-12-20T00:00:00Z",
        source=source,
    )
    config = load_config()
    config["strategy"].update({
        "lookback_sessions": 3, "rebalance_sessions": 3, "max_positions": 1,
        "max_position_weight": 0.25, "max_total_exposure": 0.25, "min_momentum": 0.0,
    })
    config["backtest"]["rolling_window_sessions"] = 8
    empty_identity = pd.DataFrame(columns=[
        "event_id", "old_symbol", "new_symbol", "effective_date", "share_ratio",
        "ratio_basis", "security_identity", "event_status", "evidence_type", "evidence_url", "evidence_summary",
    ])
    empty_actions = pd.DataFrame(columns=[
        "event_id", "old_symbol", "old_last_trade_date", "delisting_date", "new_symbol",
        "exchange_ratio", "cash_per_old_share_cny", "cash_settlement_date", "status", "source_url",
    ])
    result = run_backtest(
        bars, 100_000, config,
        security_identity_events=empty_identity, corporate_actions=empty_actions,
        accounting_mode="ledger_v2", accounting_events=(event,),
    )
    result["equity_curve"].to_csv(run_dir / "equity_curve.csv", index=False)
    result["trades"].to_csv(run_dir / "trades.csv", index=False)
    result["accounting_journal"].to_csv(run_dir / "accounting_journal.csv", index=False)
    result["security_events"].to_csv(run_dir / "security_events.csv", index=False)
    _write_json(asdict(result["accounting_snapshot"]), run_dir / "accounting_snapshot.json")
    _write_json(result["metrics"], run_dir / "diagnostic_metrics.json")
    _write_json({
        "result_type": "synthetic_code_change_ledger_v2_research_diagnostic",
        "synthetic_only": True,
        "private_market_data_used": False,
        "broker_data_used": False,
        "broker_order_sent": False,
        "real_notice_rows_used_as_events": False,
        "fictional_initial_cash_cny": "100000",
        "fictional_code_change": {
            "old_symbol": "SZ300114", "new_symbol": "SZ302132",
            "effective_date": effective_date.isoformat(), "share_ratio": "1:1",
            "known_at_utc": "2024-12-20T00:00:00Z",
        },
        "synthetic_source_sha256": source_sha256,
        "synthetic_bars_sha256": bars_sha256,
        "artifact_files": sorted([path.name for path in run_dir.iterdir()] + ["manifest.json"]),
    }, run_dir / "manifest.json")
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic 1:1 code-change ledger diagnostic; no broker calls")
    parser.add_argument("--output", default="outputs/synthetic-code-change-diagnostic")
    args = parser.parse_args()
    output_root = Path(args.output).expanduser()
    if not output_root.is_absolute():
        output_root = Path(__file__).resolve().parents[1] / output_root
    run_dir = run(output_root)
    print(f"SYNTHETIC ONLY; no private market data, account evidence, or broker orders used.\nArtifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
