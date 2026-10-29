from __future__ import annotations

import hashlib
import csv
import fcntl
import heapq
import json
import os
import socket
import tempfile
import threading
import time
import zlib
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.canonical import BAR_COLUMNS, canonical_symbol, normalize_bar_frame, write_bars
from cehuan.data.quality import check_data_quality


HISTORY_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST"
)
INDEX_FIELDS = "date,code,open,high,low,close,volume,amount"
SOURCE_NAME = "baostock.query_history_k_data_plus"


def _safe_baostock_send_msg(
    message: str,
    default_socket: Any,
    *,
    message_header_length: int,
    message_split: str,
    compressed_message_types: set[str] | tuple[str, ...],
) -> str | None:
    """Read one BaoStock protocol response and treat EOF as a transport error.

    BaoStock 0.9.4 loops forever when ``recv`` returns ``b""`` before its
    terminator arrives. A socket timeout cannot stop that EOF loop, so the
    adapter installs this small protocol-compatible guard for its session.
    """
    if default_socket is None:
        return None
    default_socket.sendall((message + "\n").encode("utf-8"))
    receive = bytearray()
    terminator = b"<![CDATA[]]>\n"
    while not receive.endswith(terminator):
        chunk = default_socket.recv(8192)
        if not chunk:
            raise EOFError("BaoStock socket closed before the response terminator")
        receive.extend(chunk)

    raw = bytes(receive)
    header_bytes = raw[:message_header_length]
    try:
        header = header_bytes.decode("utf-8")
        header_parts = header.split(message_split)
        if len(header_parts) < 3:
            raise ValueError("BaoStock response header is incomplete")
        if header_parts[1] in compressed_message_types:
            compressed_length = int(header_parts[2])
            compressed_body = raw[message_header_length:message_header_length + compressed_length]
            body = zlib.decompress(compressed_body).decode("utf-8")
            return header + body
        return raw.decode("utf-8")
    except (UnicodeDecodeError, ValueError, zlib.error) as exc:
        raise ConnectionError(f"BaoStock response framing is invalid: {exc}") from exc


def _vendor_code(symbol: object) -> str:
    canonical = canonical_symbol(symbol)
    if len(canonical) != 8 or canonical[:2] not in {"SH", "SZ"}:
        raise ValueError(f"BaoStock 当前适配范围仅验证了沪深代码：{symbol}")
    return f"{canonical[:2].lower()}.{canonical[2:]}"


def _canonical_vendor_symbol(value: object) -> str:
    """BaoStock uses `sh.600519`; project data uses `SH600519`."""
    raw = str(value).strip().upper()
    if "." in raw:
        left, right = raw.rsplit(".", 1)
        exchanges = {"SH": "SH", "SS": "SH", "SZ": "SZ", "BJ": "BJ"}
        if left in exchanges and right.isdigit() and len(right) == 6:
            return f"{exchanges[left]}{right}"
        if right in exchanges and left.isdigit() and len(left) == 6:
            return f"{exchanges[right]}{left}"
    return canonical_symbol(raw)


class BaoStockError(RuntimeError):
    """A BaoStock failure with a stable stage and retry classification."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        endpoint: str,
        error_code: str | None = None,
        error_msg: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.endpoint = endpoint
        self.error_code = error_code
        self.error_msg = error_msg


def _error_category(error_code: object | None, error_msg: object | None = None) -> str:
    code = str(error_code or "")
    if isinstance(error_msg, (ConnectionError, TimeoutError, EOFError)):
        return "transport"
    message = str(error_msg or "").lower()
    if code == "10001001":
        return "session_expired"
    if code.startswith("10002") or any(token in message for token in (
        "network", "socket", "connection", "connect", "recv", "receive", "timeout", "timed out",
        "reset by peer", "broken pipe", "remote end closed", "connection aborted", "ssl",
        "连接", "接收", "超时",
    )):
        return "transport"
    if code == "10001011":
        return "provider_access_denied"
    return "provider_api"


def _raise_for_response(response: Any, endpoint: str) -> None:
    code = str(getattr(response, "error_code", "unknown"))
    message = str(getattr(response, "error_msg", "missing response status"))
    if code != "0":
        category = _error_category(code, message)
        raise BaoStockError(
            f"BaoStock {endpoint} 失败：{code} {message}",
            category=category,
            endpoint=endpoint,
            error_code=code,
            error_msg=message,
        )


def _query_frame(response: Any, endpoint: str, before_page: Any | None = None) -> tuple[pd.DataFrame, int]:
    _raise_for_response(response, endpoint)
    fields = list(response.fields)
    rows: list[list[str]] = []
    pages = 1 if len(getattr(response, "data", [])) else 0
    while True:
        data = getattr(response, "data", [])
        current_row = int(getattr(response, "cur_row_num", 0))
        page_size = int(getattr(response, "per_page_count", 0) or 0)
        if data and current_row >= len(data) and len(data) == page_size and before_page is not None:
            before_page()
        prior_page = int(getattr(response, "cur_page_num", 1) or 1)
        if not response.next():
            _raise_for_response(response, endpoint)
            # BaoStock's pinned 0.9.4 SDK returns False on a socket receive
            # failure without changing error_code. A full page still present
            # after next() means pagination did not complete.
            data = getattr(response, "data", [])
            current_row = int(getattr(response, "cur_row_num", 0))
            page_size = int(getattr(response, "per_page_count", 0) or 0)
            if data and current_row >= len(data) and page_size > 0 and len(data) == page_size:
                raise BaoStockError(
                    f"BaoStock {endpoint} 分页未完成：SDK 在完整页面后停止，可能是连接中断",
                    category="transport",
                    endpoint=endpoint,
                    error_code="10002007",
                    error_msg="pagination stopped after a full page",
                )
            break
        if int(getattr(response, "cur_page_num", 1) or 1) > prior_page:
            pages += 1
        rows.append(response.get_row_data())
    return pd.DataFrame(rows, columns=fields), pages


class BaoStockSession:
    """Small, explicit BaoStock session. No requests are sent at import time."""

    _session_lock = threading.Lock()

    def __init__(
        self,
        *,
        socket_timeout_seconds: float = 20.0,
        minimum_interval_seconds: float = 1.0,
        max_retries: int = 1,
        retry_base_seconds: float = 2.0,
        daily_request_budget: int = 49_000,
        lock_path: str | Path | None = None,
    ) -> None:
        if (
            socket_timeout_seconds <= 0 or minimum_interval_seconds < 0 or max_retries < 0
            or retry_base_seconds < 0 or not 1 <= daily_request_budget <= 49_000
        ):
            raise ValueError("BaoStock timeout、限速和重试参数必须为非负值，timeout 必须大于 0")
        self.bs: Any | None = None
        self.package_version: str | None = None
        self.request_log: list[dict[str, Any]] = []
        self.socket_timeout_seconds = float(socket_timeout_seconds)
        self.minimum_interval_seconds = float(minimum_interval_seconds)
        self.max_retries = int(max_retries)
        self.retry_base_seconds = float(retry_base_seconds)
        self.daily_request_budget = int(daily_request_budget)
        self.lock_path = (
            Path(lock_path).expanduser().resolve()
            if lock_path is not None
            else Path(__file__).resolve().parents[3] / "data" / "private" / ".baostock-session.lock"
        )
        self._request_day: str | None = None
        self._requests_today = 0
        self._previous_socket_timeout: float | None = None
        self._socket_timeout_saved = False
        self._last_request_started: float | None = None
        self._lock_acquired = False
        self._process_lock_handle: Any | None = None
        self._socketutil_module: Any | None = None
        self._original_send_msg: Any | None = None

    def __enter__(self) -> "BaoStockSession":
        if not self._session_lock.acquire(blocking=False):
            raise BaoStockError(
                "当前进程已有 BaoStock 会话；拒绝启动并发请求",
                category="local_concurrency_stop",
                endpoint="session_lock",
                error_code="LOCAL_CONCURRENT_SESSION",
                error_msg="thread-level BaoStock lock is already held",
            )
        self._lock_acquired = True
        try:
            self._acquire_process_lock()
            self._previous_socket_timeout = socket.getdefaulttimeout()
            self._socket_timeout_saved = True
            # BaoStock 0.9.4 creates raw sockets without setting a timeout. Set
            # the process default only for this serialized session so login,
            # connect, and recv cannot block forever.
            socket.setdefaulttimeout(self.socket_timeout_seconds)
            import baostock as bs
            self._install_receive_eof_guard()
        except ImportError as exc:
            self._restore_session_globals()
            raise RuntimeError("缺少 BaoStock，可运行 `uv sync --extra baostock` 安装可选数据适配器") from exc
        except Exception:
            self._restore_session_globals()
            raise
        try:
            self.package_version = version("baostock")
        except PackageNotFoundError:
            self.package_version = "unknown"
        self.bs = bs
        try:
            self._login_with_retry()
        except (Exception, KeyboardInterrupt):
            self.bs = None
            self._close_default_socket()
            self._restore_session_globals()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if self.bs is not None:
                try:
                    self.bs.logout()
                except Exception as logout_error:
                    self.request_log.append({
                        "endpoint": "logout",
                        "status": "failed",
                        "category": "transport",
                        "error_msg": str(logout_error),
                        "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    })
                self.bs = None
        finally:
            self._close_default_socket()
            self._restore_session_globals()

    def _restore_session_globals(self) -> None:
        try:
            if self._socketutil_module is not None and self._original_send_msg is not None:
                self._socketutil_module.send_msg = self._original_send_msg
                self._socketutil_module = None
                self._original_send_msg = None
            if self._socket_timeout_saved:
                socket.setdefaulttimeout(self._previous_socket_timeout)
                self._previous_socket_timeout = None
                self._socket_timeout_saved = False
            self._release_process_lock()
        finally:
            if self._lock_acquired:
                self._lock_acquired = False
                self._session_lock.release()

    def _install_receive_eof_guard(self) -> None:
        import baostock.common.context as context
        import baostock.common.contants as constants
        import baostock.util.socketutil as socketutil

        self._socketutil_module = socketutil
        self._original_send_msg = socketutil.send_msg

        def guarded_send_msg(message: str) -> str | None:
            return _safe_baostock_send_msg(
                message,
                getattr(context, "default_socket", None),
                message_header_length=constants.MESSAGE_HEADER_LENGTH,
                message_split=constants.MESSAGE_SPLIT,
                compressed_message_types=constants.COMPRESSED_MESSAGE_TYPE_TUPLE,
            )

        socketutil.send_msg = guarded_send_msg

    def _acquire_process_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "owner metadata unavailable"
            handle.close()
            raise BaoStockError(
                f"检测到另一个进程正在使用 BaoStock：{owner}",
                category="local_concurrency_stop",
                endpoint="process_lock",
                error_code="LOCAL_CONCURRENT_SESSION",
                error_msg=owner,
            ) from exc
        self._process_lock_handle = handle
        handle.seek(0)
        handle.truncate()
        try:
            handle.write(json.dumps({
                "pid": os.getpid(),
                "acquired_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }))
            handle.flush()
        except Exception:
            self._release_process_lock()
            raise

    def _release_process_lock(self) -> None:
        handle = self._process_lock_handle
        self._process_lock_handle = None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _close_default_socket(self) -> None:
        try:
            import baostock.common.context as context
            sock = getattr(context, "default_socket", None)
            if sock is not None:
                try:
                    sock.close()
                finally:
                    context.default_socket = None
        except ImportError:
            pass

    def _wait_for_request_slot(self) -> None:
        now = time.monotonic()
        if self._last_request_started is not None:
            wait_seconds = self.minimum_interval_seconds - (now - self._last_request_started)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        self._last_request_started = time.monotonic()

    def _reserve_request(self, endpoint: str) -> None:
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if day != self._request_day:
            self._request_day = day
            self._requests_today = 0
        if self._requests_today >= self.daily_request_budget:
            raise BaoStockError(
                f"BaoStock 单日安全请求预算已达 {self.daily_request_budget}，停止请求以避开官方 50,000 次上限",
                category="daily_budget_stop",
                endpoint=endpoint,
                error_code="LOCAL_DAILY_BUDGET",
                error_msg=f"requests_today={self._requests_today}; day={day}",
            )
        self._requests_today += 1

    def _record_attempt(self, record: dict[str, Any]) -> None:
        self.request_log.append(record)

    def _login_once(self) -> Any:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        timer = time.perf_counter()
        self._wait_for_request_slot()
        self._reserve_request("login")
        try:
            login = self.bs.login()
        except KeyboardInterrupt:
            self._record_attempt({
                "endpoint": "login",
                "started_at_utc": started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "elapsed_seconds": round(time.perf_counter() - timer, 3),
                "status": "interrupted",
                "category": "operator_interrupted",
                "error_msg": "KeyboardInterrupt during BaoStock login",
                "request_count": 1,
            })
            raise
        except Exception as exc:
            error_code = "10002007" if isinstance(exc, EOFError) else None
            self._record_attempt({
                "endpoint": "login",
                "started_at_utc": started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "elapsed_seconds": round(time.perf_counter() - timer, 3),
                "status": "failed",
                "category": "transport",
                "error_code": error_code,
                "error_msg": f"{type(exc).__name__}: {exc}",
                "request_count": 1,
            })
            raise BaoStockError(
                f"BaoStock 登录客户端异常：{type(exc).__name__}: {exc}",
                category="transport",
                endpoint="login",
                error_code=error_code,
                error_msg=str(exc),
            ) from exc
        record = {
            "endpoint": "login",
            "started_at_utc": started_at,
            "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.perf_counter() - timer, 3),
            "status": "success" if str(login.error_code) == "0" else "failed",
            "error_code": str(login.error_code),
            "error_msg": str(login.error_msg),
            "category": "success" if str(login.error_code) == "0" else _error_category(login.error_code, login.error_msg),
            "request_count": 1,
        }
        self._record_attempt(record)
        if str(login.error_code) != "0":
            category = _error_category(login.error_code, login.error_msg)
            raise BaoStockError(
                f"BaoStock 登录失败：{login.error_code} {login.error_msg}",
                category=category,
                endpoint="login",
                error_code=str(login.error_code),
                error_msg=str(login.error_msg),
            )
        return login

    def _login_with_retry(self) -> None:
        for attempt in range(self.max_retries + 1):
            try:
                self._login_once()
                return
            except BaoStockError as exc:
                if exc.category != "transport" or attempt >= self.max_retries:
                    raise
                self._close_default_socket()
                time.sleep(min(self.retry_base_seconds * (2**attempt), 30.0))

    def _query(self, endpoint: str, call: Any) -> pd.DataFrame:
        if self.bs is None:
            raise RuntimeError("BaoStockSession 尚未登录")
        for attempt in range(self.max_retries + 1):
            started = datetime.now(timezone.utc).isoformat(timespec="seconds")
            timer = time.perf_counter()
            page_attempts = 1

            def wait_for_page() -> None:
                nonlocal page_attempts
                self._reserve_request(endpoint)
                page_attempts += 1
                self._wait_for_request_slot()

            try:
                self._wait_for_request_slot()
                self._reserve_request(endpoint)
                response = call()
                frame, pages = _query_frame(response, endpoint, wait_for_page)
                self._record_attempt({
                    "endpoint": endpoint,
                    "started_at_utc": started,
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "elapsed_seconds": round(time.perf_counter() - timer, 3),
                    "row_count": int(len(frame)),
                    "page_count": pages,
                    "request_count": page_attempts,
                    "status": "success",
                    "category": "success",
                })
                return frame
            except (Exception, KeyboardInterrupt) as exc:
                interrupted = isinstance(exc, KeyboardInterrupt)
                if interrupted:
                    category = "operator_interrupted"
                    error_code = None
                    error_msg = "KeyboardInterrupt during BaoStock request"
                elif isinstance(exc, BaoStockError):
                    category = exc.category
                    error_code = exc.error_code
                    error_msg = exc.error_msg or str(exc)
                else:
                    category = _error_category(None, exc)
                    error_code = "10002007" if isinstance(exc, EOFError) else None
                    error_msg = str(exc)
                self._record_attempt({
                    "endpoint": endpoint,
                    "started_at_utc": started,
                    "completed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "elapsed_seconds": round(time.perf_counter() - timer, 3),
                    "status": "failed",
                    "category": category,
                    "error_code": error_code,
                    "error_msg": error_msg,
                    "attempt": attempt + 1,
                    "request_count": page_attempts,
                })
                if interrupted:
                    raise
                if category not in {"transport", "session_expired"} or attempt >= self.max_retries:
                    if isinstance(exc, BaoStockError):
                        raise
                    raise BaoStockError(
                        f"BaoStock {endpoint} 客户端异常：{type(exc).__name__}: {exc}",
                        category=category,
                        endpoint=endpoint,
                        error_msg=str(exc),
                    ) from exc
                self._close_default_socket()
                time.sleep(min(self.retry_base_seconds * (2**attempt), 30.0))
                self._login_once()

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self._query(
            "query_trade_dates",
            lambda: self.bs.query_trade_dates(start_date=start_date, end_date=end_date),
        )

    def stock_basic(self) -> pd.DataFrame:
        return self._query("query_stock_basic", self.bs.query_stock_basic)

    def all_stocks(self, day: str) -> pd.DataFrame:
        """Return BaoStock's date-specific securities list for one market day."""
        parsed_day = _parse_date(day)
        return self._query(
            f"query_all_stock({parsed_day})",
            lambda: self.bs.query_all_stock(day=parsed_day),
        )

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


def _write_json_atomic(value: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _verify_snapshot_chunk(
    chunk_path: Path,
    *,
    expected_snapshot_id: str,
    expected_symbols: list[str],
    expected_start: str,
    expected_end: str,
) -> dict[str, Any]:
    """Validate every committed chunk artifact before it is trusted on resume."""
    manifest_path = chunk_path / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"已存在的行情批次缺少 manifest.json，保留现场并停止：{chunk_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != expected_snapshot_id:
        raise ValueError(f"行情批次编号与预期不符：{chunk_path.name}")
    if manifest.get("date_range") != {"start": expected_start, "end": expected_end}:
        raise ValueError(f"行情批次 {chunk_path.name} 的日期范围与任务不符")
    if manifest.get("universe", {}).get("symbols") != expected_symbols:
        raise ValueError(f"行情批次 {chunk_path.name} 的证券清单与任务不符")
    hashes = manifest.get("file_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError(f"行情批次 {chunk_path.name} 缺少文件哈希清单")
    root = chunk_path.resolve()
    actual_files = {
        str(path.relative_to(chunk_path))
        for path in chunk_path.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if actual_files != set(hashes):
        missing = sorted(set(hashes) - actual_files)
        unlisted = sorted(actual_files - set(hashes))
        raise ValueError(
            f"行情批次 {chunk_path.name} 文件清单与哈希不一致；缺失={missing[:5]}，未登记={unlisted[:5]}"
        )
    for relative, expected_hash in hashes.items():
        artifact = (chunk_path / relative).resolve()
        if not artifact.is_relative_to(root):
            raise ValueError(f"行情批次 {chunk_path.name} 含越界文件路径：{relative}")
        if _sha256(artifact) != expected_hash:
            raise ValueError(f"行情批次 {chunk_path.name} 文件哈希不匹配：{relative}")
    if hashes.get("bars.csv") != manifest.get("normalized_bars_sha256"):
        raise ValueError(f"行情批次 {chunk_path.name} 的规范行情哈希字段不匹配")
    quality = manifest.get("quality", {})
    if not quality.get("healthy"):
        raise ValueError(
            f"行情批次 {chunk_path.name} 质量未通过：" + "; ".join(quality.get("errors", []))
        )
    return manifest


def _capture_policy(session: Any) -> dict[str, Any]:
    return {
        "socket_timeout_seconds": float(getattr(session, "socket_timeout_seconds", 20.0)),
        "minimum_interval_seconds": float(getattr(session, "minimum_interval_seconds", 1.0)),
        "max_retries": int(getattr(session, "max_retries", 0)),
        "retry_base_seconds": float(getattr(session, "retry_base_seconds", 0.0)),
        "daily_request_budget": int(getattr(session, "daily_request_budget", 49_000)),
    }


def _merge_sorted_bar_chunks(chunk_files: list[Path], output_path: Path) -> dict[str, Any]:
    """Merge date/symbol-sorted CSV chunks without loading the full history in memory."""
    if not chunk_files:
        raise ValueError("没有可合并的日线分块")
    handles = []
    readers = []
    try:
        header: list[str] | None = None
        for path in chunk_files:
            handle = path.open("r", encoding="utf-8", newline="")
            handles.append(handle)
            reader = csv.reader(handle)
            current_header = next(reader, None)
            if current_header is None:
                continue
            if header is None:
                header = current_header
            elif current_header != header:
                raise ValueError(f"分块行情字段不一致：{path}")
            readers.append((path, reader))
        if header is None or "trade_date" not in header or "symbol" not in header:
            raise ValueError("分块行情缺少 trade_date 或 symbol")
        date_index, symbol_index = header.index("trade_date"), header.index("symbol")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows_written = 0
        symbols: set[str] = set()
        dates: set[str] = set()
        previous_key: tuple[str, str] | None = None
        with output_path.open("w", encoding="utf-8", newline="") as output_handle:
            writer = csv.writer(output_handle)
            writer.writerow(header)
            merged = heapq.merge(
                *(reader for _, reader in readers),
                key=lambda row: (row[date_index], row[symbol_index]),
            )
            for row in merged:
                key = (row[date_index], row[symbol_index])
                if previous_key == key:
                    raise ValueError(f"合并行情存在重复 trade_date/symbol：{key}")
                if previous_key is not None and key < previous_key:
                    raise ValueError(f"分块行情排序异常：{key} 出现在 {previous_key} 之前")
                writer.writerow(row)
                rows_written += 1
                symbols.add(key[1])
                dates.add(key[0])
                previous_key = key
        if not rows_written:
            raise ValueError("所有快照批次均无有效日线，无法形成研究数据集")
        return {
            "row_count": rows_written,
            "symbol_count": len(symbols),
            "symbol_set": symbols,
            "date_set": dates,
            "bar_start": min(dates),
            "bar_end": max(dates),
        }
    finally:
        for handle in handles:
            handle.close()


def _parse_date(value: str) -> str:
    parsed = pd.to_datetime(value, format="%Y%m%d" if value.isdigit() and len(value) == 8 else None, errors="raise")
    return parsed.strftime("%Y-%m-%d")


def _create_baostock_snapshot_chunk(
    session: BaoStockSession,
    symbols: list[str],
    start_date: str,
    end_date: str,
    root: str | Path,
    snapshot_id: str | None = None,
    benchmark_symbol: str | None = None,
    shared_calendar: pd.DataFrame | None = None,
    shared_basics: pd.DataFrame | None = None,
    metadata_requests: list[dict[str, Any]] | None = None,
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
        raise ValueError("单个 BaoStock 快照批次最多支持 30 个代码")

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
        calendar = shared_calendar.copy() if shared_calendar is not None else session.trade_calendar(start, end)
        basics = shared_basics.copy() if shared_basics is not None else session.stock_basic()
        if calendar.empty:
            raise RuntimeError("BaoStock 未返回请求区间的交易日历")
        metadata_log = metadata_requests if metadata_requests is not None else session.request_log
        calendar_request = next(
            (entry for entry in metadata_log if entry["endpoint"] == "query_trade_dates"),
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
        missing_symbol_reasons: dict[str, str] = {}
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
                missing_symbol_reasons[symbol] = "NO_VALID_OHLC_ROWS_IN_REQUESTED_PERIOD"
                skipped_rows[symbol] = skipped
                continue
            bars.append(normalized)
            skipped_rows[symbol] = skipped

        if not bars:
            raise RuntimeError("所选代码在请求区间内均未返回有效原始 OHLC 行")

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

        normalized_bars = normalize_bar_frame(
            pd.concat(bars, ignore_index=True) if bars else pd.DataFrame(columns=BAR_COLUMNS)
        )
        quality = check_data_quality(normalized_bars, calendar=calendar_normalized)
        if not quality["healthy"] and not normalized_bars.empty:
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
            "capture_policy": _capture_policy(session),
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
                "symbols_without_valid_bars": missing_symbol_reasons,
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
    except BaseException:
        import shutil

        shutil.rmtree(temp_path, ignore_errors=True)
        raise


def create_baostock_snapshot(
    session: BaoStockSession,
    symbols: list[str],
    start_date: str,
    end_date: str,
    root: str | Path,
    snapshot_id: str | None = None,
    benchmark_symbol: str | None = None,
    batch_size: int = 30,
    max_batches: int | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Create a fixed-symbol snapshot, splitting large requests into resumable chunks."""
    start, end = _parse_date(start_date), _parse_date(end_date)
    if start > end:
        raise ValueError("start_date 必须不晚于 end_date")
    normalized_symbols = list(dict.fromkeys(canonical_symbol(symbol) for symbol in symbols))
    if not normalized_symbols:
        raise ValueError("至少提供一个证券代码")
    if not 1 <= batch_size <= 30:
        raise ValueError("batch_size 必须在 1 到 30 之间")
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches 必须大于 0")
    if resume and not snapshot_id:
        raise ValueError("续跑必须指定与初次运行相同的 --snapshot-id")

    created_at = datetime.now(timezone.utc)
    identifier = snapshot_id or f"baostock-{start.replace('-', '')}-{end.replace('-', '')}-{created_at:%Y%m%dT%H%M%SZ}"
    if not identifier or any(part in identifier for part in ("/", "\\", "..")):
        raise ValueError("snapshot_id 只能使用路径安全的名称")
    # Keep the historic one-folder shape for small, one-shot snapshots.
    if len(normalized_symbols) <= batch_size and max_batches is None and not resume:
        return _create_baostock_snapshot_chunk(
            session, normalized_symbols, start, end, root, identifier, benchmark_symbol,
        )

    parent = Path(root).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / identifier
    partial = parent / f".{identifier}.partial"
    if destination.exists():
        raise FileExistsError(f"快照已完成，拒绝覆盖：{destination}")
    request = {
        "snapshot_id": identifier,
        "start_date": start,
        "end_date": end,
        "symbols": normalized_symbols,
        "benchmark_symbol": canonical_symbol(benchmark_symbol) if benchmark_symbol else None,
        "batch_size": batch_size,
    }
    checkpoint_path = partial / "batch_checkpoint.json"
    if resume:
        if not partial.exists() or not checkpoint_path.exists():
            raise FileNotFoundError(f"找不到可续跑快照：{partial}")
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("request") != request:
            raise ValueError("续跑参数必须与初次运行一致（日期、代码、基准和批大小）")
    else:
        if partial.exists():
            raise FileExistsError(f"发现未完成快照；请添加 --resume：{partial}")
        partial.mkdir()
        checkpoint = {"request": request, "created_at_utc": created_at.isoformat(timespec="seconds")}
        _write_json_atomic(checkpoint, checkpoint_path)

    chunks = [normalized_symbols[i:i + batch_size] for i in range(0, len(normalized_symbols), batch_size)]
    chunk_ids = {f"chunk-{i:05d}" for i in range(1, len(chunks) + 1)}
    verified_chunk_manifests: dict[str, dict[str, Any]] = {}
    verified_chunk_manifest_hashes: dict[str, str] = {}
    if resume:
        recorded_ids = checkpoint.get("completed_chunks", [])
        if not isinstance(recorded_ids, list) or len(recorded_ids) != len(set(recorded_ids)):
            raise ValueError("批次 checkpoint 中的 completed_chunks 格式无效或存在重复项")
        unexpected_ids = sorted(set(recorded_ids) - chunk_ids)
        if unexpected_ids:
            raise ValueError("批次 checkpoint 引用了请求范围之外的批次：" + ", ".join(unexpected_ids))
        recorded_hashes = checkpoint.get("completed_chunk_manifest_sha256", {})
        if not isinstance(recorded_hashes, dict):
            raise ValueError("批次 checkpoint 中的 manifest 哈希映射格式无效")
        for index, chunk_symbols in enumerate(chunks, start=1):
            chunk_id = f"chunk-{index:05d}"
            chunk_path = partial / chunk_id
            manifest_path = chunk_path / "manifest.json"
            if not chunk_path.exists():
                if chunk_id in recorded_ids:
                    raise ValueError(f"checkpoint 标记已完成但批次目录缺失：{chunk_id}")
                continue
            if not manifest_path.exists():
                raise ValueError(f"发现未封存的批次目录，停止以保留现场：{chunk_path}")
            manifest = _verify_snapshot_chunk(
                chunk_path,
                expected_snapshot_id=chunk_id,
                expected_symbols=chunk_symbols,
                expected_start=start,
                expected_end=end,
            )
            manifest_hash = _sha256(manifest_path)
            checkpoint_hash = recorded_hashes.get(chunk_id)
            if checkpoint_hash is not None and checkpoint_hash != manifest_hash:
                raise ValueError(f"行情批次 {chunk_id} 的 manifest 哈希与 checkpoint 不一致")
            verified_chunk_manifests[chunk_id] = manifest
            verified_chunk_manifest_hashes[chunk_id] = manifest_hash
        unverified_recorded = sorted(set(recorded_ids) - set(verified_chunk_manifests))
        if unverified_recorded:
            raise ValueError("以下已登记批次未能完成哈希核验：" + ", ".join(unverified_recorded))
        # Older checkpoints did not contain manifest hashes. After verifying all
        # files against each immutable chunk manifest, seed the stronger record.
        checkpoint["completed_chunks"] = sorted(verified_chunk_manifests)
        checkpoint["completed_chunk_manifest_sha256"] = verified_chunk_manifest_hashes
        checkpoint["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json_atomic(checkpoint, checkpoint_path)

    shared_dir = partial / "shared"
    shared_dir.mkdir(exist_ok=True)
    shared_calendar_path = shared_dir / "trade_calendar.csv"
    shared_basics_path = shared_dir / "stock_basic_all_types.csv"
    if not shared_calendar_path.exists() or not shared_basics_path.exists():
        session.request_log.clear()
        shared_calendar = session.trade_calendar(start, end)
        shared_basics = session.stock_basic()
        if shared_calendar.empty:
            raise RuntimeError("BaoStock 未返回请求区间的交易日历")
        _write_source_csv(shared_calendar, shared_calendar_path)
        _write_source_csv(shared_basics, shared_basics_path)
        checkpoint["metadata_requests"] = list(session.request_log)
        session.request_log.clear()
        _write_json_atomic(checkpoint, checkpoint_path)
    else:
        shared_calendar = pd.read_csv(shared_calendar_path, dtype=str)
        shared_basics = pd.read_csv(shared_basics_path, dtype=str, keep_default_na=False)
    metadata_requests = list(checkpoint.get("metadata_requests", []))

    completed_this_call = 0
    chunk_summaries: list[dict[str, Any]] = []
    for index, chunk_symbols in enumerate(chunks, start=1):
        chunk_id = f"chunk-{index:05d}"
        chunk_path = partial / chunk_id
        if (chunk_path / "manifest.json").exists():
            chunk_manifest = verified_chunk_manifests.get(chunk_id)
            if chunk_manifest is None:
                chunk_manifest = _verify_snapshot_chunk(
                    chunk_path,
                    expected_snapshot_id=chunk_id,
                    expected_symbols=chunk_symbols,
                    expected_start=start,
                    expected_end=end,
                )
                manifest_hash = _sha256(chunk_path / "manifest.json")
                verified_chunk_manifest_hashes[chunk_id] = manifest_hash
                verified_chunk_manifests[chunk_id] = chunk_manifest
            chunk_summaries.append({
                "chunk_id": chunk_id,
                "symbols": chunk_symbols,
                "snapshot_path": chunk_id,
                "normalized_bar_rows": chunk_manifest["coverage"]["normalized_bar_rows"],
                "capture_policy": chunk_manifest.get("capture_policy"),
                "requests": chunk_manifest["requests"],
            })
            continue
        if max_batches is not None and completed_this_call >= max_batches:
            break
        session.request_log.clear()
        try:
            chunk_result = _create_baostock_snapshot_chunk(
                session,
                chunk_symbols,
                start,
                end,
                partial,
                chunk_id,
                benchmark_symbol if index == 1 else None,
                shared_calendar=shared_calendar,
                shared_basics=shared_basics,
                metadata_requests=metadata_requests,
            )
        except (Exception, KeyboardInterrupt) as exc:
            failed_attempts = checkpoint.setdefault("failed_chunk_attempts", [])
            attempt_number = 1 + sum(
                1 for item in failed_attempts if item.get("chunk_id") == chunk_id
            )
            attempt_requests = [dict(item) for item in session.request_log]
            failed_attempts.append({
                "chunk_id": chunk_id,
                "attempt_number": attempt_number,
                "recorded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": {
                    "category": (
                        "operator_interrupted"
                        if isinstance(exc, KeyboardInterrupt)
                        else getattr(exc, "category", "client_or_validation")
                    ),
                    "endpoint": getattr(exc, "endpoint", None),
                    "error_code": getattr(exc, "error_code", None),
                    "message": str(exc) or type(exc).__name__,
                },
                "request_count": sum(int(item.get("request_count", 1)) for item in attempt_requests),
                "attempt_count": len(attempt_requests),
                "requests": attempt_requests,
            })
            checkpoint["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                _write_json_atomic(checkpoint, checkpoint_path)
            except Exception as checkpoint_error:
                if hasattr(exc, "add_note"):
                    exc.add_note(f"此外，批次失败日志写入断点也失败：{checkpoint_error}")
            raise
        committed_manifest = _verify_snapshot_chunk(
            chunk_path,
            expected_snapshot_id=chunk_id,
            expected_symbols=chunk_symbols,
            expected_start=start,
            expected_end=end,
        )
        verified_chunk_manifests[chunk_id] = committed_manifest
        verified_chunk_manifest_hashes[chunk_id] = _sha256(chunk_path / "manifest.json")
        chunk_summaries.append({
            "chunk_id": chunk_id,
            "symbols": chunk_symbols,
            "snapshot_path": chunk_id,
            "normalized_bar_rows": chunk_result["coverage"]["normalized_bar_rows"],
            "capture_policy": chunk_result.get("capture_policy"),
            # A new chunk result may expose the session's mutable request_log.
            # Copy it before the next iteration clears/reuses that list.
            "requests": list(chunk_result["requests"]),
        })
        completed_this_call += 1
        checkpoint["completed_chunks"] = sorted(verified_chunk_manifests)
        checkpoint["completed_chunk_manifest_sha256"] = verified_chunk_manifest_hashes
        checkpoint["updated_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_json_atomic(checkpoint, checkpoint_path)

    completed_ids = {item["chunk_id"] for item in chunk_summaries}
    missing_chunks = [f"chunk-{i:05d}" for i in range(1, len(chunks) + 1) if f"chunk-{i:05d}" not in completed_ids]
    if missing_chunks:
        return {
            "state": "partial",
            "snapshot_id": identifier,
            "partial_path": str(partial),
            "completed_chunks": len(completed_ids),
            "total_chunks": len(chunks),
            "next_chunks": missing_chunks[:5],
            "resume_command": f"cehuan snapshot --snapshot-id {identifier} --resume ...",
        }

    chunk_summaries.sort(key=lambda item: item["chunk_id"])
    first_chunk = partial / chunk_summaries[0]["chunk_id"]
    calendar_path = first_chunk / "trading_calendar.csv"
    calendar = pd.read_csv(calendar_path)
    merge_summary = _merge_sorted_bar_chunks(
        [partial / item["chunk_id"] / "bars.csv" for item in chunk_summaries],
        partial / "bars.csv",
    )
    chunk_manifests = [
        json.loads((partial / item["chunk_id"] / "manifest.json").read_text(encoding="utf-8"))
        for item in chunk_summaries
    ]
    chunk_qualities = [item.get("quality", {}) for item in chunk_manifests]
    calendar_dates = set(
        pd.to_datetime(calendar["trade_date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d")
    )
    is_open = calendar["is_open"].astype(str).str.lower().isin({"true", "1"})
    open_dates = set(pd.to_datetime(calendar.loc[is_open, "trade_date"], errors="coerce").dropna().dt.strftime("%Y-%m-%d"))
    missing_calendar_dates = sorted(merge_summary["date_set"] - calendar_dates)
    bars_on_closed_dates = sorted((merge_summary["date_set"] & calendar_dates) - open_dates)
    calendar_errors = []
    if missing_calendar_dates:
        calendar_errors.append("行情日期不在导入的交易日历中：" + ", ".join(missing_calendar_dates[:10]))
    if bars_on_closed_dates:
        calendar_errors.append("行情出现在日历标记的休市日：" + ", ".join(bars_on_closed_dates[:10]))
    errors = [error for item in chunk_qualities for error in item.get("errors", [])] + calendar_errors
    warnings = sorted({warning for item in chunk_qualities for warning in item.get("warnings", [])})
    retrieved_values = [item.get("retrieved_at_utc") for item in chunk_qualities if item.get("retrieved_at_utc")]
    required_names = ("trade_date", "symbol", "open", "high", "low", "close", "volume_shares")
    quality = {
        "healthy": not errors,
        "row_count": merge_summary["row_count"],
        "symbol_count": merge_summary["symbol_count"],
        "duplicate_count": 0,
        "missing_required": {column: 0 for column in required_names},
        "as_of": merge_summary["bar_end"],
        "retrieved_at_utc": max(retrieved_values) if retrieved_values else None,
        "retrieved_before_available_count": sum(int(item.get("retrieved_before_available_count", 0)) for item in chunk_qualities),
        "errors": errors,
        "warnings": warnings,
        "calendar_note": "未提供交易所交易日历时，不对非交易日缺口作结论。",
        "calendar_validation": {
            "healthy": not calendar_errors,
            "calendar_source": ", ".join(sorted(set(calendar.get("source", pd.Series(dtype=str)).dropna().astype(str)))),
            "calendar_start": str(calendar["trade_date"].min()) if not calendar.empty else None,
            "calendar_end": str(calendar["trade_date"].max()) if not calendar.empty else None,
            "bar_date_count": len(merge_summary["date_set"]),
            "missing_calendar_dates": missing_calendar_dates,
            "bars_on_closed_dates": bars_on_closed_dates,
            "errors": calendar_errors,
        },
    }
    if not quality["healthy"]:
        raise ValueError("合并快照质量检查未通过：" + "; ".join(errors))
    _write_source_csv(calendar, partial / "trading_calendar.csv")
    (partial / "quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    benchmark_file = next((partial / item["chunk_id"] / "benchmark.csv" for item in chunk_summaries if (partial / item["chunk_id"] / "benchmark.csv").exists()), None)
    benchmark_manifest = next((json.loads((partial / item["chunk_id"] / "manifest.json").read_text(encoding="utf-8")) for item in chunk_summaries if (partial / item["chunk_id"] / "manifest.json").exists() and item["chunk_id"] == "chunk-00001"), {})
    if benchmark_file is not None:
        _write_source_csv(pd.read_csv(benchmark_file), partial / "benchmark.csv")
        benchmark_quality_path = partial / "chunk-00001" / "benchmark_quality.json"
        if benchmark_quality_path.exists():
            (partial / "benchmark_quality.json").write_bytes(benchmark_quality_path.read_bytes())
    files = [p for p in sorted(partial.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "batch_checkpoint.json"}]
    hashes = {str(p.relative_to(partial)): _sha256(p) for p in files}
    requests = list(metadata_requests)
    for failure in checkpoint.get("failed_chunk_attempts", []):
        for request in failure.get("requests", []):
            requests.append({
                **request,
                "snapshot_chunk_id": failure.get("chunk_id"),
                "snapshot_chunk_attempt": failure.get("attempt_number"),
                "recorded_as_failed_chunk_attempt": True,
            })
    requests.extend(request for chunk in chunk_summaries for request in chunk["requests"])
    request_summary = {
        "request_count": sum(int(request.get("request_count", 1)) for request in requests),
        "attempt_count": len(requests),
        "failed_attempt_count": sum(
            request.get("status") in {"failed", "interrupted"} for request in requests
        ),
        "failed_chunk_attempt_count": len(checkpoint.get("failed_chunk_attempts", [])),
    }
    capture_policy_by_chunk = {
        item["chunk_id"]: json.loads((partial / item["chunk_id"] / "manifest.json").read_text(encoding="utf-8")).get("capture_policy")
        for item in chunk_summaries
    }
    manifest = {
        "snapshot_id": identifier,
        "created_at_utc": checkpoint.get("created_at_utc"),
        "manifest_generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": "BaoStock",
        "provider_package_version": session.package_version,
        "capture_policy_by_chunk": capture_policy_by_chunk,
        "api_endpoints": ["query_trade_dates", "query_stock_basic", "query_history_k_data_plus", "query_adjust_factor"],
        "date_range": {"start": start, "end": end},
        "universe": {
            "selection": "explicit fixed symbols supplied to snapshot; each symbol is processed in resumable chunks",
            "symbols": normalized_symbols,
            "security_basic_type_filter": "type=1 (stock)",
            "batch_size": batch_size,
            "chunks": [{k: v for k, v in item.items() if k != "requests"} for item in chunk_summaries],
        },
        "benchmark": {
            "symbol": canonical_symbol(benchmark_symbol) if benchmark_symbol else None,
            "row_count": len(pd.read_csv(partial / "benchmark.csv")) if (partial / "benchmark.csv").exists() else None,
            "file": "benchmark.csv" if (partial / "benchmark.csv").exists() else None,
            "price_field": "unadjusted index close; no constituent or dividend return adjustment",
        },
        "coverage": {
            "normalized_bar_rows": merge_summary["row_count"],
            "symbols_with_bars": merge_summary["symbol_count"],
            "symbols_without_valid_bars": {
                symbol: reason
                for item in chunk_summaries
                for symbol, reason in json.loads(
                    (partial / item["chunk_id"] / "manifest.json").read_text(encoding="utf-8")
                )["coverage"].get("symbols_without_valid_bars", {}).items()
            },
            "bar_start": merge_summary["bar_start"],
            "bar_end": merge_summary["bar_end"],
            "calendar_rows": int(len(calendar)),
            "open_sessions": int(calendar["is_open"].astype(bool).sum()) if calendar["is_open"].dtype == bool else int(calendar["is_open"].astype(str).str.lower().eq("true").sum()),
        },
        "field_mapping": {
            "volume": "shares; amount: CNY",
            "execution_prices": "unadjusted OHLC",
            "signal_price": "adjustflag=1 back-adjusted close",
            "is_suspended": "tradestatus=0",
            "is_st": "isST",
            "historical_universe": "not provided by this fixed-symbol snapshot; use universe-snapshot for date-specific membership",
            "price_limit_state_and_pct": "unknown",
            "share_lot_rules": "unknown",
        },
        "requests": requests,
        "request_summary": request_summary,
        "failed_chunk_attempts": [
            {key: value for key, value in attempt.items() if key != "requests"}
            for attempt in checkpoint.get("failed_chunk_attempts", [])
        ],
        "quality": quality,
        "benchmark_quality": benchmark_manifest.get("benchmark_quality"),
        "file_sha256": hashes,
        "normalized_bars_sha256": hashes["bars.csv"],
        "data_scope_warning": "Fixed-symbol bars only; this snapshot does not on its own remove survivorship bias.",
    }
    (partial / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, destination)
    return {"snapshot_path": str(destination), **manifest}
