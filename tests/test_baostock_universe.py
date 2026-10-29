import json
import hashlib

import pandas as pd
import pytest

from cehuan.data.universe_snapshot import (
    create_baostock_universe_snapshot,
    export_baostock_universe_symbol_union,
    audit_baostock_universe_prices,
    filter_bars_to_point_in_time_universe,
    _verify_request_log,
)
from cehuan.cli.main import main


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


def test_universe_snapshot_can_defer_finalization_until_bars_are_attached(tmp_path):
    delayed = create_baostock_universe_snapshot(
        _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
        "defer-test", batch_size=2, defer_finalize=True,
    )
    assert delayed["state"] == "partial"
    assert delayed["state_reason"] == "awaiting_price_snapshot_for_final_audit"
    assert delayed["completed_dates"] == delayed["total_dates"] == 2
    assert not (tmp_path / "defer-test").exists()

    symbols = export_baostock_universe_symbol_union(
        tmp_path / ".defer-test.partial", tmp_path / "symbols.csv",
    )
    assert symbols["symbol_count"] == 2
    assert symbols["uses_current_list_to_backfill"] is False
    assert set(pd.read_csv(tmp_path / "symbols.csv")["symbol"]) == {"SH600000", "SH600001"}

    final = create_baostock_universe_snapshot(
        _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
        "defer-test", resume=True,
    )
    assert final["state"] == "complete"


def test_universe_resume_rejects_changed_previously_verified_daily_file(tmp_path):
    first = create_baostock_universe_snapshot(
        _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
        "integrity-test", batch_size=1,
    )
    assert first["state"] == "partial"
    raw_file = tmp_path / ".integrity-test.partial" / "raw" / "daily_securities" / "2025-01-02.csv"
    original = raw_file.read_bytes()
    raw_file.write_bytes(original + b"\n")

    with pytest.raises(ValueError, match="哈希发生变化"):
        create_baostock_universe_snapshot(
            _FakeUniverseSession(), "2025-01-02", "2025-01-03", tmp_path,
            "integrity-test", resume=True,
        )
    assert raw_file.read_bytes() == original + b"\n"


def test_request_log_resume_recovers_complete_uncommitted_tail_and_rejects_prefix_tamper(tmp_path):
    path = tmp_path / "request_log.jsonl"
    checkpoint_path = tmp_path / "checkpoint.json"
    first = json.dumps({"endpoint": "login", "request_count": 1}) + "\n"
    second = json.dumps({"endpoint": "query", "request_count": 2}) + "\n"
    path.write_text(first, encoding="utf-8")
    committed = first.encode("utf-8")
    checkpoint = {
        "request_count": 1,
        "attempt_count": 1,
        "request_log_bytes": len(committed),
        "request_log_sha256": hashlib.sha256(committed).hexdigest(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(second)
    _verify_request_log(path, checkpoint, checkpoint_path)
    assert checkpoint["request_count"] == 3
    assert checkpoint["attempt_count"] == 2

    path.write_text(path.read_text(encoding="utf-8").replace("login", "other"), encoding="utf-8")
    with pytest.raises(ValueError, match="哈希不匹配"):
        _verify_request_log(path, checkpoint, checkpoint_path)


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
    assert summary["reconciliation_quality"]["healthy"] is False
    assert summary["reconciliation_quality"]["missing_bar_rows"] == 1
    assert len(discrepancies) == 1
    assert discrepancies.iloc[0]["discrepancy"] == "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR"


def test_streaming_universe_audit_keeps_delisted_sample_and_checks_status(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "0", "basic_out_date": "2025-01-02"},
        {"trade_date": "2025-01-02", "symbol": "SH600002", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
        {"trade_date": "2025-01-02", "symbol": "SZ159001", "pool_status": "FILTERED_NON_STOCK", "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": ""},
    ]).to_csv(universe / "securities_by_date.csv", index=False)
    membership_hash = hashlib.sha256((universe / "securities_by_date.csv").read_bytes()).hexdigest()
    (universe / "manifest.json").write_text(json.dumps({
        "snapshot_id": "stream-test", "date_range": {"start": "2025-01-02", "end": "2025-01-02"},
        "files_sha256": {"securities_by_date.csv": membership_hash},
    }), encoding="utf-8")
    pd.DataFrame([
        {"trade_date": "2024-12-31", "symbol": "SH600999", "open": 2, "high": 2, "low": 2, "close": 2, "volume_shares": 1000, "is_suspended": False, "is_st": False},
        {"trade_date": "2025-01-02", "symbol": "SH600000", "open": 10, "high": 11, "low": 9, "close": 10, "volume_shares": 1000, "is_suspended": False, "is_st": False},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "open": 5, "high": 5, "low": 5, "close": 5, "volume_shares": 0, "is_suspended": True, "is_st": True},
    ]).to_csv(tmp_path / "bars.csv", index=False)
    bars_hash = hashlib.sha256((tmp_path / "bars.csv").read_bytes()).hexdigest()
    (tmp_path / "quality.json").write_text(json.dumps({
        "healthy": True, "errors": [], "warnings": [], "data_sha256": bars_hash,
    }), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "file_sha256": {"bars.csv": bars_hash},
        "normalized_bars_sha256": bars_hash,
    }), encoding="utf-8")

    result = audit_baostock_universe_prices(universe, tmp_path / "bars.csv", tmp_path / "audit")
    filtered = pd.read_csv(tmp_path / "audit" / "point_in_time_bars.csv", dtype={"symbol": str})
    discrepancies = pd.read_csv(tmp_path / "audit" / "universe_price_discrepancies.csv")
    point_in_time_quality = json.loads((tmp_path / "audit" / "point_in_time_quality.json").read_text())

    assert result["daily_stock_member_rows"] == 3
    assert result["members_with_bars"] == 2
    assert result["members_without_bars"] == 1
    assert result["pre_universe_warmup_bar_rows"] == 1
    assert result["quote_rows_outside_confirmed_daily_stock_pool"] == 0
    assert result["eventually_delisted_member_rows_with_bars"] == 1
    assert result["st_known_member_rows"] == 2
    assert result["st_unknown_member_rows"] == 1
    assert result["daily_trade_status_mismatch_rows"] == 1
    assert result["raw_bar_quality"]["healthy"] is True
    assert result["universe_reconciliation_quality"]["healthy"] is False
    assert result["universe_reconciliation_quality"]["members_without_bars"] == 1
    assert result["universe_reconciliation_quality"]["daily_trade_status_mismatch_rows"] == 1
    assert result["healthy"] is False
    assert point_in_time_quality["healthy"] is False
    assert point_in_time_quality["raw_bar_quality"]["healthy"] is True
    assert point_in_time_quality["universe_reconciliation_quality"]["healthy"] is False
    assert set(filtered["symbol"]) == {"SH600000", "SH600001"}
    assert "DAILY_STOCK_MEMBER_WITHOUT_CANONICAL_BAR" in set(discrepancies["discrepancy"])
    assert "DAILY_LIST_VS_DAILY_KLINE_TRADE_STATUS_MISMATCH" in set(discrepancies["discrepancy"])

    membership_path = universe / "securities_by_date.csv"
    membership_path.write_bytes(membership_path.read_bytes() + b"\n")
    tampered_universe = audit_baostock_universe_prices(universe, tmp_path / "bars.csv", tmp_path / "audit-tampered-universe")
    assert tampered_universe["raw_bar_quality"]["healthy"] is True
    assert tampered_universe["universe_reconciliation_quality"]["healthy"] is False
    assert any("股票池文件哈希" in error for error in tampered_universe["universe_reconciliation_quality"]["errors"])

    (tmp_path / "bars.csv").write_bytes((tmp_path / "bars.csv").read_bytes() + b"\n")
    tampered = audit_baostock_universe_prices(universe, tmp_path / "bars.csv", tmp_path / "audit-tampered")
    assert tampered["raw_bar_quality"]["healthy"] is False
    assert any("manifest.json 不一致" in error for error in tampered["raw_bar_quality"]["errors"])


def test_streaming_universe_audit_rejects_a_trading_day_missing_membership(tmp_path):
    universe = tmp_path / "universe"
    universe.mkdir()
    pd.DataFrame([{
        "trade_date": "2025-01-02", "symbol": "SH600000", "pool_status": "STOCK_MEMBER",
        "daily_trade_status": "1", "basic_status_at_retrieval": "1", "basic_out_date": "",
    }]).to_csv(universe / "securities_by_date.csv", index=False)
    membership_hash = hashlib.sha256((universe / "securities_by_date.csv").read_bytes()).hexdigest()
    (universe / "manifest.json").write_text(json.dumps({
        "snapshot_id": "missing-day-test", "date_range": {"start": "2025-01-02", "end": "2025-01-03"},
        "files_sha256": {"securities_by_date.csv": membership_hash},
    }), encoding="utf-8")
    bars_path = tmp_path / "bars.csv"
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "open": 10, "high": 10, "low": 10, "close": 10, "volume_shares": 100, "is_suspended": False, "is_st": False},
        {"trade_date": "2025-01-03", "symbol": "SH600000", "open": 11, "high": 11, "low": 11, "close": 11, "volume_shares": 100, "is_suspended": False, "is_st": False},
    ]).to_csv(bars_path, index=False)
    bars_hash = hashlib.sha256(bars_path.read_bytes()).hexdigest()
    (tmp_path / "quality.json").write_text(json.dumps({"healthy": True, "errors": [], "warnings": []}), encoding="utf-8")
    (tmp_path / "manifest.json").write_text(json.dumps({"file_sha256": {"bars.csv": bars_hash}}), encoding="utf-8")

    result = audit_baostock_universe_prices(universe, bars_path, tmp_path / "audit")
    discrepancies = pd.read_csv(tmp_path / "audit" / "universe_price_discrepancies.csv")
    assert result["bars_on_missing_universe_date"] == 1
    assert result["universe_reconciliation_quality"]["healthy"] is False
    assert any("行情交易日缺少对应的点时股票池名单：1 行" in error for error in result["universe_reconciliation_quality"]["errors"])
    assert discrepancies.iloc[0]["trade_date"] == "2025-01-03"
    assert discrepancies.iloc[0]["discrepancy"] == "TRADING_DAY_MISSING_FROM_POINT_IN_TIME_UNIVERSE"


def test_official_backtest_command_rejects_universe_reconciliation_failure(tmp_path, capsys):
    bars_path = tmp_path / "bars.csv"
    pd.DataFrame([{
        "trade_date": "2025-01-02", "symbol": "SH600000", "open": 10,
        "high": 11, "low": 9, "close": 10, "adjusted_close": 10,
        "volume_shares": 1000, "is_listed": True, "is_suspended": False,
        "is_st": False, "data_source": "test",
    }]).to_csv(bars_path, index=False)
    membership_path = tmp_path / "membership.csv"
    pd.DataFrame([
        {"trade_date": "2025-01-02", "symbol": "SH600000", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1"},
        {"trade_date": "2025-01-02", "symbol": "SH600001", "pool_status": "STOCK_MEMBER", "daily_trade_status": "1"},
    ]).to_csv(membership_path, index=False)

    exit_code = main([
        "backtest", "--data", str(bars_path), "--universe", str(membership_path),
        "--cash", "100000",
    ])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "点时股票池对账未通过" in captured.err
    assert "不能把该结果作为正式基线" in captured.err
