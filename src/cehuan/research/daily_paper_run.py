"""每日小范围纸面采集与简报流程。模块按固定研究池串行抓取日历、成员、基础资料、原始/后复权行情及状态，先封存输入再调用现有排名和简报入口；门控失败时记录 NEEDS_DATA，不回填、不生成委托。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.config import load_config
from cehuan.data.baostock_adapter import (
    BaoStockSession,
    normalize_baostock_calendar,
    normalize_baostock_history,
)
from cehuan.data.canonical import canonical_symbol, normalize_bar_frame, write_bars
from cehuan.data.security_identity import normalize_security_identity_events
from cehuan.research.daily_research_brief import run_daily_research_brief
from cehuan.research.forward_ranking import create_paper_ranking_artifact
from cehuan.research.lookback_pair import FIXED_SYMBOLS


SHANGHAI = ZoneInfo("Asia/Shanghai")
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POOL = REPO_ROOT / "data/private/experiments/forward-small-20261106-akshare-v1/selected_pool.csv"
RAW_HISTORY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
REQUIRED_RAW_FIELDS = set(RAW_HISTORY_FIELDS.split(","))


class _DailyCaptureBlocked(ValueError):
    """每日采集关键输入不完整时使用的流程阻断异常。调用层将其转成带原因的 NEEDS_DATA 产物，避免缺失输入进入排名。"""
    pass


def _sha(path: Path) -> str:
    """流式计算文件 SHA-256。参数：path 为待封存文件。返回：小写十六进制摘要；文件不可读时抛出文件系统异常。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso(value: datetime) -> str:
    """把带时区时间规范为 UTC 字符串。参数：value 为采集时点。返回：秒精度 ISO 8601 字符串。异常：时间无时区时抛出 ValueError。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("每日采集时间必须带时区")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_vendor_symbol(value: object) -> str:
    """规范 BaoStock 风格代码或项目代码。参数：value 为供应商代码值。返回：项目 SH/SZ 代码；无法规范化时由 canonical_symbol 抛出异常。"""
    raw = str(value).strip().upper()
    match = re.fullmatch(r"(SH|SZ)\.(\d{6})", raw)
    return f"{match.group(1)}{match.group(2)}" if match else canonical_symbol(value)


def _vendor_code(symbol: str) -> str:
    """把项目证券代码转成 BaoStock 的市场前缀格式。参数：symbol 为 SH/SZ 规范代码。返回：小写市场前缀加点号的代码字符串。"""
    return f"{symbol[:2].lower()}.{symbol[2:]}"


def _listed(basic: pd.Series | None, day: str) -> Any:
    """用基础资料的 IPO/退市日期判断目标日是否处于上市区间。参数：basic 为单证券基础资料，day 为目标日期。返回：布尔值；无基础资料或上市日期时返回 pandas 缺失值。"""
    if basic is None:
        return pd.NA
    ipo = pd.to_datetime(basic.get("ipoDate", ""), errors="coerce")
    out_date = pd.to_datetime(basic.get("outDate", ""), errors="coerce")
    target = pd.to_datetime(day)
    if pd.isna(ipo):
        return pd.NA
    return bool(target >= ipo and (pd.isna(out_date) or target <= out_date))


def _status_flag(value: Any) -> Any:
    """解析 BaoStock 0/1 状态字段。参数：value 为供应商状态。返回：0→False、1→True，其他值保持缺失/未知。"""
    text = str(value).strip()
    return {"0": False, "1": True}.get(text, pd.NA)


def _suspension(list_status: Any, raw_status: Any, adjusted_status: Any) -> Any:
    """对照成员表及原始/复权日线的交易状态推导停牌标记。参数：三项均为来源状态值。返回：一致且明确时返回是否停牌；缺失或冲突时返回未知。"""
    values = [str(value).strip() for value in (list_status, raw_status, adjusted_status) if str(value).strip() in {"0", "1"}]
    if not values or len(set(values)) != 1:
        return pd.NA
    return values[0] == "0"


def _st(raw_row: pd.Series | None, adjusted_row: pd.Series | None) -> Any:
    """对照原始与复权日线的 isST 字段。参数：raw_row/adjusted_row 为对应日期行情行。返回：两来源一致时返回 ST 布尔值，否则返回未知。"""
    values = []
    for row in (raw_row, adjusted_row):
        if row is None:
            continue
        value = str(row.get("isST", "")).strip()
        if value in {"0", "1"}:
            values.append(value)
    if not values or len(set(values)) != 1:
        return pd.NA
    return values[0] == "1"


def _row_for_day(frame: pd.DataFrame | None, day: str) -> pd.Series | None:
    """从单证券行情中取唯一指定日期记录。参数：frame 为行情表，day 为 YYYY-MM-DD。返回：唯一行 Series；缺失、空表或重复时返回 None。"""
    if frame is None or frame.empty or "date" not in frame:
        return None
    rows = frame.loc[frame["date"].astype(str).eq(day)]
    return rows.iloc[0] if len(rows) == 1 else None


def _request_validation(request_id: str, frame: pd.DataFrame, symbols: tuple[str, ...]) -> str | None:
    """验证一次 BaoStock 请求响应的字段、固定证券范围及关键唯一性。参数：request_id 标识请求类型/证券，frame 为响应 DataFrame，symbols 为冻结池。返回：通过时 None，否则返回安全错误类别字符串；不推断非空响应必然完整。"""
    if request_id == "calendar":
        needed = {"calendar_date", "is_trading_day"}
    elif request_id.startswith("membership:"):
        needed = {"code", "tradeStatus"}
    elif request_id.startswith("basic:"):
        needed = {"code", "ipoDate", "outDate", "type"}
    elif request_id.startswith("bars_raw:"):
        needed = REQUIRED_RAW_FIELDS
    elif request_id.startswith("bars_adjusted:"):
        needed = {"date", "code", "close", "adjustflag"}
    else:
        return "UNKNOWN_REQUEST_TYPE"
    missing = needed.difference(frame.columns)
    if missing:
        return "MISSING_FIELDS:" + ",".join(sorted(missing))
    if frame.empty:
        return "EMPTY_RESPONSE"
    if request_id.startswith("membership:"):
        try:
            codes = frame["code"].map(_canonical_vendor_symbol)
        except (TypeError, ValueError):
            return "INVALID_SECURITY_CODE"
        if codes.duplicated().any():
            return "DUPLICATE_SECURITY_CODE"
        missing_symbols = sorted(set(symbols).difference(set(codes.astype(str))))
        if missing_symbols:
            return "INCOMPLETE_MEMBER_RESPONSE_UNKNOWN:" + ",".join(missing_symbols)
        if not frame["tradeStatus"].astype(str).str.strip().isin({"0", "1"}).all():
            return "UNKNOWN_TRADE_STATUS"
    if request_id.startswith("basic:"):
        codes = frame["code"].map(_canonical_vendor_symbol)
        expected = request_id.split(":", 1)[1]
        if len(frame) != 1 or codes.tolist() != [expected] or str(frame.iloc[0]["type"]).strip() != "1":
            return "SECURITY_BASIC_MISMATCH_OR_NOT_EQUITY"
    if request_id.startswith(("bars_raw:", "bars_adjusted:")):
        expected = request_id.split(":", 1)[1]
        if frame["date"].astype(str).duplicated().any():
            return "DUPLICATE_HISTORY_DATE"
        if set(frame["code"].map(_canonical_vendor_symbol).astype(str)) != {expected}:
            return "HISTORY_SECURITY_MISMATCH"
        expected_flag = "3" if request_id.startswith("bars_raw:") else "1"
        if not frame["adjustflag"].astype(str).str.strip().eq(expected_flag).all():
            return "ADJUSTFLAG_MISMATCH"
    if request_id == "calendar":
        parsed = pd.to_datetime(frame["calendar_date"], errors="coerce")
        flags = frame["is_trading_day"].astype(str).str.strip()
        if parsed.isna().any() or parsed.dt.strftime("%Y-%m-%d").duplicated().any():
            return "INVALID_OR_DUPLICATE_CALENDAR_DATE"
        if not flags.isin({"0", "1"}).all():
            return "UNKNOWN_CALENDAR_OPEN_STATE"
    return None


def run_daily_paper_workflow(
    *,
    output_root: str | Path = "data/private/experiments/daily-paper-runs",
    pool_path: str | Path = DEFAULT_POOL,
    config_path: str | Path = "configs/default.toml",
    identity_events_path: str | Path | None = None,
    brief_output_root: str | Path = "outputs/daily-research-briefs",
    agent_config_path: str | Path | None = None,
    model_mode: str,
    now: datetime | None = None,
    clock_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    session_factory: Callable[..., Any] = BaoStockSession,
) -> Path:
    """执行一次固定池每日纸面流程：检查运行时点、串行采集 21 个开市日输入、封存哈希、经现有门控生成排名并构建研究简报。参数：output_root/pool/config/identity 指定输入与私有运行根目录，brief/agent 配置简报输出与模型配置，model_mode 必须显式选择；now/clock_fn 可注入时钟，session_factory 提供数据会话。返回：唯一运行目录。副作用：发起 BaoStock 请求并保存原始及规范输入；只有排名有效后模型选项才可能运行。异常/阻断：收盘等待时间、数据缺口或哈希变化会留存 NEEDS_DATA；不连券商。"""
    if model_mode not in {"stub", "llm"}:
        raise ValueError("model_mode 必须显式为 stub 或 llm")
    local_value = now or clock_fn()
    if local_value.tzinfo is None or local_value.utcoffset() is None:
        raise ValueError("now 必须带时区")
    local_now = local_value.astimezone(SHANGHAI)
    signal_day = local_now.date()
    root = Path(output_root).expanduser()
    if not root.is_absolute():
        root = REPO_ROOT / root
    root = root.resolve()
    private_root = (REPO_ROOT / "data/private").resolve()
    if private_root not in root.parents:
        raise ValueError("每日纸面采集产物必须位于 Git 忽略的 data/private/ 下")
    run_id = f"daily-paper-{signal_day:%Y%m%d}-{uuid.uuid4().hex[:8]}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    raw_dir = run_dir / "raw" / "requests"
    raw_dir.mkdir(parents=True)
    started = local_now
    config_file = Path(config_path).expanduser()
    if not config_file.is_absolute():
        config_file = REPO_ROOT / config_file
    config_file = config_file.resolve()
    pool_file = Path(pool_path).expanduser()
    if not pool_file.is_absolute():
        pool_file = REPO_ROOT / pool_file
    pool_file = pool_file.resolve()
    blockers: list[str] = []
    config: dict[str, Any] = {}
    if not config_file.is_file():
        blockers.append(f"策略配置不存在：{config_file}")
    else:
        try:
            config = load_config(config_file)
        except Exception as exc:
            blockers.append(f"策略配置无法读取：{type(exc).__name__}")
    identity_file = Path(identity_events_path).expanduser() if identity_events_path else Path(
        config["data"].get("security_identity_events_file", "configs/security_identity_events.csv")
    )
    if not identity_file.is_absolute():
        identity_file = REPO_ROOT / identity_file
    identity_file = identity_file.resolve()
    pool_symbols: tuple[str, ...] = ()
    if not pool_file.is_file():
        blockers.append(f"固定研究池文件不存在：{pool_file}")
    else:
        try:
            pool_frame = pd.read_csv(pool_file, dtype=str, keep_default_na=False)
            if "symbol" not in pool_frame:
                blockers.append("固定研究池缺少 symbol 字段")
            else:
                pool_symbols = tuple(pool_frame["symbol"].map(canonical_symbol).astype(str))
                if pool_symbols != FIXED_SYMBOLS:
                    blockers.append("固定研究池证券或顺序不等于登记的 10 只流程试运行池")
        except (OSError, ValueError, pd.errors.ParserError):
            blockers.append("固定研究池文件无法解析")
    if not identity_file.is_file():
        blockers.append(f"证券身份事件文件不存在：{identity_file}")

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "classification": "DAILY_FIXED_POOL_PAPER_ONLY",
        "signal_date": signal_day.isoformat(),
        "started_at_asia_shanghai": started.isoformat(timespec="seconds"),
        "started_at_utc": _iso(started),
        "provider": "BaoStock",
        "provider_publication_evidence": "UNVERIFIED",
        "provider_published_at_utc": None,
        "capture_layer": "BaoStock SDK response DataFrames serialized to CSV; not wire bytes",
        "research_pool": {
            "path": str(pool_file),
            "sha256": _sha(pool_file) if pool_file.is_file() else None,
            "symbols": list(pool_symbols),
            "scope": "fixed 10-security process pilot only; not full-market selection",
        },
        "strategy_config": {
            "path": str(config_file),
            "sha256": _sha(config_file) if config_file.is_file() else None,
            "lookback_sessions": int(config.get("strategy", {}).get("lookback_sessions", 20)),
            "max_positions": int(config.get("strategy", {}).get("max_positions", 5)),
        },
        "identity_events": {
            "path": str(identity_file),
            "sha256": _sha(identity_file) if identity_file.is_file() else None,
            "coverage": "UNVERIFIED; an empty event file is not evidence that no event occurred",
        },
        "source_input_sha256_at_start": {
            "pool": _sha(pool_file) if pool_file.is_file() else None,
            "config": _sha(config_file) if config_file.is_file() else None,
            "identity_events": _sha(identity_file) if identity_file.is_file() else None,
        },
        "request_attempt_policy": "one serialized attempt per required endpoint; SDK retries disabled",
        "requests": {},
        "raw_responses": [],
        "blocking_reasons": blockers.copy(),
        "status": "CAPTURE_IN_PROGRESS",
        "rankings_generated": False,
        "model_calls": 0,
        "returns_computed": False,
        "orders_generated": False,
        "broker_connected": False,
        "formal_baseline": "NOT_READY",
    }
    _write_json(manifest, run_dir / "daily-run-manifest.json")

    if local_now.time().replace(tzinfo=None) < time(15, 5):
        blockers.append("当日 15:05 Asia/Shanghai 收盘数据等待时点尚未到；不抓取或回填上一交易日")
        manifest["capture_gate"] = "BLOCKED_BEFORE_CLOSE"
        return _finish_blocked(run_dir, manifest, blockers, "NEEDS_DATA")
    if blockers:
        return _finish_blocked(run_dir, manifest, blockers, "NEEDS_DATA_INVALID_FIXED_INPUT")

    bars_path = run_dir / "bars.csv"
    calendar_path = run_dir / "trading-calendar.csv"
    universe_path = run_dir / "point-in-time-universe.csv"
    status_path = run_dir / "vendor-status.csv"
    raw_frames: dict[str, pd.DataFrame] = {}
    request_reasons: list[str] = []
    session: Any | None = None
    try:
        session = session_factory(
            socket_timeout_seconds=20.0,
            minimum_interval_seconds=1.0,
            max_retries=0,
            retry_base_seconds=0.0,
        )
        with session:
            manifest["provider_package_version"] = getattr(session, "package_version", None)
            start_date = (signal_day - timedelta(days=120)).isoformat()
            calendar_raw = _request(
                session, manifest, raw_dir, raw_frames, "calendar",
                lambda: session.trade_calendar(start_date, signal_day.isoformat()), pool_symbols, clock_fn,
            )
            if calendar_raw is None or manifest["requests"]["calendar"]["status"] != "SUCCESS":
                request_reasons.append("当日日历请求失败或字段/日期校验不通过")
            else:
                retrieved = manifest["requests"]["calendar"]["completed_at_utc"]
                calendar = normalize_baostock_calendar(calendar_raw, retrieved)
                calendar.to_csv(calendar_path, index=False, lineterminator="\n")
                opens = calendar.loc[calendar["is_open"].fillna(False), "trade_date"].astype(str).tolist()
                if not opens or opens[-1] != signal_day.isoformat():
                    request_reasons.append("BaoStock 日历未确认本机日期为当日开市日；禁止回退至上一日采集")
                sessions = opens[-21:]
                if len(sessions) != 21 or sessions[-1] != signal_day.isoformat():
                    request_reasons.append(f"当日及此前 20 个开市日窗口不完整：取得 {len(sessions)} 个开市日")
                manifest["required_sessions"] = sessions
                manifest["calendar_range"] = {"start": start_date, "end": signal_day.isoformat()}
                if not request_reasons:
                    _collect_daily_inputs(
                        session=session,
                        manifest=manifest,
                        raw_dir=raw_dir,
                        raw_frames=raw_frames,
                        symbols=pool_symbols,
                        sessions=sessions,
                        signal_day=signal_day.isoformat(),
                        clock_fn=clock_fn,
                    )
    except Exception as exc:
        request_reasons.append(f"BaoStock 会话或采集流程失败：{type(exc).__name__}")
        manifest["session_failure_type"] = type(exc).__name__
        request_log = getattr(session, "request_log", []) if session is not None else []
        manifest["session_request_log"] = _sanitize_request_log(request_log)

    if request_reasons:
        blockers.extend(request_reasons)
    calendar_raw = raw_frames.get("calendar")
    if calendar_path.is_file():
        calendar = pd.read_csv(calendar_path, dtype=str, keep_default_na=False)
    else:
        calendar = pd.DataFrame()
    sessions = manifest.get("required_sessions", [])
    universe, bars, data_reasons = _build_daily_frames(
        symbols=pool_symbols,
        sessions=sessions,
        raw_frames=raw_frames,
        manifest=manifest,
        raw_dir=raw_dir,
    )
    blockers.extend(data_reasons)
    if not calendar.empty:
        calendar.to_csv(calendar_path, index=False, lineterminator="\n")
    if universe is not None:
        universe.to_csv(universe_path, index=False, lineterminator="\n")
        signal_status = _build_signal_status(
            universe=universe,
            symbols=pool_symbols,
            signal_day=signal_day.isoformat(),
            manifest=manifest,
        )
        signal_status.to_csv(status_path, index=False, lineterminator="\n")
    if bars is not None:
        write_bars(normalize_bar_frame(bars), bars_path)
    status_frame = pd.read_csv(status_path, dtype=str, keep_default_na=False) if status_path.is_file() else pd.DataFrame()
    manifest["status_requests"] = _signal_status_requests(
        pool_symbols, signal_day.isoformat(), manifest, status_frame,
    )
    manifest["price_requests"] = _adjusted_price_requests(pool_symbols, manifest)
    _seal_capture(
        run_dir=run_dir,
        manifest=manifest,
        blockers=blockers,
        bars_path=bars_path if bars_path.is_file() else None,
        calendar_path=calendar_path if calendar_path.is_file() else None,
        universe_path=universe_path if universe_path.is_file() else None,
        status_path=status_path if status_path.is_file() else None,
        pool_file=pool_file,
        config_file=config_file,
        identity_file=identity_file,
        raw_dir=raw_dir,
        completed_at=_clock_now(clock_fn, started),
    )
    if manifest["status"] != "CAPTURED_INPUTS_READY":
        return _write_needs_data_report(run_dir, manifest)

    try:
        identity_frame = pd.read_csv(identity_file, dtype=str, keep_default_na=False)
        identity_events = normalize_security_identity_events(identity_frame)
        window_dates = set(manifest.get("required_sessions", []))
        relevant = identity_events.loc[
            identity_events["effective_date"].astype(str).isin(window_dates)
            & (
                identity_events["old_symbol"].astype(str).isin(pool_symbols)
                | identity_events["new_symbol"].astype(str).isin(pool_symbols)
            )
        ]
        if not relevant.empty:
            return _write_needs_data_report(
                run_dir,
                _block_manifest(
                    manifest,
                    "21 日窗口存在证券身份变更；当前日常排名流程不自动拼接代码："
                    + ",".join(relevant["event_id"].astype(str)),
                ),
            )
        manifest["identity_event_window_check"] = "NO_REGISTERED_EVENT_IN_WINDOW; COVERAGE_UNVERIFIED"
    except Exception as exc:
        return _write_needs_data_report(
            run_dir,
            _block_manifest(manifest, f"证券身份事件输入无法规范化：{type(exc).__name__}"),
        )

    capture_manifest_path = run_dir / "capture-manifest.json"
    seal_path = run_dir / "capture-manifest.seal.json"
    if _sha(capture_manifest_path) != json.loads(seal_path.read_text(encoding="utf-8"))["capture_manifest_sha256"]:
        return _write_needs_data_report(run_dir, _block_manifest(manifest, "采集清单封存哈希不匹配"))
    rank_dir = run_dir / "ranking"
    try:
        ranking = create_paper_ranking_artifact(
            bars_path=bars_path,
            config_path=config_file,
            signal_date=signal_day.isoformat(),
            output_dir=rank_dir,
            classification="FORWARD_PAPER",
            source_capture_manifest_path=capture_manifest_path,
            trading_calendar_path=calendar_path,
            universe_path=universe_path,
            now_utc=_clock_now(clock_fn, started).astimezone(timezone.utc),
        )
        manifest["ranking_status"] = ranking["manifest"].get("status")
        manifest["ranking_manifest_path"] = str(rank_dir / "paper-ranking-manifest.json")
        manifest["ranking_manifest_sha256"] = _sha(rank_dir / "paper-ranking-manifest.json")
        if ranking["manifest"].get("status") not in {"CAPTURED", "CAPTURED_WITH_EXCLUSIONS"}:
            manifest["status"] = "NEEDS_DATA"
            manifest["blocking_reasons"].extend(ranking["manifest"].get("blocking_reasons", []))
            manifest["rankings_generated"] = False
            return _write_needs_data_report(run_dir, manifest)
        manifest["rankings_generated"] = True
        _write_post_rank_program_report(rank_dir)
        brief_dir = run_daily_research_brief(
            calendar_path=calendar_path,
            ranking_root=run_dir,
            output_root=brief_output_root,
            model_mode=model_mode,
            agent_config_path=agent_config_path,
            config_path=config_file,
            now=_clock_now(clock_fn, started).astimezone(SHANGHAI),
        )
        brief_manifest = json.loads((brief_dir / "brief-manifest.json").read_text(encoding="utf-8"))
        manifest["brief_path"] = str(brief_dir / "daily-brief.md")
        manifest["brief_status"] = brief_manifest.get("status")
        manifest["model_mode"] = model_mode
        manifest["model_calls"] = brief_manifest.get("model", {}).get("call_count", 0)
        manifest["llm_request_count"] = (
            brief_manifest.get("model", {}).get("call_count", 0) if model_mode == "llm" else 0
        )
        manifest["model_usage"] = brief_manifest.get("model", {}).get("usage")
        manifest["status"] = "COMPLETED_DAILY_PAPER_BRIEF" if brief_manifest.get("status", "").startswith("COMPLETED") else brief_manifest.get("status", "BRIEF_FAILED")
        manifest["output_sha256"] = {
            name: _sha(run_dir / name)
            for name in ("bars.csv", "trading-calendar.csv", "point-in-time-universe.csv", "vendor-status.csv", "capture-manifest.json", "capture-manifest.seal.json")
            if (run_dir / name).is_file()
        }
        manifest["ranking_output_sha256"] = {
            name: _sha(rank_dir / name)
            for name in (
                "ranking.csv", "h1-ranking.csv", "ranking-difference.csv",
                "window-audit.csv", "paper-ranking-manifest.json", "research-report.md",
            )
            if (rank_dir / name).is_file()
        }
        manifest["brief_manifest_sha256"] = _sha(brief_dir / "brief-manifest.json")
        _write_json(manifest, run_dir / "daily-run-manifest.json")
        _write_daily_run_report(run_dir, manifest)
        manifest["output_sha256"]["daily-run-report.md"] = _sha(run_dir / "daily-run-report.md")
        _write_json(manifest, run_dir / "daily-run-manifest.json")
        _write_manifest_seal(run_dir)
        return run_dir
    except Exception as exc:
        manifest["status"] = "DAILY_RANK_OR_BRIEF_FAILED"
        manifest["blocking_reasons"].append(f"排名或简报流程异常：{type(exc).__name__}")
        return _write_needs_data_report(run_dir, manifest)


def _collect_daily_inputs(
    *, session: Any, manifest: dict[str, Any], raw_dir: Path,
    raw_frames: dict[str, pd.DataFrame], symbols: tuple[str, ...],
    sessions: list[str], signal_day: str, clock_fn: Callable[[], datetime],
) -> None:
    """按固定顺序为所有证券和窗口日期串行发起数据请求。参数：session 为 BaoStock 会话，manifest/raw_dir/raw_frames 保存审计和响应，symbols/sessions/signal_day 定义冻结范围，clock_fn 提供请求时钟。返回：None；副作用：写入每个请求的原始响应与状态。请求失败会记录而继续其余已登记请求，不扩展股票池。"""
    for symbol in symbols:
        _request(session, manifest, raw_dir, raw_frames, f"basic:{symbol}", lambda symbol=symbol: session.stock_basic(symbol), symbols, clock_fn)
    for day in sessions:
        _request(session, manifest, raw_dir, raw_frames, f"membership:{day}", lambda day=day: session.all_stocks(day), symbols, clock_fn)
    start, end = sessions[0], sessions[-1]
    for symbol in symbols:
        _request(session, manifest, raw_dir, raw_frames, f"bars_raw:{symbol}", lambda symbol=symbol: session.history(symbol, start, end, adjustflag="3", fields=RAW_HISTORY_FIELDS), symbols, clock_fn)
        _request(session, manifest, raw_dir, raw_frames, f"bars_adjusted:{symbol}", lambda symbol=symbol: session.history(symbol, start, end, adjustflag="1", fields=RAW_HISTORY_FIELDS), symbols, clock_fn)
    manifest["signal_date"] = signal_day


def _request(
    session: Any, manifest: dict[str, Any], raw_dir: Path,
    raw_frames: dict[str, pd.DataFrame], request_id: str,
    call: Callable[[], pd.DataFrame], symbols: tuple[str, ...],
    clock_fn: Callable[[], datetime],
) -> pd.DataFrame | None:
    """执行并记录单次数据请求及响应校验。参数：session/manifest/raw_dir/raw_frames 为采集状态，request_id 标识任务，call 为供应商调用，symbols 为固定证券集合，clock_fn 记录本机时点。返回：响应 DataFrame 或 None。副作用：保存原始响应文件、行数/字段、哈希和脱敏请求日志；每项只尝试一次，响应校验失败不伪装成功。"""
    started = _clock_now(clock_fn, None)
    log_start = len(getattr(session, "request_log", []))
    frame: pd.DataFrame | None = None
    error_type: str | None = None
    try:
        frame = call()
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("BaoStock response is not a DataFrame")
        relative = Path("raw") / "requests" / f"{_safe_name(request_id)}.csv"
        path = raw_dir / relative.name
        if path.exists():
            raise FileExistsError(f"拒绝覆盖原始响应：{path.name}")
        frame.to_csv(path, index=False, lineterminator="\n")
        raw_hash = _sha(path)
        raw_frames[request_id] = frame.copy()
        validation = _request_validation(request_id, frame, symbols)
        status = "SUCCESS" if validation is None else "FAILED_VALIDATION"
        error_type = validation
    except Exception as exc:
        status = "FAILED_REQUEST"
        error_type = type(exc).__name__
        raw_hash = None
        relative = None
    completed = _clock_now(clock_fn, started)
    requests = manifest.setdefault("requests", {})
    requests[request_id] = {
        "status": status,
        "attempt_count": 1,
        "started_at_utc": _iso(started),
        "completed_at_utc": _iso(completed),
        "response_rows": int(len(frame)) if frame is not None else None,
        "response_fields": list(frame.columns) if frame is not None else None,
        "raw_response_path": str(relative) if relative else None,
        "raw_response_sha256": raw_hash,
        "error_category": error_type,
        "request_log": _sanitize_request_log(getattr(session, "request_log", [])[log_start:]),
    }
    manifest.setdefault("raw_responses", [])
    if relative:
        manifest["raw_responses"].append({
            "request_id": request_id,
            "path": str(relative),
            "sha256": raw_hash,
            "rows": int(len(frame)),
            "fields": list(frame.columns),
        })
    _write_json(manifest, raw_dir.parent.parent / "daily-run-manifest.json")
    return frame


def _build_daily_frames(
    *, symbols: tuple[str, ...], sessions: list[str], raw_frames: dict[str, pd.DataFrame],
    manifest: dict[str, Any], raw_dir: Path,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None, list[str]]:
    """把已校验原始响应组装为行情、逐日成员状态及阻断原因。参数：symbols/sessions 为冻结范围，raw_frames/manifest 保存请求结果，raw_dir 提供原件位置。返回：状态表、规范行情（缺失时为 None）和原因列表。限制：窗口必须是完整 21 开市日；成员缺失或来源状态冲突保持未知并阻断。"""
    reasons: list[str] = []
    if len(sessions) != 21:
        return None, None, ["21 个开市日窗口不可用"]
    basics: dict[str, pd.Series] = {}
    for symbol in symbols:
        frame = raw_frames.get(f"basic:{symbol}")
        if frame is not None and manifest["requests"].get(f"basic:{symbol}", {}).get("status") == "SUCCESS" and len(frame) == 1:
            basics[symbol] = frame.iloc[0]
        else:
            reasons.append(f"{symbol}: 上市日期/type 基础资料缺失或未验证")

    memberships: dict[str, dict[str, Any]] = {}
    for day in sessions:
        frame = raw_frames.get(f"membership:{day}")
        req = manifest["requests"].get(f"membership:{day}", {})
        if frame is None or req.get("status") != "SUCCESS":
            reasons.append(f"{day}: 固定池成员清单未完整验证；缺席代码状态保持 UNKNOWN")
            continue
        code_map = {_canonical_vendor_symbol(row["code"]): row for _, row in frame.iterrows()}
        memberships[day] = code_map

    bars: list[pd.DataFrame] = []
    universe_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        raw = raw_frames.get(f"bars_raw:{symbol}")
        adjusted = raw_frames.get(f"bars_adjusted:{symbol}")
        raw_request = manifest["requests"].get(f"bars_raw:{symbol}", {})
        adjusted_request = manifest["requests"].get(f"bars_adjusted:{symbol}", {})
        basic = basics.get(symbol)
        normalized: pd.DataFrame | None = None
        if raw is None or adjusted is None or raw_request.get("status") != "SUCCESS" or adjusted_request.get("status") != "SUCCESS" or basic is None:
            reasons.append(f"{symbol}: 原始/后复权 21 日行情或证券基础资料缺失")
        else:
            raw_dates = pd.to_datetime(raw["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            adjusted_dates = pd.to_datetime(adjusted["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            expected_dates = set(sessions)
            if set(raw_dates) != expected_dates or set(adjusted_dates) != expected_dates or set(raw_dates) != set(adjusted_dates):
                reasons.append(f"{symbol}: 原始/后复权价格窗口与 21 个开市日不完全一致")
            else:
                retrieved = max(raw_request["completed_at_utc"], adjusted_request["completed_at_utc"])
                try:
                    normalized, skipped = normalize_baostock_history(raw, adjusted, basic, retrieved)
                    normalized = normalized.loc[normalized["trade_date"].astype(str).isin(expected_dates)].copy()
                    numeric = normalized[["open", "high", "low", "close", "adjusted_close"]].apply(pd.to_numeric, errors="coerce")
                    invalid = numeric.isna().any(axis=1) | ~numeric.map(
                        lambda value: math.isfinite(float(value)) and float(value) > 0 if pd.notna(value) else False,
                    ).all(axis=1)
                    invalid |= numeric["low"].gt(numeric[["open", "close"]].min(axis=1))
                    invalid |= numeric["high"].lt(numeric[["open", "close"]].max(axis=1))
                    if skipped or invalid.any() or len(normalized) != 21:
                        reasons.append(f"{symbol}: 无效价格/过滤行或规范行情不足 21 行")
                except Exception as exc:
                    reasons.append(f"{symbol}: BaoStock 行情规范化失败：{type(exc).__name__}")
                    normalized = None
        if normalized is not None and len(normalized):
            bars.append(normalized)

        for day in sessions:
            membership = memberships.get(day, {}).get(symbol)
            pool_status = "STOCK_MEMBER" if membership is not None else "UNKNOWN"
            basic = basics.get(symbol)
            is_listed = _listed(basic, day)
            raw_row = _row_for_day(raw, day)
            adjusted_row = _row_for_day(adjusted, day)
            member_trade = membership.get("tradeStatus") if membership is not None else None
            raw_trade = raw_row.get("tradestatus") if raw_row is not None else None
            adjusted_trade = adjusted_row.get("tradestatus") if adjusted_row is not None else None
            is_suspended = _suspension(member_trade, raw_trade, adjusted_trade)
            is_st = _st(raw_row, adjusted_row)
            missing_status = [
                field for field, value in (
                    ("membership", pool_status), ("is_listed", is_listed),
                    ("is_suspended", is_suspended), ("is_st", is_st),
                ) if pd.isna(value) or value == "UNKNOWN"
            ]
            if pool_status != "STOCK_MEMBER":
                reasons.append(f"{day} {symbol}: 点时成员状态 UNKNOWN")
            if pd.isna(is_listed) or not bool(is_listed):
                reasons.append(f"{day} {symbol}: 上市状态未知或与固定股票池不一致")
            if pd.isna(is_suspended):
                reasons.append(f"{day} {symbol}: 停牌状态 UNKNOWN/来源冲突")
            if pd.isna(is_st):
                reasons.append(f"{day} {symbol}: ST 状态 UNKNOWN/来源冲突")
            if missing_status:
                status = "UNKNOWN"
            else:
                status = "VERIFIED"
            universe_rows.append({
                "trade_date": day,
                "symbol": symbol,
                "pool_status": pool_status,
                "is_listed": is_listed,
                "is_suspended": is_suspended,
                "is_st": is_st,
                "daily_trade_status": str(member_trade) if member_trade is not None else "UNKNOWN",
                "status_source": "BaoStock.query_all_stock+query_stock_basic+query_history_k_data_plus",
                "membership_request_id": f"membership:{day}",
                "membership_received_at_utc": manifest["requests"].get(f"membership:{day}", {}).get("completed_at_utc"),
                "listing_request_id": f"basic:{symbol}",
                "listing_received_at_utc": manifest["requests"].get(f"basic:{symbol}", {}).get("completed_at_utc"),
                "raw_bar_request_id": f"bars_raw:{symbol}",
                "raw_bar_received_at_utc": manifest["requests"].get(f"bars_raw:{symbol}", {}).get("completed_at_utc"),
                "adjusted_bar_request_id": f"bars_adjusted:{symbol}",
                "adjusted_bar_received_at_utc": manifest["requests"].get(f"bars_adjusted:{symbol}", {}).get("completed_at_utc"),
                "status_validation": status,
                "status_evidence": json.dumps({
                    "member_trade_status": str(member_trade) if member_trade is not None else None,
                    "raw_trade_status": str(raw_trade) if raw_trade is not None else None,
                    "adjusted_trade_status": str(adjusted_trade) if adjusted_trade is not None else None,
                    "missing_status_fields": missing_status,
                }, ensure_ascii=False, sort_keys=True),
            })

    universe = pd.DataFrame(universe_rows)
    bar_frame = normalize_bar_frame(pd.concat(bars, ignore_index=True)) if bars else pd.DataFrame()
    return universe, bar_frame, sorted(set(reasons))


def _build_signal_status(
    *, universe: pd.DataFrame, symbols: tuple[str, ...], signal_day: str,
    manifest: dict[str, Any],
) -> pd.DataFrame:
    """将规范状态表投影成简报验证器需要的信号日逐证券字段。参数：universe 为逐日状态，symbols 为固定池，signal_day 为目标日期，manifest 保存统计。返回：每只证券的交易状态和 ST 状态；记录不唯一时明确标 UNKNOWN。"""
    rows = universe.loc[
        universe["trade_date"].astype(str).eq(signal_day)
        & universe["symbol"].astype(str).isin(symbols)
    ]
    records: list[dict[str, str]] = []
    for symbol in symbols:
        match = rows.loc[rows["symbol"].astype(str).eq(symbol)]
        if len(match) != 1:
            records.append({"trade_date": signal_day, "symbol": symbol, "tradestatus": "UNKNOWN", "isST": "UNKNOWN"})
            continue
        row = match.iloc[0]
        status = str(row.get("daily_trade_status", "UNKNOWN"))
        st_value = row.get("is_st", pd.NA)
        st = "UNKNOWN" if pd.isna(st_value) else ("1" if bool(st_value) else "0")
        records.append({"trade_date": signal_day, "symbol": symbol, "tradestatus": status, "isST": st})
    result = pd.DataFrame(records, columns=["trade_date", "symbol", "tradestatus", "isST"])
    manifest["signal_status_row_count"] = len(result)
    return result


def _signal_status_requests(
    symbols: tuple[str, ...], signal_day: str, manifest: dict[str, Any],
    status_frame: pd.DataFrame,
) -> list[dict[str, Any]]:
    """把信号日状态值与实际来源请求和本机请求时间关联。参数：symbols/signal_day 定义范围，manifest 保存请求证据，status_frame 为状态投影。返回：逐证券来源、请求起止时间和状态记录；不声称这些时间是供应商发布时间。"""
    requests = manifest.get("requests", {})
    membership = requests.get(f"membership:{signal_day}", {})
    records = []
    for symbol in symbols:
        status_rows = status_frame.loc[
            status_frame.get("symbol", pd.Series(dtype=str)).astype(str).eq(symbol)
            & status_frame.get("trade_date", pd.Series(dtype=str)).astype(str).eq(signal_day)
        ] if not status_frame.empty else pd.DataFrame()
        source_requests = [
            membership,
            requests.get(f"bars_raw:{symbol}", {}),
            requests.get(f"bars_adjusted:{symbol}", {}),
        ]
        starts = [item.get("started_at_utc") for item in source_requests if item.get("started_at_utc")]
        finishes = [item.get("completed_at_utc") for item in source_requests if item.get("completed_at_utc")]
        records.append({
            "trade_date": signal_day,
            "symbol": symbol,
            "tradestatus": str(status_rows.iloc[0]["tradestatus"]) if len(status_rows) == 1 else "UNKNOWN",
            "isST": str(status_rows.iloc[0]["isST"]) if len(status_rows) == 1 else "UNKNOWN",
            "source": "BaoStock.query_all_stock+query_history_k_data_plus",
            "requested_at_utc": min(starts) if starts else None,
            "received_at_utc": max(finishes) if finishes else None,
        })
    return records


def _adjusted_price_requests(
    symbols: tuple[str, ...], manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """生成后复权价格请求的来源索引。参数：symbols 为固定池，manifest 含请求及原始响应记录。返回：逐证券复权方式、接口、文件路径和本机请求时间；没有成功封存原件的项不输出。"""
    requests = manifest.get("requests", {})
    response_by_id = {
        item.get("request_id"): item
        for item in manifest.get("raw_responses", [])
        if isinstance(item, dict)
    }
    result = []
    for symbol in symbols:
        request_id = f"bars_adjusted:{symbol}"
        request = requests.get(request_id, {})
        response = response_by_id.get(request_id, {})
        if not response.get("path"):
            continue
        result.append({
            "symbol": symbol,
            "source": "BaoStock.query_history_k_data_plus",
            "adjust": "hfq",
            "adjustflag": "1",
            "file": response["path"],
            "requested_at_utc": request.get("started_at_utc"),
            "received_at_utc": request.get("completed_at_utc"),
        })
    return result


def _seal_capture(
    *, run_dir: Path, manifest: dict[str, Any], blockers: list[str],
    bars_path: Path | None, calendar_path: Path | None, universe_path: Path | None,
    status_path: Path | None,
    pool_file: Path, config_file: Path, identity_file: Path, raw_dir: Path,
    completed_at: datetime,
) -> None:
    """复核采集期间输入未变并封存原始/规范产物哈希。参数：run_dir/manifest 为本次运行状态，blockers 为阻断列表，各输出路径为规范文件，pool/config/identity 为冻结输入，raw_dir 为原始响应目录，completed_at 为封存时点。返回：None。副作用：更新 manifest 和 seal 文件。异常条件以 NEEDS_DATA 阻断；供应商发布时间仍标未验证。"""
    current_sources = {
        "pool": _sha(pool_file), "config": _sha(config_file), "identity_events": _sha(identity_file),
    }
    if current_sources != manifest.get("source_input_sha256_at_start"):
        blockers.append("采集期间固定池、配置或身份事件输入发生变化")
    files = {
        name: path for name, path in (
            ("bars", bars_path), ("calendar", calendar_path), ("universe", universe_path),
            ("vendor_status", status_path),
        ) if path is not None and path.is_file()
    }
    raw_hashes = {}
    for item in manifest.get("raw_responses", []):
        candidate = run_dir / item["path"]
        if not candidate.is_file() or _sha(candidate) != item["sha256"]:
            blockers.append(f"原始响应缺失或哈希错误：{item['path']}")
        else:
            raw_hashes[item["path"]] = item["sha256"]
    for request_id, info in manifest.get("requests", {}).items():
        if info.get("status") != "SUCCESS":
            blockers.append(f"{request_id}: {info.get('error_category') or info.get('status')}")
    manifest["finished_at_utc"] = _iso(completed_at)
    manifest["finished_at_asia_shanghai"] = completed_at.astimezone(SHANGHAI).isoformat(timespec="seconds")
    manifest["file_sha256"] = {name: _sha(path) for name, path in files.items()}
    manifest["raw_response_sha256"] = raw_hashes
    manifest["source_input_sha256"] = {
        "pool": _sha(pool_file), "config": _sha(config_file), "identity_events": _sha(identity_file),
    }
    if "calendar" in files:
        manifest["normalized_sha256"] = _sha(files["calendar"])
        manifest["calendar_source"] = "BaoStock.query_trade_dates"
    manifest["data_file_sha256"] = manifest["file_sha256"].get("bars")
    manifest["status_file_sha256"] = manifest["file_sha256"].get("vendor_status")
    manifest["price_files_sha256"] = {
        item["path"]: item["sha256"] for item in manifest.get("raw_responses", [])
        if item["request_id"].startswith("bars_adjusted:")
    }
    manifest["source_id"] = "BaoStock"
    manifest["capture_started_at_utc"] = manifest["started_at_utc"]
    manifest["capture_finished_at_utc"] = manifest["finished_at_utc"]
    manifest["captured_at_utc"] = manifest["finished_at_utc"]
    manifest["capture_finished_at_asia_shanghai"] = manifest["finished_at_asia_shanghai"]
    manifest["signal_day_capture_interval"] = "SAME_DAY_AFTER_15_05_REQUIRED"
    manifest["provider_published_at_utc"] = None
    manifest["provider_publication_evidence"] = "UNVERIFIED"
    manifest["trusted_external_timestamp"] = "UNVERIFIED"
    manifest["state_window_row_count"] = 0
    if universe_path is not None and universe_path.is_file():
        try:
            manifest["state_window_row_count"] = int(len(pd.read_csv(universe_path, usecols=["trade_date", "symbol"])))
        except (OSError, ValueError, pd.errors.ParserError):
            blockers.append("逐日状态窗口文件无法复核")
    manifest["status"] = "CAPTURED_INPUTS_READY" if not blockers else "NEEDS_DATA"
    manifest["capture_manifest_status"] = manifest["status"]
    manifest["blocking_reasons"] = sorted(set(blockers))
    manifest["rankings_generated"] = False
    manifest["returns_computed"] = False
    manifest["orders_generated"] = False
    manifest["broker_connected"] = False
    manifest["formal_baseline"] = "NOT_READY"
    manifest_path = run_dir / "capture-manifest.json"
    _write_json(manifest, manifest_path)
    seal_path = run_dir / "capture-manifest.seal.json"
    _write_json({"capture_manifest_sha256": _sha(manifest_path), "sealed_at_utc": _iso(completed_at)}, seal_path)


def _finish_blocked(run_dir: Path, manifest: dict[str, Any], blockers: list[str], status: str) -> Path:
    """以指定阻断状态完成运行归档。参数：run_dir、manifest、blockers 和 status 描述本次结果。返回：运行目录。副作用：写报告与 manifest seal；不会调用排名或模型。"""
    manifest["status"] = status
    manifest["blocking_reasons"] = sorted(set(blockers))
    manifest["finished_at_utc"] = _iso(datetime.now(timezone.utc))
    manifest["finished_at_asia_shanghai"] = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    manifest["rankings_generated"] = False
    manifest["model_calls"] = 0
    _write_json(manifest, run_dir / "daily-run-manifest.json")
    _write_daily_run_report(run_dir, manifest)
    _write_manifest_seal(run_dir)
    return run_dir


def _write_needs_data_report(run_dir: Path, manifest: dict[str, Any]) -> Path:
    """保存当前 NEEDS_DATA manifest、中文运行报告和封条。参数：run_dir 为唯一运行目录，manifest 为状态事实。返回：运行目录。"""
    _write_json(manifest, run_dir / "daily-run-manifest.json")
    _write_daily_run_report(run_dir, manifest)
    _write_manifest_seal(run_dir)
    return run_dir


def _block_manifest(manifest: dict[str, Any], reason: str) -> dict[str, Any]:
    """将一个阻断原因追加到内存 manifest。参数：manifest 为运行状态，reason 为具体原因。返回：同一 manifest 对象；会将排名和模型调用标记为未发生。"""
    manifest["status"] = "NEEDS_DATA"
    manifest.setdefault("blocking_reasons", []).append(reason)
    manifest["rankings_generated"] = False
    manifest["model_calls"] = 0
    return manifest


def _write_daily_run_report(run_dir: Path, manifest: dict[str, Any]) -> None:
    """根据程序采集状态生成中文简报运行记录。参数：run_dir 为运行目录，manifest 为数据和门控事实。返回：None。副作用：写 Markdown；不从缺失记录推断正常状态。"""
    reasons = manifest.get("blocking_reasons", [])
    lines = [
        "# 每日小范围纸面研究运行记录", "",
        f"- 状态：`{manifest.get('status')}`", f"- 研究/尝试日期：`{manifest.get('signal_date')}`",
        f"- 本机启动时间：`{manifest.get('started_at_asia_shanghai')}`",
        f"- 本机抓取完成时间：`{manifest.get('finished_at_asia_shanghai', 'UNKNOWN')}`",
        f"- 来源：BaoStock；供应商发布时间：`{manifest.get('provider_publication_evidence', 'UNVERIFIED')}`",
        f"- 固定 10 只流程试运行池：`{', '.join(manifest.get('research_pool', {}).get('symbols', []))}`",
        f"- 21 开市日窗口：`{len(manifest.get('required_sessions', []))}` 日",
        f"- 研究排名已生成：`{manifest.get('rankings_generated', False)}`",
        f"- LLM 请求数：`{manifest.get('model_calls', 0)}`",
        "- 收益计算：否；订单/可执行股数：否；券商连接：否；正式基线：`NOT_READY`。", "",
        "## 数据门控与限制", "",
    ]
    lines.extend([f"- {reason}" for reason in reasons] or ["- 必需采集输入完成初步校验。"])
    lines.extend([
        "", "排名只属于固定 10 只证券的研究流程候选，不代表全市场选择或可成交证明；"
        "BaoStock 本机抓取时间不等于供应商发布时间。空身份事件表也不证明不存在公司行动或身份变更。", "",
        "## 产物", "", f"- 运行清单：`{run_dir / 'daily-run-manifest.json'}`",
        f"- 采集清单：`{run_dir / 'capture-manifest.json' if (run_dir / 'capture-manifest.json').is_file() else 'NOT_CREATED'}`",
    ])
    (run_dir / "daily-run-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_post_rank_program_report(rank_dir: Path) -> None:
    """从已封存排名文件生成程序事实简报。参数：rank_dir 为排名归档目录。返回：None。副作用：写入研究报告；只复述 CSV 和 manifest，不把排名解释为收益或成交。"""
    ranking = pd.read_csv(rank_dir / "ranking.csv", dtype={"symbol": str})
    h1 = pd.read_csv(rank_dir / "h1-ranking.csv", dtype={"symbol": str})
    manifest = json.loads((rank_dir / "paper-ranking-manifest.json").read_text(encoding="utf-8"))
    original_top = ranking.loc[ranking["rank"].astype(int).le(5)]
    h1_unchanged = (
        ranking["symbol"].astype(str).tolist() == h1["symbol"].astype(str).tolist()
        and ranking["rank"].astype(int).tolist() == h1["rank"].astype(int).tolist()
        and ranking["momentum"].astype(float).tolist() == h1["momentum"].astype(float).tolist()
    )
    scope_count = int(manifest.get("window_scope", {}).get("point_in_time_universe_members", 0))
    lines = [
        "# 排名后程序事实记录", "",
        f"- 分类：`{manifest.get('classification')}`；信号日：`{manifest.get('signal_date')}`。",
        "- 以下表格由程序排名文件直接生成；不代表策略收益、成交或可执行交易建议。", "",
        f"- H1 排名与原排名{'一致' if h1_unchanged else '发生变化'}。",
        f"- {scope_count} 只研究证券通过窗口检查；这是研究池窗口核验数，不等同于排名条数。",
        "", "## 排名", "", "| 名次 | 证券 | 动量 |", "|---:|---|---:|",
    ]
    lines.extend(
        f"| {int(row.rank)} | {row.symbol} | {float(row.momentum) * 100:.6f}% |"
        for row in original_top.itertuples(index=False)
    )
    lines.extend([
        "", "## 输入", "",
        f"- H1 窗口检查规则：{manifest.get('h1_filter_rule', {}).get('rule', '按封存窗口审计规则筛选')}。",
        "这是固定 10 只证券的小范围研究候选。H1 是候选窗口筛选，不证明收益改善或可成交性。"
        "程序未计算收益/NAV/回撤（未计算收益），没有可执行股数或委托；正式基线 NOT_READY。", "",
    ])
    (rank_dir / "research-report.md").write_text("\n".join(lines), encoding="utf-8")


def _write_manifest_seal(run_dir: Path) -> None:
    """为每日运行 manifest 写入 SHA-256 封条。参数：run_dir 为运行目录。返回：None；manifest 需先完成写入。"""
    manifest_path = run_dir / "daily-run-manifest.json"
    (run_dir / "daily-run-manifest.sha256").write_text(
        f"{_sha(manifest_path)}  daily-run-manifest.json\n", encoding="ascii",
    )


def _sanitize_request_log(items: Any) -> list[dict[str, Any]]:
    """从供应商请求日志中剔除凭据类字段。参数：items 为原始日志列表。返回：保留非密钥字段的字典列表；不写入环境变量或 API Key。"""
    result = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        result.append({key: value for key, value in item.items() if key.lower() not in {"api_key", "authorization", "token"}})
    return result


def _safe_name(value: str) -> str:
    """把请求标识转换为适合文件名的字符集。参数：value 为逻辑请求名。返回：仅由安全文件名字符构成的字符串。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _clock_now(clock_fn: Callable[[], datetime], fallback: datetime | None) -> datetime:
    """通过注入时钟取得 UTC 时间并校验时区。参数：clock_fn 为时钟函数，fallback 为兼容参数。返回：UTC datetime。异常：时钟返回无时区值时抛出 ValueError。当前实现两条分支都调用 clock_fn，fallback 不提供备用时间，语义待核实。"""
    value = clock_fn() if fallback is None else clock_fn()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock_fn 必须返回带时区的时间")
    return value.astimezone(timezone.utc)


def _write_json(value: Any, path: Path) -> None:
    """创建父目录并写入可读 JSON。参数：value 为 JSON 兼容对象，path 为目标文件。副作用：创建或覆盖目标路径；调用方负责运行目录唯一性。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
