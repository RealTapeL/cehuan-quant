from __future__ import annotations

from cehuan.research.h1_model_review import (
    _summary_by_signal,
    _validate_review_output,
    evaluate_min_momentum_effect,
)


def test_min_momentum_guard_shows_point_28_survives_threshold_05() -> None:
    result = evaluate_min_momentum_effect(2.8, 0.5)

    assert result["status"] == "DOES_NOT_EXCLUDE_FOCAL_CANDIDATE"
    assert result["eligible_under_proposed_threshold"] is True
    assert result["would_exclude_focal_candidate"] is False
    assert "不识别或证明其他模型推理问题" in result["scope"]


def test_h1_summary_keeps_unknown_suspension_separate_from_non_suspension() -> None:
    import pandas as pd

    frame = pd.DataFrame([
        {
            "scope": "ORIGINAL_CANDIDATE_POOL", "signal_date": "2024-11-05",
            "symbol": "SH603268", "rank": 1, "momentum": 2.8,
            "h1_rank": float("nan"), "h1_status": "EXCLUDED_OR_UNVERIFIED",
            "reasons": "SUSPENDED_IN_WINDOW", "original_top_n": True,
            "top_n_change": "FILTERED_BY_H1",
        },
        {
            "scope": "ORIGINAL_CANDIDATE_POOL", "signal_date": "2024-11-05",
            "symbol": "SH600001", "rank": 2, "momentum": 1.2,
            "h1_rank": 1, "h1_status": "ELIGIBLE_FOR_H1_DIAGNOSTIC",
            "reasons": "", "original_top_n": False,
            "top_n_change": "NEW_TOP_N_MEMBER_AFTER_RERANK",
        },
        {
            "scope": "ORIGINAL_CANDIDATE_POOL", "signal_date": "2024-11-05",
            "symbol": "SZ000001", "rank": 3, "momentum": 0.9,
            "h1_rank": float("nan"), "h1_status": "EXCLUDED_OR_UNVERIFIED",
            "reasons": "SUSPENSION_STATUS_UNKNOWN", "original_top_n": False,
            "top_n_change": "REMAINS_OUTSIDE_TOP_N",
        },
    ])

    [summary] = _summary_by_signal(frame, max_positions=1)

    assert summary["original_candidate_count"] == 3
    assert summary["retained_candidate_count"] == 1
    assert summary["excluded_or_unverified_candidate_count"] == 2
    counts = summary["exclusion_window_counts_by_reason"]
    assert counts["suspended_windows"] == 1
    assert counts["unknown_suspension_windows"] == 1
    assert summary["removed_from_top5"] == ["SH603268"]
    assert summary["added_to_top5"] == ["SH600001"]


def test_model_review_cannot_override_program_calculated_ranking_effect() -> None:
    facts = {
        "signal_days": [
            {"signal_date": "2024-11-05", "top5_changed": True},
            {"signal_date": "2024-12-03", "top5_changed": True},
            {"signal_date": "2024-12-31", "top5_changed": False},
        ],
        "evidence_catalog": {
            "H1_RANK_EFFECT_20241105": {},
            "H1_RANK_EFFECT_20241203": {},
            "H1_RANK_EFFECT_20241231": {},
            "H1_THRESHOLD_GUARD": {},
        },
        "deterministic_threshold_check": evaluate_min_momentum_effect(2.8, 0.5),
    }
    payload = {
        "schema": "cehuan.h1_model_review.v1",
        "review_status": "NEEDS_DATA",
        "ranking_effects": [
            {"signal_date": day, "top5_changed": changed, "interpretation": "观察到名单变化。",
             "fact_ids": [f"H1_RANK_EFFECT_{day.replace('-', '')}"]}
            for day, changed in (("2024-11-05", True), ("2024-12-03", True), ("2024-12-31", False))
        ],
        "focal_threshold_review": {
            "symbol": "SH603268", "candidate_momentum": 2.8, "proposed_min_momentum": 0.5,
            "eligible_under_proposed_threshold": True, "would_exclude_candidate": False,
            "explanation": "候选仍满足阈值。", "fact_ids": ["H1_THRESHOLD_GUARD"],
        },
        "interpretation": "仅说明排名筛选结果。", "evidence_scope": "事后重建的原候选池。",
        "known_limitations": ["时点未核实。"], "claims_not_established": ["没有收益结论。"],
        "recommended_next_experiment": "在前瞻采集中预先定义并验证停牌过滤。",
        "fact_ids": ["H1_RANK_EFFECT_20241105", "H1_RANK_EFFECT_20241203",
                     "H1_RANK_EFFECT_20241231", "H1_THRESHOLD_GUARD"],
    }

    payload["ranking_effects"][0]["top5_changed"] = False
    try:
        _validate_review_output(payload, facts)
    except ValueError as exc:
        assert "与程序结果冲突" in str(exc)
    else:
        raise AssertionError("模型输出不能覆盖程序计算的 Top-5 变化事实")
