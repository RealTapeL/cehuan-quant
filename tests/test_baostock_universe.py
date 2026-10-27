import json

import pandas as pd

from cehuan.data.universe_snapshot import (
    create_baostock_universe_snapshot,
    audit_baostock_universe_prices,
    filter_bars_to_point_in_time_universe,
)


class _FakeUniverseSession:
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
            {"calendar_date": "2025-01-03", "is_trading_day": "1"},
        ]))

    def stock_basic(self):
        return self._record("query_stock_basic", pd.DataFrame([
            {"code": "sh.600000", "code_name": "浦发银行", "ipoDate": "1999-11-10", "outDate": "", "type": "1", "status": "1"},
            {"code": "sh.600001", "code_name": "退市样例", "ipoDate": "1999-11-10", "outDate": "2025-01-03", "type": "1", "status": "0"},
            {"code": "sz.159001", "code_name": "样例 ETF", "ipoDate": "2010-01-01", "outDate": "", "type": "5", "status": "1"},
        ]))

    def all_stocks(self, day):
        frame = pd.DataFrame([
            {"code": "sh.600000", "tradeStatus": "1", "code_name": "浦发银行"},
            {"code": "sh.600001", "tradeStatus": "0", "code_name": "退市样例"},
            {"code": "sz.159001", "tradeStatus": "1", "code_name": "样例 ETF"},
            {"code": "sz.999999", "tradeStatus": "1", "code_name": "未知类型"},
        ])
        return self._record(f"query_all_stock({day})", frame)


def test_universe_snapshot_filters_nonstocks_keeps_unknowns_and_resumes(tmp_path):
    first = create_baostock_universe_snapshot(
        _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
        "universe-test", batch_size=1,
    )
    assert first["state"] == "partial"
    assert first["completed_dates"] == 1

    final = create_baostock_universe_snapshot(
        _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
        "universe-test", resume=True,
    )
    members = pd.read_csv(tmp_path / "universe-test" / "securities_by_date.csv", dtype=str)
    day_one = members[members["trade_date"] == "2025-01-02"].set_index("symbol")
    assert day_one.loc["SH600000", "pool_status"] == "STOCK_MEMBER"
    assert day_one.loc["SH600001", "eligibility_status"] == "SUSPENDED_ON_DATE"
    assert day_one.loc["SZ159001", "pool_status"] == "FILTERED_NON_STOCK"
    assert day_one.loc["SZ999999", "pool_status"] == "UNKNOWN_TYPE"
    assert day_one.loc["SH600000", "eligibility_status"] == "UNKNOWN_ST_STATUS"
    assert len(pd.read_csv(tmp_path / "universe-test" / "reconciliation.csv")) == 0
    assert final["coverage"]["unknown_type_rows"] == 2
    assert final["coverage"]["daily_list_rows_total"] == 8
    assert final["coverage"]["type_1_stock_rows"] == 4


def test_point_in_time_filter_reports_missing_quotes_and_daily_status_mismatch():
    membership = pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "pool_status": "STOCK_MEMBER", "daily_trade_status": "0", "basic_status_at_retrieval": "0", "basic_out_date": "2025-01-02"},
        {"trade_date": "2025-01-02", "symbol": "SH600002", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "0", "basic_out_date": "2025-01-02"},
        {"trade_date": "2025-01-02", "symbol": "SZ159001", "pool_status": "FILTERED_NON_STOCK", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
    ])
    bars = pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "open": 10, "high": 11, "low": 9, "close": 10, "adjusted_close": 10, "volume_shares": 1000, "is_suspended": False, "is_st": False},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "open": 5, "high": 5, "low": 5, "close": 5, "adjusted_close": 5, "volume_shares": 0, "is_suspended": True, "is_st": True},
    ])

    filtered, summary, discrepancies = filter_bars_to_point_in_time_universe(bars, membership)

    assert set(filtered["symbol"]) == {"SH600000", "SH600001"}
    assert summary["daily_stock_member_rows"] == 3
    assert summary["members_without_bars"] == 1
    assert summary["eventually_delisted_member_rows_with_bars"] == 1
    assert summary["daily_trade_status_mismatch_rows"] == 0
    assert len(discrepancies) == 1
    assert discrepancies.iloc[0]["discrepancy"] == "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR"


def test_streaming_universe_audit_keeps_delisted_sample_and_checks_status(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    (universe / "manifest.json").write_text(json.dumps({"snapshot_id": "stream-test"}), encoding="utf-8")
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "0", "basic_out_date": "2025-01-02"},
        {"trade_date": "2025-01-02", "symbol": "SH600002", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
        {"trade_date": "2025-01-02", "symbol": "SZ159001", "pool_status": "FILTERED_NON_STOCK", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
    ]).to_csv(universe / "securities_by_date.csv", index=False)
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "open": 10, "high": 11, "low": 9, "close": 10, "volume_shares": 1000, "is_suspended": False, "is_st": False},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "open": 5, "high": 5, "low": 5, "close": 5, "volume_shares": 0, "is_suspended": True, "is_st": True},
    ]).to_csv(tmp_path / "bars.csv", index=False)

    result = audit_baostock_universe_prices(universe, tmp_path / "bars.csv", tmp_path / "audit")
    filtered = pd.read_csv(tmp_path / "audit" / "point_in_time_bars.csv", dtype={"symbol": str})
    discrepancies = pd.read_csv(tmp_path / "audit" / "universe_price_discrepancies.csv")

    assert result["daily_stock_member_rows"] == 3
    assert result["members_with_bars"] == 2
    assert result["members_without_bars"] == 1
    assert result["eventually_delisted_member_rows_with_bars"] == 1
    assert result["st_known_member_rows"] == 2
    assert result["st_unknown_member_rows"] == 1
    assert result["daily_trade_status_mismatch_rows"] == 1
    assert set(filtered["symbol"]) == {"SH600000", "SH600001"}
    assert "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR" in set(discrepancies["discrepancy"])
    assert "DAILY_LIST_VS_DAILY_KLINE_TRADE_STATUS_MISMATCH" in set(discrepancies["discrepancy"])
