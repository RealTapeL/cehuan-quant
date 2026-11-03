from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


FEE_SCHEDULE_COLUMNS = [
    "valid_from", "valid_to", "market", "commission_rate", "minimum_commission_cny",
    "stamp_duty_sell_rate", "transfer_fee_rate", "source", "source_sha256",
    "review_status", "reviewed_by", "reviewed_at",
]
FEE_RATE_COLUMNS = (
    "commission_rate", "minimum_commission_cny", "stamp_duty_sell_rate", "transfer_fee_rate",
)


def _clean_text(values: pd.Series) -> pd.Series:
    return values.map(lambda value: "" if pd.isna(value) else str(value).strip())


def normalize_fee_schedule(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a reviewed, effective-dated A-share fee assumption table."""
    missing = sorted(set(FEE_SCHEDULE_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError("历史费率缺少字段：" + ", ".join(missing))
    fees = frame[FEE_SCHEDULE_COLUMNS].copy()
    fees["market"] = _clean_text(fees["market"]).str.upper()
    if fees.empty or not fees["market"].eq("A_SHARE").all():
        raise ValueError("历史费率当前仅接受非空的 A_SHARE 费率表")
    fees["valid_from"] = pd.to_datetime(fees["valid_from"], errors="coerce").dt.normalize()
    valid_to_text = fees["valid_to"].replace("", pd.NA)
    fees["valid_to"] = pd.to_datetime(valid_to_text, errors="coerce").dt.normalize()
    if fees["valid_from"].isna().any() or (valid_to_text.notna() & fees["valid_to"].isna()).any():
        raise ValueError("历史费率 valid_from/valid_to 日期无效")
    if (fees["valid_to"].notna() & fees["valid_to"].lt(fees["valid_from"])).any():
        raise ValueError("历史费率 valid_to 早于 valid_from")
    for column in FEE_RATE_COLUMNS:
        fees[column] = pd.to_numeric(fees[column].replace("", pd.NA), errors="coerce")
    fee_values = fees[list(FEE_RATE_COLUMNS)].to_numpy(dtype=float, na_value=np.nan)
    if not np.isfinite(fee_values).all() or (fee_values < 0).any():
        raise ValueError("历史费率包含缺失、非有限值或负费率")
    for column in ("source", "source_sha256", "review_status", "reviewed_by", "reviewed_at"):
        fees[column] = _clean_text(fees[column])
    digest_ok = fees["source_sha256"].map(
        lambda value: len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
    )
    reviewed_at = pd.to_datetime(fees["reviewed_at"], errors="coerce", format="mixed")
    reviewed = (
        fees["source"].ne("") & digest_ok & fees["review_status"].eq("REVIEWED")
        & fees["reviewed_by"].ne("") & reviewed_at.notna()
    )
    if not reviewed.all():
        raise ValueError("历史费率存在缺少来源 SHA-256 或人工复核字段的记录")
    fees = fees.sort_values("valid_from", kind="stable").reset_index(drop=True)
    previous_end = None
    for index, row in enumerate(fees.itertuples(index=False)):
        if index and (pd.isna(previous_end) or row.valid_from <= previous_end):
            raise ValueError("A_SHARE 历史费率有效日期区间重叠")
        previous_end = row.valid_to
    return fees


def map_fee_schedule_to_sessions(
    schedule: pd.DataFrame,
    session_dates: list[Any] | pd.Series,
) -> dict[str, dict[str, Any]]:
    """Require one reviewed fee row for every requested session date."""
    fees = normalize_fee_schedule(schedule)
    starts = fees["valid_from"].to_numpy(dtype="datetime64[ns]")
    ends = fees["valid_to"].to_numpy(dtype="datetime64[ns]")
    result: dict[str, dict[str, Any]] = {}
    for value in pd.to_datetime(pd.Series(session_dates), errors="coerce").drop_duplicates():
        if pd.isna(value):
            raise ValueError("费率匹配日期含无效值")
        day = pd.Timestamp(value).normalize()
        index = int(pd.Series(starts).searchsorted(day.to_datetime64(), side="right") - 1)
        if index < 0 or (not pd.isna(ends[index]) and day.to_datetime64() > ends[index]):
            raise ValueError(f"{day:%Y-%m-%d} 没有唯一有效的 A_SHARE 历史费率")
        row = fees.iloc[index].to_dict()
        row["valid_from"] = pd.Timestamp(row["valid_from"])
        row["valid_to"] = None if pd.isna(row["valid_to"]) else pd.Timestamp(row["valid_to"])
        result[day.strftime("%Y-%m-%d")] = row
    return result


def scheduled_fee_components(notional_cny: float, side: str, fee_row: dict[str, Any]) -> dict[str, float]:
    if notional_cny < 0:
        raise ValueError("成交金额不能为负数")
    normalized_side = side.upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side 只能是 BUY 或 SELL")
    commission = max(
        float(fee_row["minimum_commission_cny"]),
        notional_cny * float(fee_row["commission_rate"]),
    )
    stamp = notional_cny * float(fee_row["stamp_duty_sell_rate"]) if normalized_side == "SELL" else 0.0
    transfer = notional_cny * float(fee_row["transfer_fee_rate"])
    return {
        "commission_cny": commission,
        "stamp_duty_cny": stamp,
        "transfer_fee_cny": transfer,
    }


def calculate_scheduled_fees(notional_cny: float, side: str, fee_row: dict[str, Any]) -> float:
    return sum(scheduled_fee_components(notional_cny, side, fee_row).values())
