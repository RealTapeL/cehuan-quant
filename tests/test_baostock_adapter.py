import json
import pandas as pd
import pytest
import subprocess
import sys
from types import SimpleNamespace

from cehuan.data.baostock_adapter import (
    BaoStockError,
    BaoStockSession,
    _canonical_vendor_symbol,
    _query_frame,
    _safe_baostock_send_msg,
    create_baostock_snapshot,
    normalize_baostock_calendar,
    normalize_baostock_history,
    normalize_baostock_index,
)


def test_baostock_pagination_silent_stop_is_classified_as_transport_failure():
    class InterruptedResponse:
        error_code = "0"
        error_msg = "success"
        fields = ["code"]
        data = [["sh.600000"], ["sh.600001"]]
        cur_row_num = 2
        per_page_count = 2
        cur_page_num = "1"

        def next(self):
            return False

    try:
        _query_frame(InterruptedResponse(), "query_all_stock(2025-01-02)")
    except BaoStockError as exc:
        assert exc.category == "transport"
        assert exc.error_code == "10002007"
    else:
        raise AssertionError("full page silently stopping must be rejected")


def test_baostock_socket_guard_rejects_eof_and_reads_fragmented_responses():
    class FakeSocket:
        def __init__(self, chunks):
            self.chunks = list(chunks)
            self.sent = None

        def sendall(self, payload):
            self.sent = payload

        def recv(self, size):
            return self.chunks.pop(0) if self.chunks else b""

    header = b"version\x01plain\x011".ljust(21, b" ")
    response = header + b"body<![CDATA[]]>\n"
    fake = FakeSocket([response[:13], response[13:25], response[25:]])
    result = _safe_baostock_send_msg(
        "request", fake, message_header_length=21, message_split="\x01", compressed_message_types=(),
    )
    assert result == response.decode("utf-8")
    assert fake.sent == b"request\n"

    eof = FakeSocket([b"partial", b""])
    with pytest.raises(EOFError, match="before the response terminator"):
        _safe_baostock_send_msg(
            "request", eof, message_header_length=21, message_split="\x01", compressed_message_types=(),
        )


def test_baostock_socket_guard_decompresses_compressed_protocol_responses():
    import zlib

    class FakeSocket:
        def __init__(self, response):
            self.response = response

        def sendall(self, payload):
            pass

        def recv(self, size):
            response, self.response = self.response, b""
            return response

    body = b"protocol-body"
    compressed = zlib.compress(body)
    header = f"version\x01zip\x01{len(compressed)}".encode("utf-8").ljust(21, b" ")
    fake = FakeSocket(header + compressed + b"<![CDATA[]]>\n")
    result = _safe_baostock_send_msg(
        "request", fake, message_header_length=21, message_split="\x01", compressed_message_types={"zip"},
    )
    assert result == header.decode("utf-8") + body.decode("utf-8")


def test_baostock_single_response_larger_than_page_threshold_is_valid():
    class SingleLargeResponse:
        error_code = "0"
        error_msg = "success"
        fields = ["code"]
        data = [["sh.600000"], ["sh.600001"], ["sh.600002"]]
        cur_row_num = 0
        per_page_count = 2
        cur_page_num = "1"

        def next(self):
            return self.cur_row_num < len(self.data)

        def get_row_data(self):
            row = self.data[self.cur_row_num]
            self.cur_row_num += 1
            return row

    frame, page_count = _query_frame(SingleLargeResponse(), "query_history_k_data_plus")

    assert len(frame) == 3
    assert page_count == 1


def test_operator_interrupt_during_request_is_logged_and_not_retried():
    session = BaoStockSession(minimum_interval_seconds=0, max_retries=2)
    session.bs = object()
    with pytest.raises(KeyboardInterrupt):
        session._query("query_all_stock(2025-01-02)", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert session.request_log[-1]["category"] == "operator_interrupted"
    assert session.request_log[-1]["request_count"] == 1


def test_expired_baostock_session_relogs_in_once_and_retries_endpoint():
    class Response:
        def __init__(self, error_code="0", error_msg="success"):
            self.error_code = error_code
            self.error_msg = error_msg
            self.fields = ["code"]
            self.data = [["sh.600000"]] if error_code == "0" else []
            self.cur_row_num = 0
            self.per_page_count = 2
            self.cur_page_num = "1"

        def next(self):
            return bool(self.data) and self.cur_row_num < len(self.data)

        def get_row_data(self):
            row = self.data[self.cur_row_num]
            self.cur_row_num += 1
            return row

    class FakeClient:
        def __init__(self):
            self.login_count = 0

        def login(self):
            self.login_count += 1
            return SimpleNamespace(error_code="0", error_msg="success")

    client = FakeClient()
    session = BaoStockSession(minimum_interval_seconds=0, max_retries=1, retry_base_seconds=0)
    session.bs = client
    responses = [Response("10001001", "用户未登录"), Response()]
    frame = session._query("query_all_stock(2025-01-02)", lambda: responses.pop(0))

    assert frame.loc[0, "code"] == "sh.600000"
    assert client.login_count == 1
    assert [row["category"] for row in session.request_log] == ["session_expired", "success", "success"]


def test_transport_exception_is_retried_once_and_logged_as_transport():
    class Response:
        error_code = "0"
        error_msg = "success"
        fields = ["code"]
        data = [["sh.600000"]]
        cur_row_num = 0
        per_page_count = 2
        cur_page_num = "1"

        def next(self):
            if self.cur_row_num == 0:
                self.cur_row_num = 1
                return True
            return False

        def get_row_data(self):
            return self.data[self.cur_row_num - 1]

    class FakeClient:
        def login(self):
            return SimpleNamespace(error_code="0", error_msg="success")

    calls = 0

    def call():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionResetError("connection reset by peer")
        return Response()

    session = BaoStockSession(minimum_interval_seconds=0, max_retries=1, retry_base_seconds=0)
    session.bs = FakeClient()
    frame = session._query("query_all_stock(2025-01-02)", call)

    assert frame.loc[0, "code"] == "sh.600000"
    assert calls == 2
    assert session.request_log[0]["category"] == "transport"
    assert session.request_log[-1]["category"] == "success"


def test_baostock_file_lock_prevents_a_second_process_session(tmp_path):
    lock_path = tmp_path / "baostock.lock"
    first = BaoStockSession(lock_path=lock_path)
    first._acquire_process_lock()
    try:
        script = (
            "import sys; from cehuan.data.baostock_adapter import BaoStockError, BaoStockSession; "
            "session = BaoStockSession(lock_path=sys.argv[1]); "
            "\ntry: session._acquire_process_lock(); print('unexpected_lock_acquired') "
            "\nexcept BaoStockError as exc: print(exc.category)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        assert result.stdout.strip() == "local_concurrency_stop"
    finally:
        first._release_process_lock()


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
    manifest = json.loads((tmp_path / "resume-test" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["capture_policy_by_chunk"]["chunk-00001"]["minimum_interval_seconds"] == 1.0


def test_baostock_final_manifest_keeps_each_new_chunk_request_log(tmp_path):
    result = create_baostock_snapshot(
        _FakeBaoStockSession(),
        symbols=["SH600000", "SZ000001"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        root=tmp_path,
        snapshot_id="request-log-copy-test",
        batch_size=1,
    )

    manifest = json.loads((tmp_path / "request-log-copy-test" / "manifest.json").read_text(encoding="utf-8"))
    symbol_endpoints = [request["endpoint"] for request in manifest["requests"] if "(" in request["endpoint"]]
    assert any("SH600000" in endpoint for endpoint in symbol_endpoints)
    assert any("SZ000001" in endpoint for endpoint in symbol_endpoints)
    assert len(symbol_endpoints) == 6
    assert manifest["request_summary"] == {
        "request_count": 8,
        "attempt_count": 8,
        "failed_attempt_count": 0,
        "failed_chunk_attempt_count": 0,
    }


def test_baostock_failed_chunk_attempt_is_checkpointed_and_included_in_final_manifest(tmp_path):
    class OneFailedRequestSession(_FakeBaoStockSession):
        failed = False

        def history(self, symbol, start_date, end_date, adjustflag, fields=""):
            if not self.failed:
                self.failed = True
                endpoint = f"query_history_k_data_plus({symbol},adjustflag={adjustflag})"
                self.request_log.append({
                    "endpoint": endpoint,
                    "status": "failed",
                    "category": "transport",
                    "error_code": "10002007",
                    "error_msg": "pagination stopped after a full page",
                    "request_count": 2,
                })
                raise BaoStockError(
                    "page receive interrupted", category="transport",
                    endpoint=endpoint, error_code="10002007",
                )
            return super().history(symbol, start_date, end_date, adjustflag, fields)

    with pytest.raises(BaoStockError, match="page receive interrupted"):
        create_baostock_snapshot(
            OneFailedRequestSession(),
            symbols=["SH600000"],
            start_date="2025-01-02",
            end_date="2025-01-02",
            root=tmp_path,
            snapshot_id="failure-log-test",
            batch_size=1,
            max_batches=1,
        )

    partial = tmp_path / ".failure-log-test.partial"
    checkpoint = json.loads((partial / "batch_checkpoint.json").read_text(encoding="utf-8"))
    failure = checkpoint["failed_chunk_attempts"][0]
    assert failure["chunk_id"] == "chunk-00001"
    assert failure["error"]["error_code"] == "10002007"
    assert failure["request_count"] == 2
    assert failure["attempt_count"] == 1

    result = create_baostock_snapshot(
        _FakeBaoStockSession(),
        symbols=["SH600000"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        root=tmp_path,
        snapshot_id="failure-log-test",
        batch_size=1,
        resume=True,
    )
    assert result["request_summary"]["request_count"] == 7
    assert result["request_summary"]["attempt_count"] == 6
    assert result["request_summary"]["failed_attempt_count"] == 1
    assert result["request_summary"]["failed_chunk_attempt_count"] == 1
    manifest = json.loads((tmp_path / "failure-log-test" / "manifest.json").read_text(encoding="utf-8"))
    failed_request = next(request for request in manifest["requests"] if request.get("status") == "failed")
    assert failed_request["error_code"] == "10002007"
    assert failed_request["snapshot_chunk_attempt"] == 1


def test_baostock_operator_interrupt_keeps_checkpoint_and_removes_uncommitted_temp_chunk(tmp_path):
    class InterruptedSession(_FakeBaoStockSession):
        def history(self, symbol, start_date, end_date, adjustflag, fields=""):
            self.request_log.append({
                "endpoint": f"query_history_k_data_plus({symbol},adjustflag={adjustflag})",
                "status": "interrupted",
                "category": "operator_interrupted",
                "request_count": 1,
            })
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        create_baostock_snapshot(
            InterruptedSession(),
            symbols=["SH600000"],
            start_date="2025-01-02",
            end_date="2025-01-02",
            root=tmp_path,
            snapshot_id="interrupt-log-test",
            batch_size=1,
            max_batches=1,
        )

    partial = tmp_path / ".interrupt-log-test.partial"
    checkpoint = json.loads((partial / "batch_checkpoint.json").read_text(encoding="utf-8"))
    failure = checkpoint["failed_chunk_attempts"][0]
    assert failure["error"]["category"] == "operator_interrupted"
    assert failure["request_count"] == 1
    assert failure["requests"][0]["status"] == "interrupted"
    assert not list(partial.glob(".chunk-*"))


def test_baostock_resume_rejects_tampered_committed_chunk_before_fetching(tmp_path):
    initial = create_baostock_snapshot(
        _FakeBaoStockSession(),
        symbols=["SH600000", "SZ000001"],
        start_date="2025-01-02",
        end_date="2025-01-02",
        root=tmp_path,
        snapshot_id="tamper-resume-test",
        benchmark_symbol=None,
        batch_size=1,
        max_batches=1,
    )
    assert initial["state"] == "partial"
    committed_bars = tmp_path / ".tamper-resume-test.partial" / "chunk-00001" / "bars.csv"
    committed_bars.write_bytes(committed_bars.read_bytes() + b"\n")
    resumed_session = _FakeBaoStockSession()

    with pytest.raises(ValueError, match="文件哈希不匹配：bars.csv"):
        create_baostock_snapshot(
            resumed_session,
            symbols=["SH600000", "SZ000001"],
            start_date="2025-01-02",
            end_date="2025-01-02",
            root=tmp_path,
            snapshot_id="tamper-resume-test",
            benchmark_symbol=None,
            batch_size=1,
            resume=True,
        )

    assert not resumed_session.request_log


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
