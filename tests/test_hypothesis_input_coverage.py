from __future__ import annotations

import pandas as pd

from cehuan.research.hypothesis_input_coverage import compute_hypothesis_input_coverage


def _inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    open_dates = pd.bdate_range("2025-01-06", periods=23).strftime("%Y-%m-%d").tolist()
    signal_day = open_dates[20]
    decision_day = open_dates[21]
    execution_day = open_dates[22]
    open_set = set(open_dates)
    calendar = pd.DataFrame([
        {"trade_date": day, "is_open": day in open_set, "source": "synthetic-calendar"}
        for day in pd.date_range(open_dates[0], execution_day, freq="D").strftime("%Y-%m-%d")
    ])
    rows = []
    for symbol in ("SH600001", "SZ000001"):
        for index, day in enumerate(open_dates[:21]):
            if symbol == "SZ000001" and index == 5:
                continue
            rows.append({
                "trade_date": day, "symbol": symbol, "open": 10, "high": 10, "low": 10,
                "close": 10, "adjusted_close": 10, "volume_shares": 1000, "is_listed": True,
                "is_suspended": False, "is_st": False,
                "available_at": f"{day}T15:00:00+08:00", "data_source": "synthetic-bars",
            })
        for day in (decision_day, execution_day):
            rows.append({
                "trade_date": day, "symbol": symbol, "open": 10, "high": 10, "low": 10,
                "close": 10, "adjusted_close": 10, "volume_shares": 1000, "is_listed": True,
                "is_suspended": False, "is_st": False,
                "available_at": f"{day}T15:00:00+08:00", "data_source": "synthetic-bars",
            })
    bars = pd.DataFrame(rows)
    universe = pd.DataFrame([
        {"trade_date": signal_day, "symbol": symbol, "pool_status": "STOCK_MEMBER", "type_from_latest_basic": "1", "membership_source": "synthetic-list"}
        for symbol in ("SH600001", "SZ000001")
    ] + [
        {"trade_date": signal_day, "symbol": "SH000001", "pool_status": "UNKNOWN_TYPE", "type_from_latest_basic": "", "membership_source": "synthetic-list"}
    ])
    candidates = pd.DataFrame([
        {"signal_date": signal_day, "symbol": "SH600001", "rank": 1, "momentum": 0.2, "selected_target": "True"},
        {"signal_date": signal_day, "symbol": "SZ000001", "rank": 2, "momentum": 0.1, "selected_target": "True"},
    ])
    return bars, universe, calendar, candidates


def test_hypothesis_coverage_separates_full_pool_candidates_and_timing_gaps() -> None:
    bars, universe, calendar, candidates = _inputs()
    report, h1, h2 = compute_hypothesis_input_coverage(
        bars, universe, calendar, candidates,
        config={"costs": {"commission_rate": 0.0003}, "market": {"buy_lot_shares": 100}},
        created_at_utc="2026-11-04T00:00:00+00:00",
    )
    summary = report["h1"]["summary_by_signal_and_scope"]
    full = next(row for row in summary if row["scope"] == "FULL_STOCK_MEMBER_POOL")
    candidate = next(row for row in summary if row["scope"] == "ORIGINAL_CANDIDATE_POOL")
    assert full["universe_or_candidate_symbols"] == 2
    assert full["unknown_type_member_rows_excluded_from_stock_pool"] == 1
    assert full["fully_covered_valid_price_non_suspended_windows"] == 1
    assert candidate["universe_or_candidate_symbols"] == 2
    assert candidate["candidate_symbols_unique"] is True
    assert candidate["fully_covered_valid_price_non_suspended_windows"] == 1
    h1_rank = report["h1"]["candidate_rank_diagnostics_by_signal"][0]
    assert h1_rank["candidate_rows"] == 2
    assert h1_rank["h1_retained_candidate_rows"] == 1
    assert h1_rank["original_top_n_count"] == 2
    assert h1_rank["original_top_n_retained"] == 1
    candidate_detail = h1.loc[h1.scope.eq("ORIGINAL_CANDIDATE_POOL")]
    assert set(candidate_detail.top_n_change) == {"RETAINED_IN_TOP_N", "FILTERED_BY_H1"}
    assert set(h1.loc[h1.symbol.eq("SZ000001"), "reasons"].unique()) == {"MISSING_BAR_SESSIONS"}
    assert len(h2) == 2
    assert report["h2"]["summary_by_signal"][0]["candidate_symbols_unique"] is True
    assert h2.decision_open_price_present.all()
    assert not h2.decision_recorded_available_by_0931.any()
    assert not h2.execution_recorded_available_by_0931.any()
    assert not h2.decision_status_available_at_separate.any()
    assert not h2.decision_market_rule_complete.any()
    assert report["h2"]["ranking_availability_status"] == "UNVERIFIED_RECONSTRUCTED_AFTER_RUN"
    assert report["rules_fees_and_status"]["historical_fee_schedule"] == "NOT_PROVIDED_TO_COVERAGE_FUNCTION"
    assert report["rules_fees_and_status"]["market_rule_schedule"] == "NOT_PROVIDED_TO_COVERAGE_FUNCTION"
    assert report["interpretation"]["formal_baseline"] == "NOT_READY"
    assert report["interpretation"]["can_continue"] is False
    assert report["interpretation"]["strategy_returns_computed"] is False


def test_hypothesis_coverage_rejects_uncovered_future_open_sessions() -> None:
    bars, universe, calendar, candidates = _inputs()
    calendar = calendar.loc[calendar.trade_date.ne(pd.bdate_range("2025-01-06", periods=23)[-1].strftime("%Y-%m-%d"))]
    try:
        compute_hypothesis_input_coverage(bars, universe, calendar, candidates)
    except ValueError as exc:
        assert "日历" in str(exc) or "开市日" in str(exc)
    else:
        raise AssertionError("预检未拒绝缺少执行日历的输入")


def test_h1_coverage_marks_invalid_price_rows() -> None:
    bars, universe, calendar, candidates = _inputs()
    signal_day = candidates.signal_date.iloc[0]
    bars.loc[
        bars.symbol.eq("SH600001") & bars.trade_date.eq(signal_day), "adjusted_close",
    ] = 0
    _, h1, _ = compute_hypothesis_input_coverage(bars, universe, calendar, candidates)
    row = h1.loc[
        h1.scope.eq("ORIGINAL_CANDIDATE_POOL") & h1.symbol.eq("SH600001")
    ].iloc[0]
    assert row.invalid_adjusted_close_rows == 1
    assert row.h1_status == "EXCLUDED_OR_UNVERIFIED"
    assert "INVALID_ADJUSTED_CLOSE_IN_WINDOW" in row.reasons


def test_h1_candidate_filter_promotes_next_rank_without_performance_claim() -> None:
    bars, universe, calendar, candidates = _inputs()
    signal_day = candidates.signal_date.iloc[0]
    third_symbol = "SH600002"
    source_rows = bars.loc[bars.symbol.eq("SH600001") & bars.trade_date.le(signal_day)].copy()
    source_rows["symbol"] = third_symbol
    bars = pd.concat([bars, source_rows], ignore_index=True)
    candidates = pd.DataFrame([
        {"signal_date": signal_day, "symbol": "SZ000001", "rank": 1, "momentum": 0.3, "selected_target": "True"},
        {"signal_date": signal_day, "symbol": "SH600001", "rank": 2, "momentum": 0.2, "selected_target": "True"},
        {"signal_date": signal_day, "symbol": third_symbol, "rank": 3, "momentum": 0.1, "selected_target": "False"},
    ])
    report, detail, _ = compute_hypothesis_input_coverage(
        bars, universe, calendar, candidates,
        config={"strategy": {"max_positions": 2}},
    )
    summary = report["h1"]["candidate_rank_diagnostics_by_signal"][0]
    assert summary["original_top_n_count"] == 2
    assert summary["h1_retained_candidate_rows"] == 2
    assert summary["new_top_n_members_after_rerank"] == 1
    assert summary["original_top_n_dropped_after_filter"] == 1
    promoted = detail.loc[detail.symbol.eq(third_symbol)].iloc[0]
    assert promoted.h1_rank == 2
    assert promoted.top_n_change == "NEW_TOP_N_MEMBER_AFTER_RERANK"
    assert report["h1"]["result_type"] == "DEVELOPMENT_DIAGNOSTIC"
    assert report["interpretation"]["strategy_returns_computed"] is False


def test_h1_candidate_filter_promotes_next_rank_without_performance_claim() -> None:
    bars, universe, calendar, candidates = _inputs()
    signal_day = candidates.signal_date.iloc[0]
    third_symbol = "SH600002"
    source_rows = bars.loc[bars.symbol.eq("SH600001") & bars.trade_date.le(signal_day)].copy()
    source_rows["symbol"] = third_symbol
    bars = pd.concat([bars, source_rows], ignore_index=True)
    candidates = pd.DataFrame([
        {"signal_date": signal_day, "symbol": "SZ000001", "rank": 1, "momentum": 0.3, "selected_target": "True"},
        {"signal_date": signal_day, "symbol": "SH600001", "rank": 2, "momentum": 0.2, "selected_target": "True"},
        {"signal_date": signal_day, "symbol": third_symbol, "rank": 3, "momentum": 0.1, "selected_target": "False"},
    ])
    report, detail, _ = compute_hypothesis_input_coverage(
        bars, universe, calendar, candidates,
        config={"strategy": {"max_positions": 2}},
    )
    summary = report["h1"]["candidate_rank_diagnostics_by_signal"][0]
    assert summary["original_top_n_count"] == 2
    assert summary["h1_retained_candidate_rows"] == 2
    assert summary["new_top_n_members_after_rerank"] == 1
    assert summary["original_top_n_dropped_after_filter"] == 1
    promoted = detail.loc[detail.symbol.eq(third_symbol)].iloc[0]
    assert promoted.h1_rank == 2
    assert promoted.top_n_change == "NEW_TOP_N_MEMBER_AFTER_RERANK"
    assert report["h1"]["result_type"] == "DEVELOPMENT_DIAGNOSTIC"
    assert report["interpretation"]["strategy_returns_computed"] is False
