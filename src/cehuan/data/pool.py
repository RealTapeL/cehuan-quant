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
    """Fetch a configured finite pool sequentially; fail instead of saving partial data."""
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
