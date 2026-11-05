from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.config import load_config
from cehuan.data.canonical import canonical_symbol, read_bars
from cehuan.data.calendar import (
    audit_security_windows,
    normalize_trading_calendar,
    validate_bars_against_calendar,
)
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


SHANGHAI = ZoneInfo("Asia/Shanghai")


def create_paper_ranking_artifact(
    *,
    bars_path: str | Path,
    config_path: str | Path,
    signal_date: str,
    output_dir: str | Path,
    classification: str = "SYNTHETIC",
    source_capture_manifest_path: str | Path | None = None,
    trading_calendar_path: str | Path | None = None,
    universe_path: str | Path | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Generate and seal a non-order candidate ranking; forward mode needs same-day capture evidence."""
    if classification not in {"SYNTHETIC", "FORWARD_PAPER"}:
        raise ValueError("classification 只能是 SYNTHETIC 或 FORWARD_PAPER")
    signal_day = date.fromisoformat(signal_date)
    bars_file, config_file = Path(bars_path).resolve(), Path(config_path).resolve()
    capture_file = Path(source_capture_manifest_path).resolve() if source_capture_manifest_path else None
    calendar_file = Path(trading_calendar_path).resolve() if trading_calendar_path else None
    universe_file = Path(universe_path).resolve() if universe_path else None
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"拒绝覆盖已有前瞻排名归档: {target}")

    started = _coerce_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    report: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "PAPER_RANKING_RUN",
        "classification": classification,
        "paper_run_id": target.name,
        "signal_date": signal_day.isoformat(),
        "input_paths": {
            "bars": str(bars_file),
            "config": str(config_file),
            "source_capture_manifest": str(capture_file) if capture_file else None,
            "trading_calendar": str(calendar_file) if calendar_file else None,
            "point_in_time_universe": str(universe_file) if universe_file else None,
        },
        "ranking_started_at_utc": _iso_utc(started),
        "ranking_generated_at_utc": None,
        "ranking_file_sha256": None,
        "input_data_sha256": _sha256(bars_file) if bars_file.is_file() else None,
        "config_sha256": _sha256(config_file) if config_file.is_file() else None,
        "source_capture_manifest_sha256": _sha256(capture_file) if capture_file and capture_file.is_file() else None,
        "trading_calendar_sha256": _sha256(calendar_file) if calendar_file and calendar_file.is_file() else None,
        "universe_sha256": _sha256(universe_file) if universe_file and universe_file.is_file() else None,
        "source_data_capture_time_utc": None,
        "data_trust": "UNVERIFIED",
        "code_commit": _git_revision(),
        "worktree_dirty": _git_dirty(),
        "manifest_signature": "UNSIGNED",
        "trusted_external_timestamp": "UNVERIFIED",
        "independent_holdout": False,
        "strategy_returns_computed": False,
        "broker_connected": False,
        "orders_generated": False,
        "formal_baseline": "NOT_READY",
    }
    try:
        readiness = _forward_input_readiness(
            classification=classification,
            signal_day=signal_day,
            now=started,
            bars_file=bars_file,
            config_file=config_file,
            capture_file=capture_file,
            calendar_file=calendar_file,
            universe_file=universe_file,
        )
        report["readiness_checks"] = readiness["checks"]
        report["data_trust"] = readiness["data_trust"]
        report["source_data_capture_time_utc"] = readiness.get("source_data_capture_time_utc")
        if readiness["blocking_reasons"]:
            report["status"] = "NEEDS_DATA" if classification == "FORWARD_PAPER" else "FAILED_INPUT"
            report["blocking_reasons"] = readiness["blocking_reasons"]
            report["candidate_count"] = 0
        else:
            config = load_config(config_file)
            bars = read_bars(bars_file)
            bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="raise").dt.strftime("%Y-%m-%d")
            if (bars["trade_date"] > signal_day.isoformat()).any():
                raise ValueError("输入行情含 signal_date 之后的数据，拒绝可能存在前视的信息")
            if bars.duplicated(["trade_date", "symbol"]).any():
                raise ValueError("行情存在重复证券日，拒绝生成排名")
            if classification == "FORWARD_PAPER":
                assert calendar_file is not None and universe_file is not None
                calendar = normalize_trading_calendar(pd.read_csv(calendar_file, dtype=str))
                universe = pd.read_csv(universe_file, dtype=str, keep_default_na=False)
                required_universe_fields = {"trade_date", "symbol", "pool_status"}
                if not required_universe_fields.issubset(universe.columns):
                    raise ValueError("点时股票池缺少 trade_date、symbol 或 pool_status")
                universe["trade_date"] = pd.to_datetime(universe["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                signal_members = universe.loc[universe["trade_date"].eq(signal_day.isoformat())].copy()
                if signal_members["symbol"].map(canonical_symbol).duplicated().any():
                    raise ValueError("signal_date 股票池存在重复证券成员")
                members = universe.loc[
                    universe["trade_date"].eq(signal_day.isoformat())
                    & universe["pool_status"].eq("STOCK_MEMBER"), "symbol",
                ].map(canonical_symbol).astype(str).tolist()
                if not members:
                    raise ValueError("点时股票池在 signal_date 没有已确认的 STOCK_MEMBER")
                bars = bars.loc[bars["symbol"].astype(str).isin(set(members))].copy()
                calendar_check = validate_bars_against_calendar(bars, calendar)
                if not calendar_check["healthy"]:
                    raise ValueError("前瞻行情与交易日历不一致：" + "; ".join(calendar_check["errors"]))
                audit = audit_security_windows(
                    bars, calendar, members, signal_date=signal_day,
                    lookback_sessions=int(config["strategy"]["lookback_sessions"]),
                )
                audit_path = staging / "window-audit.csv"
                audit.to_csv(audit_path, index=False)
                report["window_audit_file"] = "window-audit.csv"
                report["window_audit_sha256"] = _sha256(audit_path)
                report["window_scope"] = {
                    "point_in_time_universe_members": len(members),
                    "complete_symbols": int(audit["window_status"].isin(["COMPLETE", "COMPLETE_WITH_SUSPENSION"]).sum()),
                    "excluded_symbols": int((~audit["included_in_ranking_scope"]).sum()),
                    "excluded_by_status": audit.loc[~audit["included_in_ranking_scope"], "window_status"].value_counts().to_dict(),
                    "suspended_window_symbols": int(audit["window_status"].eq("COMPLETE_WITH_SUSPENSION").sum()),
                }
                rank_symbols = set(audit.loc[audit["included_in_ranking_scope"], "symbol"].astype(str))
                bars = bars.loc[bars["symbol"].astype(str).isin(rank_symbols)].copy()
                if not rank_symbols:
                    raise ValueError("所有点时股票池证券都因逐证券窗口缺口被排除")
                _validate_signal_window_availability(
                    bars, signal_day=signal_day,
                    lookback_sessions=int(config["strategy"]["lookback_sessions"]),
                    available_by=started,
                )
            feature_input = bars.drop(columns=[column for column in ("available_at_utc", "retrieved_at_utc_parsed") if column in bars.columns])
            features = add_momentum(feature_input, int(config["strategy"]["lookback_sessions"]), normalized=True)
            day = features.loc[features["trade_date"].eq(signal_day.isoformat())].copy()
            if day.empty:
                raise ValueError("行情中没有 signal_date 对应的收盘行")
            ranked = rank_precomputed_day(
                day, config["strategy"], config["market"], require_order_rules=False,
            ).copy()
            ranked["signal_date"] = signal_day.isoformat()
            ranked["selected_target"] = ranked["rank"].le(int(config["strategy"]["max_positions"]))
            ranking = ranked[["signal_date", "symbol", "momentum", "rank", "selected_target"]].copy()
            if ranking.empty:
                raise ValueError("没有满足冻结基线筛选条件的排名候选")
            generated = datetime.now(timezone.utc) if now_utc is None else _coerce_utc(now_utc)
            report["ranking_generated_at_utc"] = _iso_utc(generated)
            report["candidate_count"] = int(len(ranking))
            report["status"] = "CAPTURED_WITH_EXCLUSIONS" if report.get("window_scope", {}).get("excluded_symbols", 0) else "CAPTURED"
            report["result_type"] = "RANKING_ONLY_NO_RETURN_OR_EXECUTION_RESULT"
            ranking_path = staging / "ranking.csv"
            ranking.to_csv(ranking_path, index=False)
            report["ranking_file_sha256"] = _sha256(ranking_path)
            report["ranking_file"] = "ranking.csv"
            report["strategy_config"] = {
                "name": config.get("strategy", {}).get("name"),
                "lookback_sessions": config.get("strategy", {}).get("lookback_sessions"),
                "max_positions": config.get("strategy", {}).get("max_positions"),
            }
        if bars_file.is_file() and report["input_data_sha256"] != _sha256(bars_file):
            report["status"] = "FAILED_INPUT_CHANGED_DURING_RUN"
            report["blocking_reasons"] = ["行情源文件在生成排名期间发生变化"]
            report.pop("ranking_file", None)
            report["ranking_file_sha256"] = None
            (staging / "ranking.csv").unlink(missing_ok=True)
        if config_file.is_file() and report["config_sha256"] != _sha256(config_file):
            report["status"] = "FAILED_INPUT_CHANGED_DURING_RUN"
            report.setdefault("blocking_reasons", []).append("配置文件在生成排名期间发生变化")
            report.pop("ranking_file", None)
            report["ranking_file_sha256"] = None
            (staging / "ranking.csv").unlink(missing_ok=True)
        if capture_file is not None:
            current_capture_hash = _sha256(capture_file) if capture_file.is_file() else None
            if report["source_capture_manifest_sha256"] != current_capture_hash:
                report["status"] = "FAILED_INPUT_CHANGED_DURING_RUN"
                report.setdefault("blocking_reasons", []).append("行情抓取清单在生成排名期间发生变化")
                report.pop("ranking_file", None)
                report["ranking_file_sha256"] = None
                (staging / "ranking.csv").unlink(missing_ok=True)
        for label, input_file, key in (
            ("交易日历", calendar_file, "trading_calendar_sha256"),
            ("点时股票池", universe_file, "universe_sha256"),
        ):
            if input_file is not None:
                current_hash = _sha256(input_file) if input_file.is_file() else None
                if report[key] != current_hash:
                    report["status"] = "FAILED_INPUT_CHANGED_DURING_RUN"
                    report.setdefault("blocking_reasons", []).append(f"{label}文件在生成排名期间发生变化")
                    report.pop("ranking_file", None)
                    report["ranking_file_sha256"] = None
                    (staging / "ranking.csv").unlink(missing_ok=True)
        report["finished_at_utc"] = _iso_utc(datetime.now(timezone.utc))
        manifest_path = staging / "paper-ranking-manifest.json"
        manifest_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if target.exists():
            raise FileExistsError(f"发布前目标已出现，拒绝覆盖: {target}")
        os.rename(staging, target)
        return {"output_dir": str(target), "manifest": report}
    except Exception as exc:
        report["status"] = "FAILED_INPUT"
        report.setdefault("blocking_reasons", []).append(f"排名生成未完成: {type(exc).__name__}: {exc}")
        report["candidate_count"] = 0
        report.pop("ranking_file", None)
        report["ranking_file_sha256"] = None
        (staging / "ranking.csv").unlink(missing_ok=True)
        report["finished_at_utc"] = _iso_utc(datetime.now(timezone.utc))
        (staging / "paper-ranking-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            if not target.exists():
                os.rename(staging, target)
                return {"output_dir": str(target), "manifest": report}
        except Exception:
            pass
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _forward_input_readiness(
    *, classification: str, signal_day: date, now: datetime,
    bars_file: Path, config_file: Path, capture_file: Path | None,
    calendar_file: Path | None, universe_file: Path | None,
) -> dict[str, Any]:
    checks: dict[str, str] = {}
    reasons = []
    if not bars_file.is_file():
        checks["bars_file"] = "MISSING"
        reasons.append("行情文件缺失")
    else:
        checks["bars_file"] = "AVAILABLE"
    if not config_file.is_file():
        checks["config_file"] = "MISSING"
        reasons.append("策略配置文件缺失")
    else:
        checks["config_file"] = "AVAILABLE"
    if classification == "SYNTHETIC":
        return {"checks": checks, "blocking_reasons": reasons, "data_trust": "SYNTHETIC_ONLY"}
    for label, file, title in (
        ("trading_calendar", calendar_file, "交易日历"),
        ("point_in_time_universe", universe_file, "点时股票池"),
    ):
        if file is None:
            checks[label] = "NOT_PROVIDED"
            reasons.append(f"前瞻排名缺少{title}输入")
        elif not file.is_file():
            checks[label] = "MISSING"
            reasons.append(f"{title}文件不存在")
        else:
            checks[label] = "AVAILABLE"
    local_now = now.astimezone(SHANGHAI)
    if local_now.date() != signal_day:
        checks["signal_date_is_today"] = "FAILED"
        reasons.append("FORWARD_PAPER 的 signal_date 必须是本机当天")
    else:
        checks["signal_date_is_today"] = "VERIFIED_AT_RUNTIME_ONLY"
    if local_now.time() < time(15, 5):
        checks["after_daily_close"] = "NEEDS_DATA"
        reasons.append("尚未到收盘数据稳定等待时间 15:05 Asia/Shanghai")
    else:
        checks["after_daily_close"] = "VERIFIED_AT_RUNTIME_ONLY"
    if capture_file is None:
        checks["source_capture_manifest"] = "NOT_PROVIDED"
        reasons.append("缺少绑定行情文件与本机抓取时间的数据清单")
        return {"checks": checks, "blocking_reasons": reasons, "data_trust": "UNVERIFIED"}
    if not capture_file.is_file():
        checks["source_capture_manifest"] = "MISSING"
        reasons.append("行情抓取清单文件不存在")
        return {"checks": checks, "blocking_reasons": reasons, "data_trust": "UNVERIFIED"}
    try:
        capture = json.loads(capture_file.read_text(encoding="utf-8"))
        captured = _parse_aware(capture.get("captured_at_utc"), "captured_at_utc")
        recorded_hash = str(capture.get("data_file_sha256", "")).lower()
        if recorded_hash != _sha256(bars_file):
            checks["capture_manifest_data_hash"] = "FAILED"
            reasons.append("抓取清单中的行情文件 SHA-256 不匹配")
        else:
            checks["capture_manifest_data_hash"] = "VERIFIED"
        source_id = str(capture.get("source_id", "")).strip()
        if not source_id:
            checks["source_id"] = "MISSING"
            reasons.append("抓取清单缺少 source_id")
        else:
            checks["source_id"] = "AVAILABLE_BUT_UNVERIFIED"
        if captured.astimezone(SHANGHAI).date() != signal_day or captured.astimezone(SHANGHAI).time() < time(15, 0):
            checks["capture_after_close_same_day"] = "FAILED"
            reasons.append("行情抓取时间不在 signal_date 收盘后")
        elif captured > now:
            checks["capture_not_after_ranking_start"] = "FAILED"
            reasons.append("行情抓取时间晚于排名任务开始时点")
        else:
            checks["capture_after_close_same_day"] = "VERIFIED_AT_RUNTIME_ONLY"
            checks["capture_not_after_ranking_start"] = "VERIFIED_AT_RUNTIME_ONLY"
        if str(capture.get("trusted_external_timestamp", "UNVERIFIED")) != "UNVERIFIED":
            checks["external_timestamp_claim"] = "FAILED"
            reasons.append("本地未签名抓取清单不得声称可信外部时间已验证")
        else:
            checks["external_timestamp_claim"] = "UNVERIFIED"
        checks["provider_publication_time"] = "UNVERIFIED" if not capture.get("provider_published_at_utc") else "RECORDED_SOURCE_UNVERIFIED"
        return {
            "checks": checks,
            "blocking_reasons": reasons,
            "data_trust": "LOCAL_CAPTURE_TIME_BOUND_PROVIDER_PUBLICATION_UNVERIFIED",
            "source_data_capture_time_utc": _iso_utc(captured),
        }
    except Exception as exc:
        checks["source_capture_manifest"] = "FAILED"
        reasons.append(f"抓取清单无效: {exc}")
        return {"checks": checks, "blocking_reasons": reasons, "data_trust": "UNVERIFIED"}


def _parse_aware(value: Any, field: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field} 缺失")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _validate_signal_window_availability(
    bars: pd.DataFrame, *, signal_day: date, lookback_sessions: int, available_by: datetime,
) -> None:
    if "available_at" not in bars.columns:
        raise ValueError("行情缺少 available_at，不能确定用于排名的数据是否已到达")
    sessions = sorted(bars["trade_date"].astype(str).unique())
    if signal_day.isoformat() not in sessions:
        raise ValueError("信号日不在输入行情交易日内")
    end = sessions.index(signal_day.isoformat())
    required = sessions[max(0, end - lookback_sessions):end + 1]
    if len(required) != lookback_sessions + 1:
        raise ValueError("输入行情不足以满足冻结动量窗口")
    window = bars.loc[bars["trade_date"].isin(required)]
    for row in window.itertuples(index=False):
        raw = getattr(row, "available_at")
        if raw is None or pd.isna(raw) or not str(raw).strip():
            raise ValueError(f"{row.symbol} {row.trade_date} 缺少 available_at")
        available = _parse_aware(raw, f"{row.symbol} {row.trade_date} available_at")
        if available > available_by:
            raise ValueError(f"{row.symbol} {row.trade_date} 的 available_at 晚于排名运行开始时间")


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_utc 必须带时区")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None


def _git_dirty() -> bool | None:
    try:
        return bool(subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True).strip())
    except Exception:
        return None
