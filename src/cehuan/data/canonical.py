"""策环行情表的代码、日期、字段和 dtype 规范。

数据适配器在此边界之后应转换成统一列；这里不判断来源可信度或历史状态
是否完整。行情成交量单位为股、成交额单位为人民币元，缺字段保留为空或
按必需字段校验失败，不以默认值填补。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import numpy as np


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
    """将常见 A 股代码格式规范为项目使用的交易所前缀形式。

    ``value`` 可为带交易所后缀、前缀或纯六位代码；返回规范代码。无法唯一识别
    交易所时不擅自补前缀；指数与股票代码冲突时保留可确认的显式前缀。
    """
    symbol = str(value).strip().upper().replace(" ", "")

    def inferred_exchange(code: str) -> str | None:
        """按代码前缀推测常见沪深京市场；无法唯一判断时返回空值。"""
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
    """把已知布尔写法映射为 nullable boolean，未知写法保留为 NA。"""
    true_values = {True, 1, "1", "true", "True", "TRUE", "是", "Y", "y"}
    false_values = {False, 0, "0", "false", "False", "FALSE", "否", "N", "n"}

    def parse(value: object) -> object:
        """解析单个状态值；不认识的值不猜测为真或假。"""
        if pd.isna(value) or value == "":
            return pd.NA
        if value in true_values:
            return True
        if value in false_values:
            return False
        return pd.NA

    return series.map(parse).astype("boolean")


def _map_categorical_values(series: pd.Series, mapper) -> pd.Series:
    """对类别列只映射唯一类别，以降低大行情表规范化的内存用量。"""
    categorical = series.astype("category")
    mapped = [mapper(value) for value in categorical.cat.categories]
    if len(set(mapped)) != len(mapped):
        return series.map(mapper)
    return pd.Series(
        pd.Categorical.from_codes(
            categorical.cat.codes,
            categories=mapped,
            ordered=categorical.cat.ordered,
        ),
        index=series.index,
        name=series.name,
    )


def _parse_categorical_bool(series: pd.Series) -> pd.Series:
    """按类别码解析布尔状态，未知类别保持 nullable boolean 缺值。"""
    categorical = series.astype("category")
    parsed = [parse_optional_bool(pd.Series([value])).iloc[0] for value in categorical.cat.categories]
    codes = categorical.cat.codes.to_numpy(copy=True)
    remap = np.full(len(parsed), -1, dtype=np.int8)
    for index, value in enumerate(parsed):
        if not pd.isna(value):
            remap[index] = int(bool(value))
    valid = codes >= 0
    output_codes = np.full(len(codes), -1, dtype=np.int8)
    output_codes[valid] = remap[codes[valid]]
    values = pd.Categorical.from_codes(output_codes, categories=[False, True])
    return pd.Series(values, index=series.index, name=series.name).astype("boolean")


def _parse_categorical_date(series: pd.Series) -> pd.Series:
    """按类别码解析日期并重建有序日期类别，坏值映射为缺值码。"""
    categorical = series.astype("category")
    parsed = pd.to_datetime(pd.Series(categorical.cat.categories), errors="coerce").dt.date.tolist()
    categories = sorted({value for value in parsed if value is not pd.NaT and not pd.isna(value)})
    remap = {value: index for index, value in enumerate(categories)}
    category_codes = np.asarray([remap.get(value, -1) for value in parsed], dtype=np.int32)
    old_codes = categorical.cat.codes.to_numpy(copy=False)
    codes = np.full(len(old_codes), -1, dtype=np.int32)
    valid = old_codes >= 0
    codes[valid] = category_codes[old_codes[valid]]
    return pd.Series(
        pd.Categorical.from_codes(codes, categories=categories, ordered=True),
        index=series.index,
        name=series.name,
    )


def normalize_bar_frame(
    frame: pd.DataFrame,
    *,
    copy_frame: bool = True,
    compact: bool = False,
    assume_sorted: bool = False,
) -> pd.DataFrame:
    """将行情数据规范为统一列、代码、日期和安全 dtype。

    参数控制是否复制、内存紧凑化及是否信任输入排序；返回规范行情表。必需字段、
    无效日期或不合法价格不会被默认值掩盖，具体校验失败时抛出异常。
    """
    data = frame.copy() if copy_frame else frame
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
    if compact and isinstance(data["trade_date"].dtype, pd.CategoricalDtype):
        data["trade_date"] = _parse_categorical_date(data["trade_date"])
    else:
        data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.date
        if compact:
            data["trade_date"] = data["trade_date"].astype("category")
    if compact and isinstance(data["symbol"].dtype, pd.CategoricalDtype):
        data["symbol"] = _map_categorical_values(data["symbol"], canonical_symbol)
    else:
        data["symbol"] = data["symbol"].map(canonical_symbol).astype("string")
        if compact:
            data["symbol"] = data["symbol"].astype("category")
    for column in PRICE_COLUMNS + [
        "volume_shares", "amount_cny", "price_limit_pct", "buy_min_shares",
        "buy_step_shares", "sell_min_shares", "sell_step_shares",
    ]:
        data[column] = pd.to_numeric(data[column], errors="coerce")
    for column in ("is_listed", "is_suspended", "is_st", "has_price_limit"):
        if compact and isinstance(data[column].dtype, pd.CategoricalDtype):
            data[column] = _parse_categorical_bool(data[column])
        else:
            data[column] = parse_optional_bool(data[column])
    if compact:
        for column in ("board", "market_rule_source", "name", "data_source", "retrieved_at_utc", "available_at"):
            data[column] = data[column].astype("category")
    else:
        data["board"] = data["board"].astype("string")
        data["market_rule_source"] = data["market_rule_source"].astype("string")
    for column in ("market_rule_valid_from", "market_rule_valid_to"):
        if compact and isinstance(data[column].dtype, pd.CategoricalDtype):
            data[column] = _parse_categorical_date(data[column])
        else:
            data[column] = pd.to_datetime(data[column], errors="coerce").dt.date
            if compact:
                data[column] = data[column].astype("category")
    data = data[BAR_COLUMNS]
    if not assume_sorted:
        data = data.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    return data


def read_bars(path: str | Path, *, compact: bool = False) -> pd.DataFrame:
    """从 CSV 读取行情并规范列名、代码、单位字段及日期类型。

    ``compact=False`` 采用易操作的常规 dtype；为批量历史数据启用
    ``compact=True`` 时使用类别列节省内存。文件 I/O 或必需字段错误会抛出。
    """
    if not compact:
        return normalize_bar_frame(pd.read_csv(path))
    dtype: dict[str, object] = {
        column: "category"
        for column in (
            "trade_date", "symbol", "name", "board", "market_rule_source",
            "market_rule_valid_from", "market_rule_valid_to", "data_source",
            "retrieved_at_utc", "available_at",
        )
    }
    dtype.update({column: "category" for column in ("is_listed", "is_suspended", "is_st", "has_price_limit")})
    frame = pd.read_csv(path, dtype=dtype)
    return normalize_bar_frame(frame, copy_frame=False, compact=True, assume_sorted=True)


def write_bars(frame: pd.DataFrame, path: str | Path) -> Path:
    """将行情按规范列顺序写入 CSV 并返回目标路径。

    输入先经 ``normalize_bar_frame`` 处理；函数会创建父目录并写文件，
    不负责为结果附加来源证明或 SHA-256 清单。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalize_bar_frame(frame).to_csv(target, index=False, date_format="%Y-%m-%d")
    return target
