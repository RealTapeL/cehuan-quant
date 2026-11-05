from __future__ import annotations

import json
from pathlib import Path

import pytest

from cehuan.research.provider_probe import capture_akshare_minute_probe, validate_akshare_probe_directory


class FakeFrame:
    def to_json(self, **kwargs):
        return json.dumps([
            {"时间": "2026-11-05 09:31:00", "开盘": 10.25, "收盘": 10.26, "最高": 10.27, "最低": 10.24},
            {"时间": "2026-11-05 09:32:00", "开盘": 10.26, "收盘": 10.28, "最高": 10.29, "最低": 10.25},
        ], ensure_ascii=False)


def test_akshare_probe_archives_sdk_response_and_rebuilds_payload(tmp_path: Path, monkeypatch) -> None:
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
    import akshare as ak

    calls = []

    def fail(**kwargs):
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
    with pytest.raises(ValueError, match="仅允许 1 或 2"):
        capture_akshare_minute_probe(
            symbol="SH600000", trade_date="2026-11-05", output_dir=tmp_path / "bad", max_attempts=3,
        )
    with pytest.raises(ValueError, match="必须使用 SH/SZ/BJ"):
        capture_akshare_minute_probe(
            symbol="600000", trade_date="2026-11-05", output_dir=tmp_path / "bad-symbol", max_attempts=1,
        )
