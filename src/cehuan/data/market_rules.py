from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


MARKET_RULE_COLUMNS = [
    "symbol", "board", "valid_from", "valid_to", "is_listed", "is_suspended", "is_st",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares", "source", "source_sha256",
    "review_status", "reviewed_by", "reviewed_at",
]
MARKET_RULE_VALUE_COLUMNS = [
    "board", "is_listed", "is_suspended", "is_st", "has_price_limit", "price_limit_pct",
    "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
    "source", "source_sha256", "review_status", "reviewed_by", "reviewed_at",
]
_BOOL_COLUMNS = ("is_listed", "is_suspended", "is_st", "has_price_limit")
_INTEGER_COLUMNS = ("buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares")


def _required_columns(frame: pd.DataFrame, required: list[str], label: str) -> None:
    missing = sorted(set(required).difference(frame.columns))
    if missing:
        raise ValueError(f"{label} 缺少字段：" + ", ".join(missing))


def _clean_text(values: pd.Series) -> pd.Series:
    return values.map(lambda value: "" if pd.isna(value) else str(value).strip())


def _parse_bool(value: Any) -> bool | None:
    if pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def _parse_date_columns(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    result = frame.copy()
    result["valid_from"] = pd.to_datetime(result["valid_from"], errors="coerce").dt.normalize()
    valid_to_text = result["valid_to"].replace("", pd.NA)
    result["valid_to"] = pd.to_datetime(valid_to_text, errors="coerce").dt.normalize()
    if result["valid_from"].isna().any() or (valid_to_text.notna() & result["valid_to"].isna()).any():
        raise ValueError(f"{label} valid_from/valid_to 日期无效")
    if (result["valid_to"].notna() & result["valid_to"].lt(result["valid_from"])).any():
        raise ValueError(f"{label} valid_to 早于 valid_from")
    return result


def _validate_source_review(frame: pd.DataFrame, label: str) -> None:
    source_hash = _clean_text(frame["source_sha256"])
    hash_ok = source_hash.map(
        lambda value: len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
    )
    reviewed_at = pd.to_datetime(_clean_text(frame["reviewed_at"]), errors="coerce", format="mixed")
    complete = (
        _clean_text(frame["source"]).ne("")
        & hash_ok
        & _clean_text(frame["review_status"]).eq("REVIEWED")
        & _clean_text(frame["reviewed_by"]).ne("")
        & reviewed_at.notna()
    )
    if not complete.all():
        raise ValueError(f"{label} 存在缺少来源 SHA-256 或人工复核字段的记录")


def _validate_non_overlapping(frame: pd.DataFrame, keys: list[str], label: str) -> None:
    duplicate = frame.duplicated(keys + ["valid_from"], keep=False)
    if duplicate.any():
        raise ValueError(f"{label} 存在重复有效期起始记录")
    for key, group in frame.sort_values(keys + ["valid_from"]).groupby(keys, sort=False, dropna=False):
        previous_end = None
        first = True
        for row in group.itertuples(index=False):
            if not first and (pd.isna(previous_end) or row.valid_from <= previous_end):
                raise ValueError(f"{label} 有效日期区间重叠：{key}")
            previous_end = row.valid_to
            first = False


def normalize_market_rule_schedule(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate the reviewed, effective-dated per-security trading-rule contract."""
    _required_columns(frame, MARKET_RULE_COLUMNS, "有效日期交易规则")
    rules = frame[MARKET_RULE_COLUMNS].copy()
    rules["symbol"] = _clean_text(rules["symbol"]).str.upper()
    rules["board"] = _clean_text(rules["board"])
    for column in ("source", "source_sha256", "review_status", "reviewed_by", "reviewed_at"):
        rules[column] = _clean_text(rules[column])
    if rules.empty or rules["symbol"].eq("").any() or rules["board"].eq("").any():
        raise ValueError("有效日期交易规则不能为空，证券代码和板块也不能为空")
    rules = _parse_date_columns(rules, "有效日期交易规则")
    for column in _BOOL_COLUMNS:
        parsed = rules[column].map(_parse_bool)
        if parsed.isna().any():
            raise ValueError(f"有效日期交易规则 {column} 必须逐行明确为 true/false")
        rules[column] = parsed.astype(bool)
    numeric_columns = ["price_limit_pct", *_INTEGER_COLUMNS]
    for column in numeric_columns:
        rules[column] = pd.to_numeric(rules[column].replace("", pd.NA), errors="coerce")
    integer_values = rules[list(_INTEGER_COLUMNS)].to_numpy(dtype=float, na_value=np.nan)
    limit_values = rules["price_limit_pct"].to_numpy(dtype=float, na_value=np.nan)
    invalid_limit_number = np.isinf(limit_values) | (
        rules["has_price_limit"].to_numpy(dtype=bool) & ~np.isfinite(limit_values)
    )
    if not np.isfinite(integer_values).all() or invalid_limit_number.any():
        raise ValueError("有效日期交易规则数值字段必须有限；交易单位和适用的涨跌幅阈值不能为 NaN/inf")
    if rules[list(_INTEGER_COLUMNS)].isna().any().any():
        raise ValueError("有效日期交易规则交易单位不能缺失")
    for column in _INTEGER_COLUMNS:
        if rules[column].lt(1).any() or rules[column].mod(1).ne(0).any():
            raise ValueError(f"有效日期交易规则 {column} 必须是正整数")
        rules[column] = rules[column].astype("int64")
    invalid_limits = (
        (rules["has_price_limit"] & (rules["price_limit_pct"].isna() | rules["price_limit_pct"].le(0) | rules["price_limit_pct"].gt(1)))
        | (~rules["has_price_limit"] & rules["price_limit_pct"].notna())
    )
    if invalid_limits.any():
        raise ValueError("有效日期交易规则涨跌幅阈值与 has_price_limit 不一致")
    _validate_source_review(rules, "有效日期交易规则")
    _validate_non_overlapping(rules, ["symbol"], "有效日期交易规则")
    return rules.sort_values(["symbol", "valid_from"], kind="stable").reset_index(drop=True)


def attach_market_rule_schedule(bars: pd.DataFrame, schedule: pd.DataFrame) -> pd.DataFrame:
    """Apply one and only one reviewed rule row to every input security-day.

    Existing rule/status fields on bars are deliberately overwritten. This avoids
    accidentally mixing provider status columns with the audited schedule.
    """
    rules = normalize_market_rule_schedule(schedule)
    required = {"trade_date", "symbol"}
    if not required.issubset(bars.columns):
        raise ValueError("行情缺少 trade_date/symbol，无法连接交易规则")
    rule_columns_to_replace = set(MARKET_RULE_VALUE_COLUMNS) | {
        "market_rule_source", "market_rule_source_sha256", "market_rule_valid_from", "market_rule_valid_to",
    }
    left = bars.drop(columns=[column for column in rule_columns_to_replace if column in bars.columns]).copy()
    left["__bar_order"] = np.arange(len(left), dtype=np.int64)
    left["__join_date"] = pd.to_datetime(left["trade_date"], errors="coerce").dt.normalize()
    left["symbol"] = left["symbol"].astype(str).str.strip().str.upper()
    if left["__join_date"].isna().any() or left["symbol"].eq("").any():
        raise ValueError("行情 trade_date/symbol 含无效值，无法连接交易规则")

    right = rules.rename(columns={
        "valid_from": "market_rule_valid_from",
        "valid_to": "market_rule_valid_to",
        "source": "market_rule_source",
        "source_sha256": "market_rule_source_sha256",
    }).copy()
    right = right.rename(columns={column: f"__rule_{column}" for column in MARKET_RULE_VALUE_COLUMNS if column not in {"source", "source_sha256"}})
    right = right.rename(columns={
        "market_rule_valid_from": "__rule_valid_from",
        "market_rule_valid_to": "__rule_valid_to",
    })
    left_sorted = left.sort_values(["__join_date", "symbol"], kind="stable")
    right_sorted = right.sort_values(["__rule_valid_from", "symbol"], kind="stable")
    joined = pd.merge_asof(
        left_sorted,
        right_sorted,
        left_on="__join_date",
        right_on="__rule_valid_from",
        by="symbol",
        direction="backward",
        allow_exact_matches=True,
    )
    covered = joined["__rule_valid_from"].notna()
    covered &= joined["__rule_valid_to"].isna() | joined["__rule_valid_to"].ge(joined["__join_date"])
    if not covered.all():
        missing = joined.loc[~covered, ["trade_date", "symbol"]].head(20)
        examples = ", ".join(f"{row.symbol}@{row.trade_date}" for row in missing.itertuples(index=False))
        raise ValueError(f"有效日期交易规则未覆盖全部行情证券日，示例：{examples}")

    result = joined.sort_values("__bar_order", kind="stable").drop(columns=["__bar_order", "__join_date"])
    result["board"] = result["__rule_board"]
    for column in _BOOL_COLUMNS:
        result[column] = result[f"__rule_{column}"].astype("boolean")
    for column in _INTEGER_COLUMNS:
        result[column] = result[f"__rule_{column}"].astype("int64")
    result["price_limit_pct"] = result["__rule_price_limit_pct"]
    result["market_rule_source"] = result["market_rule_source"]
    result["market_rule_source_sha256"] = result["market_rule_source_sha256"]
    result["market_rule_valid_from"] = result["__rule_valid_from"]
    result["market_rule_valid_to"] = result["__rule_valid_to"]
    result = result.drop(columns=[
        column for column in result.columns
        if column.startswith("__rule_")
    ])
    return result
