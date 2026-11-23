"""固定 10 只证券的前瞻采集器。它按登记时间窗口串行请求 BaoStock 日历、成员、证券基础资料及原始/后复权行情，逐次留存 SDK 响应、失败原因和哈希；不采集池外数据，未达门槛时不封为可排名输入。"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.baostock_adapter import (
    BaoStockSession,
    normalize_baostock_calendar,
    normalize_baostock_history,
)
from cehuan.data.canonical import BAR_COLUMNS, canonical_symbol, normalize_bar_frame, write_bars
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.research.lookback_pair import EXPERIMENT_ID, FIXED_SYMBOLS


SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_START = "2026-01-01"
CALENDAR_END = "2027-03-31"
RAW_HISTORY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
CAPTURE_INTERFACES = {
    "bars": "query_history_k_data_plus",
    "calendar": "query_trade_dates",
    "security_basic": "query_stock_basic(code=<fixed symbol>)",
    "universe": "query_all_stock",
}


def capture_lookback_pair_inputs(
    *,
    output_dir: str | Path,
    registration_freeze_path: str | Path,
    config_path: str | Path,
    pool_path: str | Path,
    identity_events_path: str | Path,
    signal_date: str = "2026-12-17",
    resume: bool = False,
    now: datetime | None = None,
    clock_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    session_factory: Callable[..., Any] = BaoStockSession,
) -> dict[str, Any]:
    """按冻结登记采集一次 lookback 配对实验输入。参数：output_dir 为唯一归档目录，freeze/config/pool/identity 路径绑定登记输入，signal_date 固定观察日，resume 控制续跑，now/clock_fn 提供受控时钟，session_factory 注入数据会话。返回：采集状态和 manifest。副作用：发起有限 BaoStock 请求并保存每次响应。异常：日期/时段、目录、锁或冻结哈希不合法时阻断；不调用排名器。"""
    signal_day = date.fromisoformat(signal_date)
    if signal_date != "2026-12-17":
        raise ValueError("本采集器只适用于登记冻结的 2026-12-17")
    target = Path(output_dir).expanduser().resolve()
    started = _utc(now) if now else datetime.now(timezone.utc)
    local = started.astimezone(SHANGHAI)
    if target.exists() and not resume:
        raise FileExistsError(f"采集目录已存在，拒绝覆盖：{target}")
    if not target.exists() and resume:
        raise FileNotFoundError(f"--resume 指定的采集目录不存在：{target}")
    if local.date() != signal_day:
        raise ValueError("采集入口只允许在登记的实际信号日运行；当前不是 2026-12-17")
    if local.time() < time(15, 6):
        raise ValueError("首采窗口从信号日 15:06 开始；尚未开始，不创建采集目录")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.mkdir()

    lock_path = target / ".capture.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("该采集目录已有运行中的采集进程") from exc
        try:
            return _capture_locked(
                target=target,
                signal_day=signal_day,
                config_path=Path(config_path).resolve(),
                pool_path=Path(pool_path).resolve(),
                identity_path=Path(identity_events_path).resolve(),
                freeze_path=Path(registration_freeze_path).resolve(),
                resume=resume,
                now=started,
                clock_fn=clock_fn,
                session_factory=session_factory,
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _capture_locked(
    *, target: Path, signal_day: date, config_path: Path, pool_path: Path,
    identity_path: Path, freeze_path: Path, resume: bool, now: datetime,
    clock_fn: Callable[[], datetime],
    session_factory: Callable[..., Any],
) -> dict[str, Any]:
    """在目录独占锁内执行采集状态机。参数：target、signal_day 和冻结输入路径定义运行，resume/now/clock_fn/session_factory 控制续跑和请求。返回：运行目录与最终进度。副作用：读写进度、原始响应及清单；到截止时间后只封存失败/待数据，不延长时段。"""
    local = now.astimezone(SHANGHAI)
    stage = _scheduled_stage(local)
    if local.time() >= time(15, 45):
        stage = 4
    final_manifest = target / "capture-manifest.json"
    if final_manifest.exists():
        raise FileExistsError("该采集目录已经封存，不允许追加或覆盖")
    progress_path = target / "capture-progress.json"
    if resume and not progress_path.is_file():
        raise ValueError("采集目录缺少 capture-progress.json，不能续跑")

    freeze = _load_freeze(freeze_path)
    inputs = {
        "registration_freeze": _sha(freeze_path),
        "config": _sha_required(config_path, "strategy config"),
        "pool": _sha_required(pool_path, "fixed pool"),
        "identity_events": _sha_required(identity_path, "identity events"),
    }
    _validate_freeze_inputs(freeze, config_path, pool_path, inputs)
    if resume:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress.get("experiment_id") != EXPERIMENT_ID or progress.get("signal_date") != signal_day.isoformat():
            raise ValueError("续跑目录与固定实验 ID 或信号日不一致")
        if progress.get("source_sha256") != inputs:
            raise ValueError("续跑时冻结源文件发生变化；不得在本目录续跑")
        if stage <= int(progress.get("last_stage", 0)):
            raise ValueError("该采集阶段已执行或时间倒退；拒绝重复请求")
    else:
        if stage != 1:
            progress = _initial_progress(target, signal_day, inputs, now)
            progress["status"] = "NEEDS_DATA_MISSED_INITIAL_CAPTURE_WINDOW"
            progress["blocking_reasons"].append("未在 15:06—15:20 首采窗口启动；固定日期不补采")
            progress["last_stage"] = stage
            _write_progress(progress_path, progress)
            _write_stage_manifest(target, stage, progress)
            _write_capture_failure_manifest(target, progress, now)
            return {"output_dir": str(target), "manifest": progress}
        progress = _initial_progress(target, signal_day, inputs, now)

    if local.date() != signal_day:
        progress["status"] = "NEEDS_DATA_NOT_SIGNAL_DATE"
        progress["blocking_reasons"].append("只允许在登记的实际信号日运行采集")
        progress["last_stage"] = stage
        _write_progress(progress_path, progress)
        _write_stage_manifest(target, stage, progress)
        return {"output_dir": str(target), "manifest": progress}
    if stage == 4:
        progress["status"] = "NEEDS_DATA"
        progress["blocking_reasons"].append("15:45 采集截止已到；未完成请求不再补采")
        progress["last_stage"] = 4
        _write_progress(progress_path, progress)
        _write_stage_manifest(target, 4, progress)
        _write_capture_failure_manifest(target, progress, _utc(clock_fn()))
        return {"output_dir": str(target), "manifest": progress}

    if not resume:
        _write_progress(progress_path, progress)
    if stage == 1:
        _attempt_calendar(progress, target, session_factory, stage, now, clock_fn)
        if progress["requests"].get("calendar", {}).get("status") == "SUCCESS":
            _build_dependent_requests(progress, target)
            _attempt_pending(progress, target, session_factory, stage, now, clock_fn)
    elif "calendar" in progress["requests"] and progress["requests"]["calendar"]["status"] == "FAILED":
        _attempt_calendar(progress, target, session_factory, stage, now, clock_fn)
        if progress["requests"].get("calendar", {}).get("status") == "SUCCESS":
            # Dependent first attempts are intentionally not shifted into a retry window.
            progress["blocking_reasons"].append(
                "日历仅在重试窗口恢复，依赖输入未在首采窗口请求；本观察日保持 NEEDS_DATA",
            )
    else:
        _attempt_pending(progress, target, session_factory, stage, now, clock_fn)

    progress["last_stage"] = stage
    progress["last_attempt_at_utc"] = _iso(_utc(clock_fn()))
    _write_progress(progress_path, progress)
    if _all_required_requests_succeeded(progress) and not progress["blocking_reasons"]:
        sealed_at = _utc(clock_fn())
        sealed_local = sealed_at.astimezone(SHANGHAI)
        if sealed_local.date() != signal_day or sealed_local.time() >= time(16, 0):
            progress["status"] = "NEEDS_DATA_SEAL_CUTOFF"
            progress["blocking_reasons"].append("16:00 封存截止已到；保留已取得响应但不宣称按时封存")
            _write_progress(progress_path, progress)
            _write_capture_failure_manifest(target, progress, sealed_at)
            _write_stage_manifest(target, stage, progress)
            return {"output_dir": str(target), "manifest": progress}
        try:
            _seal_capture(target, progress, config_path, pool_path, identity_path, freeze_path, sealed_at)
            _write_stage_manifest(target, stage, progress)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            progress["status"] = "NEEDS_DATA_SEAL_FAILED"
            progress["blocking_reasons"].append(f"封存校验失败：{type(exc).__name__}: {exc}")
            _write_progress(progress_path, progress)
            if not final_manifest.exists():
                _write_capture_failure_manifest(target, progress, sealed_at)
            stage_path = target / f"stage-{stage:02d}-manifest.json"
            if not stage_path.exists():
                _write_stage_manifest(target, stage, progress)
    elif stage == 3 or local.time() >= time(15, 45):
        progress["status"] = "NEEDS_DATA"
        progress["blocking_reasons"].extend(_pending_reasons(progress))
        _write_progress(progress_path, progress)
        _write_capture_failure_manifest(target, progress, _utc(clock_fn()))
        _write_stage_manifest(target, stage, progress)
    else:
        progress["status"] = f"PARTIAL_CAPTURE_RETRY_{stage + 1}_PENDING"
        _write_progress(progress_path, progress)
        _write_stage_manifest(target, stage, progress)
    return {"output_dir": str(target), "manifest": progress}


def _initial_progress(target: Path, signal_day: date, source_sha: dict[str, str], now: datetime) -> dict[str, Any]:
    """构造首次采集进度清单。参数：target 为新目录，signal_day 为登记日，source_sha 为冻结输入摘要，now 为开始时点。返回：初始状态字典。副作用：创建 raw/request 子目录；发布时点明确为未知。"""
    (target / "raw" / "requests").mkdir(parents=True, exist_ok=True)
    return {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "signal_date": signal_day.isoformat(),
        "classification": "FORWARD_PAPER_CAPTURE_ONLY",
        "status": "CAPTURE_IN_PROGRESS",
        "started_at_utc": _iso(now),
        "started_at_asia_shanghai": now.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "source_sha256": source_sha,
        "fixed_symbols": list(FIXED_SYMBOLS),
        "requested_calendar_range": {"start": CALENDAR_START, "end": CALENDAR_END},
        "provider": "BaoStock",
        "provider_package_version": None,
        "provider_published_at_utc": None,
        "provider_publication_evidence": "UNVERIFIED",
        "trusted_external_timestamp": "UNVERIFIED",
        "capture_layer": "BaoStock SDK DataFrame response serialized to CSV; not wire bytes",
        "request_schedule": {
            "stage_1": "15:06-15:19 local",
            "stage_2": "15:20-15:34 local; retry failed requests only",
            "stage_3": "15:35-15:44 local; final retry failed requests only",
            "request_cutoff": "15:45 local",
            "max_attempts_per_request": 3,
            "session_minimum_interval_seconds": 1.0,
        },
        "requests": {},
        "session_events": [],
        "blocking_reasons": [],
        "rankings_generated": False,
        "returns_computed": False,
        "orders_generated": False,
        "broker_connected": False,
        "formal_baseline": "NOT_READY",
    }


def _scheduled_stage(local: datetime) -> int:
    """根据上海本地时间选择登记的采集阶段。参数：local 为本地时间。返回：阶段编号；窗口外为 0。"""
    if local.time() < time(15, 6) or local.time() >= time(15, 45):
        return 0
    if local.time() < time(15, 20):
        return 1
    if local.time() < time(15, 35):
        return 2
    return 3


def _inside_stage_window(stage: int, value: datetime) -> bool:
    """判断某次尝试是否仍处于登记阶段内。参数：stage 为阶段编号，value 为带时区时间。返回：是否在固定日期和时间窗内；不会扩展截止时点。"""
    local = _utc(value).astimezone(SHANGHAI)
    if local.date().isoformat() != "2026-12-17":
        return False
    current = local.time()
    return {
        1: time(15, 6) <= current < time(15, 20),
        2: time(15, 20) <= current < time(15, 35),
        3: time(15, 35) <= current < time(15, 45),
    }.get(stage, False)


def _attempt_calendar(
    progress: dict[str, Any], target: Path, session_factory: Callable[..., Any], stage: int,
    now: datetime, clock_fn: Callable[[], datetime],
) -> None:
    """在允许时窗内尝试一次交易日历请求并据此生成依赖请求。参数：progress/target 保存状态，session_factory 建立会话，stage/now/clock_fn 控制时间。返回：None。副作用：记录原始响应及请求状态；窗口关闭时记录未尝试。"""
    if not _inside_stage_window(stage, clock_fn()):
        record = progress["requests"].setdefault("calendar", _new_request())
        record["status"] = "NOT_ATTEMPTED_WINDOW_CLOSED"
        progress["capture_cutoff_reached"] = True
        return
    _attempt_request(
        progress, target, session_factory, stage, now, "calendar",
        lambda session: session.trade_calendar(CALENDAR_START, CALENDAR_END),
        clock_fn,
    )
    if progress["requests"].get("calendar", {}).get("status") == "SUCCESS":
        _derive_signal_sessions(progress, target)


def _derive_signal_sessions(progress: dict[str, Any], target: Path) -> None:
    """规范化成功的日历响应，推导信号日前 20 个及信号日共 21 个开市日。参数：progress 保存日历请求，target 为运行目录。返回：None；结果写入进度并建立依赖请求。异常数据保持 FAILED/NEEDS_DATA，不猜测缺日。"""
    record = progress["requests"]["calendar"]
    try:
        frame = _read_success_response(target, record)
        normalized = normalize_baostock_calendar(frame, record["attempts"][-1]["completed_at_utc"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reason = f"BaoStock 日历响应无法规范化：{type(exc).__name__}"
        record["status"] = "FAILED"
        record["validation_error"] = reason
        progress["calendar_validation_error"] = reason
        progress["signal_sessions"] = []
        _write_progress(target / "capture-progress.json", progress)
        return
    opens = normalized.loc[normalized["is_open"], "trade_date"].tolist()
    signal_day = date.fromisoformat(progress["signal_date"])
    if signal_day not in opens:
        reason = "BaoStock 日历响应未覆盖或未标记固定信号日为开市日"
        record["status"] = "FAILED"
        record["validation_error"] = reason
        progress["calendar_validation_error"] = reason
        progress["signal_sessions"] = []
        _write_progress(target / "capture-progress.json", progress)
        return
    position = opens.index(signal_day)
    sessions = opens[max(0, position - 20): position + 1]
    progress["calendar_rows"] = int(len(normalized))
    progress["calendar_first_date"] = str(normalized["trade_date"].min()) if not normalized.empty else None
    progress["calendar_last_date"] = str(normalized["trade_date"].max()) if not normalized.empty else None
    progress["calendar_open_sessions_after_signal"] = int(sum(day > signal_day for day in opens))
    progress["calendar_outcome_horizon_status"] = (
        "VERIFIED_20_OPEN_SESSIONS" if sum(day > signal_day for day in opens) >= 20 else "NEEDS_DATA"
    )
    if len(sessions) != 21:
        reason = f"BaoStock 日历信号窗口不足 21 个开市日，实际 {len(sessions)}"
        record["status"] = "FAILED"
        record["validation_error"] = reason
        progress["calendar_validation_error"] = reason
        progress["signal_sessions"] = [day.isoformat() for day in sessions]
        _write_progress(target / "capture-progress.json", progress)
        return
    record.pop("validation_error", None)
    progress.pop("calendar_validation_error", None)
    progress["signal_sessions"] = [day.isoformat() for day in sessions]
    if not progress.get("dependent_requests_built"):
        _build_dependent_requests(progress, target)


def _build_dependent_requests(progress: dict[str, Any], target: Path) -> None:
    """基于完整 21 日会话表建立固定证券的基础资料、成员和双口径行情请求项。参数：progress 为采集进度，target 为运行目录。返回：None；重复调用通过 setdefault 保持幂等。"""
    sessions = progress.get("signal_sessions", [])
    if len(sessions) != 21:
        return
    requests = progress["requests"]
    for symbol in FIXED_SYMBOLS:
        requests.setdefault(f"stock_basic:{symbol}", _new_request())
    for day in sessions:
        requests.setdefault(f"membership:{day}", _new_request())
    for symbol in FIXED_SYMBOLS:
        requests.setdefault(f"bars_raw:{symbol}", _new_request())
        requests.setdefault(f"bars_adjusted:{symbol}", _new_request())
    progress["dependent_requests_built"] = True
    _write_progress(target / "capture-progress.json", progress)


def _new_request() -> dict[str, Any]:
    """创建单个请求的初始状态。返回：PENDING 状态及空尝试列表。"""
    return {"status": "PENDING", "attempts": []}


def _attempt_pending(
    progress: dict[str, Any], target: Path, session_factory: Callable[..., Any], stage: int,
    now: datetime, clock_fn: Callable[[], datetime],
) -> None:
    """在给定阶段重试尚未成功且仍有额度的冻结请求。参数：progress/target 保存请求，session_factory 建立串行会话，stage/now/clock_fn 控制时间。返回：None。副作用：最多每项三次、仅失败项进入后续阶段，并保留请求日志和原响应。"""
    if not progress.get("dependent_requests_built"):
        return
    tasks: list[tuple[str, Callable[[Any], pd.DataFrame]]] = [
        (f"stock_basic:{symbol}", lambda s, symbol=symbol: s.stock_basic(symbol))
        for symbol in FIXED_SYMBOLS
    ]
    for day in progress["signal_sessions"]:
        tasks.append((f"membership:{day}", lambda s, day=day: s.all_stocks(day)))
    start, end = progress["signal_sessions"][0], progress["signal_sessions"][-1]
    for symbol in FIXED_SYMBOLS:
        tasks.append((f"bars_raw:{symbol}", lambda s, symbol=symbol: s.history(symbol, start, end, adjustflag="3", fields=RAW_HISTORY_FIELDS)))
        tasks.append((f"bars_adjusted:{symbol}", lambda s, symbol=symbol: s.history(symbol, start, end, adjustflag="1", fields=RAW_HISTORY_FIELDS)))
    pending = [
        (key, call) for key, call in tasks
        if key in progress["requests"]
        and progress["requests"][key]["status"] != "SUCCESS"
        and len(progress["requests"][key]["attempts"]) < 3
        and (stage == 1 or progress["requests"][key]["status"] == "FAILED")
        and not any(item["stage"] == stage for item in progress["requests"][key]["attempts"])
    ]
    if not pending:
        return
    if not _inside_stage_window(stage, clock_fn()):
        progress["capture_cutoff_reached"] = True
        return
    session = session_factory(
        socket_timeout_seconds=20.0,
        minimum_interval_seconds=1.0,
        max_retries=0,
        retry_base_seconds=0.0,
    )
    try:
        with session:
            progress["provider_package_version"] = getattr(session, "package_version", None)
            _record_session_start(progress, session)
            for key, call in pending:
                if not _inside_stage_window(stage, clock_fn()):
                    progress["capture_cutoff_reached"] = True
                    break
                _attempt_request_with_session(progress, target, session, key, call, stage, now, clock_fn)
    except Exception as exc:
        # If session setup fails, preserve that failed scheduled attempt for each request.
        log = list(getattr(session, "request_log", []))
        for key, _ in pending:
            record = progress["requests"][key]
            if any(item.get("stage") == stage for item in record["attempts"]):
                continue
            attempt_number = len(record["attempts"]) + 1
            if attempt_number > 3:
                continue
            attempt = {
                "attempt": attempt_number,
                "stage": stage,
                "started_at_utc": _iso(now),
                "completed_at_utc": _iso(_utc(clock_fn())),
                "status": "FAILED",
                "category": "SESSION_SETUP_OR_ABORT",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "request_log": log,
                "raw_response_file": None,
                "raw_response_sha256": None,
            }
            record["attempts"].append(attempt)
            record["status"] = "FAILED"
            _write_progress(target / "capture-progress.json", progress)


def _attempt_request(
    progress: dict[str, Any], target: Path, session_factory: Callable[..., Any], stage: int,
    now: datetime, key: str, call: Callable[[Any], pd.DataFrame],
    clock_fn: Callable[[], datetime],
) -> None:
    """为一项待办创建数据会话并调用单次请求。参数：progress/target 为状态目录，session_factory 为会话构造器，stage/now 定义尝试阶段，key/call 标识任务，clock_fn 记录本机时点。返回：None；实际请求转交带会话执行器，初始化失败也留失败记录。"""
    if not _inside_stage_window(stage, clock_fn()):
        record = progress["requests"].setdefault(key, _new_request())
        record["status"] = "NOT_ATTEMPTED_WINDOW_CLOSED"
        progress["capture_cutoff_reached"] = True
        return
    session = session_factory(
        socket_timeout_seconds=20.0, minimum_interval_seconds=1.0,
        max_retries=0, retry_base_seconds=0.0,
    )
    try:
        with session:
            progress["provider_package_version"] = getattr(session, "package_version", None)
            _record_session_start(progress, session)
            if not _inside_stage_window(stage, clock_fn()):
                progress["capture_cutoff_reached"] = True
                return
            _attempt_request_with_session(progress, target, session, key, call, stage, now, clock_fn)
    except Exception as exc:
        record = progress["requests"].setdefault(key, _new_request())
        attempt_number = len(record["attempts"]) + 1
        record["attempts"].append({
            "attempt": attempt_number,
            "stage": stage,
            "started_at_utc": _iso(now),
            "completed_at_utc": _iso(_utc(clock_fn())),
            "status": "FAILED",
            "category": "SESSION_SETUP_OR_ABORT",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "request_log": list(getattr(session, "request_log", [])),
            "raw_response_file": None,
            "raw_response_sha256": None,
        })
        record["status"] = "FAILED"
        _write_progress(target / "capture-progress.json", progress)


def _attempt_request_with_session(
    progress: dict[str, Any], target: Path, session: Any, key: str,
    call: Callable[[Any], pd.DataFrame], stage: int, now: datetime,
    clock_fn: Callable[[], datetime],
) -> None:
    """通过已建立会话调用并封存一项数据请求。参数：progress/target 保存状态，session 为当前会话，key/call 标识请求，stage/now/clock_fn 记录窗口和时间。返回：None。副作用：响应序列化为私有 CSV、绑定 SHA-256 与字段校验；错误不会覆盖先前响应。"""
    record = progress["requests"].setdefault(key, _new_request())
    attempt_number = len(record["attempts"]) + 1
    start_index = len(getattr(session, "request_log", []))
    attempt_started = _utc(clock_fn())
    frame: pd.DataFrame | None = None
    try:
        frame = call(session)
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("BaoStock 适配器响应不是 DataFrame")
        relative = Path("raw") / "requests" / f"{_safe_key(key)}-attempt-{attempt_number:02d}.csv"
        destination = target / relative
        if destination.exists():
            raise FileExistsError(f"拒绝覆盖原始响应文件：{relative}")
        frame.to_csv(destination, index=False, lineterminator="\n")
        raw_digest = _sha(destination)
        validation_error = _response_validation_error(
            key,
            frame,
            expected_members=FIXED_SYMBOLS if key.startswith("membership:") else None,
        )
        outcome = "FAILED" if validation_error else "SUCCESS"
        error = ({"type": "IncompleteRequiredResponse", "message": validation_error}
                 if validation_error else None)
    except Exception as exc:
        relative = None
        outcome = "FAILED"
        error = {"type": type(exc).__name__, "message": str(exc)}
        raw_digest = None
    attempt_log = list(getattr(session, "request_log", []))[start_index:]
    attempt = {
        "attempt": attempt_number,
        "stage": stage,
        "started_at_utc": _iso(attempt_started),
        "completed_at_utc": _iso(_utc(clock_fn())),
        "status": outcome,
        "response_rows": int(len(frame)) if frame is not None else None,
        "response_fields": list(frame.columns) if frame is not None else None,
        "raw_response_file": str(relative) if relative else None,
        "raw_response_sha256": raw_digest,
        "request_log": attempt_log,
        "error": error,
    }
    record["attempts"].append(attempt)
    record["status"] = outcome
    _write_progress(target / "capture-progress.json", progress)


def _response_validation_error(
    key: str,
    frame: pd.DataFrame,
    *,
    expected_members: tuple[str, ...] | None = None,
) -> str | None:
    """检查请求响应是否包含正确类型、字段、固定证券和完整成员信息。参数：key 为请求类别，frame 为响应表，expected_members 可指定必须出现的固定池。返回：无错误时 None，否则为错误类别；非空但不完整的成员名单不能证明缺席证券不是成员。"""
    required_by_key = {
        "calendar": {"calendar_date", "is_trading_day"},
        "stock_basic": {"code", "ipoDate", "outDate", "type"},
    }
    if key.startswith("membership:"):
        required = {"code", "tradeStatus"}
    elif key.startswith("stock_basic:"):
        required = required_by_key["stock_basic"]
    elif key.startswith("bars_raw:"):
        required = {
            "date", "code", "open", "high", "low", "close", "volume", "amount",
            "adjustflag", "tradestatus", "isST",
        }
    elif key.startswith("bars_adjusted:"):
        required = {"date", "code", "close", "adjustflag"}
    else:
        required = required_by_key.get(key, set())
    missing = sorted(required.difference(frame.columns))
    if missing:
        return "缺少必需字段：" + ", ".join(missing)
    if frame.empty:
        return "必需响应返回 0 行；未知状态不能解释为正常或非成员"
    if key == "calendar":
        dates = pd.to_datetime(frame["calendar_date"], errors="coerce")
        flags = frame["is_trading_day"].astype(str).str.strip()
        if dates.isna().any() or dates.dt.strftime("%Y-%m-%d").duplicated().any():
            return "交易日历含无效或重复日期"
        if not flags.isin({"0", "1"}).all():
            return "交易日历含未知开市状态"
    if key.startswith("stock_basic"):
        codes = frame["code"].astype(str).str.strip()
        if codes.eq("").any() or codes.duplicated().any():
            return "证券基础资料含空代码或重复代码"
        if ":" in key:
            expected_symbol = key.split(":", 1)[1]
            canonical_codes = set(frame["code"].map(_canonical_baostock_symbol).astype(str))
            if canonical_codes != {expected_symbol} or len(frame) != 1:
                return "单证券基础资料响应与请求代码不一致"
    if key.startswith("membership:"):
        codes = frame["code"].astype(str).str.strip()
        if codes.eq("").any() or codes.duplicated().any():
            return "每日证券名单含空代码或重复代码"
        try:
            canonical_codes = frame["code"].map(_canonical_baostock_symbol)
        except (TypeError, ValueError):
            return "每日证券名单含无法规范化的证券代码"
        if canonical_codes.duplicated().any():
            return "每日证券名单含规范化后重复代码"
        if expected_members is not None:
            expected = set(expected_members)
            missing = sorted(expected.difference(set(canonical_codes.astype(str))))
            if missing:
                return (
                    "每日证券名单响应缺少冻结证券：" + ",".join(missing)
                    + "；接口未提供总行数或响应校验和，缺席不能判为 NOT_STOCK_MEMBER"
                )
        statuses = frame["tradeStatus"].astype(str).str.strip()
        if not statuses.isin({"0", "1"}).all():
            return "每日证券名单含未知 tradeStatus"
    if key.startswith(("bars_raw:", "bars_adjusted:")):
        if frame["date"].astype(str).duplicated().any():
            return "日线响应含重复日期"
        if frame["code"].astype(str).str.strip().eq("").any():
            return "日线响应含空证券代码"
    if key.startswith("bars_raw:") and not frame["adjustflag"].astype(str).eq("3").all():
        return "原始价格响应 adjustflag 与登记的 3 不一致"
    if key.startswith("bars_adjusted:") and not frame["adjustflag"].astype(str).eq("1").all():
        return "后复权价格响应 adjustflag 与登记的 1 不一致"
    return None


def _record_session_start(progress: dict[str, Any], session: Any) -> None:
    """将当前会话的启动信息写入进度。参数：progress 为采集状态，session 为供应商会话。返回：None；只记录允许公开的版本/请求元数据。"""
    events = list(getattr(session, "request_log", []))
    if not events:
        return
    progress.setdefault("session_events", []).append({
        "session_number": len(progress.get("session_events", [])) + 1,
        "events_before_data_request": events,
    })


def _seal_capture(
    target: Path, progress: dict[str, Any], config_path: Path, pool_path: Path,
    identity_path: Path, freeze_path: Path, now: datetime,
) -> None:
    """重算冻结输入、原始响应和规范文件哈希并封存采集结果。参数：target 为运行目录，progress 为逐请求状态，其他路径为冻结输入，now 为封存时点。返回：None。副作用：写 capture manifest 与 seal；任一请求/文件/哈希不通过则状态非 READY。"""
    current_source_hashes = {
        "registration_freeze": _sha(freeze_path),
        "config": _sha_required(config_path, "strategy config"),
        "pool": _sha_required(pool_path, "fixed pool"),
        "identity_events": _sha_required(identity_path, "identity events"),
    }
    if current_source_hashes != progress.get("source_sha256"):
        raise ValueError("采集期间冻结配置、研究池、身份事件或登记清单发生变化；禁止封存")
    _validate_raw_attempt_files(target, progress)
    successful = {
        key: _read_success_response(target, record)
        for key, record in progress["requests"].items()
        if record.get("status") == "SUCCESS"
    }
    calendar_raw = successful.get("calendar", pd.DataFrame())
    calendar_attempt = progress["requests"]["calendar"]["attempts"][-1]
    retrieved = calendar_attempt["completed_at_utc"]
    calendar = normalize_baostock_calendar(calendar_raw, retrieved)
    old_calendar_path = _registration_calendar_path(_load_freeze(freeze_path))
    overlap = _calendar_overlap(old_calendar_path, calendar)
    calendar_path = target / "trading-calendar.csv"
    _require_new_outputs(target, (
        "trading-calendar.csv", "point-in-time-universe.csv", "bars.csv",
        "capture-manifest.json", "capture-manifest.seal.json",
    ))
    calendar.to_csv(calendar_path, index=False, lineterminator="\n")

    basic_frames = [successful.get(f"stock_basic:{symbol}") for symbol in FIXED_SYMBOLS]
    basics = pd.concat([frame for frame in basic_frames if frame is not None], ignore_index=True) if any(
        frame is not None for frame in basic_frames
    ) else pd.DataFrame()
    basic_map: dict[str, pd.Series] = {}
    basic_duplicates: set[str] = set()
    if not basics.empty and "code" in basics:
        canonical_basics = basics["code"].map(_canonical_baostock_symbol)
        basic_duplicates = set(canonical_basics[canonical_basics.duplicated(keep=False)].astype(str))
        for _, row in basics.iterrows():
            basic_map[_canonical_baostock_symbol(row["code"])] = row
    sessions = progress.get("signal_sessions", [])
    membership_records: list[dict[str, Any]] = []
    status_failures: list[str] = []
    for day in sessions:
        response = successful.get(f"membership:{day}")
        codes: set[str] = set()
        daily_status: dict[str, str] = {}
        if response is not None and "code" in response.columns and not response.empty:
            codes = set(response["code"].map(_canonical_baostock_symbol).astype(str))
            if "tradeStatus" in response.columns:
                daily_status = {
                    _canonical_baostock_symbol(row["code"]): str(row["tradeStatus"]).strip()
                    for _, row in response.iterrows()
                }
        for symbol in FIXED_SYMBOLS:
            membership_record = progress["requests"].get(f"membership:{day}", {})
            missing_expected = sorted(set(FIXED_SYMBOLS).difference(codes))
            member_result_valid = (
                membership_record.get("status") == "SUCCESS"
                and response is not None
                and not response.empty
                and "code" in response.columns
                and not missing_expected
            )
            pool_status = (
                "STOCK_MEMBER" if member_result_valid and symbol in codes
                else "UNKNOWN"
            )
            basic = basic_map.get(symbol)
            if symbol in basic_duplicates:
                basic = None
            if pool_status == "STOCK_MEMBER" and (basic is None or str(basic.get("type", "")) != "1"):
                pool_status = "UNKNOWN"
            is_listed = _listed_on(basic, day) if basic is not None else pd.NA
            raw = successful.get(f"bars_raw:{symbol}", pd.DataFrame())
            adjusted = successful.get(f"bars_adjusted:{symbol}", pd.DataFrame())
            raw_row = _row_for_day(raw, day)
            adjusted_row = _row_for_day(adjusted, day)
            history_trade = _agree_value(raw_row, adjusted_row, "tradestatus")
            list_trade = daily_status.get(symbol)
            suspension = _suspension_value(list_trade, history_trade)
            is_st = _agree_st(raw_row, adjusted_row)
            if pool_status == "UNKNOWN":
                if symbol in missing_expected:
                    status_failures.append(
                        f"{day} {symbol}: pool_status UNKNOWN; fixed symbol absent from membership response"
                    )
                else:
                    status_failures.append(f"{day} {symbol}: pool_status UNKNOWN")
            elif pool_status == "STOCK_MEMBER":
                if pd.isna(is_listed):
                    status_failures.append(f"{day} {symbol}: is_listed UNKNOWN")
                if pd.isna(suspension):
                    status_failures.append(f"{day} {symbol}: suspension status UNKNOWN or conflicting")
                if pd.isna(is_st):
                    status_failures.append(f"{day} {symbol}: ST status UNKNOWN or conflicting")
            membership_records.append({
                "trade_date": day,
                "symbol": symbol,
                "pool_status": pool_status,
                "is_listed": is_listed,
                "is_suspended": suspension,
                "is_st": is_st,
                "daily_trade_status": list_trade if list_trade is not None else "UNKNOWN",
                "status_source": "query_all_stock+query_stock_basic+query_history_k_data_plus",
                "status_evidence": json.dumps({
                    "membership_request": f"membership:{day}",
                    "history_raw_present": raw_row is not None,
                    "history_adjusted_present": adjusted_row is not None,
                    "trade_status_crosscheck": "MATCH" if list_trade and history_trade and list_trade == history_trade else "UNKNOWN_OR_NOT_COMPARABLE",
                }, sort_keys=True),
            })
    universe_path = target / "point-in-time-universe.csv"
    pd.DataFrame(membership_records).to_csv(universe_path, index=False, lineterminator="\n")

    bars: list[pd.DataFrame] = []
    bar_failures: dict[str, str] = {}
    universe_by_day_symbol = {
        (str(row["trade_date"]), str(row["symbol"])): row
        for row in membership_records
    }
    for symbol in FIXED_SYMBOLS:
        raw = successful.get(f"bars_raw:{symbol}")
        adjusted = successful.get(f"bars_adjusted:{symbol}")
        basic = basic_map.get(symbol)
        if raw is None or adjusted is None or basic is None:
            bar_failures[symbol] = "RAW_OR_ADJUSTED_HISTORY_OR_BASIC_MISSING"
            continue
        if (
            ("date" in raw and raw["date"].astype(str).duplicated().any())
            or ("date" in adjusted and adjusted["date"].astype(str).duplicated().any())
        ):
            bar_failures[symbol] = "DUPLICATE_RAW_HISTORY_DATE"
            continue
        if (
            "adjustflag" not in raw or "adjustflag" not in adjusted
            or not raw["adjustflag"].astype(str).eq("3").all()
            or not adjusted["adjustflag"].astype(str).eq("1").all()
        ):
            bar_failures[symbol] = "RAW_OR_ADJUSTED_ADJUSTFLAG_MISMATCH"
            continue
        expected_code = symbol
        raw_codes = set(raw["code"].map(_canonical_baostock_symbol).astype(str))
        adjusted_codes = set(adjusted["code"].map(_canonical_baostock_symbol).astype(str))
        if raw_codes != {expected_code} or adjusted_codes != {expected_code}:
            bar_failures[symbol] = "RESPONSE_SECURITY_CODE_MISMATCH"
            continue
        raw_dates = pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        adjusted_dates = pd.to_datetime(adjusted["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        if raw_dates.isna().any() or adjusted_dates.isna().any():
            bar_failures[symbol] = "INVALID_HISTORY_DATE"
            continue
        if set(raw_dates) != set(adjusted_dates):
            bar_failures[symbol] = "RAW_ADJUSTED_SESSION_MISMATCH"
            continue
        if not set(raw_dates).issubset(set(sessions)):
            bar_failures[symbol] = "HISTORY_OUTSIDE_REGISTERED_SIGNAL_WINDOW"
            continue
        history_retrieved = max(
            progress["requests"][f"bars_raw:{symbol}"]["attempts"][-1]["completed_at_utc"],
            progress["requests"][f"bars_adjusted:{symbol}"]["attempts"][-1]["completed_at_utc"],
        )
        normalized, skipped = normalize_baostock_history(raw, adjusted, basic, history_retrieved)
        normalized = normalized.loc[normalized["trade_date"].astype(str).isin(sessions)].copy()
        if skipped:
            _append_failure(bar_failures, symbol, f"INVALID_RAW_OHLC_ROWS:{skipped}")
        price_columns = ["open", "high", "low", "close", "adjusted_close"]
        numeric_prices = normalized[price_columns].apply(pd.to_numeric, errors="coerce")
        invalid_price_rows = (
            numeric_prices.isna().any(axis=1)
            | ~numeric_prices.map(lambda value: pd.notna(value) and float(value) > 0).all(axis=1)
            | numeric_prices["low"].gt(numeric_prices["high"])
            | numeric_prices["low"].gt(numeric_prices[["open", "close"]].min(axis=1))
            | numeric_prices["high"].lt(numeric_prices[["open", "close"]].max(axis=1))
        )
        if invalid_price_rows.any():
            bad_dates = normalized.loc[invalid_price_rows, "trade_date"].astype(str).tolist()
            reason = "INVALID_OR_NONPOSITIVE_PRICE:" + ",".join(bad_dates)
            _append_failure(bar_failures, symbol, reason)
        normalized_days = set(normalized["trade_date"].astype(str))
        missing_non_suspended = [
            day for day in sessions
            if day not in normalized_days
            and not _truth(universe_by_day_symbol.get((day, symbol), {}).get("is_suspended"))
        ]
        if normalized.empty:
            _append_failure(bar_failures, symbol, "NO_VALID_NORMALIZED_ROWS")
        elif missing_non_suspended:
            _append_failure(
                bar_failures, symbol,
                "MISSING_BAR_WHEN_NOT_CONFIRMED_SUSPENDED:" + ",".join(missing_non_suspended),
            )
        if not normalized.empty:
            bars.append(normalized)
    bars_frame = normalize_bar_frame(
        pd.concat(bars, ignore_index=True) if bars else pd.DataFrame(columns=BAR_COLUMNS),
    )
    bars_path = target / "bars.csv"
    write_bars(bars_frame, bars_path)
    pool_hash = _sha_required(pool_path, "fixed pool")
    input_hashes = {
        "bars": _sha(bars_path),
        "calendar": _sha(calendar_path),
        "universe": _sha(universe_path),
        "pool": pool_hash,
        "identity_events": _sha_required(identity_path, "identity events"),
        "registration_freeze": _sha(freeze_path),
    }
    raw_responses = _raw_responses_from_progress(progress)
    manifest = {
        "schema_version": 1,
        "capture_id": target.name,
        "experiment_id": EXPERIMENT_ID,
        "signal_date": progress["signal_date"],
        "provider": "BaoStock",
        "provider_package_version": progress.get("provider_package_version"),
        "interfaces": CAPTURE_INTERFACES,
        "membership_response_scope": (
            "BaoStock documents query_all_stock(day) as the date's full securities list. The SDK adapter exhausts "
            "pages and detects a failed next() after a full page, but the response has no total-row count or checksum. "
            "Every frozen symbol is required in each response; any missing symbol leaves that date UNKNOWN and blocks capture."
        ),
        "signal_price_adjustment": "adjustflag=1",
        "raw_price_adjustment": "adjustflag=3",
        "captured_at_utc": _iso(now),
        "capture_started_at_utc": progress["started_at_utc"],
        "capture_schedule_status": progress.get("status"),
        "provider_published_at_utc": None,
        "provider_publication_evidence": "UNVERIFIED",
        "trusted_external_timestamp": "UNVERIFIED",
        "capture_layer": progress["capture_layer"],
        "input_sha256": input_hashes,
        "source_sha256": progress["source_sha256"],
        "raw_responses": raw_responses,
        "session_events": list(progress.get("session_events", [])),
        "request_attempts": progress["requests"],
        "calendar_overlap": overlap,
        "required_sessions": sessions,
        "calendar_open_sessions_after_signal": progress.get("calendar_open_sessions_after_signal"),
        "calendar_outcome_horizon_status": progress.get("calendar_outcome_horizon_status"),
        "bar_normalization_failures": bar_failures,
        "point_in_time_status_failures": status_failures,
        "bar_row_count": int(len(bars_frame)),
        "universe_row_count": int(len(membership_records)),
        "status": "CAPTURED_INPUTS_READY" if not progress.get("blocking_reasons") else "NEEDS_DATA",
        "blocking_reasons": list(progress.get("blocking_reasons", [])),
        "research_pool_scope": "fixed registered 10-security process pilot only; not full-market selection",
        "rankings_generated": False,
        "returns_computed": False,
        "orders_generated": False,
        "broker_connected": False,
        "formal_baseline": "NOT_READY",
    }
    if overlap["status"] != "MATCH":
        manifest["status"] = "NEEDS_DATA"
        manifest["blocking_reasons"].append("本次 BaoStock 日历与冻结日历重叠前缀不一致或不完整")
    if bar_failures:
        # Keep the capture manifest usable as an audit artifact, but the ranking gate will reject missing bars.
        manifest["status"] = "NEEDS_DATA"
        manifest["blocking_reasons"].append("部分证券缺少原始/复权行情或基础资料；逐证券见 bar_normalization_failures")
    if status_failures:
        manifest["status"] = "NEEDS_DATA"
        manifest["blocking_reasons"].append(
            f"逐日成员/上市/ST/停牌状态有 {len(status_failures)} 项未知或冲突；详情见 point_in_time_status_failures",
        )
    manifest_path = target / "capture-manifest.json"
    _write_new_json(manifest_path, manifest)
    manifest["capture_manifest_sha256"] = _sha(manifest_path)
    _write_new_json(target / "capture-manifest.seal.json", {
        "capture_manifest_sha256": manifest["capture_manifest_sha256"],
        "sealed_at_utc": _iso(now),
    })
    progress["status"] = manifest["status"]
    progress["sealed_at_utc"] = _iso(now)
    progress["input_sha256"] = input_hashes
    progress["blocking_reasons"] = list(manifest["blocking_reasons"])
    progress["bar_normalization_failures"] = bar_failures
    progress["point_in_time_status_failures"] = status_failures
    _write_progress(target / "capture-progress.json", progress)


def _write_capture_failure_manifest(target: Path, progress: dict[str, Any], now: datetime) -> None:
    """在采集流程未能继续时保存失败清单。参数：target 为运行目录，progress 为已收集状态，now 为记录时点。返回：None；失败产物也保留已有原始请求，不改写为成功。"""
    manifest_path = target / "capture-manifest.json"
    if manifest_path.exists() or (target / "capture-manifest.seal.json").exists():
        raise FileExistsError("采集清单或封存记录已存在，拒绝覆盖")
    _validate_raw_attempt_files(target, progress)
    membership_audit_path = target / "point-in-time-universe.csv"
    membership_audit_rows: list[dict[str, Any]] = []
    for day in progress.get("signal_sessions", []):
        record = progress.get("requests", {}).get(f"membership:{day}", {})
        response = _latest_membership_attempt_frame(target, record)
        observed_codes: set[str] = set()
        if not response.empty and "code" in response.columns:
            for code in response["code"]:
                try:
                    observed_codes.add(_canonical_baostock_symbol(code))
                except (TypeError, ValueError):
                    continue
        response_verified = record.get("status") == "SUCCESS" and not (
            set(FIXED_SYMBOLS).difference(observed_codes)
        )
        latest_error = None
        if record.get("attempts"):
            latest_error = (record["attempts"][-1].get("error") or {}).get("message")
        for symbol in FIXED_SYMBOLS:
            membership_audit_rows.append({
                "trade_date": day,
                "symbol": symbol,
                "pool_status": "STOCK_MEMBER" if response_verified and symbol in observed_codes else "UNKNOWN",
                "is_listed": pd.NA,
                "is_suspended": pd.NA,
                "is_st": pd.NA,
                "daily_trade_status": "UNKNOWN",
                "status_source": "BaoStock query_all_stock",
                "status_evidence": "response completeness not verified; no negative membership inference",
                "membership_response_status": record.get("status", "NOT_REQUESTED"),
                "observed_in_response": symbol in observed_codes if not response.empty else "UNKNOWN",
                "membership_response_error": latest_error or "membership response not verified",
            })
    membership_audit_path.parent.mkdir(parents=True, exist_ok=True)
    if membership_audit_path.exists():
        raise FileExistsError("拒绝覆盖逐日成员审计文件")
    pd.DataFrame(membership_audit_rows, columns=[
        "trade_date", "symbol", "pool_status", "is_listed", "is_suspended", "is_st",
        "daily_trade_status", "status_source", "status_evidence", "membership_response_status",
        "observed_in_response", "membership_response_error",
    ]).to_csv(membership_audit_path, index=False, lineterminator="\n")
    manifest = {
        **progress,
        "capture_id": target.name,
        "status": "NEEDS_DATA",
        "captured_at_utc": _iso(now),
        "provider_published_at_utc": None,
        "provider_publication_evidence": "UNVERIFIED",
        "trusted_external_timestamp": "UNVERIFIED",
        "input_sha256": {},
        "raw_responses": _raw_responses_from_progress(progress),
        "point_in_time_universe_review": {
            "path": str(membership_audit_path.relative_to(target)),
            "sha256": _sha(membership_audit_path),
            "row_count": len(membership_audit_rows),
            "status": "UNKNOWN_MEMBERSHIP_WHERE_RESPONSE_UNVERIFIED",
        },
        "rankings_generated": False,
        "returns_computed": False,
        "formal_baseline": "NOT_READY",
        "orders_generated": False,
        "broker_connected": False,
    }
    _write_new_json(manifest_path, manifest)
    manifest_hash = _sha(manifest_path)
    _write_new_json(target / "capture-manifest.seal.json", {
        "capture_manifest_sha256": manifest_hash,
        "sealed_at_utc": _iso(now),
        "status": "NEEDS_DATA",
    })


def _raw_responses_from_progress(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """从进度状态汇总已登记原始响应文件。参数：progress 为请求清单。返回：每次尝试的来源、路径、行数和哈希引用列表。"""
    responses = []
    for key, record in sorted(progress.get("requests", {}).items()):
        for attempt in record.get("attempts", []):
            if attempt.get("raw_response_file"):
                responses.append({
                    "request_id": key,
                    "attempt": attempt["attempt"],
                    "path": attempt["raw_response_file"],
                    "sha256": attempt["raw_response_sha256"],
                    "rows": attempt.get("response_rows"),
                    "fields": attempt.get("response_fields"),
                })
    return responses


def _validate_raw_attempt_files(target: Path, progress: dict[str, Any]) -> None:
    """核验进度中每个原始响应是否存在且哈希正确。参数：target 为采集根目录，progress 为尝试记录。返回：无效项列表或验证摘要；缺失/改写时不允许封存通过。"""
    root = target.resolve()
    for key, record in progress.get("requests", {}).items():
        for attempt in record.get("attempts", []):
            relative = attempt.get("raw_response_file")
            if not relative:
                continue
            path = (target / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError(f"请求 {key} 的原始响应文件缺失或路径越界")
            if _sha(path) != attempt.get("raw_response_sha256"):
                raise ValueError(f"请求 {key} 的原始响应 SHA-256 不匹配")


def _read_success_response(target: Path, record: dict[str, Any]) -> pd.DataFrame:
    """读取并复核成功尝试的原始响应。参数：target 为运行目录，record 为请求状态。返回：响应 DataFrame。异常：状态不是成功、文件丢失或摘要不匹配时阻断。"""
    for attempt in reversed(record.get("attempts", [])):
        if attempt.get("status") == "SUCCESS" and attempt.get("raw_response_file"):
            path = target / attempt["raw_response_file"]
            if _sha(path) != attempt.get("raw_response_sha256"):
                raise ValueError(f"原始响应哈希不匹配：{path.name}")
            return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.DataFrame()


def _latest_membership_attempt_frame(target: Path, record: dict[str, Any]) -> pd.DataFrame:
    """读取最近一次留存的成员名单响应，仅用于审计查看。

    参数：目标目录和该项请求记录。返回：最近响应可解析出的数据表；没有可读响应时为空表。
    即使名单校验失败也保留原始响应供定位问题；结果不能作为已通过校验的股票池输入。
    """
    for attempt in reversed(record.get("attempts", [])):
        relative = attempt.get("raw_response_file")
        if not relative:
            continue
        path = target / relative
        if _sha(path) != attempt.get("raw_response_sha256"):
            raise ValueError(f"成员名单原始响应哈希不匹配：{path.name}")
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.DataFrame()


def _all_required_requests_succeeded(progress: dict[str, Any]) -> bool:
    """检查所需日历及全部依赖请求是否成功。参数：progress 为请求状态。返回：布尔值；不忽略任一固定证券或窗口成员请求。"""
    requests = progress.get("requests", {})
    return bool(requests) and all(item.get("status") == "SUCCESS" for item in requests.values()) and progress.get("dependent_requests_built")


def _pending_reasons(progress: dict[str, Any]) -> list[str]:
    """汇总当前采集仍待解决的失败、未尝试和日历缺口。参数：progress 为进度字典。返回：可供清单使用的阻断原因列表。"""
    reasons = []
    for key, record in progress.get("requests", {}).items():
        if record.get("status") == "SUCCESS":
            continue
        latest_error = None
        if record.get("attempts"):
            latest_error = (record["attempts"][-1].get("error") or {}).get("message")
        detail = f"；{latest_error}" if latest_error else ""
        reasons.append(
            f"{key}: {record.get('status')}，尝试次数 {len(record.get('attempts', []))}{detail}"
        )
    return reasons


def _calendar_overlap(old_path: Path, new: pd.DataFrame) -> dict[str, Any]:
    """校验新日历与冻结旧日历共有日期的开市状态一致。参数：old_path 为冻结日历文件，new 为规范化候选日历。返回：重叠核对摘要；冲突时按实现阻断。"""
    if not old_path.is_file():
        return {"status": "UNVERIFIED", "old_calendar_path": str(old_path), "old_rows": 0, "matched_rows": 0, "missing_dates": [], "mismatches": []}
    old = normalize_trading_calendar(pd.read_csv(old_path, dtype=str))
    joined = old[["trade_date", "is_open"]].merge(new[["trade_date", "is_open"]], on="trade_date", how="left", suffixes=("_old", "_new"))
    missing = joined["is_open_new"].isna()
    mismatch = (~missing) & joined["is_open_old"].ne(joined["is_open_new"])
    return {
        "status": "MATCH" if not missing.any() and not mismatch.any() else "MISMATCH",
        "old_calendar_path": str(old_path),
        "old_calendar_sha256": _sha(old_path),
        "old_rows": int(len(old)),
        "matched_rows": int((~missing & ~mismatch).sum()),
        "missing_dates": joined.loc[missing, "trade_date"].astype(str).tolist(),
        "mismatches": joined.loc[mismatch, "trade_date"].astype(str).tolist(),
    }


def _registration_calendar_path(freeze: dict[str, Any]) -> Path:
    """从登记冻结中读取已绑定日历文件路径。参数：freeze 为登记映射。返回：仓库内解析路径；路径缺失或不完整时阻断。"""
    item = freeze.get("frozen_input_files", {}).get("calendar", {})
    path = Path(item.get("path", ""))
    return path if path.is_absolute() else _repo_root() / path


def _load_freeze(path: Path) -> dict[str, Any]:
    """读取并验证带封条的冻结登记。参数：path 为登记 JSON 路径。返回：登记映射；哈希不符或 JSON 无效时抛异常。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少 registration-freeze.json：{path}")
    seal = path.with_suffix(".sha256")
    if not seal.is_file() or _sha(path) != seal.read_text(encoding="ascii").split()[0].lower():
        raise ValueError("registration-freeze.json 的 SHA-256 封条不匹配")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("experiment_id") != EXPERIMENT_ID or value.get("signal_observation", {}).get("fixed_signal_date") != "2026-12-17":
        raise ValueError("冻结清单与本 lookback 配对实验不匹配")
    if tuple(value.get("research_pool", {}).get("symbols", [])) != FIXED_SYMBOLS:
        raise ValueError("冻结清单证券池与登记固定池不匹配")
    return value


def _validate_freeze_inputs(freeze: dict[str, Any], config: Path, pool: Path, input_hashes: dict[str, str]) -> None:
    """对照登记清单核验当前配置和研究池文件。参数：freeze 为登记内容，config/pool 为输入路径，input_hashes 为本次摘要。返回：全部一致时 None。异常：路径、哈希或固定代码顺序变化时拒绝。"""
    observation = freeze.get("signal_observation", {})
    expected_schedule = {
        "signal_capture_start_local": "2026-12-17T15:06:00+08:00",
        "retry_local_times": ["2026-12-17T15:20:00+08:00", "2026-12-17T15:35:00+08:00"],
        "capture_cutoff_local": "2026-12-17T15:45:00+08:00",
        "seal_cutoff_local": "2026-12-17T16:00:00+08:00",
    }
    for key, expected in expected_schedule.items():
        if observation.get(key) != expected:
            raise ValueError(f"冻结登记的采集时间规则 {key} 与本入口不一致")
    files = freeze.get("frozen_input_files", {})
    for key, path, digest_key in (("strategy_config", config, "config"), ("pool", pool, "pool")):
        item = files.get(key, {})
        expected_path = Path(item.get("path", ""))
        if not expected_path.is_absolute():
            expected_path = _repo_root() / expected_path
        if expected_path.resolve() != path.resolve() or item.get("sha256") != input_hashes[digest_key]:
            raise ValueError(f"采集前冻结输入 {key} 路径或 SHA-256 不匹配")
    pool_frame = pd.read_csv(pool, dtype=str, keep_default_na=False)
    if "symbol" not in pool_frame or tuple(pool_frame["symbol"].map(canonical_symbol)) != FIXED_SYMBOLS:
        raise ValueError("研究池 CSV 的代码、顺序或数量不符合冻结的 10 只证券")
    calendar_item = files.get("calendar", {})
    calendar_path = _registration_calendar_path(freeze)
    if not calendar_path.is_file() or _sha(calendar_path) != calendar_item.get("sha256"):
        raise ValueError("冻结参考日历不存在或哈希不匹配")
    doc = files.get("registration_document", {})
    doc_path = _repo_root() / doc.get("path", "")
    if not doc_path.is_file() or _sha(doc_path) != doc.get("sha256"):
        raise ValueError("登记文档哈希与私有冻结清单不匹配")


def _listed_on(basic: pd.Series | None, day: str) -> Any:
    """用 IPO/outDate 基础资料判断某证券在指定日期是否上市。参数：basic 为证券基础资料，day 为交易日期。返回：True/False；缺少上市日期时为未知。"""
    if basic is None:
        return pd.NA
    ipo = pd.to_datetime(basic.get("ipoDate", ""), errors="coerce")
    out_date = pd.to_datetime(basic.get("outDate", ""), errors="coerce")
    target = pd.to_datetime(day)
    if pd.isna(ipo):
        return pd.NA
    return bool(target >= ipo and (pd.isna(out_date) or target <= out_date))


def _canonical_baostock_symbol(value: object) -> str:
    """规范 BaoStock 代码到项目 SH/SZ 格式。参数：value 为供应商代码。返回：规范代码；无法解析时由规范器拒绝。"""
    raw = str(value).strip().upper()
    match = re.fullmatch(r"(SH|SZ)\.(\d{6})", raw)
    if match:
        return f"{match.group(1)}{match.group(2)}"
    return canonical_symbol(value)


def _row_for_day(frame: pd.DataFrame, day: str) -> pd.Series | None:
    """获取恰好一条指定日期行情。参数：frame 为证券行情，day 为交易日。返回：唯一记录，否则 None。"""
    if frame.empty or "date" not in frame:
        return None
    rows = frame.loc[frame["date"].astype(str).eq(day)]
    return rows.iloc[0] if len(rows) == 1 else None


def _agree_value(left: pd.Series | None, right: pd.Series | None, field: str) -> str | None:
    """比较两个来源记录的指定字段是否一致。参数：left/right 为可选行，field 为字段名。返回：明确一致值或未知；冲突不取任一来源作为真值。"""
    values = [str(row.get(field, "")).strip() for row in (left, right) if row is not None]
    values = [value for value in values if value]
    return values[0] if values and len(set(values)) == 1 else None


def _suspension_value(list_status: str | None, history_status: str | None) -> Any:
    """结合每日成员 tradeStatus 和历史行情 tradestatus 推导停牌状态。参数：list_status/history_status 为两个来源的状态值。返回：来源一致时返回停牌布尔值，缺失/冲突时返回 None。"""
    if list_status in {"0", "1"} and history_status in {"0", "1"} and list_status != history_status:
        return pd.NA
    status = list_status if list_status in {"0", "1"} else history_status
    return {"0": True, "1": False}.get(status, pd.NA)


def _agree_st(left: pd.Series | None, right: pd.Series | None) -> Any:
    """比较原始与复权行情行中的 ST 标记。参数：left/right 为两份来源记录。返回：两者一致的 ST 状态；冲突或缺失保持未知。"""
    value = _agree_value(left, right, "isST")
    return {"0": False, "1": True}.get(value, pd.NA)


def _write_progress(path: Path, value: dict[str, Any]) -> None:
    """持久化当前采集进度 JSON。参数：path 为进度文件，value 为状态对象。副作用：写入进度文件；目录由采集入口创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_stage_manifest(target: Path, stage: int, progress: dict[str, Any]) -> None:
    """保存指定重试阶段的进度快照。参数：target 为采集目录，stage 为阶段编号，progress 为当时状态。返回：None；用于保留阶段历史，不替代最终封条。"""
    path = target / f"stage-{stage:02d}-manifest.json"
    if path.exists():
        raise FileExistsError(f"拒绝覆盖采集阶段记录：{path.name}")
    _write_new_json(path, progress)


def _write_new_json(path: Path, value: Any) -> None:
    """只在目标不存在时新建 JSON 文件。参数：path 为目标，value 为对象。副作用：写入新文件；已有文件时拒绝覆盖。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def _require_new_outputs(target: Path, names: tuple[str, ...]) -> None:
    """确认一组预期输出文件尚未存在。参数：target 为输出目录，names 为文件名元组。返回：通过时 None。异常：任一文件已存在时阻止覆盖。"""
    existing = [name for name in names if (target / name).exists()]
    if existing:
        raise FileExistsError("拒绝覆盖已有规范化采集产物：" + ", ".join(existing))


def _append_failure(failures: dict[str, str], key: str, reason: str) -> None:
    """向错误映射追加或更新一项失败原因。参数：failures 为错误表，key 为请求项，reason 为原因。副作用：修改传入映射。"""
    previous = failures.get(key)
    failures[key] = f"{previous};{reason}" if previous else reason


def _truth(value: Any) -> bool:
    """解析常见真/假表示。参数：value 为来源单元格。返回：布尔值或未知 None。"""
    if value is pd.NA or pd.isna(value):
        return False
    return bool(value)


def _safe_key(value: str) -> str:
    """将请求标识清理为可用于文件名的安全键。参数：value 为逻辑请求 ID。返回：安全字符串。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _sha(path: Path) -> str:
    """计算文件 SHA-256。参数：path 为文件路径。返回：小写十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha_required(path: Path, label: str) -> str:
    """计算必需文件摘要并给出角色化错误。参数：path 为文件，label 为输入名称。返回：摘要。异常：文件缺失/读取失败时阻断。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少 {label} 文件：{path}")
    return _sha(path)


def _utc(value: datetime) -> datetime:
    """验证 datetime 带时区并转换为 UTC。参数：value 为输入时间。返回：UTC datetime；无时区时拒绝。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("采集运行时间必须带时区")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    """将时间写成秒精度 UTC ISO 字符串。参数：value 为带时区 datetime。返回：Z 后缀字符串。"""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_root() -> Path:
    """返回仓库根目录。返回：从模块位置推导的绝对 Path；不依赖当前工作目录。"""
    return Path(__file__).resolve().parents[3]
