from __future__ import annotations

import json

from cehuan.research.h1_model_review import (
    ReviewResponseFailure,
    audit_saved_review_quality,
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
    assert summary["removed_from_top5_details"] == [{
        "symbol": "SH603268", "original_rank": 1,
        "reason_codes": ["SUSPENDED_IN_WINDOW"],
        "direct_reason": "21 日窗口含停牌记录",
    }]


def _review_facts() -> dict:
    return {
        "signal_days": [
            {
                "signal_date": "2024-11-05", "top5_changed": True,
                "original_top5": [{"symbol": "SH603268"}, {"symbol": "SZ300489"}],
                "h1_top5": [{"symbol": "SZ002094"}, {"symbol": "SZ002583"}],
                "removed_from_top5": ["SH603268", "SZ300489"],
                "added_to_top5": ["SZ002094", "SZ002583"],
                "removed_from_top5_details": [{"symbol": "SH603268", "reason_codes": ["SUSPENDED_IN_WINDOW"]}],
            },
            {
                "signal_date": "2024-12-03", "top5_changed": True,
                "original_top5": [{"symbol": "SH600889"}, {"symbol": "SZ000833"}],
                "h1_top5": [{"symbol": "SH603955"}, {"symbol": "SZ000833"}],
                "removed_from_top5": ["SH600889"], "added_to_top5": ["SH603955"],
                "removed_from_top5_details": [{"symbol": "SH600889", "reason_codes": ["SUSPENDED_IN_WINDOW"]}],
            },
            {
                "signal_date": "2024-12-31", "top5_changed": False,
                "original_top5": [{"symbol": "SH600001"}], "h1_top5": [{"symbol": "SH600001"}],
                "removed_from_top5": [], "added_to_top5": [], "removed_from_top5_details": [],
            },
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
    interpretations = {
        "2024-11-05": "2024-11-05，SH603268 因 21 日窗口含停牌记录被移出；SZ300489 也被移出，SZ002094 和 SZ002583 补入。",
        "2024-12-03": "2024-12-03，SH600889 因停牌窗口被移出，SH603955 补入；这只说明候选排序变化。",
        "2024-12-31": "2024-12-31，SH600001 仍在 Top-5，名单未变。",
    }
    return {
        "schema": "cehuan.h1_model_review.v1",
        "review_status": "NEEDS_DATA",
        "ranking_effects": [
            {"signal_date": row["signal_date"], "top5_changed": row["top5_changed"],
             "removed_symbols": row["removed_from_top5"], "added_symbols": row["added_to_top5"],
             "interpretation": interpretations[row["signal_date"]],
             "fact_ids": [f"H1_RANK_EFFECT_{row['signal_date'].replace('-', '')}"]}
            for row in facts["signal_days"]
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
    assert payload["ranking_effects"][0]["removed_symbols"] == ["SH603268", "SZ300489"]


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


def test_synthetic_removed_added_fact_conflict_is_rejected() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][0]["removed_symbols"] = ["SH600001"]
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.code == "PROGRAM_FACT_CONFLICT"
        assert exc.field == "ranking_effects[0].removed_symbols"
    else:
        raise AssertionError("模型不能改写证券移入/移出事实")


def test_synthetic_prompt_template_echo_is_rejected_without_relaxing_contract() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][0]["interpretation"] = "根据所引程序事实解释变化或未变化，不声称收益改善。"
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.code == "TEMPLATE_ECHO"
        assert exc.field == "ranking_effects[0].interpretation"
    else:
        raise AssertionError("照抄模板的逐日解释必须被拒绝")


def test_synthetic_unsupported_security_and_date_references_are_rejected() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][0]["interpretation"] = (
        "2024-11-05，SH999999 被移出，SH603268 因停牌移出；SZ300489 被移出，SZ002094 和 SZ002583 补入。"
    )
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.code == "UNSUPPORTED_SECURITY_REFERENCE"
    else:
        raise AssertionError("未出现在该日程序结果中的证券必须被拒绝")


def test_synthetic_claim_that_h1_proves_returns_is_rejected() -> None:
    payload = _valid_review_payload()
    payload["ranking_effects"][0]["interpretation"] = (
        "2024-11-05，SH603268 因 21 日窗口含停牌记录被移出；SZ300489 被移出，"
        "SZ002094 和 SZ002583 补入，这证明了收益提升。"
    )
    try:
        _parse_and_validate_review(json.dumps(payload), _review_facts())
    except ReviewResponseFailure as exc:
        assert exc.code == "UNSUPPORTED_PERFORMANCE_OR_EXECUTION_CLAIM"
    else:
        raise AssertionError("候选排名差异不能被描述为收益提升证据")


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
    assert [item["removed_symbols"] for item in example["ranking_effects"]] == [
        item["removed_from_top5"] for item in facts["signal_days"]
    ]
    assert [item["added_symbols"] for item in example["ranking_effects"]] == [
        item["added_to_top5"] for item in facts["signal_days"]
    ]
    assert "禁止复制此说明文字" in prompt
    assert "直接窗口原因" in prompt
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


def test_saved_previous_review_is_classified_insufficient_when_it_echoes_template() -> None:
    previous = _valid_review_payload()
    for row in previous["ranking_effects"]:
        row.pop("removed_symbols")
        row.pop("added_symbols")
        row["interpretation"] = "根据所引程序事实解释变化或未变化，不声称收益改善。"

    quality = audit_saved_review_quality(previous, _review_facts())

    assert quality["status"] == "INSUFFICIENT"
    assert quality["model_called"] is False
    assert all("TEMPLATE_ECHO" in row["quality_issues"] for row in quality["per_signal_findings"])
    assert all("MISSING_CONCRETE_CHANGE_FIELDS" in row["quality_issues"] for row in quality["per_signal_findings"])
