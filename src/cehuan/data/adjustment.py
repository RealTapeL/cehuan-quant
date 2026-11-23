"""选择信号价格并追踪复权口径。

本模块服务于特征和排名流程，只在每行已有的原始收盘价与复权收盘价间
选择价格来源；它不补造复权因子，也不把混合口径窗口解释为有效收益。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


ADJUSTED_SOURCE = "adjusted_close"
RAW_SOURCE = "raw_close_no_adjustment"
INVALID_SOURCE = "unusable_mixed_or_nonpositive_adjustment"
MIXED_MODE = "point_in_time_mixed_adjustment"


def signal_price_sources(frame: pd.DataFrame) -> pd.Series:
    """逐行标记信号价格来源，不读取同一证券后续行来改变历史判断。

    ``frame`` 至少包含证券、原始收盘价和复权收盘价；返回与输入索引对齐的来源标签。
    无效或非正价格标记为不可用，不据此推定价格口径正确。
    """
    required = {"symbol", "close", "adjusted_close"}
    if not required.issubset(frame.columns):
        raise ValueError("信号价格选择需要 symbol/close/adjusted_close 字段")
    adjusted_raw = frame["adjusted_close"]
    adjusted = pd.to_numeric(adjusted_raw, errors="coerce")
    raw = pd.to_numeric(frame["close"], errors="coerce")
    adjusted_valid = adjusted.notna() & np.isfinite(adjusted) & adjusted.gt(0)
    adjusted_absent = adjusted_raw.isna() | adjusted_raw.astype("string").str.strip().eq("").fillna(False)
    raw_valid = raw.notna() & np.isfinite(raw) & raw.gt(0)
    source = pd.Series(INVALID_SOURCE, index=frame.index, dtype="string", name="signal_price_source")
    source.loc[adjusted_valid] = ADJUSTED_SOURCE
    source.loc[adjusted_absent & raw_valid] = RAW_SOURCE
    return source


def select_signal_prices(frame: pd.DataFrame) -> tuple[pd.Series, dict[str, str]]:
    """逐行选择可用价格，并汇总每只证券的价格口径。

    后续行缺少复权值不会回改早先选择。某行缺少复权价时使用该行原始收盘价并标记
    ``raw_close_no_adjustment``；动量窗口必须拒绝混用该标签与复权价。非空但非正的
    复权值标为无效，不会悄悄回退到原始收盘价。返回价格序列和证券到口径标签的映射。
    """
    sources = signal_price_sources(frame)
    adjusted = pd.to_numeric(frame["adjusted_close"], errors="coerce")
    raw = pd.to_numeric(frame["close"], errors="coerce")
    selected = pd.Series(float("nan"), index=frame.index, dtype="float64", name="signal_price")
    selected.loc[sources.eq(ADJUSTED_SOURCE)] = adjusted.loc[sources.eq(ADJUSTED_SOURCE)].astype(float)
    selected.loc[sources.eq(RAW_SOURCE)] = raw.loc[sources.eq(RAW_SOURCE)].astype(float)

    modes: dict[str, str] = {}
    for symbol, group in frame.groupby("symbol", sort=False, dropna=False, observed=True):
        row_sources = sources.loc[group.index]
        unique_sources = set(row_sources.astype(str))
        if unique_sources == {ADJUSTED_SOURCE}:
            mode = ADJUSTED_SOURCE
        elif unique_sources == {RAW_SOURCE}:
            mode = RAW_SOURCE
        elif INVALID_SOURCE in unique_sources:
            mode = INVALID_SOURCE
        else:
            mode = MIXED_MODE
        modes[str(symbol)] = mode
    return selected, modes
