from __future__ import annotations

from pathlib import Path

import pandas as pd


BAR_COLUMNS = [
    "trade_date",
    "symbol",
    "name",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume_shares",
    "amount_cny",
    "is_listed",
    "is_suspended",
    "is_st",
    "has_price_limit",
    "price_limit_pct",
    "buy_min_shares",
    "buy_step_shares",
    "sell_min_shares",
    "sell_step_shares",
    "board",
    "market_rule_source",
    "market_rule_valid_from",
    "market_rule_valid_to",
    "data_source",
    "retrieved_at_utc",
    "available_at",
]
PRICE_COLUMNS = ["open", "high", "low", "close", "adjusted_close"]
REQUIRED_BAR_COLUMNS = ["trade_date", "symbol", "open", "high", "low", "close", "volume_shares"]


def canonical_symbol(value: object) -> str:
    """Normalize common A-share code forms to Qlib-style exchange prefixes."""
    symbol = str(value).strip().upper().replace(" ", "")

    def inferred_exchange(code: str) -> str | None:
        if not code.isdigit() or len(code) != 6:
            return None
        if code.startswith("6"):
            return "SH"
        if code.startswith(("0", "3")):
            return "SZ"
        if code.startswith(("4", "8", "92")):
            return "BJ"
        return None

    if symbol.startswith(("SH", "SZ", "BJ")) and len(symbol) == 8 and symbol[2:].isdigit():
        # Shanghai equity indices use 000xxx codes, which collide with SZ stock
        # codes when the exchange prefix is ignored (e.g. SH000001 vs SZ000001).
        if inferred_exchange(symbol[2:]) == symbol[:2] or (symbol[:2] == "SH" and symbol[2:].startswith("000")):
            return symbol
        return symbol[2:]
    if "." in symbol:
        code, suffix = symbol.rsplit(".", 1)
        exchange = {"SH": "SH", "SS": "SH", "SZ": "SZ", "BJ": "BJ"}.get(suffix)
        if exchange and code.isdigit() and len(code) == 6:
            return f"{exchange}{code}" if inferred_exchange(code) == exchange else code
    if len(symbol) == 6 and symbol.isdigit():
        exchange = inferred_exchange(symbol)
        return f"{exchange}{symbol}" if exchange else symbol
    return symbol


def parse_optional_bool(series: pd.Series) -> pd.Series:
    true_values = {True, 1, "1", "true", "True", "TRUE", "是", "Y", "y"}
    false_values = {False, 0, "0", "false", "False", "FALSE", "否", "N", "n"}

    def parse(value: object) -> object:
        if pd.isna(value) or value == "":
            return pd.NA
        if value in true_values:
            return True
        if value in false_values:
            return False
        return pd.NA

    return series.map(parse).astype("boolean")


def normalize_bar_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize project CSV data to the canonical schema and safe dtypes."""
    data = frame.copy()
    data.columns = [str(column).strip() for column in data.columns]
    if "date" in data.columns and "trade_date" not in data.columns:
        data = data.rename(columns={"date": "trade_date"})
    if "volume" in data.columns and "volume_shares" not in data.columns:
        data = data.rename(columns={"volume": "volume_shares"})
    missing = [column for column in REQUIRED_BAR_COLUMNS if column not in data.columns]
    if missing:
        raise ValueError(f"缺少规范行情字段：{', '.join(missing)}")
    for column in BAR_COLUMNS:
        if column not in data.columns:
            data[column] = pd.NA
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.date
    data["symbol"] = data["symbol"].map(canonical_symbol).astype("string")
    for column in PRICE_COLUMNS + [
        "volume_shares", "amount_cny", "price_limit_pct", "buy_min_shares",
        "buy_step_shares", "sell_min_shares", "sell_step_shares",
    ]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data["is_listed"] = parse_optional_bool(data["is_listed"])
    data["is_suspended"] = parse_optional_bool(data["is_suspended"])
    data["is_st"] = parse_optional_bool(data["is_st"])
    data["has_price_limit"] = parse_optional_bool(data["has_price_limit"])
    data["board"] = data["board"].astype("string")
    data["market_rule_source"] = data["market_rule_source"].astype("string")
    for column in ("market_rule_valid_from", "market_rule_valid_to"):
        data[column] = pd.to_datetime(data[column], errors="coerce").dt.date
    data = data[BAR_COLUMNS].sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    return data


def read_bars(path: str | Path) -> pd.DataFrame:
    return normalize_bar_frame(pd.read_csv(path))


def write_bars(frame: pd.DataFrame, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalize_bar_frame(frame).to_csv(target, index=False, date_format="%Y-%m-%d")
    return target
