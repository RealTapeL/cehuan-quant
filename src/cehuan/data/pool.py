"""有限股票池日线拉取辅助函数。

该模块顺序调用注入的数据 provider，不负责构造历史点时股票池；任一证券
请求失败时汇总错误并拒绝返回部分成功数据。
"""

from __future__ import annotations

import pandas as pd
import time

from cehuan.data.canonical import canonical_symbol


def fetch_pool_daily(
    provider: object,
    universe: pd.DataFrame,
    start_date: str,
    end_date: str,
    max_symbols: int = 50,
    delay_seconds: float = 0.0,
) -> pd.DataFrame:
    """顺序获取配置股票池的指定区间日线。

    ``provider`` 提供 ``fetch_daily``，``universe`` 含证券代码与可选名称；
    日期为 ISO 字符串，``max_symbols`` 限制池规模，``delay_seconds`` 控制
    请求间等待。成功返回按日期和代码排序的合并表；任一证券失败、代码无效
    或超出上限时抛出异常，不输出部分结果文件。
    """
    if "symbol" not in universe.columns:
        raise ValueError("股票池 CSV 必须包含 symbol 列")
    if universe.empty:
        raise ValueError("股票池为空；请在模板中填入证券代码")
    if len(universe) > max_symbols:
        raise ValueError(f"股票池有 {len(universe)} 个代码，超过配置上限 {max_symbols}")
    if universe["symbol"].duplicated().any():
        raise ValueError("股票池存在重复代码")
    frames = []
    failures = []
    for position, (_, row) in enumerate(universe.iterrows()):
        if position and delay_seconds > 0:
            time.sleep(delay_seconds)
        canonical = canonical_symbol(row["symbol"])
        if len(canonical) != 8 or canonical[:2] not in {"SH", "SZ", "BJ"}:
            failures.append(f"{row['symbol']}: 不是可识别的沪深京 6 位证券代码")
            continue
        name = row.get("name")
        try:
            result = provider.fetch_daily(canonical[2:], start_date, end_date, name=None if pd.isna(name) else str(name))
            frames.append(result)
        except Exception as exc:  # Keep going to report all missing symbols, but never return partial success.
            failures.append(f"{canonical}: {exc}")
    if failures:
        raise RuntimeError("股票池行情未完整获取，未生成新数据文件：\n" + "\n".join(failures))
    if not frames:
        raise RuntimeError("股票池没有可用的日线数据")
    return pd.concat(frames, ignore_index=True).sort_values(["trade_date", "symbol"]).reset_index(drop=True)
