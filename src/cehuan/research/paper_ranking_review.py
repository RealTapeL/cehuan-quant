"""Create a hash-bound daily report and optional one-shot LLM review of a paper ranking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.research.real_market_summary import REPO_ROOT, sha256_file
from cehuan.research.research_agent import ModelReply, resolve_research_model
from cehuan.research.research_cycle import _read_settings


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_AGENT_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "research_agent.toml"
REQUIRED_LIMITATIONS = {
    "small_scope_not_marketwide",
    "unchanged_h1_not_validity_proof",
    "momentum_not_return",
    "momentum_not_tradeability",
    "vendor_publication_unverified",
    "market_rules_fees_corporate_actions_incomplete",
}
REQUIRED_FACT_IDS = {
    "RANKING_ORIGINAL",
    "RANKING_H1",
    "RANKING_DIFFERENCE",
    "WINDOW_AUDIT",
    "DATA_CAPTURE",
    "LIMITATIONS",
}
SYMBOL_PATTERN = re.compile(r"\b(?:SH|SZ|BJ)\d{6}\b")
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
ACTION_PATTERN = re.compile(
    r"(?:建议|应该|应当|可以|适合|推荐).{0,8}(?:买入|卖出|建仓|加仓|减仓|做多|做空|下单|委托|交易|目标股数)"
    r"|(?:买入|卖出|建仓|加仓|减仓|下单|委托)(?:指令|建议)"
    r"|(?:目标股数|目标仓位|可执行订单|保证收益|稳赚)"
)
POSITIVE_PERFORMANCE_PATTERN = re.compile(
    r"(?:收益|回报).{0,8}(?:提升|改善|增加|更高|优于|超额)"
    r"|(?:证明|证实|验证).{0,8}(?:策略有效|收益改善|收益提升|可以成交|可交易)"
)


class PaperReviewValidationError(ValueError):
    """A safe local validation error; never contains raw model response text."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"{code}:{field}")


def _repo_path(path: str | Path) -> Path:
    value = Path(path).expanduser()
    return (value if value.is_absolute() else REPO_ROOT / value).resolve()


def _write_json(value: Any, path: Path) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} 不可读取或不是有效 JSON：{type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} 顶层必须是 JSON 对象")
    return value


def _verify_file(path: Path, expected: str, label: str) -> str:
    if not path.is_file():
        raise ValueError(f"{label} 文件不存在")
    actual = sha256_file(path)
    if not isinstance(expected, str) or actual != expected:
        raise ValueError(f"{label} SHA-256 与封存清单不一致")
    return actual


def _to_local(utc_value: str | None) -> str | None:
    if not utc_value:
        return None
    value = datetime.fromisoformat(utc_value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _selection_rule_for_report(rule: str) -> str:
    """Render the frozen code-based selection rule in Chinese when it matches the known contract."""
    normalized = " ".join(rule.casefold().split())
    if all(token in normalized for token in ("for each of sh and sz", "ascending", "select first five", "do not use prices")):
        return "在本次抓取名单中，沪市（SH）和深市（SZ）分别按六位证券代码升序，各取前 5 只；不按价格、收益、动量、名称或策略排名挑选"
    return rule


def _csv(path: Path, label: str) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError(f"{label} 文件不存在")
    try:
        return pd.read_csv(path, low_memory=False)
    except Exception as exc:
        raise ValueError(f"{label} CSV 无法解析：{type(exc).__name__}") from None


def _check_rank_frame(frame: pd.DataFrame, label: str, *, allow_empty: bool = False) -> pd.DataFrame:
    needed = {"signal_date", "symbol", "momentum", "rank", "selected_target"}
    if not needed.issubset(frame.columns):
        raise ValueError(f"{label} 缺少必要字段")
    result = frame.copy()
    result["signal_date"] = result["signal_date"].astype(str)
    result["symbol"] = result["symbol"].astype(str)
    result["rank"] = pd.to_numeric(result["rank"], errors="coerce")
    result["momentum"] = pd.to_numeric(result["momentum"], errors="coerce")
    if result.empty and allow_empty:
        return result
    if result.empty or result[["signal_date", "symbol", "rank", "momentum"]].isna().any().any():
        raise ValueError(f"{label} 为空或含无效排名值")
    if result["signal_date"].nunique() != 1 or result["symbol"].duplicated().any():
        raise ValueError(f"{label} 必须是单一信号日且证券不重复")
    result = result.sort_values("rank", kind="stable").reset_index(drop=True)
    expected = list(range(1, len(result) + 1))
    if result["rank"].astype(int).tolist() != expected:
        raise ValueError(f"{label} 排名必须从 1 连续递增")
    if not result["selected_target"].astype(str).str.lower().isin({"true", "1"}).all():
        raise ValueError(f"{label} 含未选入目标集合的行")
    return result


def build_paper_ranking_facts(
    *, ranking_dir: str | Path, capture_manifest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Rebuild facts from a sealed paper-ranking directory without rerunning its ranking."""
    run_dir = _repo_path(ranking_dir)
    manifest_path = run_dir / "paper-ranking-manifest.json"
    manifest = _read_json(manifest_path, "排名运行清单")
    if manifest.get("record_type") != "PAPER_RANKING_RUN" or manifest.get("classification") != "FORWARD_PAPER":
        raise ValueError("输入目录不是 FORWARD_PAPER 排名运行")
    if manifest.get("status") not in {"CAPTURED", "CAPTURED_WITH_EXCLUSIONS"}:
        raise ValueError("输入排名不是已封存的 CAPTURED 状态")

    input_paths = manifest.get("input_paths") or {}
    bars_path = _repo_path(input_paths.get("bars", ""))
    config_path = _repo_path(input_paths.get("config", ""))
    calendar_path = _repo_path(input_paths.get("trading_calendar", ""))
    universe_path = _repo_path(input_paths.get("point_in_time_universe", ""))
    capture_path = _repo_path(capture_manifest_path or input_paths.get("source_capture_manifest", ""))
    source_hashes = {
        "bars": _verify_file(bars_path, manifest.get("input_data_sha256", ""), "排名输入行情"),
        "config": _verify_file(config_path, manifest.get("config_sha256", ""), "排名配置"),
        "calendar": _verify_file(calendar_path, manifest.get("trading_calendar_sha256", ""), "交易日历"),
        "universe": _verify_file(universe_path, manifest.get("universe_sha256", ""), "点时研究池"),
        "capture_manifest": _verify_file(capture_path, manifest.get("source_capture_manifest_sha256", ""), "输入抓取清单"),
    }
    capture = _read_json(capture_path, "输入抓取清单")
    if capture.get("data_file_sha256") != source_hashes["bars"]:
        raise ValueError("抓取清单未绑定排名实际使用的 bars 文件")
    status_file_hash = capture.get("status_file_sha256")
    status_candidates = [
        candidate for candidate in capture_path.parent.glob("*.csv")
        if candidate.is_file() and sha256_file(candidate) == status_file_hash
    ]
    if len(status_candidates) != 1:
        raise ValueError("抓取清单的状态文件缺失或 SHA-256 无法唯一匹配")
    status_file = status_candidates[0]
    source_hashes["status_file"] = _verify_file(status_file, status_file_hash, "供应商状态文件")
    price_file_hashes = capture.get("price_files_sha256") or {}
    if not isinstance(price_file_hashes, dict):
        raise ValueError("抓取清单 price_files_sha256 格式无效")
    for filename, digest in price_file_hashes.items():
        source_hashes[f"price_file:{filename}"] = _verify_file(capture_path.parent / filename, digest, f"后复权来源文件 {filename}")

    ranking_path = run_dir / "ranking.csv"
    h1_path = run_dir / "h1-ranking.csv"
    diff_path = run_dir / "ranking-difference.csv"
    audit_path = run_dir / "window-audit.csv"
    original = _check_rank_frame(_csv(ranking_path, "原排名"), "原排名")
    h1 = _check_rank_frame(_csv(h1_path, "H1 排名"), "H1 排名", allow_empty=True)
    diff = _csv(diff_path, "排名差异")
    window = _csv(audit_path, "窗口审计")
    artifact_hashes = {
        "ranking.csv": _verify_file(ranking_path, manifest.get("ranking_file_sha256", ""), "原排名"),
        "h1-ranking.csv": _verify_file(h1_path, manifest.get("h1_ranking_file_sha256", ""), "H1 排名"),
        "ranking-difference.csv": _verify_file(diff_path, manifest.get("ranking_difference_file_sha256", ""), "排名差异"),
        "window-audit.csv": _verify_file(audit_path, manifest.get("window_audit_sha256", ""), "窗口审计"),
    }
    if len(diff) != len(original) or not {"symbol", "window_status", "h1_eligible", "direct_reason"}.issubset(diff.columns):
        raise ValueError("排名差异记录与原排名数量或字段不一致")
    if not {"symbol", "window_status", "expected_session_count", "included_in_ranking_scope"}.issubset(window.columns):
        raise ValueError("窗口审计缺少必要字段")
    if window["symbol"].astype(str).duplicated().any():
        raise ValueError("窗口审计存在重复证券")
    expected_window_count = int(manifest.get("window_scope", {}).get("point_in_time_universe_members", -1))
    passed_window = window.loc[window["window_status"].astype(str).isin({"COMPLETE", "COMPLETE_WITH_SUSPENSION"})]
    excluded_window = window.loc[~window.index.isin(passed_window.index)]
    if len(window) != expected_window_count:
        raise ValueError("窗口审计证券数与清单声明不一致")
    if int(manifest.get("window_scope", {}).get("complete_symbols", -1)) != len(passed_window):
        raise ValueError("窗口审计与清单中的 complete_symbols 不一致")
    if int(manifest.get("window_scope", {}).get("excluded_symbols", -1)) != len(excluded_window):
        raise ValueError("窗口审计与清单中的 excluded_symbols 不一致")
    candidate_count = int(manifest.get("candidate_count", -1))
    if len(original) != candidate_count:
        raise ValueError("原排名文件数量与 manifest.candidate_count 不一致")
    if int(manifest.get("h1_candidate_count", -1)) != len(h1):
        raise ValueError("H1 排名文件数量与 manifest.h1_candidate_count 不一致")
    symbols = window["symbol"].astype(str).tolist()
    included = window["included_in_ranking_scope"].astype(str).str.lower().isin({"true", "1"})
    expected_included = window["window_status"].astype(str).isin({"COMPLETE", "COMPLETE_WITH_SUSPENSION"})
    if not included.equals(expected_included):
        raise ValueError("窗口审计的研究池准入标记与窗口状态不一致")
    if not set(original["symbol"]).issubset(set(symbols)):
        raise ValueError("排名中出现窗口审计研究池以外的证券")
    signal_date = str(manifest.get("signal_date", ""))
    if original["signal_date"].unique().tolist() != [signal_date] or (not h1.empty and h1["signal_date"].unique().tolist() != [signal_date]):
        raise ValueError("排名信号日期与 manifest 不一致")

    window_by_symbol = window.set_index(window["symbol"].astype(str))["window_status"].astype(str).to_dict()
    diff = diff.copy()
    diff["symbol"] = diff["symbol"].astype(str)
    diff["window_status"] = diff["window_status"].astype(str)
    diff["h1_eligible"] = diff["h1_eligible"].astype(str).str.lower().isin({"true", "1"})
    if set(diff["symbol"]) != set(original["symbol"]) or diff["symbol"].duplicated().any():
        raise ValueError("排名差异证券集合与原排名不一致")
    for row in diff.itertuples(index=False):
        if window_by_symbol.get(row.symbol) != row.window_status:
            raise ValueError(f"排名差异窗口状态与窗口审计不一致：{row.symbol}")
        if bool(row.h1_eligible) != (row.window_status == "COMPLETE"):
            raise ValueError(f"排名差异 H1 准入状态与窗口规则不一致：{row.symbol}")

    max_positions = int(manifest.get("strategy_config", {}).get("max_positions", 0))
    if max_positions < 1:
        raise ValueError("排名清单缺少有效 max_positions")
    eligible_symbols = set(diff.loc[diff["h1_eligible"], "symbol"])
    expected_h1 = original.loc[original["symbol"].isin(eligible_symbols)].sort_values(
        ["momentum", "symbol"], ascending=[False, True], kind="stable",
    ).reset_index(drop=True)
    actual_h1 = h1.reset_index(drop=True)
    if (
        expected_h1["symbol"].tolist() != actual_h1["symbol"].tolist()
        or expected_h1["momentum"].tolist() != actual_h1["momentum"].tolist()
        or list(range(1, len(expected_h1) + 1)) != actual_h1["rank"].astype(int).tolist()
    ):
        raise ValueError("H1 排名与原候选筛选后按动量降序、代码升序重排的结果不一致")

    original_top = original.loc[original["rank"].le(max_positions), "symbol"].tolist()
    h1_top = h1.loc[h1["rank"].le(max_positions), "symbol"].tolist()
    actual_top_changed = set(original_top) != set(h1_top)
    if manifest.get("h1_top_n_changed") is not actual_top_changed:
        raise ValueError("清单中的 h1_top_n_changed 与排名文件不一致")
    removed_from_top = [symbol for symbol in original_top if symbol not in set(h1_top)]
    added_to_top = [symbol for symbol in h1_top if symbol not in set(original_top)]
    if manifest.get("removed_from_original_top_n", []) != removed_from_top or manifest.get("added_to_h1_top_n", []) != added_to_top:
        raise ValueError("清单记录的 Top-N 移出/补入与排名文件不一致")
    ranking_unchanged = (
        original["symbol"].tolist() == h1["symbol"].tolist()
        and original["rank"].astype(int).tolist() == h1["rank"].astype(int).tolist()
        and original["momentum"].tolist() == h1["momentum"].tolist()
    )

    pool_rule = "UNKNOWN"
    pool_capture_path = universe_path.parent / "capture-manifest.json"
    if pool_capture_path.is_file():
        pool_capture = _read_json(pool_capture_path, "冻结研究池来源清单")
        pool_rule = str(pool_capture.get("selection_rule", "UNKNOWN"))
        selected_path = universe_path.parent / "selected_pool.csv"
        if selected_path.is_file():
            selected = _csv(selected_path, "冻结证券池")
            selected_symbols = set(selected["symbol"].astype(str))
            if selected_symbols != set(symbols):
                raise ValueError("排名窗口证券与冻结 selected_pool 不一致")

    table_path = run_dir / "research-report.md"
    if not table_path.is_file():
        raise ValueError("排名后的 research-report.md 缺失")
    table_text = table_path.read_text(encoding="utf-8")
    table_hash = sha256_file(table_path)
    run_finished = manifest.get("finished_at_utc") or manifest.get("ranking_generated_at_utc")
    if not run_finished:
        raise ValueError("排名清单缺少完成时间，无法核实 research-report.md 的生成先后")
    finish_dt = datetime.fromisoformat(str(run_finished).replace("Z", "+00:00"))
    if finish_dt.tzinfo is None:
        finish_dt = finish_dt.replace(tzinfo=timezone.utc)
    report_created_after_run = table_path.stat().st_mtime > finish_dt.timestamp()
    if not report_created_after_run or "research-report.md" in (manifest.get("files") or {}):
        raise ValueError("research-report.md 未能证明是在排名封存后另行生成")
    def _ranking_rows(frame: pd.DataFrame, limit: int = 5) -> list[dict[str, Any]]:
        return [{
            "rank": int(row["rank"]),
            "symbol": str(row["symbol"]),
            "momentum": float(row["momentum"]),
            "selected_target": True,
        } for row in frame.loc[frame["rank"].le(limit)].to_dict(orient="records")]

    original_top5 = _ranking_rows(original)
    h1_top5 = _ranking_rows(h1)
    excluded_candidates = diff.loc[~diff["h1_eligible"]].sort_values("symbol")
    status_counts = window["window_status"].astype(str).value_counts().sort_index().to_dict()
    exclusion_reasons = [
        {"symbol": str(row.symbol), "window_status": str(row.window_status), "direct_reason": str(row.direct_reason)}
        for row in excluded_candidates.itertuples(index=False)
    ]
    if exclusion_reasons:
        excluded_summary = "；".join(
            f"{item['symbol']}：{item['direct_reason']}（{item['window_status']}）"
            for item in exclusion_reasons
        )
        direct_reason = f"H1 从原候选中排除 {len(exclusion_reasons)} 只：{excluded_summary}。其余候选按动量降序、证券代码升序重新排名。"
    else:
        direct_reason = f"原候选 {len(original)} 只均满足 H1 窗口准入条件；未因 H1 被排除，原排名与 H1 排名{'一致' if ranking_unchanged else '存在顺序变化'}。"
    original_top_n = original.loc[original["rank"].le(max_positions), "symbol"].tolist()
    h1_top_n = h1.loc[h1["rank"].le(max_positions), "symbol"].tolist()
    removed_from_top_n = [symbol for symbol in original_top_n if symbol not in set(h1_top_n)]
    added_to_top_n = [symbol for symbol in h1_top_n if symbol not in set(original_top_n)]
    report_rows: list[tuple[int, str, float]] = []
    table_section = table_text.partition("## 排名")[2].partition("## 输入")[0]
    for line in table_section.splitlines():
        match = re.match(r"\|\s*(\d+)\s*\|\s*((?:SH|SZ|BJ)\d{6})\s*\|\s*([0-9.]+)%\s*\|", line)
        if match:
            report_rows.append((int(match.group(1)), match.group(2), float(match.group(3))))
    if len(report_rows) != len(original_top5):
        raise ValueError("排名后生成的 research-report.md 表格行数与封存排名不一致")
    for expected, reported in zip(original_top5, report_rows):
        if reported[0] != expected["rank"] or reported[1] != expected["symbol"] or abs(reported[2] - expected["momentum"] * 100) > 0.00051:
            raise ValueError("排名后生成的 research-report.md 与封存排名不一致")
    h1_same_claim = re.search(r"H1.{0,16}(?:相同|一致|未变|不变)|(?:相同|一致|未变|不变).{0,16}H1", table_text)
    h1_different_claim = re.search(r"H1.{0,16}(?:不同|变化|筛除|过滤)|(?:不同|变化|筛除|过滤).{0,16}H1", table_text)
    if (ranking_unchanged and (not h1_same_claim or re.search(r"H1.{0,16}(?:不相同|不一致|不同)", table_text))) or (not ranking_unchanged and not h1_different_claim):
        raise ValueError("排名后生成的 research-report.md 对 H1 是否变化的说明与排名不一致")
    if f"{expected_window_count} 只研究证券" not in table_text or "窗口检查" not in table_text:
        raise ValueError("排名后生成的 research-report.md 未说明研究池窗口检查范围")
    if "正式基线 NOT_READY" not in table_text or "未计算收益" not in table_text:
        raise ValueError("排名后生成的 research-report.md 缺少研究边界")

    status_requests = capture.get("status_requests", [])
    price_requests = capture.get("price_requests", [])
    if not isinstance(status_requests, list) or not isinstance(price_requests, list):
        raise ValueError("输入抓取清单缺少状态或价格请求清单")
    status_dates = {str(item.get("trade_date")) for item in status_requests if isinstance(item, Mapping)}
    status_symbols = {str(item.get("symbol")) for item in status_requests if isinstance(item, Mapping)}
    status_fields_valid = len(status_requests) == expected_window_count and status_dates == {signal_date} and status_symbols == set(symbols)
    if not status_fields_valid:
        raise ValueError("抓取清单中的当日状态请求未覆盖完整冻结池")
    status_frame = _csv(status_file, "供应商状态原始记录")
    if not {"trade_date", "symbol", "tradestatus", "isST"}.issubset(status_frame.columns):
        raise ValueError("供应商状态文件缺少日期、证券、停牌或 ST 字段")
    status_projection = {
        (str(row.trade_date), str(row.symbol), str(row.tradestatus), str(row.isST))
        for row in status_frame.itertuples(index=False)
    }
    manifest_projection = {
        (str(item.get("trade_date")), str(item.get("symbol")), str(item.get("tradestatus")), str(item.get("isST")))
        for item in status_requests
    }
    if status_projection != manifest_projection:
        raise ValueError("状态文件逐证券记录与抓取清单不一致")
    source_name = str(capture.get("source_id", "UNKNOWN"))
    status_source = "BaoStock" if "BaoStock" in source_name else source_name
    price_sources = sorted({str(item.get("source", "UNKNOWN")) for item in price_requests if isinstance(item, Mapping)})
    capture_times = [
        item.get("received_at_utc") for item in status_requests if isinstance(item, Mapping)
    ] + [item.get("received_at_utc") for item in price_requests if isinstance(item, Mapping)]
    capture_times = [str(value) for value in capture_times if value]
    source_capture_local_start = _to_local(capture.get("capture_started_at_utc"))
    source_capture_local_finish = _to_local(capture.get("capture_finished_at_utc") or capture.get("captured_at_utc"))
    status_local_start = _to_local(min(str(item["requested_at_utc"]) for item in status_requests))
    status_local_finish = _to_local(max(str(item["received_at_utc"]) for item in status_requests))
    price_local_start = _to_local(min(str(item["requested_at_utc"]) for item in price_requests)) if price_requests else None
    price_local_finish = _to_local(max(str(item["received_at_utc"]) for item in price_requests)) if price_requests else None

    bar_frame = _csv(bars_path, "排名行情输入")
    if not {"symbol", "trade_date", "adjusted_close"}.issubset(bar_frame.columns):
        raise ValueError("排名行情输入缺少证券日或后复权字段")
    bar_counts = bar_frame.groupby("symbol").size().to_dict()
    if set(bar_counts) != set(symbols) or any(int(count) != int(window.iloc[0]["expected_session_count"]) for count in bar_counts.values()):
        raise ValueError("实际排名行情与审计的 21 日研究池窗口不一致")
    if pd.to_numeric(bar_frame["adjusted_close"], errors="coerce").isna().any():
        raise ValueError("排名实际行情仍存在缺失的策略后复权价格")
    price_request_files = {str(item.get("file")): item for item in price_requests if isinstance(item, Mapping)}
    if set(price_request_files) != set(price_file_hashes):
        raise ValueError("后复权来源请求与其文件哈希清单不一致")
    for filename, request in price_request_files.items():
        if request.get("source") != "akshare.stock_zh_a_hist_tx" or request.get("adjust") != "hfq":
            raise ValueError("后复权补抓来源或复权口径不符合封存清单")
        source_price = _csv(capture_path.parent / filename, f"后复权来源文件 {filename}")
        if not {"date", "close"}.issubset(source_price.columns):
            raise ValueError(f"后复权来源文件缺少日期或价格：{filename}")
        symbol = str(request.get("symbol"))
        input_symbol = bar_frame.loc[bar_frame["symbol"].astype(str).eq(symbol), ["trade_date", "adjusted_close"]].copy()
        source_price = source_price[["date", "close"]].copy()
        input_symbol["trade_date"] = input_symbol["trade_date"].astype(str)
        source_price["date"] = source_price["date"].astype(str)
        input_symbol["adjusted_close"] = pd.to_numeric(input_symbol["adjusted_close"], errors="coerce")
        source_price["close"] = pd.to_numeric(source_price["close"], errors="coerce")
        aligned = input_symbol.merge(source_price, left_on="trade_date", right_on="date", how="outer", validate="one_to_one", indicator=True)
        expected_price_rows = int(window.iloc[0]["expected_session_count"])
        if len(source_price) != expected_price_rows or not aligned["_merge"].eq("both").all() or not (aligned["adjusted_close"] - aligned["close"]).abs().le(1e-8).all():
            raise ValueError(f"后复权来源文件与实际排名窗口价格不一致：{symbol}")

    evidence = {
        "RANKING_ORIGINAL": {"claim": "原始排名顺序及动量", "sha256": artifact_hashes["ranking.csv"]},
        "RANKING_H1": {"claim": "H1 排名顺序及动量", "sha256": artifact_hashes["h1-ranking.csv"]},
        "RANKING_DIFFERENCE": {"claim": "H1 前后逐证券差异及直接原因", "sha256": artifact_hashes["ranking-difference.csv"]},
        "WINDOW_AUDIT": {"claim": "研究池窗口完整性与状态检查", "sha256": artifact_hashes["window-audit.csv"]},
        "DATA_CAPTURE": {"claim": "本机抓取时间、供应商与字段请求记录", "sha256": source_hashes["capture_manifest"]},
        "POST_RUN_REPORT": {"claim": "排名后单独生成的中文排名报告，未包含在排名运行 manifest 中", "sha256": table_hash},
        "LIMITATIONS": {"claim": "小范围池、发布时间、规则、费用、公司行动和正式基线限制", "sha256": sha256_file(manifest_path)},
    }
    facts = {
        "schema": "cehuan.paper_ranking_program_facts.v1",
        "classification": "REAL_MARKET_SMALL_SCOPE_FORWARD_PAPER_RANKING_REVIEW",
        "signal_date": signal_date,
        "ranking_generated_at_utc": manifest.get("ranking_generated_at_utc"),
        "ranking_generated_at_local": _to_local(manifest.get("ranking_generated_at_utc")),
        "scope": {
            "label": f"{expected_window_count} 只证券的小范围沪深研究池，不代表全市场",
            "symbol_count": expected_window_count,
            "symbols": symbols,
            "selection_rule": pool_rule,
        },
        "window_gate": {
            "expected_open_sessions_per_symbol": int(window.iloc[0]["expected_session_count"]),
            "passed_symbol_count": int(len(passed_window)),
            "suspended_window_symbol_count": int(manifest.get("window_scope", {}).get("suspended_window_symbols", -1)),
            "excluded_symbol_count": int(manifest.get("window_scope", {}).get("excluded_symbols", -1)),
            "status_counts": {str(key): int(value) for key, value in status_counts.items()},
            "interpretation": "这是窗口门槛通过数，不是进入 Top-N 排名的数量。",
        },
        "ranking": {
            "original_count": len(original),
            "h1_count": len(h1),
            "max_positions": manifest.get("strategy_config", {}).get("max_positions", 5),
            "original_top5": original_top5,
            "h1_top5": h1_top5,
            "original_symbols": original["symbol"].astype(str).tolist(),
            "h1_symbols": h1["symbol"].astype(str).tolist(),
            "original_rank_by_symbol": {str(row.symbol): int(row.rank) for row in original.itertuples(index=False)},
            "h1_rank_by_symbol": {str(row.symbol): int(row.rank) for row in h1.itertuples(index=False)},
            "unchanged": ranking_unchanged,
            "removed_from_top_n": removed_from_top_n,
            "added_to_top_n": added_to_top_n,
            "excluded_candidates": exclusion_reasons,
            "direct_reason": direct_reason,
            "momentum_definition": "排名文件中的程序计算字段；它不是组合收益、成交结果或可成交性证明。",
        },
        "data_capture": {
            "status_source": status_source,
            "status_source_local_interval": [status_local_start, status_local_finish],
            "status_request_count": len(status_requests),
            "status_fields": ["tradestatus", "isST"],
            "status_observations": {
                "tradestatus_counts": {str(key): int(value) for key, value in status_frame["tradestatus"].astype(str).value_counts(dropna=False).sort_index().items()},
                "isST_counts": {str(key): int(value) for key, value in status_frame["isST"].astype(str).value_counts(dropna=False).sort_index().items()},
            },
            "price_sources": price_sources,
            "price_source_local_interval": [price_local_start, price_local_finish],
            "complete_hfq_refresh_symbols": sorted(str(item.get("symbol")) for item in price_requests if isinstance(item, Mapping)),
            "all_window_status": {"passed": len(passed_window), "excluded": len(excluded_window), "status_counts": {str(key): int(value) for key, value in status_counts.items()}},
            "local_capture_interval": [source_capture_local_start, source_capture_local_finish],
            "provider_publication_time": "UNVERIFIED",
            "input_available_at_is_provider_publish_time": False,
        },
        "limitations": [
            f"按固定选择规则确定的 {expected_window_count} 只沪深证券小范围研究池，不能代表全市场。",
            f"H1 排名{'未变' if ranking_unchanged else '变化'}只说明本次窗口筛选对本次排名的影响，不证明 H1 规则有效或无效。",
            "动量是排名信号，不是策略收益，也不证明证券可以成交。",
            "本机抓取时间可复核；供应商发布时间和可信外部时间戳未核实。",
            "全市场历史交易规则、实际费用和公司行动覆盖在本次运行时尚未完成验证。",
            f"该 {signal_date} 纸面排名不是独立样本外策略验证；正式基线保持 NOT_READY。",
        ],
        "execution_boundaries": {
            "returns_computed": False,
            "strategy_modified": False,
            "broker_connected": False,
            "orders_generated": False,
            "formal_baseline": "NOT_READY",
        },
        "sources": {
            "ranking_run_manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
            "input_capture_manifest": {"path": str(capture_path), "sha256": source_hashes["capture_manifest"]},
            "files": {**source_hashes, **artifact_hashes, "research-report.md": table_hash},
            "paths": {
                "bars": str(bars_path),
                "config": str(config_path),
                "calendar": str(calendar_path),
                "universe": str(universe_path),
                "capture_manifest": str(capture_path),
                "status_file": str(status_file),
                **{f"price_file:{filename}": str(capture_path.parent / filename) for filename in price_file_hashes},
                **{name: str(run_dir / name) for name in ("ranking.csv", "h1-ranking.csv", "ranking-difference.csv", "window-audit.csv", "research-report.md")},
            },
            "ranking_report_created_after_run_manifest": report_created_after_run,
        },
        "evidence_catalog": evidence,
    }
    return facts


def build_review_prompt(model_input: Mapping[str, Any]) -> str:
    ranking = model_input["ranking"]
    window = model_input["window_gate"]
    no_exclusion_instruction = (
        "本次程序事实显示没有原候选被 H1 排除；ranking_explanation 必须明确写出「候选全部保留」并结合实际数据解释。 "
        if ranking["unchanged"] and not ranking["excluded_candidates"] else ""
    )
    output_shape = {
        "schema": "cehuan.paper_ranking_review.v1",
        "review_status": "REVIEWED",
        "signal_date": model_input["signal_date"],
        "original_top5": [row["symbol"] for row in ranking["original_top5"]],
        "h1_top5": [row["symbol"] for row in ranking["h1_top5"]],
        "h1_unchanged": ranking["unchanged"],
        "window_passed_count": window["passed_symbol_count"],
        "ranked_count": ranking["original_count"],
        "ranking_explanation": "说明本次排名是否变化、直接原因和涉及证券。不得照抄本占位句。",
        "recorded_facts_vs_interpretation": "区分程序记录的事实和你的解释。",
        "what_ranking_establishes": "说明排名能建立的有限事实。",
        "what_it_cannot_show": [
            "小范围研究池不代表全市场。",
            "H1 排名变化或未变化都不能证明规则有效或收益改善。",
            "动量不是策略收益，也不证明可以成交。",
            "供应商发布时间未核实；历史规则、费用和公司行动覆盖有限。",
        ],
        "next_observation": "提出下一次应记录的一项可观察信息，不给交易建议。",
        "limitations_acknowledged": {key: True for key in sorted(REQUIRED_LIMITATIONS)},
        "evidence_fact_ids": sorted(REQUIRED_FACT_IDS),
    }
    return f"""你是策环的研究复盘助手。只根据下面的程序事实解释纸面排名，不调用工具，不给交易建议。

仅输出一个符合结构的 JSON 对象，不要 Markdown 代码围栏。结构化证券、日期、排名顺序和计数必须与程序事实一致。ranking_explanation 要用阿拉伯数字明确写出通过窗口的证券数和原排名条数，以区分窗口通过数与排名条数。解释要针对本次结果，不能复制占位句；排名发生变化时，引用实际变化的证券和程序记录的筛选原因；排名未变时，说明程序记录的直接原因，并明确候选是否被筛除。直接原因：{ranking["direct_reason"]}。明确区分程序事实与模型解释。

{no_exclusion_instruction}

必须明确：小范围研究池不代表全市场；H1 排名变化或未变化都不能证明 H1 有效或收益改善；动量不是策略收益，也不证明可以成交；供应商发布时间未核实；历史规则、费用和公司行动覆盖有限。不得编造证券、日期、排名、数量或来源。不得给买卖指令、目标股数、仓位或保证性结论。next_observation 只提出下一次应新增记录的一项可观察信息。

JSON 必须恰好包含这些字段：schema, review_status, signal_date, original_top5, h1_top5, h1_unchanged, window_passed_count, ranked_count, ranking_explanation, recorded_facts_vs_interpretation, what_ranking_establishes, what_it_cannot_show, next_observation, limitations_acknowledged, evidence_fact_ids。

输出结构（请按本次事实填写解释文字）：
{json.dumps(output_shape, ensure_ascii=False, sort_keys=True)}

程序事实与可引用证据：
{json.dumps(model_input, ensure_ascii=False, sort_keys=True, indent=2)}"""


def validate_model_review(content: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PaperReviewValidationError("INVALID_JSON", "$") from None
    if not isinstance(payload, dict):
        raise PaperReviewValidationError("TOP_LEVEL_NOT_OBJECT", "$")
    required = {
        "schema", "review_status", "signal_date", "original_top5", "h1_top5", "h1_unchanged",
        "window_passed_count", "ranked_count", "ranking_explanation", "recorded_facts_vs_interpretation",
        "what_ranking_establishes", "what_it_cannot_show", "next_observation",
        "limitations_acknowledged", "evidence_fact_ids",
    }
    if set(payload) != required:
        raise PaperReviewValidationError("TOP_LEVEL_FIELDS_MISMATCH", "$")
    expected_original = [row["symbol"] for row in facts["ranking"]["original_top5"]]
    expected_h1 = [row["symbol"] for row in facts["ranking"]["h1_top5"]]
    expected_scalars = {
        "schema": "cehuan.paper_ranking_review.v1",
        "review_status": "REVIEWED",
        "signal_date": facts["signal_date"],
        "original_top5": expected_original,
        "h1_top5": expected_h1,
        "h1_unchanged": facts["ranking"]["unchanged"],
        "window_passed_count": facts["window_gate"]["passed_symbol_count"],
        "ranked_count": facts["ranking"]["original_count"],
    }
    for field, expected in expected_scalars.items():
        if payload.get(field) != expected:
            raise PaperReviewValidationError("PROGRAM_FACT_CONFLICT", field)
    if type(payload.get("h1_unchanged")) is not bool or type(payload.get("window_passed_count")) is not int or type(payload.get("ranked_count")) is not int:
        raise PaperReviewValidationError("PROGRAM_FACT_TYPE_CONFLICT", "h1_unchanged/window_passed_count/ranked_count")
    for field in ("ranking_explanation", "recorded_facts_vs_interpretation", "what_ranking_establishes", "next_observation"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise PaperReviewValidationError("REQUIRED_TEXT_MISSING", field)
    template_markers = (
        "说明本次排名是否变化", "区分程序记录的事实", "说明排名能建立", "提出下一次应记录",
        "根据本次事实填写解释文字", "请按本次事实填写",
    )
    if any(marker in payload[field] for field in ("ranking_explanation", "recorded_facts_vs_interpretation", "what_ranking_establishes", "next_observation") for marker in template_markers):
        raise PaperReviewValidationError("MODEL_COPIED_TEMPLATE", "prose")
    if not isinstance(payload.get("what_it_cannot_show"), list) or len(payload["what_it_cannot_show"]) < 4 or not all(isinstance(item, str) and item.strip() for item in payload["what_it_cannot_show"]):
        raise PaperReviewValidationError("LIMITATIONS_LIST_INVALID", "what_it_cannot_show")
    acknowledged = payload.get("limitations_acknowledged")
    if not isinstance(acknowledged, dict) or set(acknowledged) != REQUIRED_LIMITATIONS or any(value is not True for value in acknowledged.values()):
        raise PaperReviewValidationError("LIMITATIONS_NOT_ACKNOWLEDGED", "limitations_acknowledged")
    evidence = payload.get("evidence_fact_ids")
    known_ids = set(facts["evidence_catalog"])
    if not isinstance(evidence, list) or not set(evidence).issubset(known_ids) or not REQUIRED_FACT_IDS.issubset(set(evidence)):
        raise PaperReviewValidationError("UNKNOWN_OR_MISSING_EVIDENCE_ID", "evidence_fact_ids")

    prose_fields = (
        "ranking_explanation", "recorded_facts_vs_interpretation", "what_ranking_establishes",
        "what_it_cannot_show", "next_observation",
    )
    prose = "\n".join(
        [payload[field] for field in prose_fields if isinstance(payload[field], str)]
        + [item for item in payload["what_it_cannot_show"] if isinstance(item, str)]
    )
    allowed_symbols = set(facts["scope"]["symbols"])
    if set(SYMBOL_PATTERN.findall(prose)) - allowed_symbols:
        raise PaperReviewValidationError("UNSUPPORTED_SECURITY_REFERENCE", "prose")
    allowed_dates = {facts["signal_date"]}
    dates = set(DATE_PATTERN.findall(prose))
    if dates - allowed_dates:
        raise PaperReviewValidationError("UNSUPPORTED_DATE_REFERENCE", "prose")
    if ACTION_PATTERN.search(prose):
        raise PaperReviewValidationError("ACTIONABLE_TRADE_LANGUAGE", "prose")
    positive_claim = POSITIVE_PERFORMANCE_PATTERN.search(prose)
    if positive_claim and not re.search(r"(?:不|未|尚未|并未|不能|无法).{0,8}" + re.escape(positive_claim.group(0)), prose):
        raise PaperReviewValidationError("UNSUPPORTED_PERFORMANCE_CLAIM", "prose")
    explain = payload["ranking_explanation"]
    ranking = facts["ranking"]
    for count in (facts["window_gate"]["passed_symbol_count"], ranking["original_count"]):
        if not re.search(rf"(?<!\d){count}(?!\d)", explain):
            raise PaperReviewValidationError("RANKING_EXPLANATION_MISSING_COUNT", "ranking_explanation")
    if ranking["unchanged"]:
        if not any(word in explain for word in (
            "未变", "不变", "一致", "相同", "保持原序", "保持原有顺序",
            "维持原有顺序", "未发生变化", "没有区别", "未见差异", "无差异",
        )):
            raise PaperReviewValidationError("RANKING_UNCHANGED_NOT_STATED", "ranking_explanation")
        if ranking["excluded_candidates"] and not any(item["symbol"] in explain for item in ranking["excluded_candidates"]):
            raise PaperReviewValidationError("RANKING_EXPLANATION_MISSING_CHANGED_SECURITY", "ranking_explanation")
        if not ranking["excluded_candidates"]:
            no_exclusion_words = (
                "未排除", "没有排除", "没有证券被 H1 排除", "未筛除", "未触发排除",
                "没有剔除", "未剔除", "未移出", "没有候选被过滤", "未过滤掉候选",
                "没有候选被剔除", "未剔除候选", "候选均保留", "候选全部保留",
                "筛选未触发", "未触发筛选", "未触发过滤", "未发生筛选",
            )
            no_suspension = bool(re.search(
                r"(?:无|没有|未记录|未发现|未见|不存在|未出现).{0,6}停牌"
                r"|停牌.{0,8}(?:为0|为零|无|没有|未发现|未记录)",
                explain,
            ))
            direct_no_exclusion = any(word in explain for word in no_exclusion_words)
            if not direct_no_exclusion and not (no_suspension and ("H1" in explain or "筛选" in explain)):
                raise PaperReviewValidationError("RANKING_NO_EXCLUSION_REASON_MISSING", "ranking_explanation")
    else:
        if not any(word in explain for word in ("变化", "不同", "调整", "重排", "移出", "排除", "补入")):
            raise PaperReviewValidationError("RANKING_CHANGE_NOT_STATED", "ranking_explanation")
        changed_symbols = {
            symbol for symbol in set(ranking["original_symbols"] + ranking["h1_symbols"])
            if ranking["original_rank_by_symbol"].get(symbol) != ranking["h1_rank_by_symbol"].get(symbol)
        }
        if changed_symbols and not (set(SYMBOL_PATTERN.findall(explain)) & changed_symbols):
            raise PaperReviewValidationError("RANKING_EXPLANATION_MISSING_CHANGED_SECURITY", "ranking_explanation")
        if ranking["excluded_candidates"] and not any(word in explain for word in ("排除", "筛除", "移出")):
            raise PaperReviewValidationError("RANKING_EXPLANATION_MISSING_DIRECT_REASON", "ranking_explanation")

    cannot_show = "\n".join(payload["what_it_cannot_show"])
    limitation_checks = {
        "small_scope_not_marketwide": "小范围" in cannot_show and "全市场" in cannot_show,
        "unchanged_h1_not_validity_proof": "H1" in cannot_show and ("有效" in cannot_show or "证明" in cannot_show),
        "momentum_not_return": "动量" in cannot_show and "收益" in cannot_show,
        "momentum_not_tradeability": "动量" in cannot_show and ("成交" in cannot_show or "可成交" in cannot_show),
        "vendor_publication_unverified": "发布时间" in cannot_show and "未核实" in cannot_show,
        "market_rules_fees_corporate_actions_incomplete": all(word in cannot_show for word in ("规则", "费用", "公司行动")),
    }
    if not all(limitation_checks.values()):
        raise PaperReviewValidationError("LIMITATION_TEXT_INCOMPLETE", "what_it_cannot_show")
    return payload


def _report_markdown(facts: Mapping[str, Any], review: Mapping[str, Any] | None, status: str, audit: Mapping[str, Any]) -> str:
    ranking = facts["ranking"]
    generated = facts["ranking_generated_at_local"]
    review_date = str(audit["review_started_at_local"])[:10]
    temporal_label = "事后复盘，不是信号日当天形成的新前瞻记录" if review_date > facts["signal_date"] else "信号日当天或之前运行；仍以封存排名时间为准"
    top_rows = []
    for index in range(max(len(ranking["original_top5"]), len(ranking["h1_top5"]))):
        original_row = ranking["original_top5"][index] if index < len(ranking["original_top5"]) else None
        h1_row = ranking["h1_top5"][index] if index < len(ranking["h1_top5"]) else None
        original_symbol = original_row["symbol"] if original_row else "—"
        original_momentum = f"{original_row['momentum']:.6%}" if original_row else "—"
        h1_symbol = h1_row["symbol"] if h1_row else "—"
        h1_momentum = f"{h1_row['momentum']:.6%}" if h1_row else "—"
        top_rows.append(f"| {index + 1} | {original_symbol} | {original_momentum} | {h1_symbol} | {h1_momentum} |")
    removed = ranking["removed_from_top_n"]
    added = ranking["added_to_top_n"]
    price_symbols = facts["data_capture"]["complete_hfq_refresh_symbols"]
    price_sources = facts["data_capture"]["price_sources"]
    source_capture = facts["data_capture"]["local_capture_interval"]
    lines = [
        f"# {facts['signal_date']} 小范围纸面排名与 AI 复盘",
        "",
        f"- 排名生成时间：`{generated}`（Asia/Shanghai）；本复盘运行时间：`{audit['review_started_at_local']}`。{temporal_label}。",
        f"- 研究范围：{_selection_rule_for_report(str(facts['scope']['selection_rule']))}；共 {facts['scope']['symbol_count']} 只沪深证券的小范围研究池，不代表全市场。",
        f"- 模型复盘状态：`{status}`；provider/model：`{audit.get('provider') or 'UNKNOWN'}` / `{audit.get('model') or 'UNKNOWN'}`；真实模型请求次数：{audit.get('call_count', 0)}；token：`{audit.get('usage') or 'UNKNOWN'}`；费用：`{audit.get('cost_status', 'UNKNOWN')}`。",
        "",
        "## 程序事实",
        "",
        f"窗口审计：{facts['window_gate']['passed_symbol_count']}/{facts['scope']['symbol_count']} 只通过每只 {facts['window_gate']['expected_open_sessions_per_symbol']} 个开市日窗口检查，窗口状态分布 `{json.dumps(facts['window_gate']['status_counts'], ensure_ascii=False, sort_keys=True)}`；通过窗口的证券数不同于排名条数。原排名输出 {ranking['original_count']} 条，H1 排名输出 {ranking['h1_count']} 条。",
        f"全量排名顺序与动量 {'一致' if ranking['unchanged'] else '不同'}；Top-{ranking['max_positions']} 成分移出：{', '.join(removed) if removed else '无'}；补入：{', '.join(added) if added else '无'}。直接原因：{ranking['direct_reason']}",
        "",
        "| 前五序号 | 原排名证券 | 原动量 | H1 排名证券 | H1 动量 |",
        "|---:|---|---:|---|---:|",
        *top_rows,
    ]
    lines.extend([
        "",
        f"信号日证券状态来自 `{facts['data_capture']['status_source']}` 记录，抓取区间 `{facts['data_capture']['status_source_local_interval'][0]}` 至 `{facts['data_capture']['status_source_local_interval'][1]}`；字段计数 `{json.dumps(facts['data_capture']['status_observations'], ensure_ascii=False, sort_keys=True)}`。",
        f"补抓后复权价格的证券为 `{', '.join(price_symbols) if price_symbols else '无'}`，来源 `{', '.join(price_sources) if price_sources else '无'}`，抓取区间 `{facts['data_capture']['price_source_local_interval'][0] or 'UNKNOWN'}` 至 `{facts['data_capture']['price_source_local_interval'][1] or 'UNKNOWN'}`。本机抓取区间为 `{source_capture[0] or 'UNKNOWN'}` 至 `{source_capture[1] or 'UNKNOWN'}`；供应商发布时间：`{facts['data_capture']['provider_publication_time']}`。",
        "",
        "## 模型解释（与程序事实分开）",
        "",
    ])
    if review is None:
        if int(audit.get("call_count", 0)) == 0:
            lines.extend([
                f"本次没有发起模型请求：`{status}`。安全审计仅记录错误类别 `{audit.get('failure_code', 'MISSING_LLM_CONFIGURATION')}`，没有回退 stub。",
                "模型解释待在配置好模型环境的终端中重新运行本命令后生成。",
            ])
        elif status == "MODEL_REVIEW_REJECTED":
            lines.extend([
                "本次真实模型请求已返回，但模型复盘未通过本地内容校验；未保存被拒绝的模型正文。",
                f"安全审计定位到 `{audit.get('failure_code', 'UNKNOWN')}`，字段 `{audit.get('failure_field', 'UNKNOWN')}`；真实请求次数 {audit.get('call_count', 0)}，token 用量 `{audit.get('usage') or 'UNKNOWN'}`。未自动重试，也没有回退 stub。",
            ])
        elif status == "MODEL_RESPONSE_JSON_INVALID":
            lines.extend([
                "本次真实模型请求已返回，但响应不是可接受的 JSON；未保存被拒绝的模型正文。",
                f"安全审计记录 `{audit.get('failure_code', 'UNKNOWN')}`，字段 `{audit.get('failure_field', 'UNKNOWN')}`；未自动重试，也没有回退 stub。",
            ])
        else:
            lines.extend([
                f"本次模型请求未形成可审核的复盘：`{status}`；安全审计错误类别 `{audit.get('failure_code', 'UNKNOWN')}`。",
                "没有保存原始模型正文；本次未自动重试，也没有回退 stub。",
            ])
    else:
        lines.extend([
            f"**排名变化解释：** {review['ranking_explanation']}",
            "",
            f"**事实与解释：** {review['recorded_facts_vs_interpretation']}",
            "",
            f"**这份排名能说明：** {review['what_ranking_establishes']}",
            "",
            "**不能据此说明：**",
            *[f"- {item}" for item in review["what_it_cannot_show"]],
            "",
            f"**下一次应记录：** {review['next_observation']}",
        ])
    lines.extend([
        "",
        "## 限制与执行边界",
        "",
        *[f"- {item}" for item in facts["limitations"]],
        "- 本次没有计算正式收益、没有修改有效策略、没有连接华泰、没有生成可执行股数或委托。",
        "",
        "## 输入哈希",
        "",
        f"- 排名运行清单：{facts['sources']['ranking_run_manifest']['path']}；SHA-256 {facts['sources']['ranking_run_manifest']['sha256']}",
        f"- 输入抓取清单：{facts['sources']['input_capture_manifest']['path']}；SHA-256 {facts['sources']['input_capture_manifest']['sha256']}",
        *[
            f"- `{name}`：{facts['sources']['paths'].get(name, '见运行清单')}；SHA-256 `{digest}`"
            for name, digest in facts["sources"]["files"].items()
        ],
        "- `program-facts.json` 与模型审计见本报告所在运行目录的 manifest。",
        "",
    ])
    if ranking["excluded_candidates"]:
        lines.extend(["", "H1 未准入候选及程序原因：", *[
            f"- `{item['symbol']}`：{item['direct_reason']}（窗口状态 `{item['window_status']}`）"
            for item in ranking["excluded_candidates"]
        ]])
    return "\n".join(lines)


def run_paper_ranking_review(
    *, ranking_dir: str | Path, capture_manifest_path: str | Path | None = None,
    output_root: str | Path = "outputs/paper-ranking-reviews", agent_config_path: str | Path | None = None,
) -> Path:
    facts = build_paper_ranking_facts(ranking_dir=ranking_dir, capture_manifest_path=capture_manifest_path)
    now = datetime.now(SHANGHAI)
    run_id = f"paper-review-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    root = _repo_path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    out_dir = root / run_id
    out_dir.mkdir(parents=False, exist_ok=False)
    _write_json(facts, out_dir / "program-facts.json")
    facts_sha = sha256_file(out_dir / "program-facts.json")
    model_input = {
        "schema": "cehuan.paper_ranking_review_input.v1",
        "review_id": run_id,
        "program_facts_sha256": facts_sha,
        "classification": facts["classification"],
        "signal_date": facts["signal_date"],
        "scope": facts["scope"],
        "window_gate": facts["window_gate"],
        "ranking": facts["ranking"],
        "data_capture": facts["data_capture"],
        "limitations": facts["limitations"],
        "evidence_catalog": facts["evidence_catalog"],
        "execution_boundaries": facts["execution_boundaries"],
    }
    _write_json(model_input, out_dir / "model-input.json")
    prompt = build_review_prompt(model_input)
    (out_dir / "model-prompt.txt").write_text(prompt, encoding="utf-8")
    audit: dict[str, Any] = {
        "requested_mode": "llm",
        "selected_mode": "llm",
        "provider": None,
        "model": None,
        "call_count": 0,
        "retry_count": 0,
        "usage": None,
        "cost_status": "UNKNOWN_NO_RELIABLE_RATE",
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "program_facts_sha256": facts_sha,
        "review_started_at_local": now.isoformat(timespec="seconds"),
        "api_key_logged": False,
        "account_data_sent": False,
        "broker_data_sent": False,
    }
    model_review: dict[str, Any] | None = None
    status = "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"
    try:
        settings = _read_settings(agent_config_path or DEFAULT_AGENT_CONFIG)
        model, resolved = resolve_research_model(settings, "llm")
    except ValueError:
        audit["failure_code"] = "MISSING_OR_INVALID_LLM_CONFIGURATION"
        audit["missing_environment"] = {
            env_name: not bool(os.environ.get(env_name))
            for env_name in ("CEHUAN_LLM_MODEL", "CEHUAN_LLM_BASE_URL", "CEHUAN_LLM_API_KEY")
        }
    else:
        audit.update({key: value for key, value in resolved.items() if key not in {"selected_mode", "api_key_env"}})
        audit["provider"] = model.provider
        audit["model"] = model.model
        audit["call_count"] = 1
        try:
            reply: ModelReply = model.generate(
                phase="review", prompt=prompt,
                context={"review_id": run_id, "program_facts_sha256": facts_sha},
            )
        except Exception as exc:
            status = "MODEL_REQUEST_FAILED"
            audit["failure_code"] = "REQUEST_FAILED"
            audit["failure_type"] = type(exc).__name__
        else:
            audit.update({"provider": reply.provider, "model": reply.model, "usage": reply.usage, "successful_response_count": 1})
            try:
                model_review = validate_model_review(reply.content, facts)
            except PaperReviewValidationError as exc:
                status = "MODEL_RESPONSE_JSON_INVALID" if exc.code == "INVALID_JSON" else "MODEL_REVIEW_REJECTED"
                audit.update({"failure_code": exc.code, "failure_field": exc.field})
            else:
                status = "COMPLETED_REAL_LLM_REVIEW"
                _write_json(model_review, out_dir / "model-review.json")

    _write_json({"status": status, **audit}, out_dir / "model-audit.json")
    report_path = out_dir / "research-report.md"
    report_path.write_text(_report_markdown(facts, model_review, status, audit), encoding="utf-8")
    outputs = ["program-facts.json", "model-input.json", "model-prompt.txt", "model-audit.json", "research-report.md"]
    if model_review is not None:
        outputs.append("model-review.json")
    manifest = {
        "schema": "cehuan.paper_ranking_review_run.v1",
        "review_id": run_id,
        "status": status,
        "ranking_run_id": facts["sources"]["ranking_run_manifest"]["path"],
        "program_facts_sha256": facts_sha,
        "model": audit,
        "files": {name: sha256_file(out_dir / name) for name in outputs},
        "formal_baseline": "NOT_READY",
        "returns_computed": False,
        "strategy_modified": False,
        "broker_connected": False,
        "orders_generated": False,
    }
    _write_json(manifest, out_dir / "review-manifest.json")
    return out_dir
