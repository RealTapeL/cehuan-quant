from __future__ import annotations

import pandas as pd

from cehuan.data.canonical import canonical_symbol, normalize_bar_frame, parse_optional_bool


MASTER_REQUIRED = [
    "symbol", "board", "valid_from", "valid_to", "is_listed", "is_suspended", "is_st",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares", "source",
]


def apply_security_master(bars: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """Apply effective-dated listing/status data without looking into the future."""
    data = normalize_bar_frame(bars)
    records = master.copy()
    records.columns = [str(column).strip() for column in records.columns]
    missing = [column for column in MASTER_REQUIRED if column not in records.columns]
    if missing:
        raise ValueError("证券主数据缺少字段：" + ", ".join(missing))
    records["symbol"] = records["symbol"].map(canonical_symbol)
    records["valid_from"] = pd.to_datetime(records["valid_from"], errors="coerce").dt.normalize()
    valid_to_input = records["valid_to"].replace("", pd.NA)
    records["valid_to"] = pd.to_datetime(valid_to_input, errors="coerce").dt.normalize()
    if records["valid_from"].isna().any():
        raise ValueError("证券主数据 valid_from 不能为空且必须是有效日期")
    invalid_valid_to = valid_to_input.notna() & records["valid_to"].isna()
    if invalid_valid_to.any():
        raise ValueError("证券主数据 valid_to 必须为空或有效日期")
    invalid_window = records["valid_to"].notna() & (records["valid_to"] < records["valid_from"])
    if invalid_window.any():
        raise ValueError("证券主数据 valid_to 不能早于 valid_from")
    records["board"] = records["board"].astype("string").str.strip()
    records["source"] = records["source"].astype("string").str.strip()
    if records["board"].isna().any() or records["board"].eq("").any():
        raise ValueError("证券主数据 board 不能为空")
    if records["source"].isna().any() or records["source"].eq("").any():
        raise ValueError("证券主数据 source 不能为空")
    for column in ("has_price_limit", "is_listed", "is_suspended", "is_st"):
        records[column] = parse_optional_bool(records[column])
    for column in ("price_limit_pct", "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares"):
        records[column] = pd.to_numeric(records[column], errors="coerce")
    invalid_pct = records["price_limit_pct"].notna() & ~records["price_limit_pct"].between(0, 1)
    if invalid_pct.any():
        raise ValueError("证券主数据 price_limit_pct 应使用 0 到 1 之间的小数")
    if "name" not in records:
        records["name"] = pd.NA
    for column in ("buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares"):
        invalid = records[column].notna() & ((records[column] < 1) | (records[column] % 1 != 0))
        if invalid.any():
            raise ValueError(f"证券主数据 {column} 必须是正整数")

    for symbol, group in records.groupby("symbol", sort=False):
        ordered = group.sort_values("valid_from").reset_index(drop=True)
        for index in range(1, len(ordered)):
            previous_end = ordered.loc[index - 1, "valid_to"]
            current_start = ordered.loc[index, "valid_from"]
            if pd.isna(previous_end) or current_start <= previous_end:
                raise ValueError(f"{symbol} 的证券主数据有效区间重叠")

    source = records.set_index("symbol", drop=False)
    for index, row in data.iterrows():
        candidates = source.loc[[row["symbol"]]] if row["symbol"] in source.index else None
        if candidates is None:
            continue
        day = pd.Timestamp(row["trade_date"])
        matches = candidates.loc[
            (candidates["valid_from"] <= day)
            & (candidates["valid_to"].isna() | (candidates["valid_to"] >= day))
        ]
        if len(matches) > 1:
            raise ValueError(f"{row['symbol']} {day} 匹配到多条证券主数据")
        if matches.empty:
            continue
        status = matches.iloc[0]
        values = {
            "is_listed": status["is_listed"],
            "is_suspended": status["is_suspended"],
            "is_st": status["is_st"],
            "has_price_limit": status["has_price_limit"],
            "price_limit_pct": status["price_limit_pct"],
            "buy_min_shares": status["buy_min_shares"],
            "buy_step_shares": status["buy_step_shares"],
            "sell_min_shares": status["sell_min_shares"],
            "sell_step_shares": status["sell_step_shares"],
            "name": status["name"],
            "board": status["board"],
            "market_rule_source": status["source"],
            "market_rule_valid_from": status["valid_from"],
            "market_rule_valid_to": status["valid_to"],
        }
        for column, value in values.items():
            if not pd.isna(value):
                data.at[index, column] = value
    return normalize_bar_frame(data)
