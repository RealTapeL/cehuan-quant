"""Build a small, hash-bound research summary from existing local A-share audits.

Only whitelisted aggregate facts and a few reconstructed ranking observations are
returned. Raw market bars and broker/account data are never put in the LLM input.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BARS = Path("data/private/snapshots/bs-bars-universe-20241008-20250110-v1/bars.csv")
DEFAULT_SNAPSHOT_MANIFEST = Path("data/private/snapshots/bs-bars-universe-20241008-20250110-v1/manifest.json")
DEFAULT_QUALITY = Path("data/private/snapshots/bs-bars-universe-20241008-20250110-v1/quality.json")
DEFAULT_UNIVERSE_AUDIT = Path("outputs/universe-audit-stream-20241008-20250110-v1/audit.json")
DEFAULT_POINT_IN_TIME_QUALITY = Path("outputs/universe-audit-stream-20241008-20250110-v1/point_in_time_quality.json")
DEFAULT_UNIVERSE = Path("data/private/inputs/baostock-universe-20241008-20250110.csv")
DEFAULT_CONFIG = Path(
    "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/"
    "run-committed-b60a7f0/config.json"
)
DEFAULT_TRADE_AUDIT = Path(
    "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/"
    "trade-audit-state-and-trace-v2-1435ac5-20261104/trade-audit-summary.json"
)
DEFAULT_RANKS = Path(
    "data/private/experiments/ckpt-v2-real-short-20241008-20250110-20261104/"
    "trade-audit-state-and-trace-v2-1435ac5-20261104/candidate-ranks-reconstructed.csv"
)

REQUIRED_BAR_COLUMNS = {
    "trade_date", "symbol", "open", "high", "low", "close", "adjusted_close",
    "volume_shares", "amount_cny", "is_listed", "is_suspended", "is_st",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares", "board", "market_rule_source",
    "market_rule_valid_from", "market_rule_valid_to", "data_source", "retrieved_at_utc",
    "available_at", "name",
}
REQUIRED_RANK_COLUMNS = {"signal_date", "symbol", "rank", "momentum", "is_suspended", "is_st", "name"}
MARKET_RULE_COLUMNS = (
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares", "board", "market_rule_source",
    "market_rule_valid_from", "market_rule_valid_to",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    return (candidate if candidate.is_absolute() else REPO_ROOT / candidate).resolve()


def _relative(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        raise ValueError("真实研究来源文件必须位于策环项目目录内") from None


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"研究来源 JSON 顶层必须是对象：{_relative(path)}")
    return value


def _record_source(sources: list[dict[str, Any]], source_id: str, path: Path, role: str, status: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"真实研究所需输入不存在：{_relative(path)}")
    digest = sha256_file(path)
    sources.append({
        "source_id": source_id,
        "path": _relative(path),
        "sha256": digest,
        "role": role,
        "provenance_status": status,
    })
    return digest


def _scan_bars(path: Path) -> dict[str, Any]:
    columns = list(REQUIRED_BAR_COLUMNS)
    symbol_dates: set[tuple[str, str]] = set()
    dates: set[str] = set()
    symbols: set[str] = set()
    data_sources: set[str] = set()
    retrieved: set[str] = set()
    available: set[str] = set()
    missing: dict[str, int] = {column: 0 for column in columns}
    suspension_rows = st_rows = name_st_flag_conflict_rows = 0
    invalid_ohlc_rows = zero_volume_rows = duplicate_rows = rows = 0
    missing_rules = {column: 0 for column in MARKET_RULE_COLUMNS}
    focused_symbol_rows: list[pd.DataFrame] = []
    for chunk in pd.read_csv(path, usecols=lambda column: column in columns, chunksize=100_000, low_memory=False):
        absent = set(columns) - set(chunk.columns)
        if absent:
            raise ValueError("规范行情缺少必需列：" + ", ".join(sorted(absent)))
        rows += len(chunk)
        for column in columns:
            missing[column] += int(chunk[column].isna().sum())
        symbol = chunk["symbol"].astype("string")
        trade_date = chunk["trade_date"].astype("string")
        symbols.update(symbol.dropna().astype(str).unique())
        dates.update(trade_date.dropna().astype(str).unique())
        for key in zip(symbol.fillna("").astype(str), trade_date.fillna("").astype(str)):
            if key in symbol_dates:
                duplicate_rows += 1
            else:
                symbol_dates.add(key)
        data_sources.update(chunk["data_source"].dropna().astype(str).unique())
        retrieved.update(chunk["retrieved_at_utc"].dropna().astype(str).unique())
        available.update(chunk["available_at"].dropna().astype(str).unique())
        suspension_rows += int(chunk["is_suspended"].fillna(False).astype(bool).sum())
        st_rows += int(chunk["is_st"].fillna(False).astype(bool).sum())
        name_st = chunk["name"].astype("string").str.startswith("*ST", na=False)
        st_false = chunk["is_st"].notna() & ~chunk["is_st"].fillna(False).astype(bool)
        name_st_flag_conflict_rows += int((name_st & st_false).sum())
        numeric = chunk[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
        invalid_ohlc_rows += int(
            (numeric.isna().any(axis=1) | numeric.le(0).any(axis=1)
             | (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1))
             | (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1))).sum()
        )
        zero_volume_rows += int(pd.to_numeric(chunk["volume_shares"], errors="coerce").eq(0).sum())
        for column in MARKET_RULE_COLUMNS:
            missing_rules[column] += int(chunk[column].isna().sum())
        focused = chunk.loc[chunk["symbol"].astype(str).eq("SH603268"), [
            "trade_date", "symbol", "adjusted_close", "is_suspended", "is_st",
        ]]
        if not focused.empty:
            focused_symbol_rows.append(focused.copy())
    if rows <= 0:
        raise ValueError("真实行情文件为空")
    focused = pd.concat(focused_symbol_rows, ignore_index=True) if focused_symbol_rows else pd.DataFrame()
    if not focused.empty:
        focused["trade_date"] = pd.to_datetime(focused["trade_date"], errors="coerce")
        focused = focused.loc[focused["trade_date"].le(pd.Timestamp("2024-11-05"))].sort_values("trade_date").tail(21)
    focus_window = None
    if len(focused):
        start_price = float(focused.iloc[0]["adjusted_close"])
        end_price = float(focused.iloc[-1]["adjusted_close"])
        focus_window = {
            "symbol": "SH603268",
            "signal_date": "2024-11-05",
            "expected_session_rows": 21,
            "actual_rows": int(len(focused)),
            "suspended_rows": int(focused["is_suspended"].fillna(False).astype(bool).sum()),
            "unknown_suspension_rows": int(focused["is_suspended"].isna().sum()),
            "adjusted_close_start": start_price,
            "adjusted_close_end": end_price,
            "reconstructed_20_session_momentum": end_price / start_price - 1.0 if start_price else None,
            "interpretation": "重建观察；不证明信号时点可得，也不证明价格调整或停牌规则正确。",
        }
    return {
        "row_count": rows,
        "unique_symbol_date_rows": len(symbol_dates),
        "duplicate_symbol_date_rows": duplicate_rows,
        "date_count": len(dates),
        "date_start": min(dates) if dates else None,
        "date_end": max(dates) if dates else None,
        "symbol_count": len(symbols),
        "symbols": sorted(symbols),
        "exchange_prefixes": sorted({symbol[:2] for symbol in symbols}),
        "missing_values": missing,
        "is_suspended_true_rows": suspension_rows,
        "is_st_true_rows": st_rows,
        "display_name_st_prefix_but_is_st_false_rows": name_st_flag_conflict_rows,
        "display_name_status_interpretation": "UNVERIFIED_NAME_TEMPORAL_PROVENANCE; this is a consistency flag, not a historical ST determination.",
        "invalid_ohlc_rows": invalid_ohlc_rows,
        "zero_volume_rows": zero_volume_rows,
        "market_rule_missing_rows_by_field": missing_rules,
        "data_sources": sorted(data_sources),
        "retrieved_at_utc_min": min(retrieved) if retrieved else None,
        "retrieved_at_utc_max": max(retrieved) if retrieved else None,
        "available_at_distinct_values": len(available),
        "available_at_sample": sorted(available)[:2],
        "available_at_interpretation": "ADAPTER_ASSIGNED_CLOSE_CUTOFF_NOT_VENDOR_PUBLICATION_TIMESTAMP",
        "focus_suspension_candidate_window": focus_window,
    }


def _rank_summary(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ranks = pd.read_csv(path, low_memory=False)
    missing = REQUIRED_RANK_COLUMNS - set(ranks.columns)
    if missing:
        raise ValueError("重建候选排名文件缺少列：" + ", ".join(sorted(missing)))
    ranks["rank"] = pd.to_numeric(ranks["rank"], errors="coerce")
    ranks["momentum"] = pd.to_numeric(ranks["momentum"], errors="coerce")
    observations: list[dict[str, Any]] = []
    for signal_date, day in ranks.groupby("signal_date", sort=True):
        top = day.sort_values(["rank", "symbol"], kind="stable").head(3)
        observations.append({
            "signal_date": str(signal_date),
            "eligible_ranked_candidate_count": int(len(day)),
            "top_candidates": [
                {
                    "rank": int(row.rank),
                    "symbol": str(row.symbol),
                    "name_label": None if pd.isna(row.name) else str(row.name),
                    "momentum": float(row.momentum),
                    "is_suspended_at_signal_row": None if pd.isna(row.is_suspended) else bool(row.is_suspended),
                    "is_st_at_signal_row": None if pd.isna(row.is_st) else bool(row.is_st),
                }
                for row in top.itertuples(index=False)
                if pd.notna(row.rank) and pd.notna(row.momentum)
            ],
        })
    return {
        "row_count": int(len(ranks)),
        "distinct_signal_dates": len(observations),
        "unique_symbols": int(ranks["symbol"].nunique(dropna=True)),
        "reconstruction_status": "POST_HOC_RECONSTRUCTED_FROM_FROZEN_BARS_AND_CONFIG; NOT_ORIGINAL_RUN_TIME_SEALED",
    }, observations


def build_real_market_summary(
    *,
    bars_path: str | Path = DEFAULT_BARS,
    snapshot_manifest_path: str | Path = DEFAULT_SNAPSHOT_MANIFEST,
    quality_path: str | Path = DEFAULT_QUALITY,
    universe_path: str | Path = DEFAULT_UNIVERSE,
    universe_audit_path: str | Path = DEFAULT_UNIVERSE_AUDIT,
    point_in_time_quality_path: str | Path = DEFAULT_POINT_IN_TIME_QUALITY,
    config_path: str | Path = DEFAULT_CONFIG,
    trade_audit_path: str | Path = DEFAULT_TRADE_AUDIT,
    candidate_ranks_path: str | Path = DEFAULT_RANKS,
    summary_id: str,
) -> dict[str, Any]:
    """Create a real-data fact pack without rerunning a backtest or sending raw rows."""
    paths = {
        "bars": _resolve(bars_path),
        "snapshot_manifest": _resolve(snapshot_manifest_path),
        "quality": _resolve(quality_path),
        "universe": _resolve(universe_path),
        "universe_audit": _resolve(universe_audit_path),
        "point_in_time_quality": _resolve(point_in_time_quality_path),
        "strategy_config": _resolve(config_path),
        "trade_audit": _resolve(trade_audit_path),
        "candidate_ranks": _resolve(candidate_ranks_path),
    }
    sources: list[dict[str, Any]] = []
    statuses = {
        "candidate_ranks": "POST_HOC_HASHED_AT_SUMMARY_GENERATION",
    }
    hashes = {
        key: _record_source(
            sources, key, path, key.replace("_", " "),
            statuses.get(key, "LOCAL_FILE_HASH_RECOMPUTED"),
        )
        for key, path in paths.items()
    }
    snapshot = _json(paths["snapshot_manifest"])
    quality = _json(paths["quality"])
    universe_audit = _json(paths["universe_audit"])
    point_quality = _json(paths["point_in_time_quality"])
    config = _json(paths["strategy_config"])
    trade_audit = _json(paths["trade_audit"])
    bars_hash = hashes["bars"]
    if snapshot.get("file_sha256", {}).get("bars.csv") != bars_hash:
        raise ValueError("行情文件 SHA-256 与快照 manifest 不一致")
    if snapshot.get("normalized_bars_sha256") != bars_hash:
        raise ValueError("规范行情 SHA-256 与快照 manifest 不一致")
    if point_quality.get("source_bars_sha256") != bars_hash or point_quality.get("data_sha256") != bars_hash:
        raise ValueError("点时质量报告未绑定到当前规范行情")
    input_hashes = trade_audit.get("input_hashes", {})
    if input_hashes.get("bars.csv") != bars_hash:
        raise ValueError("逐笔审计报告绑定的 bars.csv 与当前行情不同")
    if input_hashes.get("config.json") != hashes["strategy_config"]:
        raise ValueError("逐笔审计报告绑定的 config.json 与当前冻结配置不同")
    if not quality.get("healthy") or not point_quality.get("healthy"):
        raise ValueError("快照或点时审计质量不是 healthy；停止构造真实摘要")
    if universe_audit.get("members_without_bars") != 0 or universe_audit.get("daily_trade_status_mismatch_rows") != 0:
        raise ValueError("股票池与行情审计存在未解决缺口；停止构造真实摘要")

    data_stats = _scan_bars(paths["bars"])
    universe = pd.read_csv(paths["universe"])
    if "symbol" not in universe:
        raise ValueError("股票池 CSV 缺少 symbol 列")
    universe_symbols = set(universe["symbol"].dropna().astype(str))
    if universe_symbols != set(data_stats.pop("symbols")):
        raise ValueError("指定股票池证券集合与行情证券集合不一致")
    rank_stats, signal_observations = _rank_summary(paths["candidate_ranks"])
    strategy = config.get("strategy", {})
    market = config.get("market", {})
    costs = config.get("costs", {})
    source_start = str(snapshot.get("date_range", {}).get("start", data_stats["date_start"]))
    source_end = str(snapshot.get("date_range", {}).get("end", data_stats["date_end"]))
    if data_stats["date_start"] != source_start or data_stats["date_end"] != source_end:
        raise ValueError("快照 manifest 日期范围与行情内容不一致")
    if trade_audit.get("formal_baseline") != "NOT_READY":
        raise ValueError("逐笔审计报告正式门控状态不是预期的 NOT_READY")
    large_move_warning_count = sum(
        1 for warning in (quality.get("warnings") or [])
        if "单日原始收盘价变化超过 50%" in str(warning)
    )
    focus = data_stats.get("focus_suspension_candidate_window")
    if not focus or focus.get("actual_rows") != 21 or focus.get("suspended_rows") != 7:
        raise ValueError("SH603268 事后重建观察与既有审计基准不一致；停止生成固定研究摘要")
    first_day = next((item for item in signal_observations if item["signal_date"] == "2024-11-05"), None)
    if not first_day or not first_day["top_candidates"] or first_day["top_candidates"][0]["symbol"] != "SH603268":
        raise ValueError("既有首个信号日的第一名与候选重建不一致；停止生成固定研究摘要")

    evidence_catalog = [
        {
            "fact_id": "DATA_SCOPE_2024Q4",
            "statement": f"BaoStock 行情覆盖 {data_stats['date_start']} 至 {data_stats['date_end']}，{data_stats['date_count']} 个交易日、{data_stats['symbol_count']} 只沪深证券、{data_stats['row_count']} 行。",
            "artifact_sha256": hashes["bars"],
            "evidence_status": "RECONSTRUCTED",
        },
        {
            "fact_id": "PIT_UNIVERSE_RECONCILIATION",
            "statement": f"已分类沪深股票池审计记录 {universe_audit.get('daily_stock_member_rows')} 个成员日，行情匹配 {universe_audit.get('members_with_bars')} 行，缺行情 {universe_audit.get('members_without_bars')} 行，停牌状态差异 {universe_audit.get('daily_trade_status_mismatch_rows')} 行。",
            "artifact_sha256": hashes["universe_audit"],
            "evidence_status": "RECORDED",
        },
        {
            "fact_id": "FROZEN_MOMENTUM_CONFIG",
            "statement": f"冻结配置使用 {strategy.get('lookback_sessions')} 日后复权动量、{strategy.get('rebalance_sessions')} 日调仓、最多 {strategy.get('max_positions')} 个持仓、最小动量 {strategy.get('min_momentum')}。",
            "artifact_sha256": hashes["strategy_config"],
            "evidence_status": "RECORDED",
        },
        {
            "fact_id": "SIGNAL_RANK_RECONSTRUCTION",
            "statement": "三个已审计信号日的候选排名由冻结行情和配置事后重建；该排名产物不是原运行时封存的输入。",
            "artifact_sha256": hashes["candidate_ranks"],
            "evidence_status": "RECONSTRUCTED",
        },
        {
            "fact_id": "SUSPENDED_WINDOW_CANDIDATE",
            "statement": (
                f"{data_stats['focus_suspension_candidate_window']['symbol']} 在 "
                f"{data_stats['focus_suspension_candidate_window']['signal_date']} 的重建 20 日排名为第 1；"
                f"观察窗共 {data_stats['focus_suspension_candidate_window']['actual_rows']} 行，"
                f"其中停牌标记 {data_stats['focus_suspension_candidate_window']['suspended_rows']} 行，"
                "该现象尚不能证明历史时点可得性或信号规则正确。"
            ),
            "artifact_sha256": hashes["bars"],
            "evidence_status": "RECONSTRUCTED",
        },
        {
            "fact_id": "MARKET_RULE_FEE_ACTION_LIMITS",
            "statement": "该区间所有行情行的历史涨跌停与交易单位字段缺失；审计记录市场规则采用配置回退、历史费用未核验、公司行动覆盖不完整，正式基线为 NOT_READY。",
            "artifact_sha256": hashes["trade_audit"],
            "evidence_status": "UNVERIFIED",
        },
        {
            "fact_id": "LARGE_RAW_PRICE_MOVE_WARNING",
            "statement": f"快照质量报告记录 {large_move_warning_count} 个单日原始收盘价变化超过 50% 的警告，需结合复权、公司行动和源数据独立核对。",
            "artifact_sha256": hashes["quality"],
            "evidence_status": "RECORDED",
        },
    ]
    source_hashes = {item["sha256"] for item in sources}
    if any(item["artifact_sha256"] not in source_hashes for item in evidence_catalog):
        raise ValueError("证据目录中存在未绑定本地来源文件的摘要")
    return {
        "schema": "cehuan.real_market_research_summary.v1",
        "summary_id": summary_id,
        "classification": "REAL_MARKET_DATA_RESEARCH_SUMMARY_NOT_FORMAL_BACKTEST",
        "summary_generation": "RECONSTRUCTED_FROM_EXISTING_LOCAL_FILES_AT_THIS_RUN; NOT_ORIGINAL_RUN_TIME_SEALED",
        "scope": {
            "market": "沪深 A 股，不含北交所",
            "start_date": data_stats["date_start"],
            "end_date": data_stats["date_end"],
            "snapshot_id": snapshot.get("snapshot_id"),
            "universe_snapshot_id": universe_audit.get("universe_snapshot_id"),
            "provider": snapshot.get("provider"),
            "provider_package_version": snapshot.get("provider_package_version"),
        },
        "dataset": data_stats,
        "raw_quality_audit": {
            "healthy": quality.get("healthy"),
            "as_of": quality.get("as_of"),
            "retrieved_at_utc": quality.get("retrieved_at_utc"),
            "duplicate_count": quality.get("duplicate_count"),
            "missing_required": quality.get("missing_required"),
            "retrieved_before_available_count": quality.get("retrieved_before_available_count"),
            "calendar_validation": quality.get("calendar_validation"),
            "warnings": quality.get("warnings"),
        },
        "point_in_time_audit": {
            "status": "HEALTHY_WITH_SCOPE_LIMITS" if point_quality.get("healthy") else "NOT_HEALTHY",
            "daily_stock_member_rows": universe_audit.get("daily_stock_member_rows"),
            "members_with_bars": universe_audit.get("members_with_bars"),
            "members_without_bars": universe_audit.get("members_without_bars"),
            "price_coverage_fraction": universe_audit.get("price_coverage_fraction"),
            "st_known_member_rows": universe_audit.get("st_known_member_rows"),
            "st_unknown_member_rows": universe_audit.get("st_unknown_member_rows"),
            "daily_trade_status_comparable_rows": universe_audit.get("daily_trade_status_comparable_rows"),
            "daily_trade_status_mismatch_rows": universe_audit.get("daily_trade_status_mismatch_rows"),
            "eventually_delisted_member_rows": universe_audit.get("eventually_delisted_member_rows"),
            "eventually_delisted_member_rows_with_bars": universe_audit.get("eventually_delisted_member_rows_with_bars"),
            "quote_rows_outside_confirmed_daily_stock_pool": universe_audit.get("quote_rows_outside_confirmed_daily_stock_pool"),
            "interpretation": "仅说明当前 BaoStock 已分类沪深股票名单与取得行情的内部对账；不证明供应商全量完备、名称字段的历史时点或数据当时可得。",
        },
        "strategy": {
            "config_source_id": "strategy_config",
            "config_sha256": hashes["strategy_config"],
            "config_declared_provider": config.get("data", {}).get("provider"),
            "bars_provider_from_snapshot_manifest": snapshot.get("provider"),
            "provider_interpretation": "回测输入行情以 bars.csv 和其 hash 绑定；config.data.provider 是默认数据源配置，不单独证明本次 bars 的抓取来源。",
            "parameters": {key: strategy.get(key) for key in (
                "name", "lookback_sessions", "rebalance_sessions", "max_positions",
                "max_position_weight", "max_total_exposure", "min_momentum",
            )},
            "signal_price": config.get("data", {}).get("adjustment_for_signals"),
            "execution_prices": config.get("data", {}).get("execution_prices"),
            "signal_timing": "当日收盘后生成，按既有研究口径最早下一交易日开盘模拟；历史行情供应商实际发布时间未核实。",
        },
        "signal_observations": {
            **rank_stats,
            "dates": signal_observations,
            "source_status": "RECONSTRUCTED_POST_HOC; saved candidate ranks do not prove they were sealed at the original run time.",
        },
        "configured_fallbacks_not_historically_verified": {
            "market": {key: market.get(key) for key in (
                "buy_lot_shares", "sell_lot_shares", "default_price_limit_pct",
                "assume_price_limit_applies_when_unknown", "price_tick_cny", "exclude_st",
            )},
            "costs": {key: costs.get(key) for key in (
                "commission_rate", "minimum_commission_cny", "stamp_duty_sell_rate",
                "transfer_fee_rate", "slippage_bps",
            )},
            "rows_using_market_rule_fallback": trade_audit.get("market_rule_fallback_rows_in_window"),
            "personal_broker_fee_evidence": trade_audit.get("actual_personal_broker_fee_evidence"),
        },
        "known_limitations": [
            "历史行情由 BaoStock 于 2026-10-27 左右回取；历史供应商 vintage 和 2024 年决策时点可得性均 UNVERIFIED。",
            "available_at 是适配器赋予的交易日 15:00 收盘截止假设，不是供应商的历史发布时间证明。",
            "全部证券日的涨跌停、板块和交易单位字段在原行情中缺失；已有研究输入使用配置回退。",
            "当前费用配置不等于历史逐年法定费率，也不是个人华泰佣金证据。",
            "公司行动登记/覆盖不完整；没有事件记录不表示没有公司行动。",
            "股票池与行情对账只覆盖已分类沪深股票；不含北交所，不能表述为全 A 股完整性。",
            "candidate-ranks-reconstructed.csv 是事后重建产物；本次重算其哈希不表示它在原运行时已封存。",
            "name 中 *ST 前缀与 is_st 字段的差异只作待复核提示；名称的点时来源未验证，不据此改写 ST 状态。",
            "该短区间已用于开发与诊断，不是独立留出样本。",
            "冻结策略配置中的默认数据源字段与本次 BaoStock 快照来源不同；本摘要以快照 manifest 和回测 bars 哈希标识实际研究输入，不把默认 provider 字段当作抓取来源证明。",
        ],
        "prior_research_state": {
            "formal_baseline": "NOT_READY",
            "historical_availability_and_vendor_vintage": "UNVERIFIED",
            "market_rule_coverage": trade_audit.get("research_trust", {}).get("market_rule_coverage"),
            "fee_coverage": trade_audit.get("research_trust", {}).get("fee_coverage"),
            "corporate_action_coverage": trade_audit.get("research_trust", {}).get("corporate_action_coverage"),
        },
        "sources": sources,
        "evidence_catalog": evidence_catalog,
        "safety": {
            "account_data_included": False,
            "broker_fill_files_included": False,
            "raw_market_bars_included_in_agent_input": False,
            "orders_generated": False,
            "backtest_run_for_this_summary": False,
        },
    }


def write_real_market_summary(summary: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    output = _resolve(output_dir)
    ignored_root = (REPO_ROOT / "outputs").resolve()
    try:
        output.relative_to(ignored_root)
    except ValueError:
        raise ValueError("真实研究摘要只能写入 Git 忽略的 outputs/ 目录") from None
    output.mkdir(parents=True, exist_ok=False)
    json_path = output / "market_summary.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    facts = summary["dataset"]
    lines = [
        "# 真实行情研究摘要",
        "",
        "> 事后从本地真实行情与审计文件重建；不是原运行时封存证明，也不是正式回测。",
        "",
        f"- 摘要 ID：{summary['summary_id']}",
        f"- 市场与窗口：{summary['scope']['market']}，{summary['scope']['start_date']} 至 {summary['scope']['end_date']}",
        f"- BaoStock：{facts['date_count']} 个交易日、{facts['symbol_count']} 只证券、{facts['row_count']} 行；重复证券日 {facts['duplicate_symbol_date_rows']}。",
        f"- 点时内部对账：成员日 {summary['point_in_time_audit']['daily_stock_member_rows']}，有行情 {summary['point_in_time_audit']['members_with_bars']}，缺失 {summary['point_in_time_audit']['members_without_bars']}。",
        f"- 正式基线：{summary['prior_research_state']['formal_baseline']}。",
        "",
        "## 数据限制",
        "",
        *[f"- {item}" for item in summary["known_limitations"]],
        "",
        "## 可引用证据",
        "",
        *[f"- {item['fact_id']} [{item['evidence_status']}] {item['statement']}（来源 SHA-256 {item['artifact_sha256']}）" for item in summary["evidence_catalog"]],
        "",
        "## 输入来源与散列",
        "",
        *[f"- `{item['source_id']}`：`{item['path']}`；SHA-256 `{item['sha256']}`（{item['provenance_status']}）。" for item in summary["sources"]],
        "",
        "摘要 JSON 保存事实、来源路径和散列。未包含原始行情行或账户文件。",
        "",
    ]
    md_path = output / "market_summary.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
