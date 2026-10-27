import pandas as pd

from cehuan.data.baostock_adapter import (
    _canonical_vendor_symbol,
    create_baostock_snapshot,
    normalize_baostock_calendar,
    normalize_baostock_history,
    normalize_baostock_index,
)


class _FakeBaoStockSession:
    package_version = "test"

    def __init__(self):
        self.request_log = []

    def _record(self, endpoint, frame):
        self.request_log.append({
            "endpoint": endpoint,
            "started_at_utc": "2025-01-03T00:00:00+00:00",
            "completed_at_utc": "2025-01-03T00:00:01+00:00",
            "elapsed_seconds": 1.0,
            "row_count": len(frame),
            "fields": list(frame.columns),
        })
        return frame

    def trade_calendar(self, start, end):
        return self._record("query_trade_dates", pd.DataFrame([
            {"calendar_date": "2025-01-02", "is_trading_day": "1"},
        ]))

    def stock_basic(self):
        return self._record("query_stock_basic", pd.DataFrame([
            {"code": "sh.600000", "code_name": "浦发银行", "ipoDate": "1999-11-10", "outDate": "", "type": "1", "status": "1"},
            {"code": "sz.000001", "code_name": "平安银行", "ipoDate": "1991-04-03", "outDate": "", "type": "1", "status": "1"},
        ]))

    def history(self, symbol, start_date, end_date, adjustflag, fields=""):
        base = {
            "date": "2025-01-02", "code": "sh.600000" if "600000" in symbol else "sz.000001",
            "open": "10", "high": "11", "low": "9", "close": "10.5", "preclose": "10",
            "volume": "1000", "amount": "10500", "adjustflag": adjustflag, "turn": "1",
            "tradestatus": "1", "pctChg": "5", "isST": "0",
        }
        frame = pd.DataFrame([base])
        self._record(f"query_history_k_data_plus({symbol},adjustflag={adjustflag})", frame)
        if fields == "date,code,open,high,low,close,volume,amount":
            return frame[["date", "code", "open", "high", "low", "close", "volume", "amount"]]
        return frame

    def adjust_factors(self, symbol, start_date, end_date):
        return self._record(f"query_adjust_factor({symbol})", pd.DataFrame(columns=["code", "date", "foreAdjustFactor", "backAdjustFactor"]))


def test_baostock_snapshot_is_chunked_and_resumable(tmp_path):
    session = _FakeBaoStockSession()
    initial = create_baostock_snapshot(
        session,
        symbols=["SH600000", "SZ000001"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        root=tmp_path,
        snapshot_id="resume-test",
        benchmark_symbol=None,
        batch_size=1,
        max_batches=1,
    )
    assert initial["state"] == "partial"
    assert initial["completed_chunks"] == 1

    resumed = create_baostock_snapshot(
        _FakeBaoStockSession(),
        symbols=["SH600000", "SZ000001"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        root=tmp_path,
        snapshot_id="resume-test",
        benchmark_symbol=None,
        batch_size=1,
        resume=True,
    )
    assert resumed["coverage"]["symbols_with_bars"] == 2
    assert (tmp_path / "resume-test" / "bars.csv").exists()


def test_baostock_vendor_code_maps_to_canonical_symbol():
    assert _canonical_vendor_symbol("sh.600519") == "SH600519"
    assert _canonical_vendor_symbol("sz.000001") == "SZ000001"
    assert _canonical_vendor_symbol("sh.000300") == "SH000300"
    assert _canonical_vendor_symbol("sh.512280") == "SH512280"
    assert _canonical_vendor_symbol("sz.159579") == "SZ159579"


def test_normalize_baostock_calendar_records_open_dates_and_predecessor():
    raw = pd.DataFrame([
        {"calendar_date": "2025-01-02", "is_trading_day": "1"},
        {"calendar_date": "2025-01-03", "is_trading_day": "1"},
        {"calendar_date": "2025-01-04", "is_trading_day": "0"},
    ])

    calendar = normalize_baostock_calendar(raw, "2025-01-05T00:00:00+00:00")

    assert bool(calendar.loc[0, "is_open"])
    assert pd.isna(calendar.loc[0, "previous_trade_date"])
    assert calendar.loc[1, "previous_trade_date"].isoformat() == "2025-01-02"
    assert not bool(calendar.loc[2, "is_open"])


def test_normalize_baostock_index_marks_reference_series_nontradable():
    raw = pd.DataFrame([{
        "date": "2025-01-02", "code": "sh.000300", "open": "3900",
        "high": "3920", "low": "3880", "close": "3910",
        "volume": "1000000", "amount": "1000000000",
    }])
    security = pd.Series({"code": "sh.000300", "code_name": "沪深300"})

    index_bars = normalize_baostock_index(raw, security, "2025-01-03T08:00:00+00:00")

    assert index_bars.loc[0, "symbol"] == "SH000300"
    assert index_bars.loc[0, "adjusted_close"] == index_bars.loc[0, "close"]
    assert not bool(index_bars.loc[0, "is_listed"])


def test_normalize_baostock_maps_units_prices_and_point_in_time_flags():
    raw = pd.DataFrame([{
        "date": "2025-01-02",
        "code": "sh.600519",
        "open": "10.0",
        "high": "11.0",
        "low": "9.0",
        "close": "10.5",
        "preclose": "10.0",
        "volume": "123456",
        "amount": "1296288.0",
        "adjustflag": "3",
        "turn": "1.0",
        "tradestatus": "1",
        "pctChg": "5.0",
        "isST": "0",
    }])
    adjusted = pd.DataFrame([{"date": "2025-01-02", "close": "21.0"}])
    security = pd.Series({
        "code": "sh.600519",
        "code_name": "贵州茅台",
        "ipoDate": "2001-08-27",
        "outDate": "",
    })

    bars, skipped = normalize_baostock_history(raw, adjusted, security, "2025-01-03T08:00:00+00:00")

    assert skipped == 0
    assert bars.loc[0, "symbol"] == "SH600519"
    assert bars.loc[0, "volume_shares"] == 123456
    assert bars.loc[0, "amount_cny"] == 1296288.0
    assert bars.loc[0, "adjusted_close"] == 21.0
    assert bool(bars.loc[0, "is_listed"])
    assert not bool(bars.loc[0, "is_suspended"])
    assert not bool(bars.loc[0, "is_st"])
    assert pd.isna(bars.loc[0, "price_limit_pct"])


def test_normalize_baostock_marks_suspension_and_uses_zero_turnover():
    raw = pd.DataFrame([{
        "date": "2025-01-02",
        "code": "sz.000001",
        "open": "10.0",
        "high": "10.0",
        "low": "10.0",
        "close": "10.0",
        "preclose": "10.0",
        "volume": "",
        "amount": "",
        "adjustflag": "3",
        "turn": "",
        "tradestatus": "0",
        "pctChg": "0.0",
        "isST": "1",
    }])
    adjusted = pd.DataFrame([{"date": "2025-01-02", "close": "15.0"}])
    security = pd.Series({
        "code": "sz.000001",
        "code_name": "平安银行",
        "ipoDate": "1991-04-03",
        "outDate": "",
    })

    bars, skipped = normalize_baostock_history(raw, adjusted, security, "2025-01-03T08:00:00+00:00")

    assert skipped == 0
    assert bool(bars.loc[0, "is_suspended"])
    assert bool(bars.loc[0, "is_st"])
    assert bars.loc[0, "volume_shares"] == 0
    assert bars.loc[0, "amount_cny"] == 0
