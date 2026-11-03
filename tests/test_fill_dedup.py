from __future__ import annotations

import pandas as pd

from cehuan.review.fill_dedup import audit_cross_file_fills


def _write(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _fill(
    execution_id="", *, trade_date="2026-10-30", symbol="SH600000", side="BUY",
    quantity="100", price="10.00", trade_time="09:31:00", correction_link="", status="FILLED",
    order_id="order-1",
):
    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "time": trade_time,
        "execution": execution_id,
        "original_execution": correction_link,
        "row_state": status,
        "order_id": order_id,
    }


def test_explicit_stable_execution_id_classifies_exact_overlap_and_new_rows(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-1"), _fill("e-2"), _fill("e-3", quantity="200")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report["classification"].tolist() == ["EXACT_DUPLICATE", "NEW", "NEW"]
    assert report.iloc[0]["matched_source_file"] == str(previous.resolve())
    assert report.iloc[0]["matched_row_number"] == 2
    assert report["source_sha256"].str.len().eq(64).all()


def test_same_id_with_changed_quantity_is_a_conflict(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-1", quantity="200")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["classification"] == "CONFLICT"


def test_identical_trade_details_without_id_are_only_possible_duplicates(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill()])
    current = _write(tmp_path / "current.csv", [_fill()])

    report = audit_cross_file_fills([previous], current)

    assert report.iloc[0]["classification"] == "POSSIBLE_DUPLICATE"
    assert report.iloc[0]["matched_row_number"] == 2
    assert "不自动删除" not in report.iloc[0]["reason"]


def test_same_details_with_distinct_execution_ids_remain_distinct_fills(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-2")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["classification"] == "NEW"


def test_multiple_fills_for_one_order_and_partial_file_overlap_are_kept(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1", quantity="40"), _fill("e-2", quantity="60", trade_time="09:32:00")])
    current = _write(tmp_path / "current.csv", [
        _fill("e-2", quantity="60", trade_time="09:32:00"),
        _fill("e-3", quantity="30", trade_time="09:33:00"),
    ])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report["classification"].tolist() == ["EXACT_DUPLICATE", "NEW"]
    assert len(report) == 2


def test_distinct_execution_ids_for_partial_fills_of_same_order_are_new(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1", quantity="40")])
    current = _write(tmp_path / "current.csv", [_fill("e-2", quantity="60", trade_time="09:31:00")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["order_id"] == "order-1"
    assert report.iloc[0]["classification"] == "NEW"


def test_explicit_correction_link_is_separated_from_new_fills(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [
        _fill("e-2", quantity="90", correction_link="e-1", status="CORRECTION"),
    ])

    report = audit_cross_file_fills(
        [previous], current,
        execution_id_column="execution",
        correction_link_column="original_execution",
        status_column="row_state",
    )

    assert report.iloc[0]["classification"] == "CORRECTION_OR_REVERSAL"
    assert report.iloc[0]["matched_row_number"] == 2


def test_two_identical_current_rows_without_id_are_both_flagged_not_collapsed(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill("other")])
    current = _write(tmp_path / "current.csv", [_fill(), _fill()])

    report = audit_cross_file_fills([previous], current)

    assert report["classification"].tolist() == ["POSSIBLE_DUPLICATE", "POSSIBLE_DUPLICATE"]
    assert report["source_row_number"].tolist() == [2, 3]
    assert len(report) == 2


def test_named_execution_column_must_exist_in_all_exports(tmp_path):
    previous = _write(tmp_path / "prior.csv", [_fill()])
    current = _write(tmp_path / "current.csv", [_fill()])

    try:
        audit_cross_file_fills([previous], current, execution_id_column="broker_fill_no")
    except ValueError as exc:
        assert "指定的可选字段不存在" in str(exc)
    else:
        raise AssertionError("expected missing explicit id column to fail")
