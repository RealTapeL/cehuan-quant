from __future__ import annotations

import json

from cehuan.research.h1_model_review import (
    ReviewResponseFailure,
    _build_review_prompt,
    _parse_and_validate_review,
    _request_failure_audit,
    _summary_by_signal,
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


def _review_facts() -> dict:
    return {
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


def _valid_review_payload() -> dict:
    facts = _review_facts()
    return {
        "schema": "cehuan.h1_model_review.v1",
        "review_status": "NEEDS_DATA",
        "ranking_effects": [
            {"signal_date": day, "top5_changed": changed, "interpretation": "观察到名单变化。",
             "fact_ids": [f"H1_RANK_EFFECT_{day.replace('-', '')}"]}
            for day, changed in ((row["signal_date"], row["top5_changed"]) for row in facts["signal_days"])
        ],
        "focal_threshold_review": {
            "symbol": "SH603268", "candidate_momentum": 2.8, "proposed_min_momentum": 0.5,
            "eligible_under_proposed_threshold": True, "would_exclude_candidate": False,
            "explanation": "候选仍满足阈值。", "fact_ids": ["H1_THRESHOLD_GUARD"],
        },
        "interpretation": "仅说明排名筛选结果。", "evidence_scope": "事后重建的原候选池。",
        "known_limitations": ["时点未核实。"], "claims_not_established": ["没有收益结论。"],
        "recommended_next_experiment": "在前瞻采集中预先定义并验证停牌过滤。",
        "fact_ids": sorted(facts["evidence_catalog"]),
    }


def test_synthetic_model_review_response_passes_existing_strict_contract() -> None:
    payload = _parse_and_validate_review(json.dumps(_valid_review_payload()), _review_facts())

    assert payload["review_status"] == "NEEDS_DATA"
    assert len(payload["ranking_effects"]) == 3


def test_synthetic_missing_field_has_safe_location() -> None:
    payload = _valid_review_payload()
    del payload["known_limitations"]
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.stage == "CONTENT_VALIDATION"
        assert exc.code == "TOP_LEVEL_FIELDS_MISMATCH"
        assert exc.field == "$"
    else:
        raise AssertionError("缺少字段必须被拒绝")


def test_synthetic_unknown_evidence_id_is_rejected_at_field_path() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][1]["fact_ids"] = ["MADE_UP_FACT"]
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.stage == "CONTENT_VALIDATION"
        assert exc.code == "UNKNOWN_OR_MISSING_EVIDENCE_ID"
        assert exc.field == "ranking_effects[1].fact_ids"
    else:
        raise AssertionError("未登记的证据 ID 必须被拒绝")


def test_synthetic_fact_conflict_is_rejected_at_conflicting_field() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][0]["top5_changed"] = False
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.stage == "CONTENT_VALIDATION"
        assert exc.code == "PROGRAM_FACT_CONFLICT"
        assert exc.field == "ranking_effects[0].top5_changed"
    else:
        raise AssertionError("模型输出不能覆盖程序计算的排名变化事实")


def test_synthetic_invalid_json_is_separate_from_content_validation() -> None:
    try:
        _parse_and_validate_review('{"schema":', _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.stage == "JSON_PARSE"
        assert exc.code == "INVALID_JSON"
        assert exc.field == "$"
        assert exc.location and exc.location["line"] == 1
    else:
        raise AssertionError("无效 JSON 必须被单独分类")


def test_prompt_example_matches_all_dates_values_and_sorted_fact_ids() -> None:
    facts = _review_facts()
    prompt = _build_review_prompt(facts)
    example_line = next(line for line in prompt.splitlines() if '"ranking_effects"' in line and '"schema"' in line)
    example = json.loads(example_line)

    assert [item["signal_date"] for item in example["ranking_effects"]] == [
        item["signal_date"] for item in facts["signal_days"]
    ]
    assert [item["top5_changed"] for item in example["ranking_effects"]] == [
        item["top5_changed"] for item in facts["signal_days"]
    ]
    assert example["fact_ids"] == sorted(set(example["fact_ids"]))
    assert "顶层 fact_ids 必须排序且去重" in prompt


def test_request_failure_metadata_never_includes_exception_message() -> None:
    safe = _request_failure_audit(RuntimeError("sensitive response text must not be stored"))

    assert safe == {
        "failure_stage": "MODEL_REQUEST",
        "failure_code": "MODEL_ADAPTER_EXCEPTION",
        "failure_field": None,
        "failure_type": "RuntimeError",
    }
    assert "sensitive" not in json.dumps(safe)
