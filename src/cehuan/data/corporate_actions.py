from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.canonical import canonical_symbol


DEFAULT_CORPORATE_ACTIONS = (
    Path(__file__).resolve().parents[3] / "configs" / "corporate_actions.csv"
)

CORPORATE_ACTION_COLUMNS = [
    "event_id",
    "event_type",
    "old_symbol",
    "new_symbol",
    "record_date",
    "old_last_trade_date",
    "old_delisting_date",
    "new_share_registration_date",
    "new_shares_tradable_date",
    "allocation_result_announcement_date",
    "share_ratio",
    "ratio_basis",
    "integer_allocation_method",
    "allocation_status",
    "evidence_url",
    "evidence_title",
    "evidence_page_reference",
    "evidence_sha256",
    "old_last_trade_evidence_url",
    "old_last_trade_evidence_title",
    "old_last_trade_evidence_page_reference",
    "old_last_trade_evidence_sha256",
    "evidence_summary",
    "review_status",
    "reviewed_by",
    "reviewed_at",
]


def normalize_corporate_actions(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Validate corporate actions separately from simple code identity changes."""
    if frame is None or frame.empty:
        return pd.DataFrame(columns=CORPORATE_ACTION_COLUMNS)
    missing = set(CORPORATE_ACTION_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError("公司行动表缺少字段：" + ", ".join(sorted(missing)))
    actions = frame[CORPORATE_ACTION_COLUMNS].copy()
    for column in CORPORATE_ACTION_COLUMNS:
        actions[column] = actions[column].astype("string").fillna("").str.strip()
    actions["old_symbol"] = actions["old_symbol"].map(canonical_symbol)
    actions["new_symbol"] = actions["new_symbol"].map(canonical_symbol)
    if not actions["event_type"].eq("SHARE_SWAP_MERGER").all():
        raise ValueError("当前公司行动处理器只接受 event_type=SHARE_SWAP_MERGER")
    if actions["old_symbol"].eq(actions["new_symbol"]).any():
        raise ValueError("吸收合并的旧证券与新证券代码必须不同")
    if not actions["ratio_basis"].eq("old_shares_to_new_shares").all():
        raise ValueError("公司行动 share_ratio 必须按旧股数到新股数解释")
    if not actions["integer_allocation_method"].eq(
        "FRACTIONAL_REMAINDER_RANKING_RANDOM_TIEBREAK"
    ).all():
        raise ValueError("吸收合并整数股分配方法缺失或未知")
    if not actions["allocation_status"].isin(
        {"UNRESOLVED_ACCOUNT_ALLOCATION", "CONFIRMED_ACCOUNT_ALLOCATION"}
    ).all():
        raise ValueError("allocation_status 必须明确标为未决或已核实")
    ratio = pd.to_numeric(actions["share_ratio"], errors="coerce")
    if ratio.isna().any() or (~ratio.map(math.isfinite)).any() or (ratio <= 0).any():
        raise ValueError("公司行动 share_ratio 必须为有限正数")
    actions["share_ratio"] = ratio.astype(float)

    date_columns = [
        "old_last_trade_date", "record_date", "old_delisting_date",
        "new_share_registration_date", "new_shares_tradable_date",
        "allocation_result_announcement_date",
    ]
    for column in date_columns:
        parsed = pd.to_datetime(actions[column], errors="coerce")
        if parsed.isna().any():
            raise ValueError(f"公司行动 {column} 必须是明确日期")
        actions[column] = parsed.dt.strftime("%Y-%m-%d")
    for row in actions.to_dict(orient="records"):
        ordered = [row[column] for column in date_columns]
        if ordered[:5] != sorted(ordered[:5]):
            raise ValueError(f"{row['event_id']} 的公告、登记、交易、摘牌和新股日期顺序不成立")
        if not row["record_date"] <= row["allocation_result_announcement_date"] <= row["new_shares_tradable_date"]:
            raise ValueError(f"{row['event_id']} 的换股结果公告日与新股可流通日期顺序不成立")
    if actions["event_id"].eq("").any() or actions["evidence_summary"].eq("").any():
        raise ValueError("公司行动 event_id 和 evidence_summary 不能为空")
    if not actions["evidence_url"].str.startswith("https://").all():
        raise ValueError("公司行动必须登记 HTTPS 公告来源")
    if not actions["old_last_trade_evidence_url"].str.startswith("https://").all():
        raise ValueError("旧证券最后交易日必须有独立 HTTPS 公告来源")
    for column in (
        "evidence_title", "evidence_page_reference", "old_last_trade_evidence_title",
        "old_last_trade_evidence_page_reference",
    ):
        if not actions[column].str.strip().ne("").all():
            raise ValueError(f"公司行动 {column} 不能为空")
    if not actions["reviewed_by"].ne("").all() or not actions["reviewed_at"].ne("").all():
        raise ValueError("公司行动证据缺少 reviewed_by 或 reviewed_at")
    if not actions["review_status"].ne("").all():
        raise ValueError("公司行动证据缺少 review_status")
    if actions.duplicated("event_id").any():
        raise ValueError("公司行动 event_id 不能重复")
    return actions.sort_values(["old_last_trade_date", "event_id"]).reset_index(drop=True)


def read_corporate_actions(path: str | Path = DEFAULT_CORPORATE_ACTIONS) -> pd.DataFrame:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"公司行动表不存在：{source}")
    return normalize_corporate_actions(pd.read_csv(source, dtype=str, keep_default_na=False))


def corporate_action_phase(action: pd.Series | dict[str, Any], trade_date: str) -> dict[str, Any]:
    """Return trading availability without treating an unallocated right as stock."""
    row = dict(action)
    day = pd.Timestamp(trade_date).strftime("%Y-%m-%d")
    if day <= str(row["old_last_trade_date"]):
        return {"phase": "OLD_SECURITY", "tradable_symbol": str(row["old_symbol"])}
    if day < str(row["new_shares_tradable_date"]):
        return {"phase": "PENDING_NEW_SHARES", "tradable_symbol": None}
    if str(row["allocation_status"]) != "CONFIRMED_ACCOUNT_ALLOCATION":
        return {"phase": "ALLOCATION_UNRESOLVED", "tradable_symbol": None}
    return {"phase": "NEW_SECURITY", "tradable_symbol": str(row["new_symbol"])}


def make_share_swap_claim(
    action: pd.Series | dict[str, Any],
    old_shares: int,
    cost_basis_cny: float,
    holding_since: str,
    *,
    final_allocated_new_shares: int | None = None,
) -> dict[str, Any]:
    """Represent the unlisted merger entitlement without inventing fractional shares.

    An exact per-position allocation is required before tradable new shares can
    be issued. Until then the entitlement exposes a floor/ceiling scenario range.
    Cost basis remains attached to the claim and no cash is created.
    """
    row = dict(action)
    old_shares = int(old_shares)
    if old_shares < 0 or old_shares != float(old_shares):
        raise ValueError("公司行动旧股数量必须为非负整数")
    if cost_basis_cny < 0 or not math.isfinite(float(cost_basis_cny)):
        raise ValueError("公司行动持仓成本必须为有限非负数")
    try:
        exact = Decimal(old_shares) * Decimal(str(row["share_ratio"]))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("公司行动换股比例无效") from exc
    floor_shares = int(exact.to_integral_value(rounding=ROUND_FLOOR))
    ceiling_shares = int(exact.to_integral_value(rounding=ROUND_CEILING))
    if row["allocation_status"] == "CONFIRMED_ACCOUNT_ALLOCATION":
        if final_allocated_new_shares is None:
            raise ValueError("已确认分配状态必须提供该持仓批次的最终获配股数")
        final_allocated_new_shares = int(final_allocated_new_shares)
        if not floor_shares <= final_allocated_new_shares <= ceiling_shares:
            raise ValueError("最终获配股数不在公告换股比例的整数股范围内")
    elif final_allocated_new_shares is not None:
        raise ValueError("未核实个人最终获配股数时，不得注入一个假定整数股数")
    return {
        "event_id": str(row["event_id"]),
        "old_symbol": str(row["old_symbol"]),
        "new_symbol": str(row["new_symbol"]),
        "old_shares": old_shares,
        "expected_new_shares_exact": str(exact),
        "new_shares_lower_bound": floor_shares,
        "new_shares_upper_bound": ceiling_shares,
        "final_allocated_new_shares": final_allocated_new_shares,
        "cost_basis_cny": float(cost_basis_cny),
        "holding_since": str(holding_since),
        "new_shares_tradable_date": str(row["new_shares_tradable_date"]),
        "claim_status": "PENDING_FINAL_ALLOCATION" if final_allocated_new_shares is None else "ALLOCATED_NOT_YET_TRADABLE",
        "cash_change_cny": 0.0,
        "formal_valuation_status": "UNVALUED_PENDING_CORPORATE_ACTION_REVIEW",
    }
