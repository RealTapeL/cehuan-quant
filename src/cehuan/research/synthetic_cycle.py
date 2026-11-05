"""A fully synthetic plan-to-review cycle; never connects to a broker."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.review.reconcile import (
    compare_plan_revisions,
    compare_plan_and_fills,
    compare_positions_to_plan,
    read_fills,
    read_order_statuses,
    read_positions,
    review_markdown,
)
from cehuan.review.import_registry import DuplicateFillImportError, register_fill_import
from cehuan.signals.plan import generate_plan, plan_markdown


def synthetic_cycle_bars(now: datetime | None = None) -> pd.DataFrame:
    local_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    end_day = (local_now - timedelta(days=1)).date()
    dates = pd.bdate_range(end=end_day, periods=35)
    rows: list[dict[str, object]] = []
    for symbol, name, base, drift in (
        ("SZ000001", "合成趋势甲", 10.0, 0.15),
        ("SZ000002", "合成趋势乙", 30.0, -0.04),
    ):
        previous = base
        for index, stamp in enumerate(dates):
            close = base + drift * index
            opened = previous
            available = pd.Timestamp(stamp.date()).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
            retrieved = available.tz_convert("UTC") + pd.Timedelta(minutes=1)
            rows.append({
                "trade_date": stamp.date().isoformat(),
                "symbol": symbol,
                "name": name,
                "open": round(opened, 2),
                "high": round(max(opened, close) + 0.03, 2),
                "low": round(min(opened, close) - 0.03, 2),
                "close": round(close, 2),
                "adjusted_close": round(close, 4),
                "volume_shares": 1_000_000,
                "amount_cny": round(close * 1_000_000, 2),
                "is_listed": True,
                "is_suspended": False,
                "is_st": False,
                "has_price_limit": True,
                "price_limit_pct": 0.10,
                "buy_min_shares": 100,
                "buy_step_shares": 100,
                "sell_min_shares": 100,
                "sell_step_shares": 100,
                "board": "SYNTHETIC",
                "market_rule_source": "synthetic_fixture_not_market_evidence",
                "market_rule_valid_from": dates[0].date().isoformat(),
                "data_source": "synthetic_cycle_generator",
                "retrieved_at_utc": retrieved.isoformat(),
                "available_at": available.isoformat(),
            })
            previous = close
    return pd.DataFrame(rows).sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def run_synthetic_cycle(
    output_root: str | Path,
    now: datetime | None = None,
    *,
    scenario: str = "partial_fill",
) -> Path:
    """Write one isolated synthetic plan/review scenario; never calls a broker."""
    allowed_scenarios = {
        "partial_fill", "no_fill", "duplicate_import", "partial_fill_cancelled",
        "modified_plan", "missing_positions",
    }
    if scenario not in allowed_scenarios:
        raise ValueError(f"scenario 必须为以下之一：{', '.join(sorted(allowed_scenarios))}")
    local_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    run_id = f"synthetic-{local_now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    run_dir = Path(output_root).expanduser().resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    config = load_config()
    config["strategy"].update({
        "lookback_sessions": 5,
        "rebalance_sessions": 5,
        "max_positions": 1,
        "max_position_weight": 0.20,
        "max_total_exposure": 0.20,
    })
    bars = synthetic_cycle_bars(local_now)
    bars_path = run_dir / "synthetic_bars.csv"
    bars.to_csv(bars_path, index=False)
    backtest = run_backtest(
        bars,
        100_000.0,
        config,
        security_identity_events=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        accounting_mode="ledger_v2",
    )
    baseline_metrics = dict(backtest["metrics"])
    baseline_metrics.update({
        "cycle_run_id": run_id,
        "result_type": "synthetic_workflow_demo_not_market_research",
        "underlying_backtest_result_type": backtest["metrics"]["result_type"],
    })
    (run_dir / "baseline_metrics.json").write_text(
        json.dumps(baseline_metrics, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    backtest["equity_curve"].to_csv(run_dir / "baseline_equity_curve.csv", index=False)
    backtest["trades"].to_csv(run_dir / "baseline_trades.csv", index=False)

    plan = generate_plan(bars, 100_000.0, config, now=local_now, purpose="SYNTHETIC_DIAGNOSTIC")
    if plan.empty:
        raise RuntimeError("合成示例未生成计划行")
    plan["cycle_run_id"] = run_id
    original_plan_path = None
    plan_change_path = None
    if scenario == "modified_plan":
        original_plan_path = run_dir / "plan_original.csv"
        plan.to_csv(original_plan_path, index=False)
        actionable_before = plan.loc[plan["action"].isin(["BUY", "SELL"]) & plan["planned_shares"].gt(0)]
        if actionable_before.empty:
            raise RuntimeError("合成示例计划没有可编辑的买卖行")
        edit_index = actionable_before.index[0]
        old_quantity = int(plan.at[edit_index, "planned_shares"])
        new_quantity = old_quantity - 100 if old_quantity >= 200 else old_quantity + 100
        current_quantity = int(plan.at[edit_index, "current_shares"])
        old_target = int(plan.at[edit_index, "target_shares"])
        new_target = current_quantity + new_quantity if plan.at[edit_index, "action"] == "BUY" else max(0, current_quantity - new_quantity)
        plan.at[edit_index, "planned_shares"] = new_quantity
        plan.at[edit_index, "target_shares"] = new_target
        plan["plan_revision"] = 2
        plan_change_path = run_dir / "plan_changes.csv"
        revision_diff = compare_plan_revisions(pd.read_csv(original_plan_path), plan)
        revision_diff.insert(0, "cycle_run_id", run_id)
        revision_diff.insert(0, "evidence", "synthetic_fixture; demonstrates revision tracking only")
        revision_diff.insert(0, "change_source", "synthetic_user_edit_after_plan_generation")
        revision_diff.to_csv(plan_change_path, index=False)

    plan_path = run_dir / "plan.csv"
    plan.to_csv(plan_path, index=False)
    (run_dir / "plan.md").write_text(
        "**SYNTHETIC ONLY — 不是市场信号，不得用于真实交易。**\n\n" + plan_markdown(plan),
        encoding="utf-8",
    )
    actionable = plan.loc[plan["action"].isin(["BUY", "SELL"]) & plan["planned_shares"].gt(0)]
    if actionable.empty:
        raise RuntimeError("合成示例计划没有可模拟的买卖行")
    chosen = actionable.iloc[0]
    planned_quantity = int(chosen["planned_shares"])
    fill_quantity = max(100, (planned_quantity // 2 // 100) * 100)
    fill_quantity = min(fill_quantity, planned_quantity)
    reference_price = float(chosen["reference_price"])
    simulated_price = round(reference_price * 1.001, 2)
    next_session = pd.bdate_range(
        start=pd.Timestamp(chosen["data_cutoff_time"][:10]) + pd.Timedelta(days=1), periods=1,
    )[0].date().isoformat()
    fills_path = run_dir / "fictitious_fills.csv"
    fill_rows = [] if scenario == "no_fill" else [{
        "trade_date": next_session,
        "order_id": f"SIM-{run_id}",
        "cycle_run_id": run_id,
        "symbol": chosen["symbol"],
        "side": chosen["action"],
        "quantity": fill_quantity,
        "price": simulated_price,
        "fees_cny": 2.0,
        "cost_basis_per_share": simulated_price,
    }]
    pd.DataFrame(fill_rows, columns=[
        "trade_date", "order_id", "cycle_run_id", "symbol", "side", "quantity", "price",
        "fees_cny", "cost_basis_per_share",
    ]).to_csv(fills_path, index=False)
    fills = read_fills(fills_path)

    import_receipt = None
    duplicate_import_result = None
    if scenario == "duplicate_import":
        registry_path = run_dir / "fill_import_registry.json"
        import_receipt = register_fill_import(fills_path, registry_path, row_count=len(fills), cycle_run_id=run_id)
        try:
            register_fill_import(fills_path, registry_path, row_count=len(fills), cycle_run_id=run_id)
        except DuplicateFillImportError as exc:
            duplicate_import_result = {"rejected": True, "reason": str(exc)}
        else:
            raise AssertionError("重复成交导入没有被登记簿拒绝")
        (run_dir / "duplicate_import_attempt.json").write_text(
            json.dumps(duplicate_import_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )

    order_statuses = None
    order_status_path = None
    if scenario == "partial_fill_cancelled":
        evidence_path = run_dir / "synthetic_order_status_evidence.txt"
        evidence_path.write_text("SYNTHETIC ONLY: simulated order status CANCELLED after a partial fill.\n", encoding="utf-8")
        evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        order_status_path = run_dir / "fictitious_order_statuses.csv"
        pd.DataFrame([{
            "order_id": f"SIM-{run_id}",
            "plan_id": str(chosen["plan_id"]),
            "symbol": str(chosen["symbol"]),
            "side": str(chosen["action"]),
            "status": "CANCELLED",
            "reported_at_utc": (local_now + timedelta(days=1)).astimezone(ZoneInfo("UTC")).isoformat(),
            "evidence_file": evidence_path.name,
            "evidence_sha256": evidence_hash,
        }]).to_csv(order_status_path, index=False)
        order_statuses = read_order_statuses(order_status_path)

    positions_path = run_dir / "fictitious_positions.csv"
    if scenario != "missing_positions":
        pd.DataFrame([{
            "cycle_run_id": run_id,
            "symbol": chosen["symbol"],
            "shares": 0 if scenario == "no_fill" else (fill_quantity if chosen["action"] == "BUY" else 0),
            "available_to_sell": 0,
            "average_cost": simulated_price,
            "last_price": simulated_price,
            "name": chosen["name"],
        }]).to_csv(positions_path, index=False)
        positions = read_positions(positions_path)
        position_reconciliation = compare_positions_to_plan(plan, positions)
    else:
        positions = None
        position_reconciliation = None

    reconciliation = compare_plan_and_fills(plan, fills, order_statuses=order_statuses)
    reconciliation.insert(0, "cycle_run_id", run_id)
    reconciliation.to_csv(run_dir / "reconciliation.csv", index=False)
    if position_reconciliation is not None:
        position_reconciliation.insert(0, "cycle_run_id", run_id)
        position_reconciliation.to_csv(run_dir / "position_reconciliation.csv", index=False)
    review = review_markdown(reconciliation, position_reconciliation)
    (run_dir / "review.md").write_text(
        "**SYNTHETIC ONLY — 假成交与假持仓，不是华泰账户数据。**\n\n" + review,
        encoding="utf-8",
    )

    row = reconciliation.iloc[0]
    hypothesis = {
        "schema_version": "cehuan.synthetic_next_hypothesis.v1",
        "cycle_run_id": run_id,
        "source_plan_id": str(plan["plan_id"].iloc[0]),
        "result_type": "synthetic_workflow_demo_not_market_research",
        "scenario": scenario,
        "facts": {
            "source": "generated_plan_and_fictitious_fill_csv",
            "symbol": str(row["symbol"]),
            "side": str(row["side"]),
            "planned_quantity": int(row["planned_quantity"]),
            "filled_quantity": int(row["filled_quantity"]),
            "fill_ratio": None if pd.isna(row["fill_ratio"]) else float(row["fill_ratio"]),
            "reconciliation_status": str(row["reconciliation_status"]),
            "order_status": str(row["order_status"]),
            "position_snapshot": "NOT_PROVIDED" if positions is None else "PROVIDED_SYNTHETIC",
            "confidence": "high_for_synthetic_arithmetic_only; none_for_market_execution",
        },
        "interpretation": (
            "没有成交行只能证明本次成交 CSV 未含匹配成交；委托状态未知，不能推断已撤单。"
            if scenario == "no_fill" else
            "合成案例展示部分成交后有明确撤单状态证据；状态证据与成交行分开保存。"
            if scenario == "partial_fill_cancelled" else
            "合成案例展示计划修改前后版本与字段差异留痕；对账使用用户修改后的计划。"
            if scenario == "modified_plan" else
            "合成案例仅检查 CSV 导入和复盘算术；所有数据均为虚构。"
        ),
        "research_suggestion": {
            "hypothesis": "真实前瞻纸面记录应保留计划版本、委托状态原件、成交 CSV 和持仓快照，以区分未执行原因。",
            "next_validation": "持续记录未见过的盘后计划、次日委托状态、成交和持仓；单次合成结果不支持调整策略参数。",
            "confidence": "low; synthetic-only evidence",
        },
        "automatic_parameter_change": False,
        "broker_connection_or_order": False,
    }
    (run_dir / "next_hypothesis.json").write_text(
        json.dumps(hypothesis, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    artifact_map = {
        "synthetic_bars": "synthetic_bars.csv",
        "baseline_metrics": "baseline_metrics.json",
        "baseline_equity_curve": "baseline_equity_curve.csv",
        "baseline_trades": "baseline_trades.csv",
        "plan_id": str(plan["plan_id"].iloc[0]),
        "plan": "plan.csv",
        "fictitious_fills": "fictitious_fills.csv",
        "reconciliation": "reconciliation.csv",
        "review": "review.md",
        "next_hypothesis": "next_hypothesis.json",
    }
    if positions is not None:
        artifact_map["fictitious_positions"] = "fictitious_positions.csv"
        artifact_map["position_reconciliation"] = "position_reconciliation.csv"
    if original_plan_path:
        artifact_map["original_plan"] = original_plan_path.name
        artifact_map["plan_changes"] = plan_change_path.name
    if order_status_path:
        artifact_map["fictitious_order_statuses"] = order_status_path.name
        artifact_map["order_status_evidence"] = "synthetic_order_status_evidence.txt"
    if import_receipt:
        artifact_map["fill_import_registry"] = "fill_import_registry.json"
        artifact_map["duplicate_import_attempt"] = "duplicate_import_attempt.json"
    manifest = {
        "run_id": run_id,
        "scenario": scenario,
        "result_type": "synthetic_workflow_demo_not_market_research",
        **artifact_map,
        "synthetic_bars_sha256": hashlib.sha256(bars_path.read_bytes()).hexdigest(),
        "fill_import_receipt": import_receipt,
        "duplicate_import_attempt": duplicate_import_result,
        "position_snapshot_status": "NOT_PROVIDED" if positions is None else "PROVIDED_SYNTHETIC",
        "broker_connection_or_order": False,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir
