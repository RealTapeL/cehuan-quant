from __future__ import annotations

import hashlib
from io import BytesIO
import tomllib
from pathlib import Path

import pandas as pd

from cehuan.data.canonical import canonical_symbol


FILL_COLUMNS = ["trade_date", "order_id", "symbol", "side", "quantity", "price", "fees_cny", "cost_basis_per_share"]
ORDER_STATUS_COLUMNS = [
    "order_id", "plan_id", "symbol", "side", "status", "reported_at_utc", "evidence_file", "evidence_sha256",
]
VALID_ORDER_STATUSES = {"OPEN", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
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


def read_order_statuses(path: str | Path) -> pd.DataFrame:
    raw_bytes = Path(path).expanduser().read_bytes()
    statuses = pd.read_csv(BytesIO(raw_bytes), dtype=str, keep_default_na=False)
    missing = sorted(set(ORDER_STATUS_COLUMNS).difference(statuses.columns))
    if missing:
        raise ValueError("委托状态 CSV 缺少字段：" + ", ".join(missing))
    statuses = statuses[ORDER_STATUS_COLUMNS].copy()
    for column in ORDER_STATUS_COLUMNS:
        statuses[column] = statuses[column].astype(str).str.strip()
    if statuses.empty:
        return statuses
    if statuses[ORDER_STATUS_COLUMNS].eq("").any().any():
        raise ValueError("委托状态证据缺少订单、计划、证券、方向、状态、时间或原件散列")
    statuses["symbol"] = statuses["symbol"].map(canonical_symbol)
    statuses["side"] = statuses["side"].str.upper()
    statuses["status"] = statuses["status"].str.upper()
    if not statuses["side"].isin({"BUY", "SELL"}).all():
        raise ValueError("委托状态方向只能是 BUY/SELL")
    if not statuses["status"].isin(VALID_ORDER_STATUSES).all():
        bad = sorted(set(statuses["status"]).difference(VALID_ORDER_STATUSES))
        raise ValueError("委托状态值不支持：" + ", ".join(bad))
    digest_ok = statuses["evidence_sha256"].str.fullmatch(r"[0-9a-fA-F]{64}")
    if not digest_ok.all():
        raise ValueError("委托状态必须绑定 64 位 evidence_sha256")
    source_root = Path(path).expanduser().resolve().parent
    evidence_files: list[str] = []
    for row in statuses.to_dict(orient="records"):
        evidence_path = Path(row["evidence_file"]).expanduser()
        if not evidence_path.is_absolute():
            evidence_path = source_root / evidence_path
        evidence_path = evidence_path.resolve()
        if not evidence_path.is_file():
            raise FileNotFoundError(f"委托状态 {row['order_id']} 的来源原件不存在：{evidence_path}")
        observed_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        if observed_hash != row["evidence_sha256"].lower():
            raise ValueError(f"委托状态 {row['order_id']} 的来源原件 SHA-256 不匹配")
        evidence_files.append(str(evidence_path))
    statuses["evidence_file"] = evidence_files
    statuses["evidence_sha256"] = statuses["evidence_sha256"].str.lower()
    timestamp = pd.to_datetime(statuses["reported_at_utc"], errors="coerce", utc=True, format="mixed")
    has_zone = statuses["reported_at_utc"].str.contains(r"(?:Z|[+-]\d\d:\d\d)$", case=False, regex=True)
    if timestamp.isna().any() or not has_zone.all():
        raise ValueError("reported_at_utc 必须是带时区的 ISO 时间")
    statuses["reported_at_utc"] = timestamp.dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    if statuses["order_id"].duplicated().any() or statuses.duplicated(["plan_id", "symbol", "side"]).any():
        raise ValueError("委托状态证据不能重复登记订单或计划证券方向")
    return statuses


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


def compare_plan_revisions(original: pd.DataFrame, revised: pd.DataFrame) -> pd.DataFrame:
    """Return an explicit field-level diff between an issued and user-edited plan."""
    required = {"plan_id", "symbol", "action", "planned_shares", "target_shares", "reference_price"}
    for name, frame in (("原计划", original), ("修改后计划", revised)):
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(f"{name}缺少修订审计字段：" + ", ".join(missing))
    keys = ["plan_id", "symbol"]
    left = original[list(required)].copy()
    right = revised[list(required)].copy()
    left["symbol"] = left["symbol"].map(canonical_symbol)
    right["symbol"] = right["symbol"].map(canonical_symbol)
    if left.duplicated(keys).any() or right.duplicated(keys).any():
        raise ValueError("计划修订审计要求每个 plan_id/symbol 唯一")
    merged = left.merge(right, on=keys, how="outer", suffixes=("_original", "_revised"), indicator=True)
    if not merged["_merge"].eq("both").all():
        unmatched = merged.loc[merged["_merge"] != "both", keys + ["_merge"]].to_dict(orient="records")
        raise ValueError("修改前后计划行集合不一致，无法安全计算修订差异：" + str(unmatched))
    fields = ["action", "planned_shares", "target_shares", "reference_price"]
    rows: list[dict[str, object]] = []
    for _, row in merged.iterrows():
        changed: list[str] = []
        for field in fields:
            old_value, new_value = row[f"{field}_original"], row[f"{field}_revised"]
            if field == "action":
                differs = str(old_value) != str(new_value)
            else:
                old_number = pd.to_numeric(pd.Series([old_value]), errors="coerce").iloc[0]
                new_number = pd.to_numeric(pd.Series([new_value]), errors="coerce").iloc[0]
                differs = not (pd.isna(old_number) and pd.isna(new_number)) and old_number != new_number
            if differs:
                changed.append(field)
        if changed:
            record: dict[str, object] = {"plan_id": row["plan_id"], "symbol": row["symbol"], "changed_fields": ",".join(changed)}
            for field in fields:
                record[f"{field}_original"] = row[f"{field}_original"]
                record[f"{field}_revised"] = row[f"{field}_revised"]
            rows.append(record)
    columns = ["plan_id", "symbol", "changed_fields"] + [
        name for field in fields for name in (f"{field}_original", f"{field}_revised")
    ]
    return pd.DataFrame(rows, columns=columns)


def compare_plan_and_fills(
    plan: pd.DataFrame,
    fills: pd.DataFrame,
    order_statuses: pd.DataFrame | None = None,
) -> pd.DataFrame:
    required = {"symbol", "action", "planned_shares", "reference_price"}
    if not required.issubset(plan.columns):
        raise ValueError("计划 CSV 缺少 symbol/action/planned_shares/reference_price 字段")
    expected_columns = ["symbol", "action", "planned_shares", "reference_price"]
    if "plan_id" in plan.columns:
        expected_columns.append("plan_id")
    expected = plan.loc[plan["action"].isin(["BUY", "SELL"]), expected_columns].copy()
    expected["symbol"] = expected["symbol"].map(canonical_symbol)
    expected = expected.rename(columns={"action": "side", "planned_shares": "planned_quantity", "reference_price": "plan_reference_price"})
    if "plan_id" not in expected:
        expected["plan_id"] = ""
    actual = fills.copy()
    actual["symbol"] = actual["symbol"].map(canonical_symbol)
    grouped = actual.groupby(["symbol", "side"], as_index=False).agg(
        filled_quantity=("quantity", "sum"),
        actual_vwap=("price", lambda values: float((values * actual.loc[values.index, "quantity"]).sum() / actual.loc[values.index, "quantity"].sum())),
        fees_cny=("fees_cny", "sum"),
    )
    result = expected.merge(grouped, on=["symbol", "side"], how="outer")
    result["plan_id"] = result["plan_id"].fillna("")
    result["order_status"] = "UNKNOWN"
    result["order_status_evidence_sha256"] = pd.NA
    if order_statuses is not None:
        if not result["plan_id"].ne("").all():
            raise ValueError("比对委托状态证据时，计划必须包含 plan_id")
        statuses = order_statuses.copy()
        if not set(ORDER_STATUS_COLUMNS).issubset(statuses.columns):
            raise ValueError("委托状态 DataFrame 必须先通过 read_order_statuses 校验")
        if not statuses.empty:
            valid_keys = set(zip(result["plan_id"], result["symbol"], result["side"]))
            status_keys = set(zip(statuses["plan_id"], statuses["symbol"], statuses["side"]))
            unmatched = status_keys.difference(valid_keys)
            if unmatched:
                raise ValueError("委托状态证据与计划行不匹配：" + ", ".join(map(str, sorted(unmatched))))
            status_index = statuses.set_index(["plan_id", "symbol", "side"])
            for index, row in result.iterrows():
                key = (row["plan_id"], row["symbol"], row["side"])
                if key in status_index.index:
                    status_row = status_index.loc[key]
                    matching_fills = actual.loc[
                        (actual["symbol"] == row["symbol"]) & (actual["side"] == row["side"])
                    ]
                    if (
                        "order_id" in matching_fills
                        and matching_fills["order_id"].notna().any()
                        and not matching_fills["order_id"].dropna().astype(str).eq(str(status_row["order_id"])).all()
                    ):
                        raise ValueError(f"{key} 的委托状态 order_id 与成交记录不匹配")
                    result.at[index, "order_status"] = status_row["status"]
                    result.at[index, "order_status_evidence_sha256"] = status_row["evidence_sha256"]
    result["plan_reference_price"] = pd.to_numeric(result["plan_reference_price"], errors="coerce")
    result["planned_quantity"] = pd.to_numeric(result["planned_quantity"], errors="coerce").fillna(0).astype(int)
    result["filled_quantity"] = pd.to_numeric(result["filled_quantity"], errors="coerce").fillna(0).astype(int)
    result["unfilled_quantity"] = (result["planned_quantity"] - result["filled_quantity"]).clip(lower=0)
    result["fill_ratio"] = result.apply(lambda row: row["filled_quantity"] / row["planned_quantity"] if row["planned_quantity"] else pd.NA, axis=1)
    def reconcile_status(row: pd.Series) -> str:
        planned = int(row["planned_quantity"])
        filled = int(row["filled_quantity"])
        order_status = str(row["order_status"])
        if order_status == "CANCELLED":
            if filled == 0:
                return "CANCELLED_NO_FILL"
            if filled < planned:
                return "PARTIAL_FILL_CANCELLED"
            return "FILLED_WITH_CANCEL_STATUS_CONFLICT"
        if order_status == "REJECTED":
            return "REJECTED_NO_FILL" if filled == 0 else "REJECTED_WITH_FILL_STATUS_CONFLICT"
        if order_status == "EXPIRED":
            return "EXPIRED_NO_FILL" if filled == 0 else "PARTIAL_FILL_EXPIRED" if filled < planned else "EXPIRED_STATUS_FILL_DATA_CONFLICT"
        if order_status == "PARTIALLY_FILLED":
            return "PARTIAL_FILL" if 0 < filled < planned else "ORDER_STATUS_FILL_DATA_CONFLICT"
        if order_status == "FILLED":
            return "MATCHED" if filled == planned else "ORDER_STATUS_FILL_DATA_CONFLICT"
        if order_status == "OPEN":
            return "NOT_FILLED_OPEN" if filled == 0 else "PARTIAL_FILL_OPEN" if filled < planned else "OPEN_STATUS_FILL_DATA_CONFLICT"
        if planned == 0 and filled > 0:
            return "UNPLANNED_FILL"
        if planned > 0 and filled == 0:
            return "NO_FILL"
        if 0 < filled < planned:
            return "PARTIAL_FILL"
        if filled > planned:
            return "OVERFILL"
        return "MATCHED"

    result["reconciliation_status"] = result.apply(reconcile_status, axis=1)
    result["price_difference_pct"] = result["actual_vwap"].div(result["plan_reference_price"]).sub(1.0)
    result["fees_cny"] = pd.to_numeric(result["fees_cny"], errors="coerce").fillna(0.0)
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
        "| 代码 | 方向 | 对账状态 | 委托状态 | 委托证据 SHA-256 | 计划数量 | 成交数量 | 未成交 | 计划参考价 | 成交 VWAP | 价格差异 | 费用 | 已实现盈亏 |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    if not report.empty and ((report["planned_quantity"] > 0) & (report["filled_quantity"] == 0)).any():
        unknown_rows = report.loc[
            (report["planned_quantity"] > 0)
            & (report["filled_quantity"] == 0)
            & report["order_status"].eq("UNKNOWN")
        ]
        if not unknown_rows.empty:
            lines += [
                "",
                "**未成交说明：**没有匹配到成交行只表示当前 CSV 未提供成交；委托是否仍有效、已撤单、拒绝或过期均未知，不能据此认定已取消。",
            ]
    for _, row in report.iterrows():
        def value(field: str, fmt: str) -> str:
            val = row.get(field)
            return "" if pd.isna(val) else format(float(val), fmt)
        evidence_hash = "" if pd.isna(row.get("order_status_evidence_sha256")) else str(row["order_status_evidence_sha256"])
        lines.append(
            f"| {row['symbol']} | {row['side']} | {row['reconciliation_status']} | {row['order_status']} | "
            f"{evidence_hash} | {row['planned_quantity']} | {row['filled_quantity']} | {row['unfilled_quantity']} | "
            f"{value('plan_reference_price', '.2f')} | {value('actual_vwap', '.2f')} | "
            f"{value('price_difference_pct', '.2%')} | ¥{value('fees_cny', '.2f')} | "
            f"¥{value('realized_pnl_cny', '.2f')} |"
        )
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
    else:
        lines += ["", "## 持仓状态", "", "未提供成交后持仓快照；当前持仓、可卖数量及相对计划的仓位偏差均未知，不能视为零持仓。"]
    return "\n".join(lines) + "\n"
