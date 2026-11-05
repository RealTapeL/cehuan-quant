from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from cehuan.research.forward_ranking import create_paper_ranking_artifact


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
    assert manifest["independent_holdout"] is False


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


def test_forward_ranking_excludes_security_with_missing_session_even_when_other_symbol_has_bar(tmp_path: Path) -> None:
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
    assert manifest["status"] == "CAPTURED_WITH_EXCLUSIONS"
    assert manifest["window_scope"]["point_in_time_universe_members"] == 2
    assert manifest["window_scope"]["excluded_symbols"] == 1
    audit = pd.read_csv(Path(result["output_dir"]) / "window-audit.csv")
    missing = audit.loc[audit["symbol"].eq("SH600001")].iloc[0]
    assert missing["window_status"] == "MISSING_BAR"
    ranking = pd.read_csv(Path(result["output_dir"]) / "ranking.csv")
    assert "SH600001" not in set(ranking["symbol"])


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
