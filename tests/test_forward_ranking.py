from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from cehuan.research.forward_ranking import _build_h1_ranking_comparison, create_paper_ranking_artifact


def _bars_csv(path: Path) -> tuple[Path, str]:
    dates = pd.bdate_range(end="2026-11-05", periods=21).strftime("%Y-%m-%d")
    rows = []
    for symbol, slope in (("SH600001", 0.20), ("SZ000001", 0.10)):
        for index, day in enumerate(dates):
            close = 10.0 + slope * index
            rows.append({
                "trade_date": day, "symbol": symbol, "name": symbol,
                "open": close, "high": close, "low": close, "close": close,
                "adjusted_close": close, "volume_shares": 10000,
                "is_listed": True, "is_suspended": False, "is_st": False,
                "available_at": f"{day}T15:00:00+08:00", "data_source": "synthetic",
                "retrieved_at_utc": f"{day}T07:01:00Z",
            })
    pd.DataFrame(rows).to_csv(path, index=False)
    return path, dates[-1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calendar_and_universe(tmp_path: Path, bars_path: Path, signal_date: str) -> tuple[Path, Path]:
    bars = pd.read_csv(bars_path, dtype=str)
    dates = sorted(bars["trade_date"].unique())
    calendar = tmp_path / "calendar.csv"
    pd.DataFrame({"trade_date": dates, "is_open": True, "source": "synthetic_calendar"}).to_csv(calendar, index=False)
    universe = tmp_path / "universe.csv"
    members = sorted(bars.loc[bars["trade_date"].eq(signal_date), "symbol"].unique())
    pd.DataFrame({
        "trade_date": [signal_date] * len(members), "symbol": members,
        "pool_status": ["STOCK_MEMBER"] * len(members),
    }).to_csv(universe, index=False)
    return calendar, universe


def test_synthetic_paper_ranking_is_generated_and_bound_to_current_inputs(tmp_path: Path) -> None:
    bars, signal_date = _bars_csv(tmp_path / "bars.csv")
    repo = Path(__file__).resolve().parents[1]
    config = repo / "configs/default.toml"
    result = create_paper_ranking_artifact(
        bars_path=bars, config_path=config, signal_date=signal_date,
        output_dir=tmp_path / "paper-ranking", classification="SYNTHETIC",
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    manifest = result["manifest"]
    ranking_file = Path(result["output_dir"]) / "ranking.csv"
    assert manifest["status"] == "CAPTURED"
    assert manifest["classification"] == "SYNTHETIC"
    assert manifest["ranking_file_sha256"] == _sha(ranking_file)
    assert manifest["input_data_sha256"] == _sha(bars)
    assert manifest["config_sha256"] == _sha(config)
    assert manifest["orders_generated"] is False
    assert manifest["strategy_returns_computed"] is False


def test_forward_ranking_without_capture_manifest_creates_needs_data_record(tmp_path: Path) -> None:
    bars, signal_date = _bars_csv(tmp_path / "bars.csv")
    repo = Path(__file__).resolve().parents[1]
    now = datetime(2026, 11, 5, 4, 0, tzinfo=timezone.utc)  # 12:00 Asia/Shanghai
    result = create_paper_ranking_artifact(
        bars_path=bars, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-not-ready", classification="FORWARD_PAPER", now_utc=now,
    )
    manifest = result["manifest"]
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["candidate_count"] == 0
    assert manifest["research_rules_status"] == "RECORDED_FROM_CONFIG_AND_IMPLEMENTATION"
    assert manifest["research_rules"]["h1_filter"]["required_open_session_count"] == 21
    assert manifest["readiness_checks"]["local_capture_time"] == "NOT_AVAILABLE"
    assert manifest["readiness_checks"]["provider_publication_time"] == "UNVERIFIED"
    assert manifest["provider_published_at_utc"] is None
    assert not (Path(result["output_dir"]) / "ranking.csv").exists()
    assert "尚未到收盘数据稳定等待时间" in " ".join(manifest["blocking_reasons"])


def test_forward_ranking_requires_bound_same_day_capture_and_marks_unsigned_time(tmp_path: Path) -> None:
    bars, signal_date = _bars_csv(tmp_path / "bars.csv")
    repo = Path(__file__).resolve().parents[1]
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars),
        "trusted_external_timestamp": "UNVERIFIED",
        "provider_published_at_utc": None,
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars, signal_date)
    result = create_paper_ranking_artifact(
        bars_path=bars, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-fixture", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture,
        trading_calendar_path=calendar, universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    manifest = result["manifest"]
    assert manifest["status"] == "CAPTURED"
    assert manifest["readiness_checks"]["capture_manifest_data_hash"] == "VERIFIED"
    assert manifest["data_trust"] == "LOCAL_CAPTURE_TIME_BOUND_PROVIDER_PUBLICATION_UNVERIFIED"
    assert manifest["trusted_external_timestamp"] == "UNVERIFIED"
    assert manifest["readiness_checks"]["local_capture_time"] == "RECORDED_IN_CAPTURE_MANIFEST"
    assert manifest["provider_publication_evidence"] == "UNVERIFIED"
    assert manifest["provider_published_at_utc"] is None
    assert manifest["independent_holdout"] is False


def test_h1_forward_comparison_excludes_suspension_and_reranks_same_pool() -> None:
    baseline = pd.DataFrame([
        {"signal_date": "2026-11-05", "symbol": "SH600001", "momentum": 0.2, "rank": 1, "selected_target": True},
        {"signal_date": "2026-11-05", "symbol": "SZ000001", "momentum": 0.1, "rank": 2, "selected_target": False},
    ])
    audit = pd.DataFrame([
        {"symbol": "SH600001", "window_status": "COMPLETE_WITH_SUSPENSION"},
        {"symbol": "SZ000001", "window_status": "COMPLETE"},
    ])

    h1, difference, metadata = _build_h1_ranking_comparison(baseline, audit, max_positions=1)

    assert h1["symbol"].tolist() == ["SZ000001"]
    assert h1["rank"].tolist() == [1]
    assert metadata == {
        "top_n_changed": True,
        "removed_from_original_top_n": ["SH600001"],
        "added_to_h1_top_n": ["SZ000001"],
    }
    removed = difference.loc[difference["symbol"].eq("SH600001")].iloc[0]
    assert removed["top_n_change"] == "FILTERED_BY_H1"
    assert removed["direct_reason"] == "21 日窗口含停牌记录"


def test_forward_capture_writes_baseline_h1_and_difference_artifacts(tmp_path: Path) -> None:
    bars_path, signal_date = _bars_csv(tmp_path / "bars.csv")
    bars = pd.read_csv(bars_path)
    suspension_date = sorted(bars["trade_date"].unique())[-2]
    bars.loc[bars["symbol"].eq("SH600001") & bars["trade_date"].eq(suspension_date), "is_suspended"] = True
    bars.to_csv(bars_path, index=False)
    config_source = Path(__file__).resolve().parents[1] / "configs/default.toml"
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_source.read_text(encoding="utf-8").replace("max_positions = 5", "max_positions = 1"), encoding="utf-8")
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars_path),
        "trusted_external_timestamp": "UNVERIFIED",
        "provider_published_at_utc": None,
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars_path, signal_date)

    result = create_paper_ranking_artifact(
        bars_path=bars_path, config_path=config_path, signal_date=signal_date,
        output_dir=tmp_path / "forward-with-h1", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture, trading_calendar_path=calendar,
        universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    out = Path(result["output_dir"])
    manifest = result["manifest"]
    baseline = pd.read_csv(out / "ranking.csv")
    h1 = pd.read_csv(out / "h1-ranking.csv")
    diff = pd.read_csv(out / "ranking-difference.csv")

    assert manifest["status"] == "CAPTURED"
    assert manifest["h1_top_n_changed"] is True
    assert manifest["removed_from_original_top_n"] == ["SH600001"]
    assert manifest["added_to_h1_top_n"] == ["SZ000001"]
    assert baseline.loc[0, "symbol"] == "SH600001"
    assert h1.loc[0, "symbol"] == "SZ000001"
    assert manifest["h1_ranking_file_sha256"] == _sha(out / "h1-ranking.csv")
    assert manifest["ranking_difference_file_sha256"] == _sha(out / "ranking-difference.csv")
    assert diff.loc[diff["symbol"].eq("SH600001"), "direct_reason"].iloc[0] == "21 日窗口含停牌记录"


def test_forward_ranking_rejects_capture_manifest_changed_during_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cehuan.research.forward_ranking as module

    bars, signal_date = _bars_csv(tmp_path / "bars.csv")
    repo = Path(__file__).resolve().parents[1]
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars),
        "trusted_external_timestamp": "UNVERIFIED",
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars, signal_date)
    original_rank = module.rank_precomputed_day

    def mutate_capture(*args: object, **kwargs: object) -> pd.DataFrame:
        capture.write_text(capture.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return original_rank(*args, **kwargs)

    monkeypatch.setattr(module, "rank_precomputed_day", mutate_capture)
    result = create_paper_ranking_artifact(
        bars_path=bars, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-mutated-manifest", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture,
        trading_calendar_path=calendar, universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    manifest = result["manifest"]
    assert manifest["status"] == "FAILED_INPUT_CHANGED_DURING_RUN"
    assert "行情抓取清单在生成排名期间发生变化" in manifest["blocking_reasons"]
    assert not (Path(result["output_dir"]) / "ranking.csv").exists()


def test_forward_ranking_blocks_instead_of_silently_shrinking_pool_on_missing_session(tmp_path: Path) -> None:
    bars_path, signal_date = _bars_csv(tmp_path / "bars.csv")
    bars = pd.read_csv(bars_path)
    missing_date = sorted(bars["trade_date"].unique())[-2]
    bars = bars.loc[~(bars["symbol"].eq("SH600001") & bars["trade_date"].eq(missing_date))]
    bars.to_csv(bars_path, index=False)
    repo = Path(__file__).resolve().parents[1]
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars_path),
        "trusted_external_timestamp": "UNVERIFIED",
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars_path, signal_date)
    result = create_paper_ranking_artifact(
        bars_path=bars_path, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-window-audit", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture, trading_calendar_path=calendar, universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    manifest = result["manifest"]
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["window_scope"]["point_in_time_universe_members"] == 2
    assert manifest["window_scope"]["excluded_symbols"] == 1
    audit = pd.read_csv(Path(result["output_dir"]) / "window-audit.csv")
    missing = audit.loc[audit["symbol"].eq("SH600001")].iloc[0]
    assert missing["window_status"] == "MISSING_BAR"
    assert "SH600001" in " ".join(manifest["blocking_reasons"])
    assert not (Path(result["output_dir"]) / "ranking.csv").exists()
    assert not (Path(result["output_dir"]) / "h1-ranking.csv").exists()


def test_forward_ranking_reports_unknown_status_as_needs_data_for_each_symbol(tmp_path: Path) -> None:
    bars_path, signal_date = _bars_csv(tmp_path / "bars.csv")
    bars = pd.read_csv(bars_path)
    bars["is_suspended"] = pd.NA
    bars.to_csv(bars_path, index=False)
    repo = Path(__file__).resolve().parents[1]
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars_path),
        "trusted_external_timestamp": "UNVERIFIED",
        "provider_published_at_utc": None,
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars_path, signal_date)
    result = create_paper_ranking_artifact(
        bars_path=bars_path, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-unknown-status", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture, trading_calendar_path=calendar, universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    manifest = result["manifest"]
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["candidate_count"] == 0
    assert len(manifest["per_security_window_status"]) == 2
    assert {row["window_status"] for row in manifest["per_security_window_status"]} == {"UNKNOWN_STATUS"}
    assert all("is_suspended" in row["unknown_status_fields"] for row in manifest["per_security_window_status"])
    assert not (Path(result["output_dir"]) / "ranking.csv").exists()


def test_forward_ranking_rejects_bars_on_calendar_closed_date(tmp_path: Path) -> None:
    bars_path, signal_date = _bars_csv(tmp_path / "bars.csv")
    repo = Path(__file__).resolve().parents[1]
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({
        "source_id": "synthetic-provider-fixture",
        "captured_at_utc": "2026-11-05T07:05:00Z",
        "data_file_sha256": _sha(bars_path),
        "trusted_external_timestamp": "UNVERIFIED",
    }), encoding="utf-8")
    calendar, universe = _calendar_and_universe(tmp_path, bars_path, signal_date)
    calendar_data = pd.read_csv(calendar)
    closed_date = sorted(calendar_data["trade_date"].astype(str).tolist())[-2]
    calendar_data.loc[calendar_data["trade_date"].astype(str).eq(closed_date), "is_open"] = False
    calendar_data.to_csv(calendar, index=False)
    result = create_paper_ranking_artifact(
        bars_path=bars_path, config_path=repo / "configs/default.toml", signal_date=signal_date,
        output_dir=tmp_path / "forward-calendar-mismatch", classification="FORWARD_PAPER",
        source_capture_manifest_path=capture, trading_calendar_path=calendar, universe_path=universe,
        now_utc=datetime(2026, 11, 5, 7, 10, tzinfo=timezone.utc),
    )
    assert result["manifest"]["status"] == "FAILED_INPUT"
    assert "标记的休市日" in " ".join(result["manifest"]["blocking_reasons"])
