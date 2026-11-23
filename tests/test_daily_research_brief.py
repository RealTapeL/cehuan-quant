"""策环测试模块：验证每日研究简报的交易日选择、来源时间、哈希和无数据状态。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.config import load_config
from cehuan.data.calendar import normalize_trading_calendar
from cehuan.research.controlled_experiments import build_experiment_capabilities
from cehuan.research.daily_research_brief import (
    _report_markdown,
    _scan_incomplete_ranking_statuses,
    latest_completed_session,
    run_daily_research_brief,
)
from cehuan.research.research_agent import StubResearchModel, generate_validated_hypothesis


SHANGHAI = ZoneInfo("Asia/Shanghai")
SYNTHETIC_INPUT = Path(__file__).resolve().parents[1] / "examples" / "strategy-research-agent-input.synthetic.json"


def _sha(path: Path) -> str:
    """私有测试辅助函数，用于SHA。 参数：`path`：路径场景输入。 返回：str，供调用处构造夹具或断言复用。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_calendar(path: Path) -> None:
    """私有测试辅助函数，用于写入日历。 参数：`path`：路径场景输入。 返回：None，供调用处构造夹具或断言复用。"""
    frame = pd.DataFrame([
        {"trade_date": "2026-11-20", "is_open": True, "previous_trade_date": "", "source": "synthetic.calendar", "retrieved_at_utc": "2026-11-21T00:00:00Z"},
        {"trade_date": "2026-11-21", "is_open": False, "previous_trade_date": "", "source": "synthetic.calendar", "retrieved_at_utc": "2026-11-21T00:00:00Z"},
        {"trade_date": "2026-11-23", "is_open": True, "previous_trade_date": "2026-11-20", "source": "synthetic.calendar", "retrieved_at_utc": "2026-11-21T00:00:00Z"},
    ])
    frame = normalize_trading_calendar(frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False)
    capture = {
        "status": "SUCCESS",
        "normalized_sha256": _sha(path),
        "provider_publication_evidence": "UNVERIFIED",
        "capture_finished_at_asia_shanghai": "2026-11-21T08:00:00+08:00",
    }
    (path.parent / "capture-manifest.json").write_text(json.dumps(capture), encoding="utf-8")


def test_latest_completed_session_respects_close_and_excludes_future_calendar_days() -> None:
    """验证最新完成交易日遵守收盘与排除未来日历日期。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    frame = normalize_trading_calendar(pd.DataFrame([
        {"trade_date": "2026-11-20", "is_open": True, "source": "test"},
        {"trade_date": "2026-11-23", "is_open": True, "source": "test"},
    ]))
    before_close = datetime(2026, 11, 23, 15, 4, tzinfo=SHANGHAI)
    after_close = datetime(2026, 11, 23, 15, 5, tzinfo=SHANGHAI)
    assert latest_completed_session(frame, before_close) == "2026-11-20"
    assert latest_completed_session(frame, after_close) == "2026-11-23"


def test_incomplete_daily_capture_is_reported_with_hash_and_local_times(tmp_path: Path) -> None:
    """验证不完整每日采集报告带有哈希与本地时间。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    observation = tmp_path / "observation"
    observation.mkdir()
    (observation / "ranking-status.json").write_text(json.dumps({
        "signal_date": "2026-11-19",
        "status": "NEEDS_DATA",
        "blocking_reasons": ["10 security-session rows have unknown listing/ST/suspension state"],
    }), encoding="utf-8")
    (observation / "capture-manifest.json").write_text(json.dumps({
        "capture_started_at_utc": "2026-11-19T07:06:42Z",
        "capture_finished_at_utc": "2026-11-19T07:11:07Z",
    }), encoding="utf-8")

    result = _scan_incomplete_ranking_statuses(tmp_path)

    assert len(result) == 1
    assert result[0]["signal_date"] == "2026-11-19"
    assert result[0]["capture_finished_at_local"] == "2026-11-19T15:11:07+08:00"
    assert result[0]["sha256"] == _sha(observation / "ranking-status.json")
    assert result[0]["capture_manifest_sha256"] == _sha(observation / "capture-manifest.json")


def test_synthetic_agent_and_brief_renderer_are_explicitly_stubbed(tmp_path: Path) -> None:
    """验证合成Agent与简报渲染器明确地使用stub。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    config = load_config()
    payload = json.loads(SYNTHETIC_INPUT.read_text(encoding="utf-8"))
    payload["frozen_strategy"]["config_sha256"] = "a" * 64
    payload["frozen_strategy"]["parameters"] = {
        key: config["strategy"][key]
        for key in ("lookback_sessions", "rebalance_sessions", "min_momentum")
    }
    payload["experiment_capabilities"] = build_experiment_capabilities(config)
    payload_path = tmp_path / "synthetic-agent-input.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    digest = _sha(payload_path)
    hypothesis, audit = generate_validated_hypothesis(
        StubResearchModel(),
        input_payload=payload,
        phase="initial",
        input_data_digest=digest,
        experiment_id="synthetic-daily-brief-test",
        max_retries=0,
    )
    assert hypothesis["status"] == "PROPOSED"
    assert hypothesis["evidence"][0]["evidence_status"] == "SYNTHETIC"
    assert audit["provider"] == "stub"
    assert audit["attempt_count"] == 1

    synthetic_facts = {
        "signal_date": "2026-11-20",
        "scope": {"label": "合成小范围测试池", "symbol_count": 2, "symbols": ["SH600000", "SZ000001"]},
        "window_gate": {"passed_symbol_count": 2},
        "ranking": {"max_positions": 2, "original_count": 2, "h1_count": 2, "original_top5": [], "h1_top5": [], "direct_reason": "合成测试", "excluded_candidates": []},
        "data_capture": {"price_sources": ["synthetic"], "status_source": "synthetic", "local_capture_interval": ["UNKNOWN", "UNKNOWN"], "provider_publication_time": "UNVERIFIED"},
        "limitations": ["仅为合成流程验证，不代表真实行情或策略结论。"],
        "evidence_catalog": {},
    }
    calendar_info = {
        "path": "synthetic-calendar.csv", "sha256": "b" * 64,
        "source": ["synthetic.calendar"], "retrieved_at_utc": ["2026-11-21T00:00:00Z"],
        "provider_publication_time": "UNVERIFIED",
    }
    audit_view = {"selected_mode": "stub", "provider": "stub", "model": audit["model"], "call_count": 0, "cost_status": "NOT_APPLICABLE_STUB"}
    report = _report_markdown(
        status="SYNTHETIC_TEST_ONLY", target_date="2026-11-20", run_time="2026-11-21T09:00:00+08:00",
        calendar_info=calendar_info, facts=synthetic_facts, hypothesis=hypothesis,
        proposal_review={"status": "STUB_FLOW_ONLY_NOT_LLM"}, model_audit=audit_view,
        blocking_reasons=[], latest_ranking=None,
    )
    assert "研究候选简报" in report
    assert "Stub" in report or "stub" in report
    assert "不修改当前策略" in report
    report_path = tmp_path / "synthetic-daily-brief.md"
    report_path.write_text(report, encoding="utf-8")
    assert _sha(report_path) == hashlib.sha256(report.encode("utf-8")).hexdigest()


def test_missing_fresh_rank_writes_needs_data_without_model_request(tmp_path: Path) -> None:
    """验证缺失新鲜排序写入需要数据缺少模型请求。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    calendar_path = tmp_path / "calendar" / "trading-calendar.csv"
    _write_calendar(calendar_path)
    now = datetime(2026, 11, 21, 10, 0, tzinfo=SHANGHAI)
    run_dir = run_daily_research_brief(
        calendar_path=calendar_path,
        ranking_root=tmp_path / "no-rankings",
        output_root=tmp_path / "briefs",
        model_mode="llm",
        config_path=Path(__file__).resolve().parents[1] / "configs" / "default.toml",
        now=now,
    )
    manifest = json.loads((run_dir / "brief-manifest.json").read_text(encoding="utf-8"))
    report = (run_dir / "daily-brief.md").read_text(encoding="utf-8")
    assert manifest["signal_date"] == "2026-11-20"
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["model"]["call_count"] == 0
    assert manifest["orders_generated"] is False
    assert "没有 CAPTURED 的 FORWARD_PAPER 排名" in report
    assert "NEEDS_DATA" in report
