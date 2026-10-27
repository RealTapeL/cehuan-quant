from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.canonical import canonical_symbol, normalize_bar_frame, write_bars
from cehuan.data.quality import check_data_quality


HISTORY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST"
)
INDEX_FIELDS = "date,code,open,high,low,close,volume,amount"
SOURCE_NAME = "baostock.query_history_k_data_plus"


def _vendor_code(symbol: object) -> str:
    canonical = canonical_symbol(symbol)
    if len(canonical) != 8 or canonical[:2] not in {"SH", "SZ"}:
        raise ValueError(f"BaoStock 当前适配范围仅验证了沪深代码：{symbol}")
    return f"{canonical[:2].lower()}.{canonical[2:]}"


def _canonical_vendor_symbol(value: object) -> str:
    """BaoStock uses `sh.600519`; project data uses `SH600519`."""
    return canonical_symbol(str(value).strip().replace(".", ""))


def _query_frame(response: Any, endpoint: str) -> pd.DataFrame:
    if str(response.error_code) != "0":
        raise RuntimeError(f"BaoStock {endpoint} 失败：{response.error_code} {response.error_msg}")
    fields = list(response.fields)
    rows: list[list[str]] = []
    while response.next():
        rows.append(response.get_row_data())
    return pd.DataFrame(rows, columns=fields)


class BaoStockSession:
    """Small, explicit BaoStock session. No requests are sent at import time."""

    def __init__(self) -> None:
        self.bs: Any | None = None
        self.package_version: str | None = None
        self.request_log: list[dict[str, Any]] = []

    def __enter__(self) -> "BaoStockSession":
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError("缺少 BaoStock，可运行 `uv sync --extra baostock` 安装可选数据适配器") from exc
        try:
            self.package_version = version("baostock")
        except PackageNotFoundError:
            self.package_version = "unknown"
        self.bs = bs
        login = bs.login()
        if str(login.error_code) != "0":
            self.bs = None
            raise RuntimeError(f"BaoStock 登录失败：{login.error_code} {login.error_msg}")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.bs is not None:
            self.bs.logout()
            self.bs = None

    def _query(self, endpoint: str, call: Any) -> pd.DataFrame:
        if self.bs is None:
            raise RuntimeError("BaoStockSession 尚未登录")
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        timer = time.perf_counter()
        frame = _query_frame(call(), endpoint)
        self.request_log.append({
            "endpoint": endpoint,
            "started_at_utc": started,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.perf_counter() - timer, 3),
            "row_count": int(len(frame)),
            "fields": list(frame.columns),
        })
        return frame

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self._query(
            "query_trade_dates",
            lambda: self.bs.query_trade_dates(start_date=start_date, end_date=end_date),
        )

    def stock_basic(self) -> pd.DataFrame:
        return self._query("query_stock_basic", self.bs.query_stock_basic)

    def history(
        self,
        symbol: object,
        start_date: str,
        end_date: str,
        adjustflag: str,
        fields: str = HISTORY_FIELDS,
    ) -> pd.DataFrame:
        code = _vendor_code(symbol)
        return self._query(
            f"query_history_k_data_plus({code},adjustflag={adjustflag})",
            lambda: self.bs.query_history_k_data_plus(
                code,
                fields,
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag,
            ),
        )

    def adjust_factors(self, symbol: object, start_date: str, end_date: str) -> pd.DataFrame:
        code = _vendor_code(symbol)
        return self._query(
            f"query_adjust_factor({code})",
            lambda: self.bs.query_adjust_factor(code=code, start_date=start_date, end_date=end_date),
        )


def normalize_baostock_history(
    raw: pd.DataFrame,
    back_adjusted: pd.DataFrame,
    security: pd.Series,
    retrieved_at_utc: str,
) -> tuple[pd.DataFrame, int]:
    """Map BaoStock raw fields and point-in-time flags to canonical daily bars.

    `adjustflag=1` is retained as the feature price series; raw OHLC remains the
    execution-price series. Suspended rows keep their carried prices and a zero
    volume/amount when the vendor returns blanks for those fields.
    """
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount", "tradestatus", "isST"}
    missing = required.difference(raw.columns)
    if missing:
        raise RuntimeError(f"BaoStock 日线返回字段变化，缺少：{', '.join(sorted(missing))}")
    if not {"date", "close"}.issubset(back_adjusted.columns):
        raise RuntimeError("BaoStock 后复权日线缺少 date/close 字段")

    adjusted = back_adjusted[["date", "close"]].copy()
    adjusted = adjusted.rename(columns={"close": "adjusted_close"})
    out = raw.copy().merge(adjusted, on="date", how="left", validate="one_to_one")
    out = out.rename(columns={"date": "trade_date"})
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    vendor_symbol = security.get("code", raw["code"].iloc[0] if not raw.empty else "")
    out["symbol"] = _canonical_vendor_symbol(vendor_symbol)
    name = security.get("code_name", pd.NA)
    out["name"] = None if pd.isna(name) else str(name)

    for column in ("open", "high", "low", "close", "adjusted_close", "volume", "amount"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    suspended = out["tradestatus"].astype("string").eq("0").astype("boolean")
    out["is_suspended"] = suspended
    # BaoStock's daily rows carry prior close and may leave turnover fields blank
    # during suspensions. Those blanks are economically zero, not unknown volume.
    out.loc[suspended.fillna(False), ["volume", "amount"]] = out.loc[
        suspended.fillna(False), ["volume", "amount"]
    ].fillna(0)
    out["volume_shares"] = out.pop("volume")
    out["amount_cny"] = out.pop("amount")

    ipo = pd.to_datetime(security.get("ipoDate", pd.NA), errors="coerce")
    out_date = pd.to_datetime(security.get("outDate", pd.NA), errors="coerce")
    dates = pd.to_datetime(out["trade_date"], errors="coerce")
    out["is_listed"] = dates.ge(ipo) & (pd.isna(out_date) | dates.le(out_date)) if pd.notna(ipo) else pd.NA
    st = out["isST"].astype("string").map({"1": True, "0": False})
    out["is_st"] = st.astype("boolean")
    out["has_price_limit"] = pd.NA
    out["price_limit_pct"] = pd.NA
    out["data_source"] = SOURCE_NAME
    out["retrieved_at_utc"] = retrieved_at_utc
    out["available_at"] = pd.to_datetime(out["trade_date"]).map(
        lambda day: day.replace(hour=15, minute=0).tz_localize(ZoneInfo("Asia/Shanghai")).isoformat()
    )

    # Keep only rows with usable raw OHLC. The unmodified vendor responses remain
    # in the snapshot, and their row counts are recorded in the manifest.
    valid_ohlc = out[["open", "high", "low", "close"]].notna().all(axis=1)
    valid_ohlc &= out[["open", "high", "low", "close"]].gt(0).all(axis=1)
    skipped = int((~valid_ohlc).sum())
    return normalize_bar_frame(out.loc[valid_ohlc]), skipped


def normalize_baostock_calendar(raw: pd.DataFrame, retrieved_at_utc: str) -> pd.DataFrame:
    required = {"calendar_date", "is_trading_day"}
    missing = required.difference(raw.columns)
    if missing:
        raise RuntimeError(f"BaoStock 交易日历返回字段变化，缺少：{', '.join(sorted(missing))}")
    normalized = raw.rename(columns={
        "calendar_date": "trade_date",
        "is_trading_day": "is_open",
    }).copy()
    normalized["trade_date"] = pd.to_datetime(normalized["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    normalized["is_open"] = normalized["is_open"].astype("string")
    open_dates = pd.to_datetime(normalized.loc[normalized["is_open"].eq("1"), "trade_date"])
    previous_by_date = dict(zip(
        open_dates.dt.strftime("%Y-%m-%d"),
        open_dates.shift(1).dt.strftime("%Y-%m-%d"),
    ))
    normalized["previous_trade_date"] = normalized["trade_date"].map(previous_by_date)
    normalized["source"] = "baostock.query_trade_dates"
    normalized["retrieved_at_utc"] = retrieved_at_utc
    return normalize_trading_calendar(normalized)


def normalize_baostock_index(
    raw: pd.DataFrame,
    security: pd.Series,
    retrieved_at_utc: str,
) -> pd.DataFrame:
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount"}
    missing = required.difference(raw.columns)
    if missing:
        raise RuntimeError(f"BaoStock 指数日线返回字段变化，缺少：{', '.join(sorted(missing))}")
    out = raw.copy().rename(columns={"date": "trade_date"})
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    out["symbol"] = _canonical_vendor_symbol(security.get("code", out["code"].iloc[0]))
    name = security.get("code_name", pd.NA)
    out["name"] = None if pd.isna(name) else str(name)
    for column in ("open", "high", "low", "close", "volume", "amount"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["adjusted_close"] = out["close"]
    out["volume_shares"] = out.pop("volume")
    out["amount_cny"] = out.pop("amount")
    # This series is stored separately and never enters the tradable universe.
    out["is_listed"] = False
    out["is_suspended"] = False
    out["is_st"] = False
    out["has_price_limit"] = False
    out["data_source"] = SOURCE_NAME
    out["retrieved_at_utc"] = retrieved_at_utc
    out["available_at"] = pd.to_datetime(out["trade_date"]).map(
        lambda day: day.replace(hour=15, minute=0).tz_localize(ZoneInfo("Asia/Shanghai")).isoformat()
    )
    return normalize_bar_frame(out)


def _write_source_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_date(value: str) -> str:
    parsed = pd.to_datetime(value, format="%Y%m%d" if value.isdigit() and len(value) == 8 else None, errors="raise")
    return parsed.strftime("%Y-%m-%d")


def create_baostock_snapshot(
    session: BaoStockSession,
    symbols: list[str],
    start_date: str,
    end_date: str,
    root: str | Path,
    snapshot_id: str | None = None,
    benchmark_symbol: str | None = None,
) -> dict[str, Any]:
    """Fetch an immutable, provenance-rich snapshot into a private local folder."""
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        raise ValueError("start_date 必须不晚于 end_date")
    normalized_symbols = list(dict.fromkeys(canonical_symbol(symbol) for symbol in symbols))
    if not normalized_symbols:
        raise ValueError("至少提供一个证券代码")
    if len(normalized_symbols) > 30:
        raise ValueError("首轮真实数据快照最多支持 30 个代码；扩大股票池前请先审查调用和覆盖")

    created_at = datetime.now(timezone.utc)
    identifier = snapshot_id or f"baostock-{start.replace('-', '')}-{end.replace('-', '')}-{created_at:%Y%m%dT%H%M%SZ}"
    if not identifier or any(part in identifier for part in ("/", "\\", "..")):
        raise ValueError("snapshot_id 只能使用路径安全的名称")
    parent = Path(root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / identifier
    if destination.exists():
        raise FileExistsError(f"快照已存在，拒绝覆盖：{destination}")

    temp_path = Path(tempfile.mkdtemp(prefix=f".{identifier}-", dir=parent))
    raw_dir = temp_path / "raw"
    raw_dir.mkdir()
    try:
        calendar = session.trade_calendar(start, end)
        basics = session.stock_basic()
        if calendar.empty:
            raise RuntimeError("BaoStock 未返回请求区间的交易日历")
        calendar_request = next(
            (entry for entry in session.request_log if entry["endpoint"] == "query_trade_dates"),
            {},
        )
        calendar_normalized = normalize_baostock_calendar(
            calendar,
            str(calendar_request.get("completed_at_utc") or datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        equity_basics = basics.loc[basics["type"].astype(str).eq("1")].copy()
        basic_by_symbol = {
            _canonical_vendor_symbol(row["code"]): row
            for _, row in equity_basics.iterrows()
        }
        other_basics = basics.loc[~basics["type"].astype(str).eq("1")].copy()
        other_basic_by_symbol = {
            _canonical_vendor_symbol(row["code"]): row
            for _, row in other_basics.iterrows()
        }
        missing_symbols = [symbol for symbol in normalized_symbols if symbol not in basic_by_symbol]
        if missing_symbols:
            raise ValueError("以下代码未在 BaoStock 股票基础资料 type=1 中找到：" + ", ".join(missing_symbols))

        _write_source_csv(calendar, raw_dir / "trade_calendar.csv")
        calendar_normalized.to_csv(temp_path / "trading_calendar.csv", index=False, encoding="utf-8")
        _write_source_csv(basics, raw_dir / "stock_basic_all_types.csv")
        chosen_basic = pd.DataFrame([basic_by_symbol[symbol] for symbol in normalized_symbols])
        _write_source_csv(chosen_basic, raw_dir / "selected_stock_basic.csv")

        bars: list[pd.DataFrame] = []
        skipped_rows: dict[str, int] = {}
        for symbol in normalized_symbols:
            safe_symbol = symbol.lower()
            basic = basic_by_symbol[symbol]
            unadjusted = session.history(symbol, start, end, adjustflag="3")
            back_adjusted = session.history(symbol, start, end, adjustflag="1")
            factors = session.adjust_factors(symbol, start, end)
            _write_source_csv(unadjusted, raw_dir / f"{safe_symbol}_daily_unadjusted.csv")
            _write_source_csv(back_adjusted, raw_dir / f"{safe_symbol}_daily_back_adjusted.csv")
            _write_source_csv(factors, raw_dir / f"{safe_symbol}_adjustment_factors.csv")
            retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
            normalized, skipped = normalize_baostock_history(unadjusted, back_adjusted, basic, retrieved)
            if normalized.empty:
                raise RuntimeError(f"BaoStock 未返回 {symbol} 的有效原始 OHLC 行")
            bars.append(normalized)
            skipped_rows[symbol] = skipped

        normalized_benchmark: pd.DataFrame | None = None
        benchmark_quality: dict[str, Any] | None = None
        normalized_benchmark_symbol: str | None = None
        if benchmark_symbol:
            normalized_benchmark_symbol = canonical_symbol(benchmark_symbol)
            benchmark_basic = other_basic_by_symbol.get(normalized_benchmark_symbol)
            if benchmark_basic is None:
                raise ValueError(f"基准 {benchmark_symbol} 未在 BaoStock 非股票基础资料中找到")
            index_raw = session.history(
                normalized_benchmark_symbol,
                start,
                end,
                adjustflag="3",
                fields=INDEX_FIELDS,
            )
            if index_raw.empty:
                raise RuntimeError(f"BaoStock 未返回指数基准 {normalized_benchmark_symbol} 的日线")
            _write_source_csv(index_raw, raw_dir / f"{normalized_benchmark_symbol.lower()}_index_unadjusted.csv")
            normalized_benchmark = normalize_baostock_index(
                index_raw,
                benchmark_basic,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            benchmark_quality = check_data_quality(normalized_benchmark, calendar=calendar_normalized)
            if not benchmark_quality["healthy"]:
                raise ValueError("指数基准质量检查未通过：" + "; ".join(benchmark_quality["errors"]))
            _write_source_csv(normalized_benchmark, temp_path / "benchmark.csv")
            (temp_path / "benchmark_quality.json").write_text(
                json.dumps(benchmark_quality, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        normalized_bars = normalize_bar_frame(pd.concat(bars, ignore_index=True))
        quality = check_data_quality(normalized_bars, calendar=calendar_normalized)
        if not quality["healthy"]:
            raise ValueError("规范行情/交易日历质量检查未通过：" + "; ".join(quality["errors"]))
        bars_path = temp_path / "bars.csv"
        write_bars(normalized_bars, bars_path)
        (temp_path / "quality.json").write_text(
            json.dumps(quality, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        file_hashes = {
            str(path.relative_to(temp_path)): _sha256(path)
            for path in sorted(temp_path.rglob("*"))
            if path.is_file()
        }
        manifest: dict[str, Any] = {
            "snapshot_id": identifier,
            "created_at_utc": created_at.isoformat(timespec="seconds"),
            "manifest_generated_at_utc": generated_at,
            "provider": "BaoStock",
            "provider_package_version": session.package_version,
            "provider_package_metadata": "PyPI package; see docs/data-provider-audit.md for provenance caveats",
            "api_endpoints": ["query_trade_dates", "query_stock_basic", "query_history_k_data_plus", "query_adjust_factor"],
            "date_range": {"start": start, "end": end},
            "universe": {
                "selection": "explicit fixed symbols supplied to the snapshot command; not a complete historical point-in-time universe",
                "symbols": normalized_symbols,
                "security_basic_type_filter": "type=1 (stock)",
            },
            "benchmark": {
                "symbol": normalized_benchmark_symbol,
                "row_count": int(len(normalized_benchmark)) if normalized_benchmark is not None else None,
                "file": "benchmark.csv" if normalized_benchmark is not None else None,
                "price_field": "unadjusted index close; no constituent or dividend return adjustment",
            },
            "coverage": {
                "normalized_bar_rows": int(len(normalized_bars)),
                "symbols_with_bars": int(normalized_bars["symbol"].nunique()),
                "bar_start": str(normalized_bars["trade_date"].min()),
                "bar_end": str(normalized_bars["trade_date"].max()),
                "raw_rows_by_symbol": {
                    symbol: int(len(normalized_bars.loc[normalized_bars["symbol"] == symbol])) + skipped_rows[symbol]
                    for symbol in normalized_symbols
                },
                "skipped_rows_with_invalid_ohlc_by_symbol": skipped_rows,
                "calendar_rows": int(len(calendar)),
                "calendar_fields": list(calendar.columns),
                "open_sessions": int(calendar_normalized["is_open"].sum()),
                "security_basic_rows_all_types": int(len(basics)),
                "security_basic_equity_rows_type_1": int(len(equity_basics)),
            },
            "field_mapping": {
                "volume": "shares (BaoStock daily K-line field)",
                "amount": "CNY",
                "execution_prices": "unadjusted open/high/low/close",
                "signal_price": "adjustflag=1 back-adjusted close",
                "is_suspended": "tradestatus=0",
                "is_st": "isST",
                "is_listed": "inclusive ipoDate through outDate from query_stock_basic",
                "price_limit_state_and_pct": "unknown; not supplied by this daily K-line endpoint",
                "share_lot_rules": "unknown; not supplied by these endpoints",
            },
            "availability_assumption": "daily bars available after 15:00 Asia/Shanghai on each trade_date; actual request timestamps are recorded per endpoint and per normalized symbol batch",
            "requests": session.request_log,
            "quality": quality,
            "benchmark_quality": benchmark_quality,
            "file_sha256": file_hashes,
            "normalized_bars_sha256": file_hashes["bars.csv"],
            "data_scope_warning": "Small, manually selected pipeline-validation snapshot; survivorship-free strategy evaluation requires a reconstructed date-specific universe and complete rule data.",
        }
        (temp_path / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, destination)
        return {"snapshot_path": str(destination), **manifest}
    except Exception:
        import shutil

        shutil.rmtree(temp_path, ignore_errors=True)
        raise
