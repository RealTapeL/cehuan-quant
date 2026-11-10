from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from cehuan.research.paper_ranking_review import (
    REQUIRED_LIMITATIONS,
    PaperReviewValidationError,
    _report_markdown,
    build_paper_ranking_facts,
    build_review_prompt,
    run_paper_ranking_review,
    validate_model_review,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, pd.DataFrame):
        value.to_csv(path, index=False)
    elif isinstance(value, dict):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")


def _sealed_fixture(root: Path, *, h1_changed: bool = False) -> tuple[Path, Path]:
    run_dir = root / "rankings" / "paper-fixture"
    inputs = root / "inputs"
    pool_dir = inputs / "pool"
    symbols = ["SH600000", "SZ000001", "SH600001"] if h1_changed else ["SH600000", "SZ000001"]
    original_rows = [
        {"signal_date": "2026-11-06", "symbol": symbol, "momentum": momentum, "rank": index, "selected_target": True}
        for index, (symbol, momentum) in enumerate(zip(symbols, [0.05, 0.02, 0.01]), start=1)
    ]
    ranking = pd.DataFrame(original_rows)
    window = pd.DataFrame([
        {"symbol": symbol, "signal_date": "2026-11-06", "expected_session_count": 2,
         "window_status": "COMPLETE_WITH_SUSPENSION" if h1_changed and symbol == "SH600000" else "COMPLETE",
         "included_in_ranking_scope": True}
        for symbol in symbols
    ])
    diff = pd.DataFrame([
        {"signal_date": "2026-11-06", "symbol": row.symbol,
         "window_status": "COMPLETE_WITH_SUSPENSION" if h1_changed and row.symbol == "SH600000" else "COMPLETE",
         "h1_eligible": not (h1_changed and row.symbol == "SH600000"),
         "h1_rank": None if h1_changed and row.symbol == "SH600000" else (row.rank - 1 if h1_changed else row.rank),
         "top_n_change": "FILTERED_BY_H1" if h1_changed and row.symbol == "SH600000" else "REMAINS_OUTSIDE_TOP_N",
         "direct_reason": "21 日窗口含停牌记录" if h1_changed and row.symbol == "SH600000" else "未因 H1 停牌筛选排除"}
        for row in ranking.itertuples()
    ])
    h1_ranking = ranking.loc[~ranking["symbol"].eq("SH600000")].copy() if h1_changed else ranking.copy()
    if h1_changed:
        h1_ranking["rank"] = range(1, len(h1_ranking) + 1)
    bars = pd.DataFrame([
        {"symbol": symbol, "trade_date": day, "adjusted_close": close}
        for index, symbol in enumerate(symbols)
        for day, close in (("2026-11-05", 10.0 + index), ("2026-11-06", 10.5 + index))
    ])
    calendar = pd.DataFrame([{"trade_date": "2026-11-05"}, {"trade_date": "2026-11-06"}])
    universe = pd.DataFrame([{"symbol": symbol, "membership_status": "STOCK_MEMBER"} for symbol in symbols])
    selected_pool = pd.DataFrame([{"symbol": symbol, "name": symbol} for symbol in symbols])
    config_path = inputs / "default.toml"
    bars_path, calendar_path, universe_path = inputs / "bars.csv", inputs / "calendar.csv", pool_dir / "point_in_time_universe.csv"
    _write(config_path, "[strategy]\nmax_positions=2\n")
    _write(bars_path, bars)
    _write(calendar_path, calendar)
    _write(universe_path, universe)
    _write(pool_dir / "selected_pool.csv", selected_pool)
    _write(pool_dir / "capture-manifest.json", {"selection_rule": "固定代码顺序合成测试池"})

    capture_path = inputs / "capture-manifest.json"
    status_requests = [
        {"trade_date": "2026-11-06", "symbol": symbol, "tradestatus": "1", "isST": "0",
         "requested_at_utc": "2026-11-06T09:00:00Z", "received_at_utc": "2026-11-06T09:00:01Z"}
        for symbol in symbols
    ]
    status_path = inputs / "status.csv"
    status_frame = pd.DataFrame([
        {"trade_date": row["trade_date"], "symbol": row["symbol"],
         "tradestatus": row["tradestatus"], "isST": row["isST"]}
        for row in status_requests
    ])
    _write(status_path, status_frame)
    price_path = inputs / "SH600000-tx-hfq.csv"
    price_frame = bars.loc[bars["symbol"].eq("SH600000"), ["trade_date", "adjusted_close"]].rename(
        columns={"trade_date": "date", "adjusted_close": "close"}
    )
    _write(price_path, price_frame)
    price_requests = [{
        "symbol": "SH600000", "source": "akshare.stock_zh_a_hist_tx", "adjust": "hfq",
        "requested_at_utc": "2026-11-06T09:01:00Z", "received_at_utc": "2026-11-06T09:01:01Z",
        "file": price_path.name,
    }]
    _write(capture_path, {
        "source_id": "BaoStock status + AKShare Tencent price",
        "data_file_sha256": _sha(bars_path),
        "status_file_sha256": _sha(status_path),
        "price_files_sha256": {price_path.name: _sha(price_path)},
        "capture_started_at_utc": "2026-11-06T09:00:00Z",
        "capture_finished_at_utc": "2026-11-06T09:01:01Z",
        "status_requests": status_requests,
        "price_requests": price_requests,
    })

    run_dir.mkdir(parents=True)
    for filename, frame in (("ranking.csv", ranking), ("h1-ranking.csv", h1_ranking),
                            ("ranking-difference.csv", diff), ("window-audit.csv", window)):
        _write(run_dir / filename, frame)
    finished_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    manifest = {
        "schema_version": 1, "record_type": "PAPER_RANKING_RUN", "classification": "FORWARD_PAPER",
        "status": "CAPTURED", "paper_run_id": "paper-fixture", "signal_date": "2026-11-06",
        "input_paths": {"bars": str(bars_path), "config": str(config_path), "trading_calendar": str(calendar_path),
                        "point_in_time_universe": str(universe_path), "source_capture_manifest": str(capture_path)},
        "input_data_sha256": _sha(bars_path), "config_sha256": _sha(config_path),
        "trading_calendar_sha256": _sha(calendar_path), "universe_sha256": _sha(universe_path),
        "source_capture_manifest_sha256": _sha(capture_path),
        "ranking_file_sha256": _sha(run_dir / "ranking.csv"),
        "h1_ranking_file_sha256": _sha(run_dir / "h1-ranking.csv"),
        "ranking_difference_file_sha256": _sha(run_dir / "ranking-difference.csv"),
        "window_audit_sha256": _sha(run_dir / "window-audit.csv"),
        "candidate_count": len(ranking), "h1_candidate_count": len(h1_ranking), "h1_top_n_changed": h1_changed,
        "removed_from_original_top_n": ["SH600000"] if h1_changed else [],
        "added_to_h1_top_n": ["SH600001"] if h1_changed else [],
        "strategy_config": {"max_positions": 2},
        "window_scope": {"point_in_time_universe_members": len(symbols), "complete_symbols": len(symbols),
                         "excluded_symbols": 0, "suspended_window_symbols": int(h1_changed)},
        "ranking_generated_at_utc": finished_at.isoformat(), "finished_at_utc": finished_at.isoformat(),
        "formal_baseline": "NOT_READY",
    }
    _write(run_dir / "paper-ranking-manifest.json", manifest)
    report_rows = "".join(
        f"| {row.rank} | {row.symbol} | {row.momentum * 100:.3f}% |\n"
        for row in ranking.itertuples(index=False)
    )
    report = (
        "# 2026-11-06 纸面候选\n\n## 排名\n\n| 排名 | 代码 | 动量 |\n|---:|---|---:|\n"
        + report_rows
        + ("\n原排名与 H1 排名不同。\n" if h1_changed else "\n原排名与 H1 排名相同。\n")
        + f"\n{len(symbols)} 只研究证券的窗口检查均通过。\n\n## 输入\n\n未计算收益；正式基线 NOT_READY。\n"
    )
    _write(run_dir / "research-report.md", report)
    return run_dir, capture_path


def _valid_review(facts: dict) -> dict:
    symbols = [row["symbol"] for row in facts["ranking"]["original_top5"]]
    return {
        "schema": "cehuan.paper_ranking_review.v1",
        "review_status": "REVIEWED",
        "signal_date": facts["signal_date"],
        "original_top5": symbols,
        "h1_top5": [row["symbol"] for row in facts["ranking"]["h1_top5"]],
        "h1_unchanged": facts["ranking"]["unchanged"],
        "window_passed_count": facts["window_gate"]["passed_symbol_count"],
        "ranked_count": facts["ranking"]["original_count"],
        "ranking_explanation": (
            f"窗口通过{facts['window_gate']['passed_symbol_count']}只，原排名{facts['ranking']['original_count']}条；"
            "本次没有证券被 H1 排除，原排名与 H1 排名顺序一致。"
            if facts["ranking"]["unchanged"] else
            f"窗口通过{facts['window_gate']['passed_symbol_count']}只，原排名{facts['ranking']['original_count']}条；"
            "H1 排除了 SH600000，因为其 21 日窗口含停牌记录；候选随后按规则重新排名，Top-N 成分发生变化。"
        ),
        "recorded_facts_vs_interpretation": "程序事实是排名文件和窗口审计中的记录；对筛选效果的说明属于基于这些记录的解释。",
        "what_ranking_establishes": "它展示这个小范围池在指定信号日按动量字段得到的排名。",
        "what_it_cannot_show": [
            "小范围池不代表全市场。",
            "H1 排名变化或未变化都不能证明 H1 规则有效，也不能证明收益改善。",
            "动量不是策略收益，也不证明可以成交。",
            "供应商发布时间未核实；市场规则、费用和公司行动覆盖尚未完整验证。",
        ],
        "next_observation": "下一次记录同一研究池的状态快照来源时间和状态字段，以便检查重复日排名是否可复核。",
        "limitations_acknowledged": {key: True for key in REQUIRED_LIMITATIONS},
        "evidence_fact_ids": ["RANKING_ORIGINAL", "RANKING_H1", "RANKING_DIFFERENCE", "WINDOW_AUDIT", "DATA_CAPTURE", "LIMITATIONS"],
    }


def test_build_facts_verifies_sealed_rankings_and_distinguishes_window_and_top_n(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)

    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)

    assert facts["window_gate"]["passed_symbol_count"] == 2
    assert facts["ranking"]["original_count"] == 2
    assert facts["ranking"]["unchanged"] is True
    assert facts["sources"]["ranking_report_created_after_run_manifest"] is True


def test_build_facts_and_review_support_h1_exclusion_and_top_n_replacement(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path, h1_changed=True)

    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)

    assert facts["ranking"]["unchanged"] is False
    assert facts["ranking"]["original_symbols"] == ["SH600000", "SZ000001", "SH600001"]
    assert facts["ranking"]["h1_symbols"] == ["SZ000001", "SH600001"]
    assert facts["ranking"]["removed_from_top_n"] == ["SH600000"]
    assert facts["ranking"]["added_to_top_n"] == ["SH600001"]
    assert facts["ranking"]["excluded_candidates"] == [{
        "symbol": "SH600000",
        "window_status": "COMPLETE_WITH_SUSPENSION",
        "direct_reason": "21 日窗口含停牌记录",
    }]

    result = validate_model_review(json.dumps(_valid_review(facts), ensure_ascii=False), facts)

    assert result["h1_unchanged"] is False


def test_build_facts_rejects_changed_ranking_bytes(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    with (run_dir / "h1-ranking.csv").open("a", encoding="utf-8") as handle:
        handle.write(" ")

    with pytest.raises(ValueError, match="H1 排名 SHA-256"):
        build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)


def test_review_response_accepts_consistent_synthetic_response(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)

    result = validate_model_review(json.dumps(_valid_review(facts), ensure_ascii=False), facts)

    assert result["review_status"] == "REVIEWED"


def test_review_response_accepts_equivalent_no_suspension_explanation(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    passed = facts["window_gate"]["passed_symbol_count"]
    ranked = facts["ranking"]["original_count"]
    payload["ranking_explanation"] = (
        f"{passed}只证券的窗口均完整，{ranked}条原排名保持原有顺序；观察窗口未发现停牌记录，"
        "所以 H1 筛选没有剔除候选。"
    )

    result = validate_model_review(json.dumps(payload, ensure_ascii=False), facts)

    assert result["review_status"] == "REVIEWED"


def test_review_response_accepts_equivalent_exclusion_wording(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    passed = facts["window_gate"]["passed_symbol_count"]
    ranked = facts["ranking"]["original_count"]
    payload["ranking_explanation"] = (
        f"{passed}只窗口通过，原排名共{ranked}条且与H1结果完全相同；筛选未触发，所有原候选均保留。"
    )

    result = validate_model_review(json.dumps(payload, ensure_ascii=False), facts)

    assert result["review_status"] == "REVIEWED"


def test_review_response_error_identifies_missing_direct_reason(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload["ranking_explanation"] = (
        f"{facts['window_gate']['passed_symbol_count']}只窗口通过，"
        f"{facts['ranking']['original_count']}条原排名与H1排名保持相同。"
    )

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == "RANKING_NO_EXCLUSION_REASON_MISSING"


def test_review_response_rejects_unchanged_claim_without_direct_reason(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload["ranking_explanation"] = (
        f"{facts['window_gate']['passed_symbol_count']}只窗口通过检查，"
        f"{facts['ranking']['original_count']}条排名结果与 H1 排名保持相同。"
    )

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == "RANKING_NO_EXCLUSION_REASON_MISSING"


def test_rejected_model_report_states_that_request_was_sent(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    audit = {
        "review_started_at_local": "2026-11-09T10:52:49+08:00",
        "provider": "openai-compatible-chat-completions",
        "model": "test-model",
        "call_count": 1,
        "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
        "cost_status": "UNKNOWN_NO_RELIABLE_RATE",
        "failure_code": "RANKING_NO_EXCLUSION_REASON_MISSING",
        "failure_field": "ranking_explanation",
    }

    report = _report_markdown(facts, None, "MODEL_REVIEW_REJECTED", audit)

    assert "真实模型请求已返回" in report
    assert "没有发起模型请求" not in report
    assert "RANKING_NO_EXCLUSION_REASON_MISSING" in report


@pytest.mark.parametrize("field,value,code", [
    ("ranked_count", 10, "PROGRAM_FACT_CONFLICT"),
    ("h1_top5", ["SH600000", "SH999999"], "PROGRAM_FACT_CONFLICT"),
    ("limitations_acknowledged", {key: False for key in REQUIRED_LIMITATIONS}, "LIMITATIONS_NOT_ACKNOWLEDGED"),
    ("evidence_fact_ids", ["RANKING_ORIGINAL"], "UNKNOWN_OR_MISSING_EVIDENCE_ID"),
])
def test_review_response_rejects_fact_or_boundary_conflict(tmp_path: Path, field: str, value: object, code: str) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload[field] = value

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == code


def test_review_response_rejects_unknown_security_and_actionable_trade_language(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload["next_observation"] = "下一步买入 SH999999。"

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == "UNSUPPORTED_SECURITY_REFERENCE"


def test_review_response_rejects_positive_performance_claim(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload["what_ranking_establishes"] = "这证明策略收益提升。"

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == "UNSUPPORTED_PERFORMANCE_CLAIM"


def test_review_response_rejects_prompt_placeholder_copy(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    payload = _valid_review(facts)
    payload["ranking_explanation"] = "说明本次排名是否变化，并引用实际变化的证券和原因。"

    with pytest.raises(PaperReviewValidationError) as error:
        validate_model_review(json.dumps(payload, ensure_ascii=False), facts)
    assert error.value.code == "MODEL_COPIED_TEMPLATE"


def test_llm_mode_without_environment_does_not_fallback_or_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    for name in ("CEHUAN_LLM_MODEL", "CEHUAN_LLM_BASE_URL", "CEHUAN_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    output_dir = run_paper_ranking_review(
        ranking_dir=run_dir,
        capture_manifest_path=capture_path,
        output_root=tmp_path / "outputs",
    )
    audit = json.loads((output_dir / "model-audit.json").read_text(encoding="utf-8"))
    report = (output_dir / "research-report.md").read_text(encoding="utf-8")

    assert audit["status"] == "BLOCKED_CONFIGURATION_MISSING_NO_REQUEST"
    assert audit["call_count"] == 0
    assert audit["selected_mode"] == "llm"
    assert all(audit["missing_environment"].values())
    assert "没有回退 stub" in report



def test_review_prompt_requires_explicit_no_exclusion_reason_only_when_true(tmp_path: Path) -> None:
    run_dir, capture_path = _sealed_fixture(tmp_path)
    facts = build_paper_ranking_facts(ranking_dir=run_dir, capture_manifest_path=capture_path)
    prompt = build_review_prompt(facts)
    assert "候选全部保留" in prompt
    changed_dir, changed_capture = _sealed_fixture(tmp_path / "changed", h1_changed=True)
    changed = build_paper_ranking_facts(ranking_dir=changed_dir, capture_manifest_path=changed_capture)
    assert "ranking_explanation 必须明确写出「候选全部保留」" not in build_review_prompt(changed)
