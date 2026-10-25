from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.canonical import normalize_bar_frame


AKSHARE_FIELD_MAP = {
    "日期": "trade_date",
    "股票代码": "symbol",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume_lots",
    "成交额": "amount_cny",
}


def normalize_akshare_history(
    raw: pd.DataFrame,
    adjusted: pd.DataFrame | None,
    symbol: str,
    name: str | None,
    retrieved_at_utc: str,
    data_source: str = "akshare.stock_zh_a_hist",
) -> pd.DataFrame:
    """Convert documented Chinese AKShare columns to the stable project schema."""
    unexpected = set(AKSHARE_FIELD_MAP).difference(raw.columns)
    if unexpected:
        raise RuntimeError(f"AKShare 返回字段变化，缺少：{', '.join(sorted(unexpected))}")
    out = raw.rename(columns=AKSHARE_FIELD_MAP).copy()
    out["symbol"] = str(symbol).zfill(6)
    out["volume_shares"] = pd.to_numeric(out.pop("volume_lots"), errors="coerce") * 100
    if adjusted is not None and not adjusted.empty and {"日期", "收盘"}.issubset(adjusted.columns):
        adj = adjusted[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "adjusted_close"})
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.merge(adj, on="trade_date", how="left", validate="one_to_one")
    else:
        out["adjusted_close"] = pd.NA
    out["name"] = name
    # Historical ST/board/trading-status values do not come from this endpoint.
    out["is_listed"] = pd.NA
    out["is_suspended"] = pd.NA
    out["is_st"] = pd.NA
    out["price_limit_pct"] = pd.NA
    out["data_source"] = data_source
    out["retrieved_at_utc"] = retrieved_at_utc
    out["available_at"] = pd.to_datetime(out["trade_date"]).map(
        lambda day: day.replace(hour=15, minute=0).tz_localize(ZoneInfo("Asia/Shanghai")).isoformat()
    )
    return normalize_bar_frame(out)


class AKShareProvider:
    """AKShare adapter for its documented daily A-share history endpoint."""

    name = "akshare.stock_zh_a_hist"

    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        name: str | None = None,
        timeout: float = 20.0,
    ) -> pd.DataFrame:
        if not re.fullmatch(r"\d{6}", symbol):
            raise ValueError("A 股代码需为 6 位数字，例如 600519")
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("缺少 AKShare，请先运行 uv sync") from exc

        raw = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="",
            timeout=timeout,
        )
        adjusted = ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start_date,
            end_date=end_date,
            adjust="hfq",
            timeout=timeout,
        )
        if raw is None or raw.empty:
            raise RuntimeError(f"AKShare 未返回 {symbol} 的日线数据")
        return normalize_akshare_history(
            raw,
            adjusted,
            symbol=symbol,
            name=name,
            retrieved_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            data_source=self.name,
        )
