from __future__ import annotations

import pandas as pd


def select_signal_prices(frame: pd.DataFrame) -> tuple[pd.Series, dict[str, str]]:
    """Choose one internally consistent price series per security.

    A security uses adjusted close only when every row has a positive adjusted
    price. Fully absent adjustment data falls back to raw close. Partial or
    non-positive adjustment data makes that security's signal prices unknown;
    mixing adjusted and raw prices inside one return series is not allowed.
    """
    required = {"symbol", "close", "adjusted_close"}
    if not required.issubset(frame.columns):
        raise ValueError("信号价格选择需要 symbol/close/adjusted_close 字段")
    selected = pd.Series(float("nan"), index=frame.index, dtype="float64", name="signal_price")
    modes: dict[str, str] = {}
    for symbol, group in frame.groupby("symbol", sort=False, dropna=False):
        indices = group.index
        adjusted = pd.to_numeric(group["adjusted_close"], errors="coerce")
        raw = pd.to_numeric(group["close"], errors="coerce")
        if adjusted.isna().all():
            selected.loc[indices] = raw
            modes[str(symbol)] = "raw_close_no_adjustment"
        elif adjusted.notna().all() and (adjusted > 0).all():
            selected.loc[indices] = adjusted
            modes[str(symbol)] = "adjusted_close"
        else:
            modes[str(symbol)] = "unusable_mixed_or_nonpositive_adjustment"
    return selected, modes
