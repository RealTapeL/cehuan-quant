from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.canonical import canonical_symbol, normalize_bar_frame


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
TENCENT_FIELD_MAP = {
    "date": "trade_date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume_shares",
    "amount": "amount_cny",
}


def _complete_daily_history(
    out: pd.DataFrame,
    symbol: str,
    name: str | None,
    retrieved_at_utc: str,
    data_source: str,
) -> pd.DataFrame:
    out["symbol"] = canonical_symbol(symbol)
    out["name"] = name
    # Daily bar endpoints do not supply historical security-status facts.
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
    out["symbol"] = canonical_symbol(symbol)
    out["volume_shares"] = pd.to_numeric(out.pop("volume_lots"), errors="coerce") * 100
    if adjusted is not None and not adjusted.empty and {"日期", "收盘"}.issubset(adjusted.columns):
        adj = adjusted[["日期", "收盘"]].rename(columns={"日期": "trade_date", "收盘": "adjusted_close"})
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.merge(adj, on="trade_date", how="left", validate="one_to_one")
    else:
        out["adjusted_close"] = pd.NA
    return _complete_daily_history(out, symbol, name, retrieved_at_utc, data_source)


def normalize_tencent_history(
    raw: pd.DataFrame,
    adjusted: pd.DataFrame | None,
    symbol: str,
    name: str | None,
    retrieved_at_utc: str,
    data_source: str = "akshare.stock_zh_a_hist_tx",
) -> pd.DataFrame:
    """Normalize Tencent daily bars; its volume field is already in shares."""
    missing = set(TENCENT_FIELD_MAP).difference(raw.columns)
    if missing:
        raise RuntimeError(f"腾讯行情返回字段变化，缺少：{', '.join(sorted(missing))}")
    out = raw.rename(columns=TENCENT_FIELD_MAP).copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["volume_shares"] = pd.to_numeric(out["volume_shares"], errors="coerce")
    if adjusted is not None and not adjusted.empty and {"date", "close"}.issubset(adjusted.columns):
        adj = adjusted[["date", "close"]].rename(columns={"date": "trade_date", "close": "adjusted_close"})
        adj["trade_date"] = pd.to_datetime(adj["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        out = out.merge(adj, on="trade_date", how="left", validate="one_to_one")
    else:
        out["adjusted_close"] = pd.NA
    return _complete_daily_history(out, symbol, name, retrieved_at_utc, data_source)


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


class AKShareTencentProvider:
    """AKShare adapter for Tencent daily history, kept separate from Eastmoney."""

    name = "akshare.stock_zh_a_hist_tx"

    def fetch_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        name: str | None = None,
        timeout: float = 20.0,
    ) -> pd.DataFrame:
        canonical = canonical_symbol(symbol)
        if len(canonical) != 8 or canonical[:2] not in {"SH", "SZ", "BJ"}:
            raise ValueError("A 股代码需为可映射的 6 位沪深京代码")
        try:
            import akshare as ak
        except ImportError as exc:
            raise RuntimeError("缺少 AKShare，请先运行 uv sync") from exc

        tencent_symbol = canonical[:2].lower() + canonical[2:]
        raw = ak.stock_zh_a_hist_tx(
            symbol=tencent_symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="",
            timeout=timeout,
        )
        adjusted = ak.stock_zh_a_hist_tx(
            symbol=tencent_symbol,
            start_date=start_date,
            end_date=end_date,
            adjust="hfq",
            timeout=timeout,
        )
        if raw is None or raw.empty:
            raise RuntimeError(f"腾讯行情未返回 {canonical} 的日线数据")
        return normalize_tencent_history(
            raw,
            adjusted,
            symbol=canonical,
            name=name,
            retrieved_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            data_source=self.name,
        )


def make_akshare_provider(source: str) -> AKShareProvider | AKShareTencentProvider:
    providers = {
        "akshare": AKShareProvider,
        "eastmoney": AKShareProvider,
        "tencent": AKShareTencentProvider,
    }
    try:
        return providers[source.lower()]()
    except KeyError as exc:
        raise ValueError(f"未知 AKShare 行情来源 {source!r}；可选 eastmoney 或 tencent") from exc
