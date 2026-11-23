"""策环测试模块：验证成交跨文件去重分类，确保缺少稳定编号时不自动合并记录。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

import pandas as pd

from cehuan.review.fill_dedup import audit_cross_file_fills


def _write(path, rows):
    """私有测试辅助函数，用于写入。 参数：`path`：路径场景输入；`rows`：行场景输入。 返回值供调用处构造夹具或断言复用。"""
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _fill(
    execution_id="", *, trade_date="2026-10-30", symbol="SH600000", side="BUY",
    quantity="100", price="10.00", trade_time="09:31:00", correction_link="", status="FILLED",
    order_id="order-1",
):
    """私有测试辅助函数，用于成交。 参数：`execution_id`：成交编号场景输入；`trade_date`：交易日期场景输入；`symbol`：证券代码场景输入；`side`：代码标识“side”（含义待核实）场景输入；`quantity`：数量场景输入；`price`：价格场景输入；`trade_time`：交易时间场景输入；`correction_link`：更正关联场景输入；`status`：状态场景输入；`order_id`：订单编号场景输入。 返回值供调用处构造夹具或断言复用。"""
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
    """验证明确稳定成交编号分类完全一致重叠与新行。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-1"), _fill("e-2"), _fill("e-3", quantity="200")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report["classification"].tolist() == ["EXACT_DUPLICATE", "NEW", "NEW"]
    assert report.iloc[0]["matched_source_file"] == str(previous.resolve())
    assert report.iloc[0]["matched_row_number"] == 2
    assert report["source_sha256"].str.len().eq(64).all()


def test_same_id_with_changed_quantity_is_a_conflict(tmp_path):
    """验证相同编号带有变更数量冲突。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-1", quantity="200")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["classification"] == "CONFLICT"


def test_identical_trade_details_without_id_are_only_possible_duplicates(tmp_path):
    """验证一致交易明细缺少编号仅可能重复。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill()])
    current = _write(tmp_path / "current.csv", [_fill()])

    report = audit_cross_file_fills([previous], current)

    assert report.iloc[0]["classification"] == "POSSIBLE_DUPLICATE"
    assert report.iloc[0]["matched_row_number"] == 2
    assert "不自动删除" not in report.iloc[0]["reason"]


def test_same_details_with_distinct_execution_ids_remain_distinct_fills(tmp_path):
    """验证相同明细带有不同成交编号保持不同成交。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("e-1")])
    current = _write(tmp_path / "current.csv", [_fill("e-2")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["classification"] == "NEW"


def test_multiple_fills_for_one_order_and_partial_file_overlap_are_kept(tmp_path):
    """验证多个成交针对一项订单与部分文件重叠保留。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("e-1", quantity="40"), _fill("e-2", quantity="60", trade_time="09:32:00")])
    current = _write(tmp_path / "current.csv", [
        _fill("e-2", quantity="60", trade_time="09:32:00"),
        _fill("e-3", quantity="30", trade_time="09:33:00"),
    ])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report["classification"].tolist() == ["EXACT_DUPLICATE", "NEW"]
    assert len(report) == 2


def test_distinct_execution_ids_for_partial_fills_of_same_order_are_new(tmp_path):
    """验证不同成交编号针对部分成交的相同订单新。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("e-1", quantity="40")])
    current = _write(tmp_path / "current.csv", [_fill("e-2", quantity="60", trade_time="09:31:00")])

    report = audit_cross_file_fills([previous], current, execution_id_column="execution")

    assert report.iloc[0]["order_id"] == "order-1"
    assert report.iloc[0]["classification"] == "NEW"


def test_explicit_correction_link_is_separated_from_new_fills(tmp_path):
    """验证明确更正关联分开从新成交。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
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
    """验证两项一致当前行缺少编号两组标记不合并。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill("other")])
    current = _write(tmp_path / "current.csv", [_fill(), _fill()])

    report = audit_cross_file_fills([previous], current)

    assert report["classification"].tolist() == ["POSSIBLE_DUPLICATE", "POSSIBLE_DUPLICATE"]
    assert report["source_row_number"].tolist() == [2, 3]
    assert len(report) == 2


def test_named_execution_column_must_exist_in_all_exports(tmp_path):
    """验证命名成交列必须存在在全部导出。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    previous = _write(tmp_path / "prior.csv", [_fill()])
    current = _write(tmp_path / "current.csv", [_fill()])

    try:
        audit_cross_file_fills([previous], current, execution_id_column="broker_fill_no")
    except ValueError as exc:
        assert "指定的可选字段不存在" in str(exc)
    else:
        raise AssertionError("expected missing explicit id column to fail")
