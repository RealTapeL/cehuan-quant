from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import pandas as pd

from cehuan.data.canonical import read_bars
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_audit_context() -> dict[str, Any]:
    repo = Path(__file__).resolve().parents[1]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout
    code_paths = [
        Path(__file__).resolve(),
        repo / "src/cehuan/signals/strategy.py",
        repo / "src/cehuan/data/canonical.py",
        repo / "src/cehuan/backtest/engine.py",
        repo / "src/cehuan/backtest/checkpoint.py",
        repo / "src/cehuan/backtest/input_hashes.py",
        repo / "src/cehuan/backtest/input_proof_builder.py",
    ]
    return {
        "git_head": head,
        "working_tree_dirty": bool(status.strip()),
        "implementation_files": [
            {"path": str(path), "sha256": _sha256(path)} for path in code_paths
        ],
        "source": "local git metadata and current file SHA-256; does not imply those files generated the frozen checkpoint",
    }


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return value


def _inventory(
    experiment_dir: Path,
    output_dir: Path,
    commit: str,
    external_artifacts: list[tuple[Path, str]] | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(experiment_dir.rglob("*")):
        if not path.is_file() or output_dir == path or output_dir in path.parents:
            continue
        rel = path.relative_to(experiment_dir).as_posix()
        if rel.startswith(("audit-timing-", "proof-audit-")):
            continue
        if rel.startswith("run/"):
            role = "superseded_precommit_diagnostic"
        elif rel.startswith("proof/"):
            role = "superseded_precommit_proof"
        elif rel.startswith("inputs/"):
            role = "frozen_input_or_source_metadata"
        elif rel.startswith("run-committed-"):
            role = "derived_run_output"
        elif rel.startswith("proof-committed-") or rel.startswith("proof-audit-"):
            role = "proof_or_audit_output"
        elif rel == "manifest.json":
            role = "experiment_manifest"
        elif rel == "history-manifest.json":
            role = "historical_window_chain_manifest"
        elif rel == "summary.md":
            role = "experiment_summary"
        else:
            role = "other_private_experiment_artifact"
        rows.append({
            "relative_path": rel,
            "absolute_path": str(path.resolve()),
            "role": role,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "code_commit": commit,
            "contains_private_or_restricted_data": rel.startswith(("inputs/", "run/", "run-committed-")),
            "status": "PRESENT",
        })
    for path, role in external_artifacts or []:
        path = path.expanduser().resolve()
        if not path.is_file():
            rows.append({
                "relative_path": f"external::{path.name}",
                "absolute_path": str(path),
                "role": role,
                "size_bytes": None,
                "sha256": None,
                "code_commit": commit,
                "contains_private_or_restricted_data": True,
                "status": "MISSING",
            })
            continue
        rows.append({
            "relative_path": f"external::{path.name}",
            "absolute_path": str(path),
            "role": role,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "code_commit": commit,
            "contains_private_or_restricted_data": True,
            "status": "PRESENT",
        })
    return rows


def _next_open_after(calendar: pd.DataFrame, as_of: date) -> date | None:
    """Return the first explicitly open session after as_of; fail closed on bad calendar rows."""
    required = {"trade_date", "is_open"}
    missing = required - set(calendar.columns)
    if missing:
        raise ValueError(f"交易日历缺少字段: {', '.join(sorted(missing))}")
    dates = pd.to_datetime(calendar["trade_date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("交易日历含无法解析的 trade_date")
    if dates.duplicated().any():
        raise ValueError("交易日历 trade_date 存在重复日期")
    open_values = calendar["is_open"].astype(str).str.strip().str.lower()
    if not open_values.isin({"true", "false", "1", "0"}).all():
        raise ValueError("交易日历 is_open 必须明确为 true/false/1/0")
    is_open = open_values.isin({"true", "1"})
    candidates = dates.loc[(dates.dt.date > as_of) & is_open]
    if candidates.empty:
        return None
    return candidates.min().date()


def _load_event_registry(path_value: str | None, expected_sha: str | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    if not path_value:
        return pd.DataFrame(), {"status": "UNVERIFIED", "reason": "manifest 未登记源事件表"}
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        return pd.DataFrame(), {"status": "UNVERIFIED", "reason": f"事件登记表不存在: {path}"}
    observed = _sha256(path)
    if not expected_sha:
        return pd.read_csv(path, dtype=str, keep_default_na=False), {
            "status": "UNVERIFIED", "reason": "manifest 未登记事件表 SHA-256",
            "path": str(path), "sha256": observed,
        }
    if expected_sha and observed != expected_sha:
        return pd.read_csv(path, dtype=str, keep_default_na=False), {
            "status": "FAILED", "reason": "事件登记表 SHA-256 与实验 manifest 不一致",
            "path": str(path), "sha256": observed,
        }
    return pd.read_csv(path, dtype=str, keep_default_na=False), {
        "status": "VERIFIED", "reason": None, "path": str(path), "sha256": observed,
    }


def _phase_dates(row: dict[str, Any]) -> list[date]:
    output = []
    for key in (
        "effective_date", "record_date", "old_last_trade_date", "old_delisting_date",
        "new_share_registration_date", "new_shares_tradable_date", "allocation_result_announcement_date",
    ):
        value = row.get(key)
        if value:
            parsed = pd.to_datetime(value, errors="coerce")
            if not pd.isna(parsed):
                output.append(parsed.date())
    return output


def _event_hits(
    symbol: str, start_eod: date, end_eod: date, identity: pd.DataFrame, actions: pd.DataFrame,
) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for family, frame in (("security_identity", identity), ("corporate_action", actions)):
        for row in frame.to_dict(orient="records"):
            symbols = {str(row.get(key, "")).strip() for key in ("symbol", "old_symbol", "new_symbol")}
            if symbol not in symbols:
                continue
            dates = _phase_dates(row)
            if any(start_eod <= day <= end_eod for day in dates):
                hits.append({
                    "family": family,
                    "event_id": str(row.get("event_id", "")),
                    "event_type": str(row.get("event_type", "")),
                    "phase_dates": ",".join(sorted(day.isoformat() for day in dates)),
                })
    return hits


def _event_symbols(row: dict[str, Any]) -> set[str]:
    return {
        str(row.get(key, "")).strip().upper()
        for key in ("symbol", "old_symbol", "new_symbol")
        if str(row.get(key, "")).strip()
    }


def _position_intervals(trades: pd.DataFrame, sessions: list[date], window_end: date) -> list[dict[str, Any]]:
    session_before = {day: sessions[index - 1] for index, day in enumerate(sessions) if index > 0}
    quantities: dict[str, int] = {}
    starts: dict[str, date] = {}
    intervals: list[dict[str, Any]] = []
    ordered = trades.sort_values("_row_order", kind="stable")
    for row in ordered.itertuples(index=False):
        symbol = str(row.symbol)
        day = pd.Timestamp(row.trade_date).date()
        quantity = int(row.quantity)
        current = quantities.get(symbol, 0)
        if row.side == "BUY":
            if current == 0:
                starts[symbol] = day
            quantities[symbol] = current + quantity
        elif row.side == "SELL":
            if quantity > current:
                raise ValueError(f"成交流水卖出超过累计买入: {symbol} {day}")
            quantities[symbol] = current - quantity
            if quantities[symbol] == 0:
                start = starts.pop(symbol)
                prior = session_before.get(day)
                intervals.append({
                    "symbol": symbol,
                    "holding_start_trade_date_at_open": start.isoformat(),
                    "holding_end_trade_date_before_open": day.isoformat(),
                    "held_at_end_of_day_through": prior.isoformat() if prior else None,
                    "closure": "SOLD_AT_OPEN",
                    "remaining_shares": 0,
                })
        else:
            raise ValueError(f"未知成交方向: {row.side}")
    for symbol, quantity in quantities.items():
        if quantity:
            intervals.append({
                "symbol": symbol,
                "holding_start_trade_date_at_open": starts[symbol].isoformat(),
                "holding_end_trade_date_before_open": None,
                "held_at_end_of_day_through": window_end.isoformat(),
                "closure": "OPEN_AT_WINDOW_END",
                "remaining_shares": quantity,
            })
    return sorted(intervals, key=lambda item: (item["holding_start_trade_date_at_open"], item["symbol"]))


def build_audit(experiment_dir: Path, output_dir: Path, proof_report_path: Path, preflight_path: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"拒绝覆盖已有审计目录: {output_dir}")
    output_dir.mkdir(parents=True)
    manifest_path = experiment_dir / "manifest.json"
    manifest = _json(manifest_path)
    commit = str(manifest.get("environment", {}).get("git_head", "unknown"))
    scope = manifest["window"]
    start, end = date.fromisoformat(scope["start"]), date.fromisoformat(scope["end"])
    run_dir = Path(manifest["run"]["experiment_output_path"]).resolve()
    inputs_dir = experiment_dir / "inputs"
    bars_path = inputs_dir / "bars.csv"
    bars = read_bars(bars_path)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.strftime("%Y-%m-%d")
    bars["available_at_utc"] = pd.to_datetime(bars["available_at"], utc=True, errors="coerce")
    bars["retrieved_at_utc_parsed"] = pd.to_datetime(bars["retrieved_at_utc"], utc=True, errors="coerce")
    sessions = [pd.Timestamp(value).date() for value in sorted(bars["trade_date"].unique())]
    calendar = pd.read_csv(inputs_dir / "trading_calendar.csv", dtype=str, keep_default_na=False)
    checkpoint_as_of = date.fromisoformat(manifest["run"]["checkpoint_as_of_date"])
    checkpoint_next = date.fromisoformat(manifest["run"]["checkpoint_next_expected_session"])
    calendar_next_open = _next_open_after(calendar, checkpoint_as_of)
    calendar_dates = pd.to_datetime(calendar["trade_date"], errors="coerce")
    calendar_window = calendar.loc[calendar_dates.between(pd.Timestamp(start), pd.Timestamp(end))]
    run_config = _json(run_dir / "config.json")
    strategy, market, costs = run_config["strategy"], run_config["market"], run_config["costs"]

    # Re-run the project's signal adapter only to reconstruct the candidate set
    # for each frozen rebalance date. No backtest state or checkpoint is changed.
    features = add_momentum(bars.drop(columns=["available_at_utc", "retrieved_at_utc_parsed"]), int(strategy["lookback_sessions"]), normalized=True)
    signal_days: list[dict[str, Any]] = []
    candidates: list[pd.DataFrame] = []
    lookback, cadence = int(strategy["lookback_sessions"]), int(strategy["rebalance_sessions"])
    max_positions = int(strategy["max_positions"])
    for index, signal_day in enumerate(sessions):
        if index < lookback or (index - lookback) % cadence != 0 or index + 1 >= len(sessions):
            continue
        day = features.loc[pd.to_datetime(features["trade_date"]).dt.date.eq(signal_day)]
        ranked = rank_precomputed_day(day, strategy, market, require_order_rules=False).copy()
        execution_day = sessions[index + 1]
        ranked["signal_date"] = signal_day.isoformat()
        ranked["signal_session_index"] = index
        ranked["execution_date"] = execution_day.isoformat()
        ranked["selected_target"] = ranked["rank"].le(max_positions)
        candidates.append(ranked)
        signal_rows = day.loc[day["available_at"].notna(), "available_at"]
        signal_days.append({
            "signal_date": signal_day.isoformat(),
            "decision_timing_rule": "signal from close on t; earliest simulated execution is next session open",
            "decision_time_assumption": "15:00 Asia/Shanghai (generated available_at assumption; vendor publication time not evidenced)",
            "decision_cutoff_status": "UNVERIFIED_EXTERNAL_AVAILABILITY",
            "max_signal_bar_available_at": str(signal_rows.max()) if not signal_rows.empty else None,
            "execution_date": execution_day.isoformat(),
            "eligible_candidate_count": int(len(ranked)),
            "target_count": int(ranked["selected_target"].sum()),
            "selected_targets": ranked.loc[ranked["selected_target"], ["symbol", "momentum", "rank"]].to_dict(orient="records"),
        })

    candidate_frame = pd.concat(candidates, ignore_index=True) if candidates else pd.DataFrame()
    trades = pd.read_csv(run_dir / "trades.csv", dtype={"symbol": str})
    trades["_row_order"] = range(len(trades))
    intervals = _position_intervals(trades, sessions, end)
    intervals_frame = pd.DataFrame(intervals)

    master_identity_path = manifest["input_sources"]["identity-events-window.csv"]["source_registry_path"]
    identity_expected = manifest["input_sources"]["identity-events-window.csv"]["source_registry_sha256"]
    identity, identity_file_audit = _load_event_registry(master_identity_path, identity_expected)
    master_actions_path = manifest["input_sources"]["corporate-actions-window.csv"]["source_registry_path"]
    actions_expected = manifest["input_sources"]["corporate-actions-window.csv"]["source_registry_sha256"]
    actions, actions_file_audit = _load_event_registry(master_actions_path, actions_expected)
    provenance_path = inputs_dir / "provenance-registry.csv"
    provenance = pd.read_csv(provenance_path, dtype=str, keep_default_na=False)
    if provenance.empty:
        provenance_status = "UNVERIFIED"
    else:
        review_columns = {"review_status", "reviewed_by", "reviewed_at"}
        if review_columns.issubset(provenance.columns):
            reviewed = (
                provenance["review_status"].astype(str).str.upper().eq("REVIEWED")
                & provenance["reviewed_by"].astype(str).str.strip().ne("")
                & provenance["reviewed_at"].astype(str).str.strip().ne("")
            )
            provenance_status = "VERIFIED" if bool(reviewed.all()) else "UNVERIFIED"
        else:
            provenance_status = "UNVERIFIED"
    for interval in intervals:
        held_start = date.fromisoformat(interval["holding_start_trade_date_at_open"])
        held_end = date.fromisoformat(interval["held_at_end_of_day_through"])
        hits = _event_hits(interval["symbol"], held_start, held_end, identity, actions)
        interval["current_registry_event_hits"] = hits
        interval["company_action_coverage_status"] = "UNVERIFIED" if not hits else "REGISTERED_EVENT_REVIEW_REQUIRED"
    pd.DataFrame(intervals).to_csv(output_dir / "actual-holding-intervals.csv", index=False)
    if not candidate_frame.empty:
        candidate_frame.to_csv(
            output_dir / "candidate-pool-by-signal.csv",
            columns=["signal_date", "execution_date", "symbol", "momentum", "rank", "selected_target", "is_listed", "is_suspended", "is_st"],
            index=False,
        )

    input_symbols = set(bars["symbol"].astype(str).unique())
    candidate_symbols = set(candidate_frame["symbol"].astype(str)) if not candidate_frame.empty else set()
    traded_symbols = set(trades["symbol"].astype(str))
    symbols_rows = []
    interval_by_symbol = {item["symbol"]: item for item in intervals}
    for symbol in sorted(input_symbols):
        interval = interval_by_symbol.get(symbol, {})
        symbols_rows.append({
            "symbol": symbol,
            "layer_A_input_universe": True,
            "layer_B_eligible_candidate_any_signal_day": symbol in candidate_symbols,
            "layer_C_actual_trade_or_holding": symbol in traded_symbols,
            "holding_start": interval.get("holding_start_trade_date_at_open"),
            "held_at_end_of_day_through": interval.get("held_at_end_of_day_through"),
            "project_registry_company_action_hits": len(interval.get("current_registry_event_hits", [])),
            "company_action_coverage_status": (
                interval.get("company_action_coverage_status", "UNVERIFIED_NOT_AUDITED_IN_NONEXHAUSTIVE_REGISTRY")
            ),
        })
    pd.DataFrame(symbols_rows).to_csv(output_dir / "universe-layers.csv", index=False)

    trade_audit = trades.copy()
    trade_audit["market_rule_application"] = "CONFIG_FALLBACK"
    trade_audit["market_rule_source_status"] = "UNVERIFIED"
    trade_audit["fallback_price_limit_pct"] = market["default_price_limit_pct"]
    trade_audit["fallback_assume_limit_applies_when_unknown"] = market["assume_price_limit_applies_when_unknown"]
    trade_audit["fallback_buy_lot_shares"] = market["buy_lot_shares"]
    trade_audit["fallback_sell_lot_shares"] = market["sell_lot_shares"]
    trade_audit["fee_application"] = "CONFIG_FLAT_ASSUMPTION"
    trade_audit["fee_source_status"] = "UNVERIFIED_HISTORICAL_AND_PERSONAL_RATE"
    trade_audit["fallback_commission_rate"] = costs["commission_rate"]
    trade_audit["fallback_minimum_commission_cny"] = costs["minimum_commission_cny"]
    trade_audit["fallback_stamp_duty_sell_rate"] = costs["stamp_duty_sell_rate"]
    trade_audit["fallback_transfer_fee_rate"] = costs["transfer_fee_rate"]
    trade_audit["fallback_slippage_bps"] = costs.get("slippage_bps", 0)
    trade_audit.drop(columns=["_row_order"]).to_csv(output_dir / "trade-fallback-detail.csv", index=False)

    def missing_count(column: str) -> int:
        return int(bars[column].isna().sum()) if column in bars else len(bars)

    source_manifest_path = Path(manifest["source"]["raw_snapshot_path"]).resolve() / "manifest.json"
    source_manifest = _json(source_manifest_path)
    universe_manifest_path = Path(manifest["source"]["universe_snapshot_path"]).resolve() / "manifest.json"
    universe_manifest = _json(universe_manifest_path)
    source_requests = source_manifest.get("requests", [])
    security_requests = [
        item for item in universe_manifest.get("requests", [])
        if str(item.get("endpoint", "")).startswith("query_all_stock(")
    ]
    run_time = manifest["run"].get("generated_at_utc")
    run_time_shanghai = None
    if run_time:
        run_time_shanghai = datetime.fromisoformat(run_time.replace("Z", "+00:00")).astimezone().isoformat()

    proof_report = _json(proof_report_path)
    preflight = _json(preflight_path)
    audit_implementation = _git_audit_context()
    current_identity_rows = identity.to_dict(orient="records")
    current_action_rows = actions.to_dict(orient="records")
    registered_events_in_window = 0
    event_layer_rows: list[dict[str, Any]] = []
    layer_symbols = {
        "A_INPUT_UNIVERSE": input_symbols,
        "B_SIGNAL_CANDIDATES": candidate_symbols,
        "C_ACTUAL_TRADES": traded_symbols,
    }
    for family, event_rows in (("security_identity", current_identity_rows), ("corporate_action", current_action_rows)):
        for row in event_rows:
            phases = _phase_dates(row)
            in_window = [phase for phase in phases if start <= phase <= end]
            if not in_window:
                continue
            registered_events_in_window += 1
            event_codes = _event_symbols(row)
            for layer, symbols in layer_symbols.items():
                matches = sorted(event_codes & symbols)
                if matches:
                    event_layer_rows.append({
                        "layer": layer,
                        "family": family,
                        "event_id": str(row.get("event_id", "")),
                        "event_type": str(row.get("event_type", "")),
                        "matched_symbols": ",".join(matches),
                        "phase_dates_in_window": ",".join(sorted(day.isoformat() for day in in_window)),
                        "coverage_status": "REGISTERED_MATCH_ONLY_NONEXHAUSTIVE_SOURCE",
                    })
    event_layer_path = output_dir / "registered-event-matches-by-layer.csv"
    pd.DataFrame(event_layer_rows, columns=[
        "layer", "family", "event_id", "event_type", "matched_symbols", "phase_dates_in_window", "coverage_status",
    ]).to_csv(event_layer_path, index=False)

    external_artifacts = [
        (source_manifest_path, "source_bars_snapshot_manifest"),
        (universe_manifest_path, "date_specific_universe_snapshot_manifest"),
        (Path(master_identity_path), "identity_event_source_registry"),
        (Path(master_actions_path), "corporate_action_source_registry"),
        (Path(__file__).resolve(), "audit_implementation_script"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/signals/strategy.py", "audit_signal_reconstruction_source"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/data/canonical.py", "audit_bar_normalization_source"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/backtest/engine.py", "checkpoint_and_execution_source"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/backtest/checkpoint.py", "checkpoint_validation_source"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/backtest/input_hashes.py", "input_hash_algorithm_source"),
        (Path(__file__).resolve().parents[1] / "src/cehuan/backtest/input_proof_builder.py", "proof_reconstruction_source"),
        (proof_report_path.parent / "input-proof-baseline.json", "rebuilt_input_proof_baseline"),
        (proof_report_path, "input_proof_rebuild_report"),
        (preflight_path, "incremental_preflight_report"),
    ]
    inventory = _inventory(experiment_dir, output_dir, commit, external_artifacts)
    with (output_dir / "artifact-inventory.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = sorted({key for row in inventory for key in row}) or ["relative_path"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(inventory)

    report = {
        "schema_version": 1,
        "report_type": "real_checkpoint_decision_time_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment_id": manifest["run"]["experiment_id"],
        "run_code_commit": commit,
        "audit_implementation": audit_implementation,
        "supersedes_intermediate_audit_note": "Earlier audit-timing drafts generated during this task contained an incorrect adjustflag interpretation; use this corrected report. Frozen experiment files were not changed.",
        "window": {"start": start.isoformat(), "end": end.isoformat(), "sessions": len(sessions)},
        "scope": "read-only audit; does not modify historical checkpoint, inputs, trades, or proof artifacts",
        "actual_experiment_run": {
            "recorded_generated_at_utc": run_time,
            "recorded_generated_at_local": run_time_shanghai,
            "data_snapshot_created_at_utc": source_manifest.get("created_at_utc"),
            "data_snapshot_manifest_generated_at_utc": source_manifest.get("manifest_generated_at_utc"),
            "bars_retrieved_at_min_utc": bars["retrieved_at_utc_parsed"].min().isoformat(),
            "bars_retrieved_at_max_utc": bars["retrieved_at_utc_parsed"].max().isoformat(),
            "bars_available_at_unique_count": int(bars["available_at"].nunique()),
            "bars_available_at_formula": "adapter-generated trade_date 15:00 Asia/Shanghai assumption; not provider publication evidence",
        },
        "simulation_timing": {
            "strategy_rule_source": "src/cehuan/signals/strategy.py add_momentum docstring and src/cehuan/backtest/engine.py next-session open execution",
            "strategy_rule_status": "VERIFIED_FROM_CODE_AND_RUN_REASON_FIELDS",
            "historical_decision_cutoff_status": "UNVERIFIED_EXTERNAL_AVAILABILITY",
            "decision_points": signal_days,
            "checkpoint_as_of_date": manifest["run"].get("checkpoint_as_of_date"),
            "checkpoint_next_expected_session": manifest["run"].get("checkpoint_next_expected_session"),
            "calendar_next_open_after_checkpoint_as_of": calendar_next_open.isoformat() if calendar_next_open else None,
            "next_session_matches_checkpoint": calendar_next_open == checkpoint_next,
            "actual_2026_run_time_is_not_used_as_historical_known_at": True,
        },
        "input_timing_and_provenance": [
            {
                "family": "daily_bars_and_adjustment",
                "provider": "BaoStock",
                "provider_snapshot_created_at_utc": source_manifest.get("created_at_utc"),
                "historical_effective_range": source_manifest.get("date_range"),
                "current_snapshot_retrieved_range_utc": [bars["retrieved_at_utc_parsed"].min().isoformat(), bars["retrieved_at_utc_parsed"].max().isoformat()],
                "available_at_status": "ASSUMPTION_ONLY",
                "historical_known_at_status": "UNVERIFIED",
                "adjustment_request": source_manifest.get("field_mapping", {}).get("signal_price"),
                "current_semantics": "Early snapshot requested adjustflag=1 (BaoStock backward-adjusted / 后复权), matching the configured hfq signal mode; the historical vendor vintage and revision history remain unverified.",
                "possible_vendor_revision_or_vintage_status": "UNVERIFIED",
            },
            {
                "family": "date_specific_universe_and_status",
                "provider": "BaoStock query_all_stock(day) plus query_stock_basic captured at snapshot time",
                "retrieved_range_utc": [
                    min((item.get("started_at_utc") for item in security_requests if item.get("started_at_utc")), default=None),
                    max((item.get("completed_at_utc") for item in security_requests if item.get("completed_at_utc")), default=None),
                ],
                "historical_effective_range": source_manifest.get("date_range"),
                "historical_known_at_status": "UNVERIFIED",
                "notes": "membership responses are date-specific but were collected later; ST/停牌来自历史日线字段，挂牌基础资料在采集时取得；not proof of the vendor's then-visible version",
            },
            {
                "family": "trading_calendar",
                "provider": "BaoStock query_trade_dates",
                "retrieved_at_values_utc": sorted(set(calendar_window.get("retrieved_at_utc", pd.Series(dtype=str)).astype(str)))[:5],
                "range_rows": int(len(calendar_window)),
                "historical_known_at_status": "UNVERIFIED",
                "next_open_after_checkpoint_as_of": calendar_next_open.isoformat() if calendar_next_open else None,
                "checkpoint_next_expected_session": checkpoint_next.isoformat(),
                "next_session_matches_checkpoint": calendar_next_open == checkpoint_next,
            },
            {
                "family": "historical_market_rules",
                "source": "no effective-date rule schedule was bound to this run",
                "historical_known_at_status": "NOT_PROVIDED",
            },
            {
                "family": "historical_fees",
                "source": "no effective-date fee schedule was bound to this run",
                "historical_known_at_status": "NOT_PROVIDED",
                "account_specific_huatai_rate_status": "NOT_PROVIDED",
            },
            {
                "family": "corporate_actions_and_security_identity",
                "source_registry_sha256_status": {
                    "identity": identity_file_audit["status"], "corporate_actions": actions_file_audit["status"],
                },
                "registered_identity_rows": int(len(identity)),
                "registered_corporate_action_rows": int(len(actions)),
                "provenance_registry_path": str(provenance_path.resolve()),
                "provenance_registry_file_sha256": _sha256(provenance_path),
                "provenance_registry_data_rows": int(len(provenance)),
                "source_content_review_status": provenance_status,
                "registered_rows_with_any_phase_in_window": registered_events_in_window,
                "event_coverage_status": "UNVERIFIED_NONEXHAUSTIVE_REGISTRIES",
                "interpretation": "0 rows in the filtered event files/master registers do not establish that the market had no other events.",
            },
        ],
        "universe_layers": {
            "A_input_symbols": int(len(input_symbols)),
            "A_input_rows": int(len(bars)),
            "B_candidate_rows_across_signal_dates": int(len(candidate_frame)),
            "B_unique_candidate_symbols": int(len(candidate_symbols)),
            "B_signal_days": [row["signal_date"] for row in signal_days],
            "C_unique_traded_symbols": int(len(traded_symbols)),
            "C_trade_fill_rows": int(len(trades)),
            "C_position_intervals": int(len(intervals)),
            "current_registry_event_hits_in_actual_position_intervals": sum(len(x.get("current_registry_event_hits", [])) for x in intervals),
            "registered_event_match_counts_by_layer": {
                layer: sum(row["layer"] == layer for row in event_layer_rows)
                for layer in layer_symbols
            },
            "registered_event_match_detail_path": "registered-event-matches-by-layer.csv",
            "candidate_and_universe_event_search_coverage": "UNVERIFIED; configured registries are not an exhaustive issuer-action source",
            "details": "universe-layers.csv, candidate-pool-by-signal.csv, actual-holding-intervals.csv",
        },
        "rules_and_fees": {
            "market_rule_mode": manifest["run"].get("market_rule_mode"),
            "input_bar_rows": int(len(bars)),
            "rows_missing_board": missing_count("board"),
            "rows_missing_rule_source": missing_count("market_rule_source"),
            "rows_missing_rule_valid_from": missing_count("market_rule_valid_from"),
            "rows_missing_price_limit_pct": missing_count("price_limit_pct"),
            "rows_missing_buy_min": missing_count("buy_min_shares"),
            "rows_missing_buy_step": missing_count("buy_step_shares"),
            "rows_missing_sell_min": missing_count("sell_min_shares"),
            "rows_missing_sell_step": missing_count("sell_step_shares"),
            "market_rule_fallback_row_count": manifest["run"].get("market_rule_fallback_row_count"),
            "market_rule_fallback_assumptions": manifest["run"].get("market_rule_fallback_assumptions"),
            "fee_mode": manifest["run"].get("fee_mode"),
            "fee_schedule_rows": manifest["run"].get("fee_schedule_rows"),
            "fee_parameters": costs,
            "trade_fill_rows_using_market_rule_fallback": int(len(trade_audit)),
            "trade_fill_rows_using_fee_fallback": int(len(trade_audit)),
            "trade_dates": sorted(trades["trade_date"].astype(str).unique()),
            "trade_fallback_detail_path": "trade-fallback-detail.csv",
        },
        "proof_status": {
            "input_file_hashes_valid": proof_report.get("input_file_hashes_valid"),
            "provided_input_artifact_hashes_valid": proof_report.get("provided_input_artifact_hashes_valid"),
            "source_provenance_evidence_file_hash_valid": proof_report.get("source_provenance_evidence_file_hash_valid"),
            "decision_cutoff_evidence_file_hash_valid": proof_report.get("decision_cutoff_evidence_file_hash_valid"),
            "canonical_payload_digest_valid": proof_report.get("canonical_payload_digest_valid"),
            "normalized_inputs_binding_valid": proof_report.get("normalized_inputs_binding_valid"),
            "checkpoint_binding_valid": proof_report.get("checkpoint_binding_valid"),
            "historical_data_chain": proof_report.get("historical_data_chain"),
            "source_content_review_status": proof_report.get("source_content_review_status"),
            "known_at_evidence_status": proof_report.get("known_at_evidence_status"),
            "can_continue": preflight.get("can_continue"),
            "engine_resume_supported": preflight.get("engine_resume_supported"),
            "preflight_blockers": preflight.get("blockers", []),
        },
        "limitations": [
            "historical signal cutoffs have no independent time-of-day evidence",
            "the vendor's point-in-time data vintage/revision history is not present",
            "date-level available_at is generated by the adapter as a 15:00 assumption, not a provider publication timestamp",
            "market rules and fees are fallback assumptions for every fill",
            "registered event tables are not comprehensive; empty event inputs do not prove no events",
            "universe and candidate layers are not fully independently audited for corporate actions",
            "the historical snapshot is a later-retrieved BaoStock adjustflag=1 back-adjusted series; vendor vintage and revision history remain unverified",
        ],
    }
    with (output_dir / "decision-time-audit.json").open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")

    summary = [
        "# 真实 checkpoint 的历史决策时点与覆盖审计",
        "",
        f"实验编号：`{report['experiment_id']}`；代码提交：`{commit}`。本报告为只读重建，不改写原 checkpoint、输入或旧报告。",
        f"本审计工具 HEAD：`{audit_implementation['git_head']}`；运行时工作区有未提交改动：`{audit_implementation['working_tree_dirty']}`。实现文件 SHA-256 见 JSON 与清单。此报告取代本轮早先的临时审计草稿；冻结实验产物未改写。",
        "",
        "## 两个时间",
        "",
        f"- 实际实验输出记录时间：`{run_time}`（程序运行记录，2026 年）。",
        f"- 历史模拟窗口：`{start}` 至 `{end}`；策略在信号日收盘形成信号，次交易日开盘模拟执行。",
        "- 行情 `available_at` 是适配器按交易日 15:00 Asia/Shanghai 写入的可用时间假设，不是供应商当年发布时间证据；历史决策时点仍 `UNVERIFIED`。",
        "- 实际实验运行时间不用于填充 2024/2025 年的 `known_at`。",
        "",
        "| 信号日 | 假定决策时间 | 次日开盘执行 | 合格候选数 | 目标代码 |",
        "|---|---|---|---:|---|",
    ]
    for row in signal_days:
        names = ", ".join(item["symbol"] for item in row["selected_targets"])
        summary.append(f"| {row['signal_date']} | 15:00 亚洲/上海（假设） | {row['execution_date']} | {row['eligible_candidate_count']} | {names} |")
    summary.extend([
        "",
        "## 数据来源和时点状态",
        "",
        f"BaoStock 行情快照在 `{source_manifest.get('created_at_utc')}` 采集，行级 `retrieved_at_utc` 范围为 `{bars['retrieved_at_utc_parsed'].min().isoformat()}` 至 `{bars['retrieved_at_utc_parsed'].max().isoformat()}`。它是晚于历史窗口取得的当前历史快照；供应商当年版本和后续修订状态未知。",
        "",
        f"股票池按日期调用 `query_all_stock(day)`，相关快照请求发生于 `{min((x.get('started_at_utc') for x in security_requests if x.get('started_at_utc')), default='未知')}` 至 `{max((x.get('completed_at_utc') for x in security_requests if x.get('completed_at_utc')), default='未知')}`；日期专属返回不等于当年即时可得证据。交易日历也在后来采集。checkpoint 声明下一开市日 `{checkpoint_next}`；按输入日历精确计算为 `{calendar_next_open}`，匹配状态：`{calendar_next_open == checkpoint_next}`。",
        "",
        "行情价格口径：旧快照实际请求 BaoStock `adjustflag=1` 后复权，与配置的 `hfq` 信号口径一致。该数据在历史窗口后取得，供应商 vintage 和修订状态未知。",
        "",
        "## 股票池、候选和实际成交分层",
        "",
        f"- A 输入股票池：{len(input_symbols)} 个代码、{len(bars)} 行。",
        f"- B 三个调仓信号日共 {len(candidate_frame)} 条候选行、{len(candidate_symbols)} 个不同代码。",
        f"- C 实际成交涉及 {len(traded_symbols)} 个代码，{len(trades)} 条模拟成交、{len(intervals)} 段持仓区间。",
        f"- 项目登记表中该窗口有阶段日期的身份/公司行动记录：{registered_events_in_window} 条；持仓范围匹配到登记事件：{sum(len(x.get('current_registry_event_hits', [])) for x in intervals)} 条。登记表不完整，因此所有“未匹配”仍标为未验证，不能解释为无事件。",
        "",
        "私有明细：`universe-layers.csv`、`candidate-pool-by-signal.csv`、`actual-holding-intervals.csv`。",
        "",
        "## 规则和费用回退",
        "",
        f"- {len(bars)} 行行情缺少有效日期规则字段；回测 {manifest['run'].get('market_rule_fallback_row_count')} 行使用配置回退：涨跌幅阈值 {market['default_price_limit_pct']:.0%}、未知时假定适用、买卖单位 {market['buy_lot_shares']} 股。",
        f"- 历史费率表为 0 行；{len(trades)} 条模拟成交都使用配置费率假设：佣金 {costs['commission_rate']:.4%}、最低佣金 ¥{costs['minimum_commission_cny']:.2f}、卖出印花税 {costs['stamp_duty_sell_rate']:.4%}、过户费 {costs['transfer_fee_rate']:.5%}、滑点 {costs.get('slippage_bps', 0)} bps。它不是历史费率，也不是华泰个人佣金。",
        "- `trade-fallback-detail.csv` 列出逐笔交易日期、代码、方向、数量和实际采用的回退参数。",
        "",
        "## 证明与门控",
        "",
        f"- 已提供的计算输入文件 SHA-256：`{proof_report.get('provided_input_artifact_hashes_valid')}`。来源登记 CSV 的文件 SHA-256：`{proof_report.get('source_provenance_evidence_file_hash_valid')}`，但其数据行数为 `{len(provenance)}`，因此来源内容复核仍为 `{provenance_status}`；文件字节匹配不等于来源已核验。截止时点证据文件散列：`{proof_report.get('decision_cutoff_evidence_file_hash_valid')}`。",
        f"- 规范化绑定：`{proof_report.get('normalized_inputs_binding_valid')}`；checkpoint 绑定：`{proof_report.get('checkpoint_binding_valid')}`；历史链：`{proof_report.get('historical_data_chain', {}).get('status')}`。",
        f"- 整体输入文件哈希与证据完整性门控：`{proof_report.get('input_file_hashes_valid')}`；来源内容复核：`{proof_report.get('source_content_review_status')}`；`known_at`：`{proof_report.get('known_at_evidence_status')}`。",
        f"- 增量续跑：`can_continue={preflight.get('can_continue')}`，`engine_resume_supported={preflight.get('engine_resume_supported')}`。",
        "- 股票池/候选/实际交易代码与当前登记事件的匹配明细见 `registered-event-matches-by-layer.csv`；它只检查非穷尽登记表，未匹配不代表无事件。",
        "- 详细文件清单（含外部来源 manifest 与登记表）见 `artifact-inventory.csv`；机器可读报告见 `decision-time-audit.json`。",
        "",
        "正式基线仍为 `NOT_READY`。本审计不说明策略有效，不生成个人交易计划，也不改变手动交易边界。",
        "",
    ])
    (output_dir / "summary.md").write_text("\n".join(summary), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="审计真实 checkpoint 的历史决策时点、输入层次和规则/费用回退；只读生成私有产物")
    parser.add_argument("--experiment-dir", required=True, help="已冻结真实实验目录")
    parser.add_argument("--proof-report", required=True, help="本轮重新生成的 input-proof-rebuild-report.json")
    parser.add_argument("--preflight-report", required=True, help="只读 incremental-preflight.json")
    parser.add_argument("--output-dir", required=True, help="新的、不得已存在的私有审计目录")
    args = parser.parse_args()
    experiment_dir = Path(args.experiment_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    report = build_audit(
        experiment_dir, output_dir,
        Path(args.proof_report).expanduser().resolve(),
        Path(args.preflight_report).expanduser().resolve(),
    )
    with (output_dir / "artifact-inventory.csv").open(encoding="utf-8", newline="") as handle:
        artifact_count = sum(1 for _ in csv.DictReader(handle))
    print(json.dumps({
        "report": str(output_dir / "decision-time-audit.json"),
        "summary": str(output_dir / "summary.md"),
        "artifact_count": artifact_count,
        "decision_points": len(report["simulation_timing"]["decision_points"]),
        "can_continue": report["proof_status"]["can_continue"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
