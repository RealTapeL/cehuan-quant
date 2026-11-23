"""Qlib 静态数据桥接。模块把策环规范行情转换为 Qlib StaticDataLoader 可读的 Parquet 特征，并计算点时动量排名；不会训练模型或生成券商订单。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from cehuan.data.adjustment import (
    ADJUSTED_SOURCE,
    RAW_SOURCE,
    select_signal_prices,
    signal_price_sources,
)
from cehuan.data.canonical import normalize_bar_frame


def export_qlib_frame(frame: pd.DataFrame, target: str | Path) -> Path:
    """导出 Qlib StaticDataLoader 可读取的 Parquet 快照。参数：frame 为规范行情，target 为输出路径。返回：写入后的 Path。副作用：创建父目录并写 Parquet；signal price 按项目复权适配器选择，执行字段不作为订单价格。"""
    data = normalize_bar_frame(frame)
    signal_prices, _ = select_signal_prices(data)
    sources = signal_price_sources(data)
    factor = signal_prices.div(data["close"]).where(data["close"] > 0)
    source_code = sources.map({ADJUSTED_SOURCE: 1, RAW_SOURCE: 2}).fillna(0).astype("int8")
    qlib = pd.DataFrame({
        "open": data["open"] * factor,
        "close": signal_prices,
        "high": data["high"] * factor,
        "low": data["low"] * factor,
        "volume": data["volume_shares"].div(factor),
        "factor": factor,
        "price_source_code": source_code,
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
    """通过 Qlib StaticDataLoader 读取刚导出的快照并检查索引。参数：frame 为规范行情，output_path 为 Parquet 路径。返回：Qlib 版本、区间、行列和校验状态。异常：依赖未安装或索引不符合 datetime/instrument 时抛 RuntimeError。不会训练模型。"""
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
    """在 Qlib 的 datetime/instrument 多级索引上计算动量及逐日排名。参数：loaded 为包含 close 的 Qlib 表，lookback_sessions 为回看开市日数。返回：保留输入列并附 momentum/rank 的 DataFrame；复权来源码不一致或窗口不足时动量为空。异常：索引、特征或窗口参数不合约定时抛 ValueError。"""
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    if not isinstance(loaded.index, pd.MultiIndex) or list(loaded.index.names) != ["datetime", "instrument"]:
        raise ValueError("输入需要 Qlib datetime/instrument 多级索引")
    if "close" not in loaded.columns:
        raise ValueError("Qlib 数据缺少 close 特征")
    columns = ["close"] + (["price_source_code"] if "price_source_code" in loaded.columns else [])
    result = loaded[columns].sort_index().copy()

    def calculate(group: pd.DataFrame) -> pd.Series:
        """对单只 instrument 的时间序列计算连续价格口径的窗口动量。参数：group 为该证券按日期排序的数据。返回：与索引对齐的动量 Series；混合来源码或不完整窗口被掩为缺失。"""
        close = pd.to_numeric(group["close"], errors="coerce")
        source = (
            pd.to_numeric(group["price_source_code"], errors="coerce").fillna(0)
            if "price_source_code" in group.columns
            else pd.Series(1, index=group.index, dtype="int8")
        )
        consistent = pd.Series(True, index=group.index, dtype="bool")
        for offset in range(lookback_sessions + 1):
            consistent &= source.eq(source.shift(offset)).fillna(False) & source.ne(0)
        valid_window = close.notna().rolling(
            lookback_sessions + 1, min_periods=lookback_sessions + 1,
        ).sum().eq(lookback_sessions + 1)
        momentum = close.div(close.shift(lookback_sessions)).sub(1.0)
        return momentum.where(consistent & valid_window)

    momentum = pd.Series(float("nan"), index=result.index, dtype="float64", name="momentum")
    for _, group in result.groupby(level="instrument", sort=False):
        momentum.loc[group.index] = calculate(group)
    result["momentum"] = momentum
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
    """导出行情、调用 Qlib 加载并生成动量特征 CSV。参数：frame 为规范行情，data_path 为 Qlib 快照路径，output_path 为信号文件，lookback_sessions 为回看开市日数。返回：版本、文件、覆盖行数和可用时间说明。副作用：写 Parquet 与 CSV；特征在收盘后可得，最早执行日为下一开市日，不是订单。异常：依赖缺失或特征输入失败时阻断。"""
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
