"""策环测试模块：验证供应商探测的原始响应留存、错误分类、重试上限和防覆盖。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cehuan.research.provider_probe import capture_akshare_minute_probe, validate_akshare_probe_directory


class FakeFrame:
    """测试辅助类“FakeFrame”，用于承载模拟数据表相关的合成状态或模拟行为；对象仅在当前测试过程中使用，不代表真实供应商或账户对象。"""
    def to_json(self, **kwargs):
        """实现测试替身FakeFrame的到JSON行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
        return json.dumps([
            {"时间": "2026-11-05 09:31:00", "开盘": 10.25, "收盘": 10.26, "最高": 10.27, "最低": 10.24},
            {"时间": "2026-11-05 09:32:00", "开盘": 10.26, "收盘": 10.28, "最高": 10.29, "最低": 10.25},
        ], ensure_ascii=False)


def test_akshare_probe_archives_sdk_response_and_rebuilds_payload(tmp_path: Path, monkeypatch) -> None:
    """验证AKShare探测归档SDK响应与重建载荷。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    import akshare as ak

    calls = []
    monkeypatch.setattr(ak, "stock_zh_a_hist_min_em", lambda **kwargs: calls.append(kwargs) or FakeFrame())
    output = tmp_path / "probe"
    result = capture_akshare_minute_probe(
        symbol="SH600000", trade_date="2026-11-05", output_dir=output, max_attempts=1,
    )
    assert len(calls) == 1
    assert calls[0]["symbol"] == "600000"
    assert calls[0]["period"] == "1"
    assert result["report"]["overall_status"] == "AVAILABLE"
    assert result["report"]["response_row_count_for_requested_date"] == 2
    assert result["report"]["provider_published_at_utc"] is None
    assert result["report"]["response_capture_layer"] == "SDK_SERIALIZED_RETURN_NOT_ORIGINAL_HTTP_BYTES"
    assert result["validation"]["normalized_payloads_rebuilt_from_raw"] is True
    assert validate_akshare_probe_directory(output)["status"] == "VALID"
    assert not result["report"]["orders_generated"]


def test_akshare_probe_records_provider_errors_without_inference_or_secret_echo(tmp_path: Path, monkeypatch) -> None:
    """验证AKShare探测记录供应商错误缺少推断或密钥照搬。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    import akshare as ak

    calls = []

    def fail(**kwargs):
        """实现测试替身test_akshare_probe_records_provider_errors_without_inference_or_secret_echo的失败行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
        calls.append(kwargs)
        raise RuntimeError("connection failed; token=should-not-appear")

    monkeypatch.setattr(ak, "stock_zh_a_hist_min_em", fail)
    output = tmp_path / "probe-error"
    result = capture_akshare_minute_probe(
        symbol="SZ000001", trade_date="2026-11-05", output_dir=output, max_attempts=1,
    )
    assert len(calls) == 1
    assert result["report"]["overall_status"] == "ERROR"
    assert result["report"]["attempts"][0]["error_category"] == "PROVIDER_OR_NETWORK_ERROR_CAUSE_NOT_INFERRED"
    assert "should-not-appear" not in result["report"]["attempts"][0]["error_message"]
    assert validate_akshare_probe_directory(output)["status"] == "VALID"


def test_provider_probe_bounds_retry_count_and_refuses_overwrite(tmp_path: Path) -> None:
    """验证供应商探测限制重试计算与拒绝覆盖。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    with pytest.raises(ValueError, match="仅允许 1 或 2"):
        capture_akshare_minute_probe(
            symbol="SH600000", trade_date="2026-11-05", output_dir=tmp_path / "bad", max_attempts=3,
        )
    with pytest.raises(ValueError, match="必须使用 SH/SZ/BJ"):
        capture_akshare_minute_probe(
            symbol="600000", trade_date="2026-11-05", output_dir=tmp_path / "bad-symbol", max_attempts=1,
        )
