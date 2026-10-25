from __future__ import annotations

import tomllib
from pathlib import Path

import pandas as pd

from cehuan.data.canonical import canonical_symbol


FILL_COLUMNS = ["trade_date", "order_id", "symbol", "side", "quantity", "price", "fees_cny", "cost_basis_per_share"]
POSITION_REVIEW_COLUMNS = [
    "symbol", "plan_action", "planned_target_shares", "actual_shares", "shares_deviation",
    "available_to_sell", "average_cost", "last_price", "unrealized_pnl_cny",
    "position_status", "evidence", "confidence",
]
DEFAULT_MAPPING = Path(__file__).resolve().parents[3] / "configs" / "huatai_columns.toml"


def _read_mapping(path: str | Path | None) -> dict[str, object]:
    with (Path(path) if path else DEFAULT_MAPPING).open("rb") as handle:
        return tomllib.load(handle)


def _rename_aliases(frame: pd.DataFrame, aliases: dict[str, list[str]]) -> pd.DataFrame:
    data = frame.copy()
    rename: dict[str, str] = {}
    for canonical, choices in aliases.items():
        if canonical in data.columns:
            continue
        source = next((choice for choice in choices if choice in data.columns), None)
        if source:
            rename[source] = canonical
    return data.rename(columns=rename)


def read_fills(path: str | Path, mapping_path: str | Path | None = None) -> pd.DataFrame:
    mapping = _read_mapping(mapping_path)
    fills = _rename_aliases(pd.read_csv(path), mapping.get("fills", {}))
    if "direction" in fills.columns and "side" not in fills.columns:
        fills = fills.rename(columns={"direction": "side"})
    required = ["trade_date", "symbol", "side", "quantity", "price"]
    missing = [column for column in required if column not in fills.columns]
    if missing:
        raise ValueError("成交 CSV 缺少必需字段：" + ", ".join(missing))
    for column in FILL_COLUMNS:
        if column not in fills:
            fills[column] = pd.NA
    fills["symbol"] = fills["symbol"].map(canonical_symbol)
    side_map = {"买入": "BUY", "买": "BUY", "BUY": "BUY", "B": "BUY", "证券买入": "BUY",
                "卖出": "SELL", "卖": "SELL", "SELL": "SELL", "S": "SELL", "证券卖出": "SELL"}
    side_map.update(mapping.get("side_values", {}))
    fills["side"] = fills["side"].astype(str).str.strip().str.upper().replace(side_map)
    if not fills["side"].isin(["BUY", "SELL"]).all():
        raise ValueError("成交方向只能识别为 BUY/SELL 或 买入/卖出")
    for column in ["quantity", "price", "fees_cny", "cost_basis_per_share"]:
        fills[column] = pd.to_numeric(fills[column], errors="coerce")
    if fills[["quantity", "price"]].isna().any().any() or (fills["quantity"] <= 0).any() or (fills["price"] <= 0).any():
        raise ValueError("成交数量与价格必须是正数")
    fills["fees_cny"] = fills["fees_cny"].fillna(0.0)
    return fills[FILL_COLUMNS]


def read_positions(path: str | Path, mapping_path: str | Path | None = None) -> pd.DataFrame:
    mapping = _read_mapping(mapping_path)
    positions = _rename_aliases(pd.read_csv(path), mapping.get("positions", {}))
    required = ["symbol", "shares"]
    missing = [column for column in required if column not in positions.columns]
    if missing:
        raise ValueError("持仓 CSV 缺少必需字段：" + ", ".join(missing))
    for column in ("available_to_sell", "average_cost", "last_price", "name"):
        if column not in positions:
            positions[column] = pd.NA
    positions["symbol"] = positions["symbol"].map(canonical_symbol)
    for column in ("shares", "available_to_sell", "average_cost", "last_price"):
        positions[column] = pd.to_numeric(positions[column], errors="coerce")
    if positions["shares"].isna().any() or (positions["shares"] < 0).any():
        raise ValueError("持仓数量必须是非负数")
    return positions[["symbol", "shares", "available_to_sell", "average_cost", "last_price", "name"]]


def compare_positions_to_plan(plan: pd.DataFrame, positions: pd.DataFrame) -> pd.DataFrame:
    """Compare a supplied position snapshot with plan targets and known costs."""
    if not {"symbol", "target_shares"}.issubset(plan.columns):
        raise ValueError("计划 CSV 缺少 symbol/target_shares 字段")
    expected = plan[[column for column in ("symbol", "action", "target_shares") if column in plan]].copy()
    expected["symbol"] = expected["symbol"].map(canonical_symbol)
    expected = expected.rename(columns={"action": "plan_action", "target_shares": "planned_target_shares"})
    actual = positions.copy()
    actual["symbol"] = actual["symbol"].map(canonical_symbol)
    merged = expected.merge(actual, on="symbol", how="outer")
    merged["planned_target_shares"] = pd.to_numeric(merged["planned_target_shares"], errors="coerce")
    merged["actual_shares"] = pd.to_numeric(merged["shares"], errors="coerce").fillna(0).astype(int)
    merged["shares_deviation"] = merged["actual_shares"] - merged["planned_target_shares"]
    merged["unrealized_pnl_cny"] = (
        (pd.to_numeric(merged["last_price"], errors="coerce") - pd.to_numeric(merged["average_cost"], errors="coerce"))
        * merged["actual_shares"]
    )

    def classify(row: pd.Series) -> str:
        if pd.isna(row.get("plan_action")):
            return "UNPLANNED_POSITION" if row["actual_shares"] else "NO_POSITION"
        if pd.isna(row.get("planned_target_shares")):
            return "TARGET_UNKNOWN"
        if int(row["actual_shares"]) == int(row["planned_target_shares"]):
            return "MATCHED_TARGET"
        return "ABOVE_TARGET" if int(row["actual_shares"]) > int(row["planned_target_shares"]) else "BELOW_TARGET"

    merged["position_status"] = merged.apply(classify, axis=1)
    merged["evidence"] = "导入的持仓快照与交易计划目标股数对照；未包含账户外转入转出"
    merged["confidence"] = "high_for_share_count_math; medium_for_snapshot_timing"
    return merged.reindex(columns=POSITION_REVIEW_COLUMNS).sort_values("symbol").reset_index(drop=True)


def compare_plan_and_fills(plan: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "action", "planned_shares", "reference_price"}
    if not required.issubset(plan.columns):
        raise ValueError("计划 CSV 缺少 symbol/action/planned_shares/reference_price 字段")
    expected = plan.loc[plan["action"].isin(["BUY", "SELL"]), ["symbol", "action", "planned_shares", "reference_price"]].copy()
    expected["symbol"] = expected["symbol"].map(canonical_symbol)
    expected = expected.rename(columns={"action": "side", "planned_shares": "planned_quantity", "reference_price": "plan_reference_price"})
    actual = fills.copy()
    actual["symbol"] = actual["symbol"].map(canonical_symbol)
    grouped = actual.groupby(["symbol", "side"], as_index=False).agg(
        filled_quantity=("quantity", "sum"),
        actual_vwap=("price", lambda values: float((values * actual.loc[values.index, "quantity"]).sum() / actual.loc[values.index, "quantity"].sum())),
        fees_cny=("fees_cny", "sum"),
    )
    result = expected.merge(grouped, on=["symbol", "side"], how="outer")
    result["plan_reference_price"] = pd.to_numeric(result["plan_reference_price"], errors="coerce")
    result["planned_quantity"] = pd.to_numeric(result["planned_quantity"], errors="coerce").fillna(0).astype(int)
    result["filled_quantity"] = pd.to_numeric(result["filled_quantity"], errors="coerce").fillna(0).astype(int)
    result["unfilled_quantity"] = (result["planned_quantity"] - result["filled_quantity"]).clip(lower=0)
    result["fill_ratio"] = result.apply(lambda row: row["filled_quantity"] / row["planned_quantity"] if row["planned_quantity"] else pd.NA, axis=1)
    result["reconciliation_status"] = result.apply(
        lambda row: "UNPLANNED_FILL" if row["planned_quantity"] == 0 and row["filled_quantity"] > 0
        else "NO_FILL" if row["planned_quantity"] > 0 and row["filled_quantity"] == 0
        else "PARTIAL_FILL" if 0 < row["filled_quantity"] < row["planned_quantity"]
        else "OVERFILL" if row["filled_quantity"] > row["planned_quantity"]
        else "MATCHED",
        axis=1,
    )
    result["price_difference_pct"] = result["actual_vwap"].div(result["plan_reference_price"]).sub(1.0)
    result["fees_cny"] = result["fees_cny"].fillna(0.0)
    result["realized_pnl_cny"] = pd.NA
    for index, row in result.iterrows():
        if row["side"] != "SELL":
            continue
        match = actual.loc[(actual["symbol"] == row["symbol"]) & (actual["side"] == "SELL")]
        if match["cost_basis_per_share"].notna().all() and not match.empty:
            realized = ((match["price"] - match["cost_basis_per_share"]) * match["quantity"] - match["fees_cny"]).sum()
            result.at[index, "realized_pnl_cny"] = float(realized)
    result["evidence"] = "成交 CSV 与计划数量、参考价、成交价和费用的算术对照"
    result["confidence"] = "high_for_reconciliation_math; low_for_explaining_cause"
    return result.sort_values(["symbol", "side"]).reset_index(drop=True)


def review_markdown(report: pd.DataFrame, position_report: pd.DataFrame | None = None) -> str:
    fees = float(report["fees_cny"].sum()) if not report.empty else 0.0
    unfilled = int(report["unfilled_quantity"].sum()) if not report.empty else 0
    lines = [
        "# 策环交易复盘（CSV 对账）", "",
        f"- 汇总费用：¥{fees:.2f}",
        f"- 未成交/未匹配计划数量：{unfilled} 股",
        "- 证据：导入成交记录与本次交易计划；方向、数量、VWAP、费用按 CSV 计算。",
        "- 置信度：计算结果对已提供字段为高；未成交原因与策略调整解释为低，需继续核实委托/盘口记录。",
        "- 本报告不读取券商账户、不推断缺失成交，也不会自动修改策略。", "",
        "| 代码 | 方向 | 对账状态 | 计划数量 | 成交数量 | 未成交 | 计划参考价 | 成交 VWAP | 价格差异 | 费用 | 已实现盈亏 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in report.iterrows():
        def value(field: str, fmt: str) -> str:
            val = row.get(field)
            return "" if pd.isna(val) else format(float(val), fmt)
        lines.append(f"| {row['symbol']} | {row['side']} | {row['reconciliation_status']} | {row['planned_quantity']} | {row['filled_quantity']} | {row['unfilled_quantity']} | {value('plan_reference_price', '.2f')} | {value('actual_vwap', '.2f')} | {value('price_difference_pct', '.2%')} | ¥{value('fees_cny', '.2f')} | {value('realized_pnl_cny', '.2f')} |")
    if position_report is not None:
        lines += ["", "## 持仓快照与计划目标", "", "| 代码 | 计划动作 | 目标股数 | 快照股数 | 差异 | 可卖股数 | 浮动盈亏（有成本/现价时） | 状态 |", "|---|---|---:|---:|---:|---:|---:|---|"]
        for _, row in position_report.iterrows():
            def position_value(field: str) -> str:
                value = row.get(field)
                return "" if pd.isna(value) else str(int(value))
            pnl = row.get("unrealized_pnl_cny")
            pnl_text = "" if pd.isna(pnl) else f"¥{float(pnl):.2f}"
            action = "" if pd.isna(row.get("plan_action")) else str(row["plan_action"])
            lines.append(f"| {row['symbol']} | {action} | {position_value('planned_target_shares')} | {position_value('actual_shares')} | {position_value('shares_deviation')} | {position_value('available_to_sell')} | {pnl_text} | {row['position_status']} |")
    return "\n".join(lines) + "\n"
