from __future__ import annotations

import numpy as np
import pandas as pd


ADJUSTED_SOURCE = "adjusted_close"
RAW_SOURCE = "raw_close_no_adjustment"
INVALID_SOURCE = "unusable_mixed_or_nonpositive_adjustment"
MIXED_MODE = "point_in_time_mixed_adjustment"


def signal_price_sources(frame: pd.DataFrame) -> pd.Series:
    """Classify each row without looking at later rows in the same security."""
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
    """Select each row's available basis; signal windows enforce a single basis.

    A later missing adjustment value cannot change an earlier selected price.
    Missing adjustment is represented by that row's raw close and tagged as
    ``raw_close_no_adjustment``. Momentum callers must reject windows that mix
    this tag with adjusted prices. Non-positive, non-null adjusted values are
    treated as invalid rather than silently replaced with raw close.
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
