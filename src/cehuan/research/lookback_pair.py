from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.config import load_config
from cehuan.data.adjustment import ADJUSTED_SOURCE, signal_price_sources
from cehuan.data.canonical import canonical_symbol, read_bars
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.data.security_identity import normalize_security_identity_events
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


EXPERIMENT_ID = "lookback-10-vs-20-forward-20261217-v1"
FIXED_SYMBOLS = (
    "SH600000", "SH600004", "SH600006", "SH600007", "SH600008",
    "SZ000001", "SZ000002", "SZ000006", "SZ000007", "SZ000008",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
RANK_COLUMNS = ["signal_date", "symbol", "momentum", "rank", "in_top5"]
AUDIT_COLUMNS = [
    "symbol", "eligibility_status", "reasons", "expected_session_count",
    "actual_row_count", "missing_sessions", "duplicate_sessions",
    "unknown_status_sessions", "suspended_sessions", "price_basis_status",
    "identity_events",
]


class _BlockedInput(ValueError):
    pass


def create_lookback_pair_ranking(
    *,
    bars_path: str | Path,
    config_path: str | Path,
    calendar_path: str | Path,
    universe_path: str | Path,
    pool_path: str | Path,
    identity_events_path: str | Path,
    registration_freeze_path: str | Path,
    capture_manifest_path: str | Path,
    signal_date: str,
    output_dir: str | Path,
    mode: str = "FORWARD_PAPER",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Generate a paired 10/20-session ranking only; no orders or returns.

    ``SYNTHETIC`` mode is intended for offline tests. ``FORWARD_PAPER`` adds
    same-day and capture-time gates and must only be used with a fresh capture.
    """
    if mode not in {"SYNTHETIC", "FORWARD_PAPER"}:
        raise ValueError("mode 只能是 SYNTHETIC 或 FORWARD_PAPER")
    signal_day = date.fromisoformat(signal_date)
    paths = {
        "bars": Path(bars_path).expanduser().resolve(),
        "config": Path(config_path).expanduser().resolve(),
        "calendar": Path(calendar_path).expanduser().resolve(),
        "universe": Path(universe_path).expanduser().resolve(),
        "pool": Path(pool_path).expanduser().resolve(),
        "identity_events": Path(identity_events_path).expanduser().resolve(),
        "registration_freeze": Path(registration_freeze_path).expanduser().resolve(),
        "capture_manifest": Path(capture_manifest_path).expanduser().resolve(),
    }
    target = Path(output_dir).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"拒绝覆盖已有配对排名产物：{target}")

    started = _as_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    input_hashes = {key: _sha(path) if path.is_file() else None for key, path in paths.items()}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "LOOKBACK_PAIR_RANKING_ONLY",
        "experiment_id": EXPERIMENT_ID,
        "classification": "SYNTHETIC_ONLY" if mode == "SYNTHETIC" else "FORWARD_PAPER_CANDIDATE_ONLY",
        "status": "NEEDS_DATA",
        "signal_date": signal_day.isoformat(),
        "generated_at_utc": _iso_utc(started),
        "generated_at_asia_shanghai": started.astimezone(SHANGHAI).isoformat(),
        "research_pool_scope": "固定 10 只证券的流程试运行，不代表全市场",
        "fixed_symbols": list(FIXED_SYMBOLS),
        "input_paths": {key: str(value) for key, value in paths.items()},
        "input_sha256": input_hashes,
        "provider_published_at_utc": None,
        "provider_publication_evidence": "UNVERIFIED",
        "strategy_returns_computed": False,
        "forward_outcomes_computed": False,
        "orders_generated": False,
        "broker_connected": False,
        "active_strategy_modified": False,
        "formal_baseline": "NOT_READY",
        "blocking_reasons": [],
    }
    audit_rows: list[dict[str, Any]] = []
    try:
        missing_files = [key for key, path in paths.items() if not path.is_file()]
        if missing_files:
            raise _BlockedInput("必需输入文件缺失：" + ", ".join(missing_files))

        freeze = _read_registration_freeze(paths["registration_freeze"])
        _validate_frozen_sources(freeze, paths, input_hashes)
        manifest["registration_freeze_sha256"] = input_hashes["registration_freeze"]
        manifest["code_commit"] = _git_revision()
        manifest["worktree_dirty"] = _git_dirty()
        manifest["code_sha256"] = _code_sha256()

        config = load_config(paths["config"])
        baseline_config = deepcopy(config)
        candidate_config = deepcopy(config)
        if int(baseline_config.get("strategy", {}).get("lookback_sessions", -1)) != 20:
            raise _BlockedInput("冻结基线配置的 strategy.lookback_sessions 不等于 20")
        candidate_config["strategy"]["lookback_sessions"] = 10
        config_diff = _config_diff(baseline_config, candidate_config)
        if config_diff != [{"path": "strategy.lookback_sessions", "baseline": 20, "candidate": 10}]:
            raise _BlockedInput("候选配置不是仅将 lookback_sessions 从 20 改为 10")
        _write_json(baseline_config, staging / "baseline-config.json")
        _write_json(candidate_config, staging / "candidate-config.json")
        _write_json({"differences": config_diff}, staging / "config-difference.json")

        capture = _read_json(paths["capture_manifest"])
        capture_checks = _validate_capture_manifest(
            capture, signal_day=signal_day, paths=paths, hashes=input_hashes,
            mode=mode, now=started,
        )
        manifest["capture_checks"] = capture_checks
        manifest["provider"] = capture.get("provider")
        manifest["capture_evidence"] = {
            "captured_at_utc": capture.get("captured_at_utc"),
            "interfaces": capture.get("interfaces"),
            "signal_price_adjustment": capture.get("signal_price_adjustment"),
            "raw_price_adjustment": capture.get("raw_price_adjustment"),
        }
        manifest["provider_published_at_utc"] = capture.get("provider_published_at_utc")
        manifest["provider_publication_evidence"] = capture.get(
            "provider_publication_evidence", "UNVERIFIED",
        )

        bars = read_bars(paths["bars"])
        bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce").dt.date
        bars["symbol"] = bars["symbol"].map(canonical_symbol).astype(str)
        if (bars["symbol"].isin(FIXED_SYMBOLS) & bars["trade_date"].gt(signal_day)).any():
            raise _BlockedInput("固定研究池行情包含信号日之后的数据，拒绝前视输入")
        calendar = normalize_trading_calendar(pd.read_csv(paths["calendar"], dtype=str))
        if mode == "FORWARD_PAPER":
            calendar_sources = set(calendar["source"].astype(str).str.casefold())
            if len(calendar_sources) != 1 or not all("baostock" in source for source in calendar_sources):
                raise _BlockedInput("前瞻运行日历必须来自单一 BaoStock 来源")
        universe = pd.read_csv(paths["universe"], dtype=str, keep_default_na=False)
        identity = normalize_security_identity_events(
            pd.read_csv(paths["identity_events"], dtype=str, keep_default_na=False),
        )

        open_sessions = calendar.loc[calendar["is_open"].fillna(False), "trade_date"].tolist()
        if signal_day not in open_sessions:
            raise _BlockedInput(f"信号日 {signal_day} 不在日历开市日中")
        end = open_sessions.index(signal_day)
        required_sessions = open_sessions[max(0, end - 20):end + 1]
        if len(required_sessions) != 21:
            raise _BlockedInput(f"信号日前后窗口不足 21 个开市日，实际 {len(required_sessions)}")
        required_text = [item.isoformat() for item in required_sessions]
        manifest["signal_window"] = {
            "required_open_sessions": required_text,
            "required_open_session_count": 21,
            "lookbacks": [20, 10],
        }

        scoped_universe = _normalize_universe(universe, required_text)
        scoped_bars = bars.loc[
            bars["symbol"].isin(FIXED_SYMBOLS)
            & bars["trade_date"].isin(required_sessions),
        ].copy()
        _validate_bar_provenance(scoped_bars, signal_day=signal_day, mode=mode, now=started)
        scoped_identity = _identity_events_in_window(identity, set(FIXED_SYMBOLS), required_text)
        audit_rows, included = _audit_common_candidates(
            scoped_bars, scoped_universe, required_sessions, signal_day,
            identity_events=scoped_identity,
        )
        if scoped_identity:
            manifest["blocking_reasons"].extend(
                "固定池证券存在窗口内身份事件，当前不自动拼接代码："
                f"{item['event_id']} {item['old_symbol']}->{item['new_symbol']}@{item['effective_date']}"
                for item in scoped_identity
            )
        if any(row["eligibility_status"] == "BLOCKED" for row in audit_rows):
            manifest["blocking_reasons"].extend(
                f"{row['symbol']}: {row['reasons']}" for row in audit_rows
                if row["eligibility_status"] == "BLOCKED"
            )

        if manifest["blocking_reasons"]:
            manifest["status"] = "NEEDS_DATA"
            _write_csv(audit_rows, AUDIT_COLUMNS, staging / "per-security-audit.csv")
            manifest["audit_file"] = "per-security-audit.csv"
            manifest["audit_sha256"] = _sha(staging / "per-security-audit.csv")
        elif not included:
            manifest["status"] = "NO_COMMON_ELIGIBLE_CANDIDATES"
            manifest["result_type"] = "EMPTY_PAIRED_RANKING_ALL_EXCLUSIONS_DOCUMENTED"
            manifest["common_candidate_count"] = 0
            manifest["candidate_count_20d"] = 0
            manifest["candidate_count_10d"] = 0
            manifest["top5"] = {"20d": [], "10d": [], "overlap": [], "only_20d": [], "only_10d": []}
            _write_csv([], RANK_COLUMNS, staging / "ranking-20d.csv")
            _write_csv([], RANK_COLUMNS, staging / "ranking-10d.csv")
            _write_csv([], [
                "symbol", "momentum_20d", "candidate_eligible_20d", "rank_20d", "in_top5_20d",
                "momentum_10d", "candidate_eligible_10d", "rank_10d", "in_top5_10d",
                "rank_delta_10d_minus_20d", "top5_change",
            ], staging / "ranking-difference.csv")
            _write_csv(audit_rows, AUDIT_COLUMNS, staging / "per-security-audit.csv")
            _write_json({"differences": config_diff}, staging / "config-difference.json")
            manifest["output_files"] = {
                name: _sha(staging / name)
                for name in (
                    "ranking-20d.csv", "ranking-10d.csv", "ranking-difference.csv",
                    "per-security-audit.csv", "baseline-config.json", "candidate-config.json",
                    "config-difference.json",
                )
            }
        else:
            common_bars = scoped_bars.loc[scoped_bars["symbol"].isin(included)].copy()
            baseline_rank, baseline_scores = _rank(common_bars, signal_day, baseline_config)
            candidate_rank, candidate_scores = _rank(common_bars, signal_day, candidate_config)
            baseline_symbols = baseline_rank["symbol"].tolist()
            candidate_symbols = candidate_rank["symbol"].tolist()
            differences = _build_rank_difference(baseline_scores, candidate_scores)
            _write_csv(baseline_rank.to_dict(orient="records"), RANK_COLUMNS, staging / "ranking-20d.csv")
            _write_csv(candidate_rank.to_dict(orient="records"), RANK_COLUMNS, staging / "ranking-10d.csv")
            _write_csv(differences, [
                "symbol", "momentum_20d", "candidate_eligible_20d", "rank_20d", "in_top5_20d",
                "momentum_10d", "candidate_eligible_10d", "rank_10d", "in_top5_10d",
                "rank_delta_10d_minus_20d", "top5_change",
            ], staging / "ranking-difference.csv")
            _write_csv(audit_rows, AUDIT_COLUMNS, staging / "per-security-audit.csv")
            _write_json({"differences": config_diff}, staging / "config-difference.json")
            manifest["status"] = "CAPTURED_SYNTHETIC_RANKING_ONLY" if mode == "SYNTHETIC" else "CAPTURED_FORWARD_RANKING_ONLY"
            manifest["result_type"] = "PAIRED_CANDIDATE_RANKING_ONLY_NO_RETURNS_NO_ORDERS"
            manifest["common_candidate_count"] = len(included)
            manifest["candidate_count_20d"] = len(baseline_rank)
            manifest["candidate_count_10d"] = len(candidate_rank)
            manifest["top5"] = {
                "20d": baseline_symbols[:5],
                "10d": candidate_symbols[:5],
                "overlap": sorted(set(baseline_symbols[:5]) & set(candidate_symbols[:5])),
                "only_20d": [symbol for symbol in baseline_symbols[:5] if symbol not in set(candidate_symbols[:5])],
                "only_10d": [symbol for symbol in candidate_symbols[:5] if symbol not in set(baseline_symbols[:5])],
            }
            manifest["output_files"] = {
                name: _sha(staging / name)
                for name in (
                    "ranking-20d.csv", "ranking-10d.csv", "ranking-difference.csv",
                    "per-security-audit.csv", "baseline-config.json", "candidate-config.json",
                    "config-difference.json",
                )
            }

        _validate_inputs_unchanged(paths, input_hashes)
        manifest["finished_at_utc"] = _iso_utc(datetime.now(timezone.utc))
        _write_json(manifest, staging / "manifest.json")
        (staging / "manifest.sha256").write_text(
            f"{_sha(staging / 'manifest.json')}  manifest.json\n", encoding="ascii",
        )
        if target.exists():
            raise FileExistsError(f"发布前目标已出现，拒绝覆盖：{target}")
        os.rename(staging, target)
        return {"output_dir": str(target), "manifest": manifest}
    except (ValueError, OSError, KeyError, TypeError) as exc:
        manifest["status"] = "NEEDS_DATA"
        manifest["blocking_reasons"].append(str(exc))
        manifest["finished_at_utc"] = _iso_utc(datetime.now(timezone.utc))
        _write_csv(audit_rows, AUDIT_COLUMNS, staging / "per-security-audit.csv")
        manifest["audit_file"] = "per-security-audit.csv"
        manifest["audit_sha256"] = _sha(staging / "per-security-audit.csv")
        for name in ("ranking-20d.csv", "ranking-10d.csv", "ranking-difference.csv"):
            (staging / name).unlink(missing_ok=True)
        for key in (
            "output_files", "top5", "common_candidate_count", "candidate_count_20d",
            "candidate_count_10d", "result_type",
        ):
            manifest.pop(key, None)
        _write_json(manifest, staging / "manifest.json")
        (staging / "manifest.sha256").write_text(
            f"{_sha(staging / 'manifest.json')}  manifest.json\n", encoding="ascii",
        )
        if target.exists():
            shutil.rmtree(staging, ignore_errors=True)
            raise FileExistsError(f"发布前目标已出现，拒绝覆盖：{target}")
        os.rename(staging, target)
        return {"output_dir": str(target), "manifest": manifest}
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _read_registration_freeze(path: Path) -> dict[str, Any]:
    freeze = _read_json(path)
    seal = path.with_suffix(".sha256")
    if not seal.is_file():
        raise _BlockedInput("登记冻结文件缺少 .sha256 封条")
    expected = seal.read_text(encoding="ascii").split()[0].lower()
    if not _valid_sha256(expected) or expected != _sha(path):
        raise _BlockedInput("登记冻结文件 SHA-256 封条不匹配")
    if freeze.get("experiment_id") != EXPERIMENT_ID:
        raise _BlockedInput("登记文件 experiment_id 与本入口不匹配")
    if freeze.get("signal_observation", {}).get("fixed_signal_date") != "2026-12-17":
        raise _BlockedInput("登记文件未冻结 2026-12-17 为本实验信号日")
    symbols = freeze.get("research_pool", {}).get("symbols")
    if tuple(symbols or ()) != FIXED_SYMBOLS:
        raise _BlockedInput("登记冻结文件中的固定研究池与本入口不一致")
    return freeze


def _validate_frozen_sources(freeze: dict[str, Any], paths: dict[str, Path], hashes: dict[str, str | None]) -> None:
    frozen = freeze.get("frozen_input_files", {})
    for freeze_key, runtime_key in (("strategy_config", "config"), ("pool", "pool")):
        item = frozen.get(freeze_key)
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            raise _BlockedInput(f"登记冻结文件缺少 {freeze_key} 路径或 SHA-256")
        frozen_path = Path(item["path"])
        if not frozen_path.is_absolute():
            frozen_path = Path(__file__).resolve().parents[3] / frozen_path
        if runtime_key == "config":
            if frozen_path.resolve() != paths["config"] or item["sha256"] != hashes["config"]:
                raise _BlockedInput("实际策略配置路径或哈希与登记冻结值不匹配")
        else:
            if frozen_path.resolve() != paths["pool"] or not frozen_path.is_file() or _sha(frozen_path) != item["sha256"]:
                raise _BlockedInput("固定研究池源文件与登记哈希不匹配")
            pool = pd.read_csv(frozen_path, dtype=str, keep_default_na=False)
            if "symbol" not in pool or tuple(pool["symbol"].map(canonical_symbol)) != FIXED_SYMBOLS:
                raise _BlockedInput("固定研究池源文件证券或顺序与登记名单不一致")
    doc_item = frozen.get("registration_document")
    if isinstance(doc_item, dict) and doc_item.get("path") and doc_item.get("sha256"):
        document = Path(doc_item["path"])
        if not document.is_absolute():
            document = Path(__file__).resolve().parents[3] / document
        if not document.is_file() or _sha(document) != doc_item["sha256"]:
            raise _BlockedInput("实验登记文档与冻结 SHA-256 不匹配")
    strategy_item = frozen.get("strategy_implementation")
    if isinstance(strategy_item, dict) and strategy_item.get("path") and strategy_item.get("sha256"):
        strategy_file = Path(strategy_item["path"])
        if not strategy_file.is_absolute():
            strategy_file = Path(__file__).resolve().parents[3] / strategy_file
        if not strategy_file.is_file() or _sha(strategy_file) != strategy_item["sha256"]:
            raise _BlockedInput("动量计算实现与登记时冻结的 SHA-256 不一致")
    calendar_item = frozen.get("calendar")
    if isinstance(calendar_item, dict) and calendar_item.get("path"):
        frozen_calendar = Path(calendar_item["path"])
        if not frozen_calendar.is_absolute():
            frozen_calendar = Path(__file__).resolve().parents[3] / frozen_calendar
        if not frozen_calendar.is_file() or _sha(frozen_calendar) != calendar_item.get("sha256"):
            raise _BlockedInput("登记引用日历缺失或哈希不匹配")
        old = normalize_trading_calendar(pd.read_csv(frozen_calendar, dtype=str))
        new = normalize_trading_calendar(pd.read_csv(paths["calendar"], dtype=str))
        joined = old[["trade_date", "is_open"]].merge(
            new[["trade_date", "is_open"]], on="trade_date", how="left", suffixes=("_frozen", "_new"),
        )
        if joined["is_open_new"].isna().any() or not joined["is_open_frozen"].eq(joined["is_open_new"]).all():
            raise _BlockedInput("运行日历缺少登记日历前缀或改写了已冻结的开市状态")


def _validate_capture_manifest(
    capture: dict[str, Any], *, signal_day: date, paths: dict[str, Path],
    hashes: dict[str, str | None], mode: str, now: datetime,
) -> dict[str, str]:
    if capture.get("experiment_id") != EXPERIMENT_ID:
        raise _BlockedInput("采集清单 experiment_id 与本入口不匹配")
    if capture.get("signal_date") != signal_day.isoformat():
        raise _BlockedInput("采集清单 signal_date 与请求日期不匹配")
    bound = capture.get("input_sha256")
    if not isinstance(bound, dict):
        raise _BlockedInput("采集清单缺少 input_sha256 绑定")
    required = ("bars", "calendar", "universe", "pool", "identity_events", "registration_freeze")
    checks: dict[str, str] = {}
    for key in required:
        if bound.get(key) != hashes.get(key):
            raise _BlockedInput(f"采集清单 {key} SHA-256 与实际输入不一致")
        checks[key] = "VERIFIED"
    provider = str(capture.get("provider", "")).strip()
    if mode == "FORWARD_PAPER":
        if "baostock" not in provider.casefold():
            raise _BlockedInput("前瞻采集清单 provider 必须为 BaoStock")
        expected_interfaces = {
            "bars": "query_history_k_data_plus",
            "calendar": "query_trade_dates",
            "universe": "query_all_stock",
        }
        if capture.get("interfaces") != expected_interfaces:
            raise _BlockedInput("前瞻采集清单接口必须与登记的 BaoStock 接口一致")
        if capture.get("signal_price_adjustment") != "adjustflag=1":
            raise _BlockedInput("前瞻采集清单必须确认信号价格来自 adjustflag=1")
        if capture.get("raw_price_adjustment") != "adjustflag=3":
            raise _BlockedInput("前瞻采集清单必须确认原始价格来自 adjustflag=3")
        captured = _parse_aware(capture.get("captured_at_utc"), "captured_at_utc")
        local_now = now.astimezone(SHANGHAI)
        if local_now.date() != signal_day or local_now.time() < time(15, 5):
            raise _BlockedInput("前瞻排名只允许在信号日当天 15:05 后运行")
        if captured.astimezone(SHANGHAI).date() != signal_day or captured.astimezone(SHANGHAI).time() < time(15, 5):
            raise _BlockedInput("抓取时间不在信号日 15:05 后")
        if captured > now:
            raise _BlockedInput("采集时间晚于排名运行时点")
        if capture.get("trusted_external_timestamp", "UNVERIFIED") != "UNVERIFIED":
            raise _BlockedInput("本地采集清单不能声称可信外部时间已验证")
        checks["capture_time"] = "LOCAL_TIME_VERIFIED_PROVIDER_PUBLICATION_UNVERIFIED"
    else:
        checks["capture_time"] = "SYNTHETIC_FIXTURE"
    if capture.get("provider_published_at_utc") in (None, ""):
        checks["provider_publication"] = "UNVERIFIED"
    else:
        checks["provider_publication"] = "RECORDED_NOT_INDEPENDENTLY_VERIFIED"
    return checks


def _validate_bar_provenance(
    bars: pd.DataFrame, *, signal_day: date, mode: str, now: datetime,
) -> None:
    if bars.empty:
        return
    if "data_source" not in bars or bars["data_source"].isna().any() or bars["data_source"].astype(str).str.strip().eq("").any():
        raise _BlockedInput("规范行情缺少 data_source 来源字段")
    if bars["data_source"].astype(str).nunique() != 1:
        raise _BlockedInput("21 日窗口行情混用了多个 data_source")
    if mode == "FORWARD_PAPER" and not bars["data_source"].astype(str).str.casefold().str.contains("baostock").all():
        raise _BlockedInput("前瞻运行行情必须来自 BaoStock")
    if "retrieved_at_utc" not in bars:
        raise _BlockedInput("规范行情缺少本机 retrieved_at_utc 抓取时点")
    retrieved = pd.to_datetime(bars["retrieved_at_utc"], errors="coerce", utc=True)
    if retrieved.isna().any():
        raise _BlockedInput("规范行情 retrieved_at_utc 缺失或无效")
    if mode == "FORWARD_PAPER":
        lower = datetime.combine(signal_day, time(15, 5), tzinfo=SHANGHAI).astimezone(timezone.utc)
        if (retrieved < lower).any() or (retrieved > now).any():
            raise _BlockedInput("规范行情本机抓取时点不在信号日 15:05 至运行时点之间")


def _normalize_universe(frame: pd.DataFrame, required_dates: list[str]) -> pd.DataFrame:
    required = {"trade_date", "symbol", "pool_status"}
    if not required.issubset(frame.columns):
        raise _BlockedInput("点时成员文件缺少 trade_date、symbol、pool_status")
    data = frame.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    data["symbol"] = data["symbol"].map(canonical_symbol).astype(str)
    data["pool_status"] = data["pool_status"].astype(str).str.strip().str.upper()
    future = data.loc[data["symbol"].isin(FIXED_SYMBOLS) & data["trade_date"].gt(required_dates[-1])]
    if not future.empty:
        raise _BlockedInput("点时成员文件包含信号日之后的固定池状态")
    data = data.loc[
        data["trade_date"].isin(required_dates) & data["symbol"].isin(FIXED_SYMBOLS),
    ].copy()
    if data.duplicated(["trade_date", "symbol"]).any():
        raise _BlockedInput("点时成员文件在 21 日窗口内存在重复证券日")
    return data


def _identity_events_in_window(frame: pd.DataFrame, symbols: set[str], dates: list[str]) -> list[dict[str, str]]:
    if frame.empty:
        return []
    start, end = dates[0], dates[-1]
    relevant = frame.loc[
        frame["effective_date"].astype(str).between(start, end)
        & (frame["old_symbol"].astype(str).isin(symbols) | frame["new_symbol"].astype(str).isin(symbols)),
    ]
    return [
        {
            "event_id": str(row.event_id),
            "old_symbol": str(row.old_symbol),
            "new_symbol": str(row.new_symbol),
            "effective_date": str(row.effective_date),
        }
        for row in relevant.itertuples(index=False)
    ]


def _audit_common_candidates(
    bars: pd.DataFrame, universe: pd.DataFrame, required_sessions: list[date], signal_day: date,
    *, identity_events: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[str]]:
    required_text = [item.isoformat() for item in required_sessions]
    universe_idx = universe.set_index(["symbol", "trade_date"], drop=False) if not universe.empty else None
    if universe_idx is not None and universe_idx.index.duplicated().any():
        raise _BlockedInput("点时成员文件证券日不唯一")
    date_text = bars["trade_date"].map(lambda value: value.isoformat() if not pd.isna(value) else "")
    scoped = bars.loc[date_text.isin(required_text)].copy()
    dup_mask = scoped.duplicated(["symbol", "trade_date"], keep=False)
    dup_keys: dict[str, list[str]] = {}
    for row in scoped.loc[dup_mask, ["symbol", "trade_date"]].itertuples(index=False):
        dup_keys.setdefault(str(row.symbol), []).append(row.trade_date.isoformat())
    records: list[dict[str, Any]] = []
    included: list[str] = []
    for symbol in FIXED_SYMBOLS:
        reasons: list[str] = []
        fatal_reasons: list[str] = []
        unknown_membership: list[str] = []
        membership_values: dict[str, str] = {}
        if universe_idx is None:
            unknown_membership = required_text
        else:
            for day in required_text:
                key = (symbol, day)
                if key not in universe_idx.index:
                    unknown_membership.append(day)
                    continue
                status = str(universe_idx.loc[key, "pool_status"]).strip().upper()
                if status not in {"STOCK_MEMBER", "NOT_STOCK_MEMBER", "UNKNOWN"}:
                    unknown_membership.append(day)
                else:
                    membership_values[day] = status
                    if status == "UNKNOWN":
                        unknown_membership.append(day)
        if unknown_membership:
            message = "成员状态未知或缺失:" + ";".join(sorted(set(unknown_membership)))
            reasons.append(message)
            fatal_reasons.append(message)
        rows = scoped.loc[scoped["symbol"].astype(str).eq(symbol)].copy()
        rows["trade_date_text"] = rows["trade_date"].map(lambda value: value.isoformat())
        counts = rows["trade_date_text"].value_counts().to_dict()
        member_dates = [day for day in required_text if membership_values.get(day) == "STOCK_MEMBER"]
        missing = [day for day in member_dates if counts.get(day, 0) == 0]
        duplicates = sorted(set(dup_keys.get(symbol, [])))
        if duplicates:
            message = "重复行情日:" + ";".join(duplicates)
            reasons.append(message)
            fatal_reasons.append(message)
        member_states = [membership_values.get(day) for day in required_text]
        known_nonmember = any(value == "NOT_STOCK_MEMBER" for value in member_states)
        if known_nonmember:
            reasons.append("21 日共同窗口内存在已确认的非成员日期")
        unknown_status_dates: list[str] = []
        suspended_dates: list[str] = []
        price_basis = "ADJUSTED_CLOSE"
        member_rows = rows.loc[rows["trade_date_text"].isin(member_dates)].copy()
        if missing:
            message = "成员日缺少行情:" + ";".join(missing)
            reasons.append(message)
            fatal_reasons.append(message)
            unknown_status_dates.extend(missing)
        for field in ("is_listed", "is_suspended", "is_st"):
            if field not in member_rows:
                unknown_status_dates.extend(member_dates)
                continue
            unknown_status_dates.extend(member_rows.loc[member_rows[field].isna(), "trade_date_text"].tolist())
        if unknown_status_dates:
            message = "必需成员/上市/ST/停牌状态未知:" + ";".join(sorted(set(unknown_status_dates)))
            reasons.append(message)
            fatal_reasons.append(message)
        if "is_suspended" in member_rows:
            suspended_dates = sorted(member_rows.loc[
                member_rows["is_suspended"].fillna(False).astype(bool), "trade_date_text",
            ].unique().tolist())
        if "is_listed" in member_rows:
            not_listed_dates = sorted(member_rows.loc[
                member_rows["is_listed"].fillna(False).eq(False), "trade_date_text",
            ].unique().tolist())
        else:
            not_listed_dates = []
        if not_listed_dates:
            message = "点时成员状态与上市状态冲突:" + ";".join(not_listed_dates)
            reasons.append(message)
            fatal_reasons.append(message)
        signal_rows = member_rows.loc[member_rows["trade_date_text"].eq(signal_day.isoformat())]
        signal_not_member = membership_values.get(signal_day.isoformat()) == "NOT_STOCK_MEMBER"
        signal_not_listed = False
        signal_is_st = False
        signal_suspended = False
        if not signal_rows.empty:
            signal = signal_rows.iloc[0]
            signal_not_listed = not pd.isna(signal.get("is_listed")) and not bool(signal.get("is_listed"))
            signal_is_st = not pd.isna(signal.get("is_st")) and bool(signal.get("is_st"))
            signal_suspended = not pd.isna(signal.get("is_suspended")) and bool(signal.get("is_suspended"))
        if signal_not_member:
            reasons.append("信号日已确认非 STOCK_MEMBER")
        if signal_not_listed:
            message = "信号日点时成员状态与上市状态冲突"
            reasons.append(message)
            fatal_reasons.append(message)
        if signal_is_st:
            reasons.append("信号日为 ST")
        if signal_suspended:
            reasons.append("信号日停牌")
        if suspended_dates:
            reasons.append("共同窗口内已确认停牌:" + ";".join(suspended_dates))
        if not member_rows.empty:
            price_columns = ["open", "high", "low", "close", "adjusted_close"]
            numeric = member_rows[price_columns].apply(pd.to_numeric, errors="coerce")
            finite_positive = numeric.map(
                lambda value: math.isfinite(float(value)) and float(value) > 0 if pd.notna(value) else False,
            )
            bad_price_rows = ~finite_positive.all(axis=1)
            bad_ohlc = (
                numeric["low"].gt(numeric["high"])
                | numeric["low"].gt(numeric[["open", "close"]].min(axis=1))
                | numeric["high"].lt(numeric[["open", "close"]].max(axis=1))
            )
            if bad_price_rows.any() or bad_ohlc.any():
                message = "窗口含无效或不一致价格"
                reasons.append(message)
                fatal_reasons.append(message)
            sources = signal_price_sources(member_rows)
            if not sources.eq(ADJUSTED_SOURCE).all():
                price_basis = "INVALID_OR_MIXED"
                message = "信号价格不是完整一致的 adjusted_close"
                reasons.append(message)
                fatal_reasons.append(message)
        if identity_events:
            relevant_events = [
                item for item in identity_events
                if symbol in {item["old_symbol"], item["new_symbol"]}
            ]
            if relevant_events:
                message = "窗口内证券身份事件未由本入口自动迁移"
                reasons.append(message)
                fatal_reasons.append(message)
        if fatal_reasons:
            status = "BLOCKED"
        elif known_nonmember or signal_not_member:
            status = "EXCLUDED_KNOWN_NONMEMBER_IN_COMMON_WINDOW"
        elif signal_is_st:
            status = "EXCLUDED_SIGNAL_DATE_ST"
        elif suspended_dates:
            status = "EXCLUDED_KNOWN_SUSPENSION_IN_COMMON_WINDOW"
        else:
            status = "ELIGIBLE_COMMON"
            included.append(symbol)
        records.append({
            "symbol": symbol,
            "eligibility_status": status,
            "reasons": " | ".join(dict.fromkeys(reasons)),
            "expected_session_count": len(required_text),
            "actual_row_count": len(set(counts) & set(member_dates)),
            "missing_sessions": ";".join(missing),
            "duplicate_sessions": ";".join(duplicates),
            "unknown_status_sessions": ";".join(sorted(set(unknown_status_dates))),
            "suspended_sessions": ";".join(suspended_dates),
            "price_basis_status": price_basis,
            "identity_events": json.dumps(
                [item for item in identity_events if symbol in {item["old_symbol"], item["new_symbol"]}],
                ensure_ascii=False,
                sort_keys=True,
            ),
        })
    return records, included


def _rank(bars: pd.DataFrame, signal_day: date, config: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    feature = add_momentum(
        bars.drop(columns=[name for name in ("available_at_utc", "retrieved_at_utc_parsed") if name in bars]),
        int(config["strategy"]["lookback_sessions"]), normalized=True,
    )
    day = feature.loc[feature["trade_date"].eq(signal_day)].copy()
    if day.empty or day["momentum"].isna().any() or not pd.to_numeric(day["momentum"], errors="coerce").map(math.isfinite).all():
        raise _BlockedInput(f"{config['strategy']['lookback_sessions']} 日动量窗口未产生有限信号")
    score_frame = day[["symbol", "momentum"]].copy()
    ranked = rank_precomputed_day(
        day, config["strategy"], config["market"], require_order_rules=False,
    )
    score_frame["in_candidate_list"] = score_frame["momentum"].ge(float(config["strategy"].get("min_momentum", 0.0)))
    rank_by_symbol = ranked.set_index("symbol")["rank"] if not ranked.empty else pd.Series(dtype="Int64")
    score_frame["rank"] = score_frame["symbol"].map(rank_by_symbol).astype("Int64")
    score_frame["in_top5"] = score_frame["rank"].le(5).fillna(False)
    if not ranked.empty:
        ranked = ranked.copy()
        ranked["signal_date"] = signal_day.isoformat()
        ranked["in_top5"] = ranked["rank"].le(5)
        ranked = ranked[RANK_COLUMNS]
    else:
        ranked = pd.DataFrame(columns=RANK_COLUMNS)
    return ranked, score_frame


def _build_rank_difference(baseline: pd.DataFrame, candidate: pd.DataFrame) -> list[dict[str, Any]]:
    left = baseline.rename(columns={
        "momentum": "momentum_20d", "rank": "rank_20d",
        "in_candidate_list": "candidate_eligible_20d", "in_top5": "in_top5_20d",
    })
    right = candidate.rename(columns={
        "momentum": "momentum_10d", "rank": "rank_10d",
        "in_candidate_list": "candidate_eligible_10d", "in_top5": "in_top5_10d",
    })
    joined = left.merge(right, on="symbol", how="outer", validate="one_to_one")
    joined["rank_delta_10d_minus_20d"] = joined["rank_10d"] - joined["rank_20d"]
    for column in ("candidate_eligible_20d", "candidate_eligible_10d"):
        joined[column] = joined[column].fillna(False).astype(bool)
    joined["in_top5_20d"] = joined["in_top5_20d"].fillna(False).astype(bool)
    joined["in_top5_10d"] = joined["in_top5_10d"].fillna(False).astype(bool)
    joined["top5_change"] = "UNCHANGED"
    joined.loc[joined["in_top5_20d"] & ~joined["in_top5_10d"], "top5_change"] = "LEFT_20D_TOP5"
    joined.loc[~joined["in_top5_20d"] & joined["in_top5_10d"], "top5_change"] = "ENTERED_10D_TOP5"
    joined = joined.sort_values("symbol")
    return joined[[
        "symbol", "momentum_20d", "candidate_eligible_20d", "rank_20d", "in_top5_20d",
        "momentum_10d", "candidate_eligible_10d", "rank_10d", "in_top5_10d",
        "rank_delta_10d_minus_20d", "top5_change",
    ]].to_dict(orient="records")


def _config_diff(left: dict[str, Any], right: dict[str, Any]) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    def visit(a: Any, b: Any, prefix: str = "") -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                visit(a.get(key), b.get(key), f"{prefix}.{key}" if prefix else key)
        elif a != b:
            differences.append({"path": prefix, "baseline": a, "candidate": b})
    visit(left, right)
    return differences


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _BlockedInput(f"JSON 输入无效：{path.name} ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise _BlockedInput(f"JSON 输入顶层必须是对象：{path.name}")
    return value


def _validate_inputs_unchanged(paths: dict[str, Path], initial: dict[str, str | None]) -> None:
    current = {key: _sha(path) if path.is_file() else None for key, path in paths.items()}
    changed = [key for key in paths if current[key] != initial[key]]
    if changed:
        raise _BlockedInput("运行期间输入文件哈希发生变化：" + ", ".join(changed))


def _write_csv(rows: list[dict[str, Any]], columns: list[str], path: Path) -> None:
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, lineterminator="\n")


def _write_json(value: object, path: Path) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _parse_aware(value: Any, field: str) -> datetime:
    if value is None or not str(value).strip():
        raise _BlockedInput(f"采集清单缺少 {field}")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise _BlockedInput(f"采集清单 {field} 无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _BlockedInput(f"采集清单 {field} 必须带时区")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now_utc 必须带时区")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _code_sha256() -> dict[str, str]:
    repo = Path(__file__).resolve().parents[3]
    sources = (
        "src/cehuan/research/lookback_pair.py",
        "src/cehuan/cli/main.py",
        "src/cehuan/signals/strategy.py",
        "src/cehuan/data/canonical.py",
        "src/cehuan/data/calendar.py",
        "src/cehuan/data/adjustment.py",
        "src/cehuan/data/security_identity.py",
    )
    return {name: _sha(repo / name) for name in sources}
