from __future__ import annotations

from pathlib import Path

import pandas as pd

from cehuan.data.adjustment import select_signal_prices
from cehuan.data.canonical import normalize_bar_frame


def export_qlib_frame(frame: pd.DataFrame, target: str | Path) -> Path:
    """Write a Qlib StaticDataLoader-compatible Parquet snapshot.

    The MultiIndex follows Qlib's documented (datetime, instrument) layout.
    ``factor`` is the ratio of post-adjusted to raw close; execution fields stay
    available in canonical data and are not used as live order prices.
    """
    data = normalize_bar_frame(frame)
    signal_prices, _ = select_signal_prices(data)
    factor = signal_prices.div(data["close"]).where(data["close"] > 0)
    qlib = pd.DataFrame({
        "open": data["open"] * factor,
        "close": signal_prices,
        "high": data["high"] * factor,
        "low": data["low"] * factor,
        "volume": data["volume_shares"].div(factor),
        "factor": factor,
    })
    qlib.index = pd.MultiIndex.from_arrays(
        [pd.to_datetime(data["trade_date"]), data["symbol"].astype(str)],
        names=["datetime", "instrument"],
    )
    qlib = qlib.sort_index()
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    qlib.to_parquet(path)
    return path


def qlib_static_loader_check(frame: pd.DataFrame, output_path: str | Path) -> dict[str, object]:
    try:
        import qlib
        from qlib.data.dataset.loader import StaticDataLoader
    except ImportError as exc:
        raise RuntimeError("Qlib 尚未安装；运行 uv sync --extra qlib") from exc
    path = export_qlib_frame(frame, output_path)
    loaded = StaticDataLoader(str(path)).load()
    if not isinstance(loaded.index, pd.MultiIndex) or list(loaded.index.names) != ["datetime", "instrument"]:
        raise RuntimeError("Qlib StaticDataLoader 未返回预期的 datetime/instrument 索引")
    return {
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "data_path": str(path),
        "rows": int(len(loaded)),
        "columns": list(loaded.columns),
        "index_names": list(loaded.index.names),
        "start": str(loaded.index.get_level_values("datetime").min().date()),
        "end": str(loaded.index.get_level_values("datetime").max().date()),
        "validation": "Qlib StaticDataLoader Parquet read passed; no model was trained.",
    }


def calculate_qlib_momentum(loaded: pd.DataFrame, lookback_sessions: int) -> pd.DataFrame:
    """Calculate per-instrument momentum and per-date ranking on Qlib's MultiIndex data."""
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    if not isinstance(loaded.index, pd.MultiIndex) or list(loaded.index.names) != ["datetime", "instrument"]:
        raise ValueError("输入需要 Qlib datetime/instrument 多级索引")
    if "close" not in loaded.columns:
        raise ValueError("Qlib 数据缺少 close 特征")
    result = loaded[["close"]].sort_index().copy()
    result["momentum"] = result.groupby(level="instrument", sort=False)["close"].transform(
        lambda values: values.div(values.shift(lookback_sessions)).sub(1.0)
    )
    result["rank"] = result.groupby(level="datetime")["momentum"].rank(
        ascending=False, method="first"
    )
    return result


def qlib_momentum_signals(
    frame: pd.DataFrame,
    data_path: str | Path,
    output_path: str | Path,
    lookback_sessions: int,
) -> dict[str, object]:
    """Load the project snapshot through Qlib and calculate a point-in-time rank feature."""
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    try:
        import qlib
        from qlib.data.dataset.loader import StaticDataLoader
    except ImportError as exc:
        raise RuntimeError("Qlib 尚未安装；运行 uv sync --extra qlib") from exc
    parquet_path = export_qlib_frame(frame, data_path)
    loaded = StaticDataLoader(str(parquet_path)).load().sort_index()
    try:
        result = calculate_qlib_momentum(loaded, lookback_sessions).reset_index()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(target, index=False)
    return {
        "qlib_version": getattr(qlib, "__version__", "unknown"),
        "data_path": str(parquet_path),
        "signals_path": str(target),
        "rows": int(len(result)),
        "non_null_momentum_rows": int(result["momentum"].notna().sum()),
        "lookback_sessions": lookback_sessions,
        "available_after": "each trade_date close; earliest execution is the next exchange session",
        "validation": "Qlib StaticDataLoader read and momentum rank calculation passed; feature output is not a trading order.",
    }
