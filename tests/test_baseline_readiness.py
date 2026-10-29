from __future__ import annotations

import hashlib
import json
import copy
from pathlib import Path

import pandas as pd
import pytest

from cehuan.backtest.engine import run_backtest
from cehuan.config import load_config
from cehuan.data.canonical import read_bars
from cehuan.data.security_identity import IDENTITY_EVENT_COLUMNS, sha256_file
from cehuan.data.unknown_types import audit_unknown_security_types
from cehuan.cli.main import main
from cehuan.research.baseline_readiness import (
    build_baseline_readiness,
    readiness_input_hashes,
    require_report_matches_recomputed,
    require_baseline_ready,
)


def _write_inputs(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    data_path = root / "bars.csv"
    dates = ["2025-01-02", "2025-01-03"]
    bars = pd.DataFrame([
        {
            "trade_date": day, "symbol": "SH600000", "open": 10.0, "high": 10.2,
            "low": 9.8, "close": 10.0, "adjusted_close": 10.0,
            "volume_shares": 10000, "is_listed": True, "is_suspended": False,
            "is_st": False, "data_source": "synthetic_readiness_test",
        }
        for day in dates
    ])
    bars.to_csv(data_path, index=False)
    benchmark_path = root / "benchmark.csv"
    benchmark = bars.copy()
    benchmark["symbol"] = "SH000300"
    benchmark.to_csv(benchmark_path, index=False)

    universe = root / "universe"
    universe.mkdir()
    membership = pd.DataFrame([
        {
            "trade_date": day, "symbol": "SH600000", "pool_status": "STOCK_MEMBER",
            "daily_trade_status": "1", "is_st": "False", "basic_status_at_retrieval": "1",
            "basic_out_date": "",
        }
        for day in dates
    ])
    membership.to_csv(universe / "securities_by_date.csv", index=False)
    (universe / "reconciliation.csv").write_text(
        "trade_date,symbol,outDate,discrepancy\n", encoding="utf-8",
    )
    (universe / "manifest.json").write_text(
        json.dumps({"snapshot_id": "synthetic-readiness-test"}), encoding="utf-8",
    )

    identity = root / "identity.csv"
    pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS).to_csv(identity, index=False)
    evidence = root / "out-date-evidence.csv"
    pd.DataFrame(columns=[
        "trade_date", "symbol", "basic_out_date", "confirmed_delisting_date", "evidence_type",
        "evidence_url", "evidence_summary", "announcement_title", "page_reference", "source_sha256",
        "additional_action_required", "review_status", "reviewed_by", "reviewed_at",
    ]).to_csv(evidence, index=False)
    unknown_review = root / "unknown-types.csv"
    audit_unknown_security_types(universe, data_path, unknown_review)
    unknown_summary = unknown_review.with_suffix(".csv.summary.json")

    quality = {
        "schema_version": 1,
        "healthy": True,
        "raw_bar_quality": {"healthy": True},
        "universe_reconciliation_quality": {
            "healthy": True,
            "source_artifact_sha256": sha256_file(universe / "securities_by_date.csv"),
        },
        "data_sha256": sha256_file(data_path),
        "security_identity_events_sha256": sha256_file(identity),
        "universe_snapshot_id": "synthetic-readiness-test",
    }
    quality_path = root / "point-in-time-quality.json"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")

    actions = root / "actions.csv"
    actions.write_text(
        "event_id,event_type,old_symbol,new_symbol,record_date,old_last_trade_date,old_delisting_date,"
        "new_share_registration_date,new_shares_tradable_date,allocation_result_announcement_date,"
        "share_ratio,ratio_basis,integer_allocation_method,allocation_status,evidence_url,evidence_title,"
        "evidence_page_reference,evidence_sha256,old_last_trade_evidence_url,old_last_trade_evidence_title,"
        "old_last_trade_evidence_page_reference,old_last_trade_evidence_sha256,evidence_summary,review_status,"
        "reviewed_by,reviewed_at\n",
        encoding="utf-8",
    )
    action_coverage = root / "action-coverage.csv"
    action_coverage.write_text(
        "symbol,valid_from,valid_to,source,source_sha256,review_status,reviewed_by,reviewed_at\n",
        encoding="utf-8",
    )
    rules = root / "rules.csv"
    rules.write_text(
        "symbol,board,valid_from,valid_to,is_listed,is_suspended,is_st,has_price_limit,price_limit_pct,"
        "buy_min_shares,buy_step_shares,sell_min_shares,sell_step_shares,source,source_sha256,"
        "review_status,reviewed_by,reviewed_at\n",
        encoding="utf-8",
    )
    fees = root / "fees.csv"
    fees.write_text(
        "valid_from,valid_to,market,commission_rate,minimum_commission_cny,stamp_duty_sell_rate,"
        "transfer_fee_rate,source,source_sha256,review_status,reviewed_by,reviewed_at\n",
        encoding="utf-8",
    )
    config = Path(__file__).resolve().parents[1] / "configs" / "default.toml"
    return {
        "data": data_path,
        "benchmark": benchmark_path,
        "universe": universe,
        "quality": quality_path,
        "identity": identity,
        "evidence": evidence,
        "unknown_review": unknown_review,
        "unknown_summary": unknown_summary,
        "actions": actions,
        "action_coverage": action_coverage,
        "rules": rules,
        "fees": fees,
        "config": config,
    }


def _formal_inputs(paths: dict[str, Path]):
    context = {
        "data": paths["data"],
        "point_in_time_quality": paths["quality"],
        "universe": paths["universe"],
        "identity_events": paths["identity"],
        "out_date_evidence": paths["evidence"],
        "unknown_type_review": paths["unknown_review"],
        "unknown_type_summary": paths["unknown_summary"],
        "unknown_type_bars": paths["data"],
        "corporate_actions": paths["actions"],
        "corporate_action_coverage": paths["action_coverage"],
        "market_rules": paths["rules"],
        "fees": paths["fees"],
        "benchmark": paths["benchmark"],
        "config": paths["config"],
    }
    hashes = readiness_input_hashes(**context)
    report = build_baseline_readiness(
        data=read_bars(paths["data"]), input_hashes=hashes,
        universe=paths["universe"], identity_events_path=paths["identity"],
        out_date_evidence_path=paths["evidence"],
        unknown_type_review_path=paths["unknown_review"],
        unknown_type_summary_path=paths["unknown_summary"],
        unknown_type_bars_path=paths["data"],
        point_in_time_quality_path=paths["quality"],
        corporate_actions_path=paths["actions"],
        corporate_action_coverage_path=paths["action_coverage"],
        market_rules_path=paths["rules"], fees_path=paths["fees"],
        benchmark=read_bars(paths["benchmark"]),
        evaluation_start="2025-01-02", evaluation_end="2025-01-03",
    )
    return context, hashes, report


def test_forged_healthy_quality_alone_never_marks_baseline_ready(tmp_path):
    paths = _write_inputs(tmp_path / "ready")
    hashes = readiness_input_hashes(
        data=paths["data"], point_in_time_quality=paths["quality"], universe=paths["universe"],
        identity_events=paths["identity"], out_date_evidence=paths["evidence"],
        unknown_type_review=paths["unknown_review"], unknown_type_summary=paths["unknown_summary"],
        unknown_type_bars=paths["data"],
        corporate_actions=paths["actions"], corporate_action_coverage=paths["action_coverage"],
        market_rules=paths["rules"], fees=paths["fees"], benchmark=paths["benchmark"],
        config=paths["config"],
    )
    quality = json.loads(paths["quality"].read_text(encoding="utf-8"))
    quality.update({"healthy": True, "baseline_ready": True})
    paths["quality"].write_text(json.dumps(quality), encoding="utf-8")
    hashes["point_in_time_quality"] = sha256_file(paths["quality"])
    report = build_baseline_readiness(
        data=pd.read_csv(paths["data"]), input_hashes=hashes, universe=paths["universe"],
        identity_events_path=paths["identity"], out_date_evidence_path=paths["evidence"],
        unknown_type_review_path=paths["unknown_review"], unknown_type_summary_path=paths["unknown_summary"],
        unknown_type_bars_path=paths["data"],
        point_in_time_quality_path=paths["quality"], corporate_actions_path=paths["actions"],
        corporate_action_coverage_path=paths["action_coverage"], market_rules_path=paths["rules"],
        fees_path=paths["fees"], benchmark=pd.read_csv(paths["benchmark"]),
        evaluation_start="2025-01-02", evaluation_end="2025-01-03",
    )
    assert report["ready"] is False
    assert report["decision"] == "NOT_READY"
    assert "CORPORATE_ACTION_COVERAGE_OR_ACCOUNTING_INCOMPLETE" in report["blockers"]
    assert "EFFECTIVE_DATED_MARKET_RULES_INCOMPLETE" in report["blockers"]
    assert "HISTORICAL_FEES_INCOMPLETE_OR_UNREVIEWED" in report["blockers"]
    assert "EXECUTION_ENGINE_DOES_NOT_YET_CONSUME_AUDITED_RULES_FEES_AND_ACTION_LEDGER" in report["blockers"]
    with pytest.raises(ValueError, match="正式基线未就绪"):
        require_baseline_ready(report, hashes, "2025-01-02", "2025-01-03")

    forged = copy.deepcopy(report)
    forged["ready"] = True
    forged["decision"] = "READY"
    forged["blockers"] = []
    forged["checks"]["execution_engine"]["complete"] = True
    with pytest.raises(ValueError, match="重新审计结果不一致"):
        require_report_matches_recomputed(forged, report)


def test_formal_backtest_recomputes_complete_readiness_and_reports_blockers(tmp_path):
    paths = _write_inputs(tmp_path / "formal-complete")
    context, hashes, report = _formal_inputs(paths)

    with pytest.raises(ValueError, match="正式基线未就绪.*EXECUTION_ENGINE_DOES_NOT_YET_CONSUME_AUDITED_RULES_FEES_AND_ACTION_LEDGER"):
        run_backtest(
            read_bars(paths["data"]), 100_000, load_config(paths["config"]),
            benchmark_frame=read_bars(paths["benchmark"]),
            prevalidated_quality=json.loads(paths["quality"].read_text(encoding="utf-8")),
            formal=True, readiness_report=report, readiness_input_hashes=hashes,
            evaluation_start="2025-01-02", evaluation_end="2025-01-03",
            readiness_context=context,
        )


def test_formal_backtest_rejects_incomplete_readiness_context_before_reaudit(tmp_path):
    paths = _write_inputs(tmp_path / "formal-missing-context")
    context, hashes, report = _formal_inputs(paths)
    context.pop("unknown_type_review")

    with pytest.raises(ValueError, match="正式基线缺少 readiness 输入路径：unknown_type_review"):
        run_backtest(
            read_bars(paths["data"]), 100_000, load_config(paths["config"]),
            benchmark_frame=read_bars(paths["benchmark"]),
            formal=True, readiness_report=report, readiness_input_hashes=hashes,
            evaluation_start="2025-01-02", evaluation_end="2025-01-03",
            readiness_context=context,
        )


def test_market_rules_with_missing_required_column_are_rejected(tmp_path):
    paths = _write_inputs(tmp_path / "missing-rule-column")
    paths["rules"].write_text("symbol,board,valid_from\n", encoding="utf-8")
    hashes = readiness_input_hashes(
        data=paths["data"], point_in_time_quality=paths["quality"], universe=paths["universe"],
        identity_events=paths["identity"], out_date_evidence=paths["evidence"],
        unknown_type_review=paths["unknown_review"], unknown_type_summary=paths["unknown_summary"],
        unknown_type_bars=paths["data"],
        corporate_actions=paths["actions"], corporate_action_coverage=paths["action_coverage"],
        market_rules=paths["rules"], fees=paths["fees"], benchmark=paths["benchmark"],
        config=paths["config"],
    )
    with pytest.raises(ValueError, match="有效日期交易规则文件缺少字段"):
        build_baseline_readiness(
            data=pd.read_csv(paths["data"]), input_hashes=hashes, universe=paths["universe"],
            identity_events_path=paths["identity"], out_date_evidence_path=paths["evidence"],
            unknown_type_review_path=paths["unknown_review"], unknown_type_summary_path=paths["unknown_summary"],
            unknown_type_bars_path=paths["data"],
            point_in_time_quality_path=paths["quality"], corporate_actions_path=paths["actions"],
            corporate_action_coverage_path=paths["action_coverage"], market_rules_path=paths["rules"],
            fees_path=paths["fees"], benchmark=pd.read_csv(paths["benchmark"]),
            evaluation_start="2025-01-02", evaluation_end="2025-01-03",
        )


def test_changing_identity_event_file_breaks_quality_hash_binding(tmp_path):
    paths = _write_inputs(tmp_path / "identity-hash")
    with paths["identity"].open("a", encoding="utf-8") as handle:
        handle.write("\n")
    hashes = readiness_input_hashes(
        data=paths["data"], point_in_time_quality=paths["quality"], universe=paths["universe"],
        identity_events=paths["identity"], out_date_evidence=paths["evidence"],
        unknown_type_review=paths["unknown_review"], unknown_type_summary=paths["unknown_summary"],
        unknown_type_bars=paths["data"], corporate_actions=paths["actions"],
        corporate_action_coverage=paths["action_coverage"], market_rules=paths["rules"],
        fees=paths["fees"], benchmark=paths["benchmark"], config=paths["config"],
    )
    report = build_baseline_readiness(
        data=pd.read_csv(paths["data"]), input_hashes=hashes, universe=paths["universe"],
        identity_events_path=paths["identity"], out_date_evidence_path=paths["evidence"],
        unknown_type_review_path=paths["unknown_review"], unknown_type_summary_path=paths["unknown_summary"],
        unknown_type_bars_path=paths["data"], point_in_time_quality_path=paths["quality"],
        corporate_actions_path=paths["actions"], corporate_action_coverage_path=paths["action_coverage"],
        market_rules_path=paths["rules"], fees_path=paths["fees"],
        benchmark=pd.read_csv(paths["benchmark"]),
        evaluation_start="2025-01-02", evaluation_end="2025-01-03",
    )
    assert "POINT_IN_TIME_IDENTITY_EVENTS_HASH_MISMATCH" in report["blockers"]


def test_baseline_readiness_cli_writes_a_blocked_report_without_running_backtest(tmp_path, capsys):
    paths = _write_inputs(tmp_path / "cli")
    output = tmp_path / "cli-readiness.json"
    exit_code = main([
        "baseline-readiness",
        "--data", str(paths["data"]),
        "--universe", str(paths["universe"]),
        "--point-in-time-quality", str(paths["quality"]),
        "--identity-events", str(paths["identity"]),
        "--out-date-evidence", str(paths["evidence"]),
        "--unknown-type-review", str(paths["unknown_review"]),
        "--unknown-type-bars", str(paths["data"]),
        "--corporate-actions", str(paths["actions"]),
        "--corporate-action-coverage", str(paths["action_coverage"]),
        "--market-rules", str(paths["rules"]),
        "--fees", str(paths["fees"]),
        "--benchmark-data", str(paths["benchmark"]),
        "--config", str(paths["config"]),
        "--start", "2025-01-02", "--end", "2025-01-03",
        "--output", str(output),
    ])
    captured = capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert '"decision": "NOT_READY"' in captured.out
    assert report["ready"] is False
    assert "EXECUTION_ENGINE_DOES_NOT_YET_CONSUME_AUDITED_RULES_FEES_AND_ACTION_LEDGER" in report["blockers"]
