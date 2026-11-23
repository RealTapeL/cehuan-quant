"""策环测试模块：验证固定研究池采集、有限重试、原始响应留存和封存校验。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cehuan.research.lookback_pair import EXPERIMENT_ID, FIXED_SYMBOLS
from cehuan.research.lookback_pair_capture import capture_lookback_pair_inputs


SHANGHAI = ZoneInfo("Asia/Shanghai")
REPO = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    """私有测试辅助函数，用于SHA。 参数：`path`：路径场景输入。 返回：str，供调用处构造夹具或断言复用。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict[str, Path], pd.DataFrame]:
    """私有测试辅助函数，用于夹具。 参数：`tmp_path`：pytest 提供的临时目录。 返回：tuple[dict[str, Path], pd.DataFrame]，供调用处构造夹具或断言复用。"""
    config = tmp_path / "default.toml"
    config.write_bytes((REPO / "configs/default.toml").read_bytes())
    pool = tmp_path / "pool.csv"
    pd.DataFrame({"symbol": FIXED_SYMBOLS, "name": ["test"] * len(FIXED_SYMBOLS)}).to_csv(pool, index=False)
    identity = tmp_path / "identity.csv"
    identity.write_text("event_id,event_type,old_symbol,new_symbol,effective_date\n", encoding="utf-8")

    dates = pd.date_range("2026-01-01", "2026-12-31", freq="D")
    reference = pd.DataFrame({
        "trade_date": dates.strftime("%Y-%m-%d"),
        "is_open": dates.dayofweek < 5,
        "previous_trade_date": "",
        "source": "synthetic.calendar",
        "retrieved_at_utc": "2026-11-20T00:00:00Z",
    })
    calendar = tmp_path / "frozen-calendar.csv"
    reference.to_csv(calendar, index=False)

    registration_doc = REPO / "docs/lookback-10-vs-20-forward-registration.md"
    freeze_data: dict[str, Any] = {
        "experiment_id": EXPERIMENT_ID,
        "signal_observation": {
            "fixed_signal_date": "2026-12-17",
            "signal_capture_start_local": "2026-12-17T15:06:00+08:00",
            "retry_local_times": ["2026-12-17T15:20:00+08:00", "2026-12-17T15:35:00+08:00"],
            "capture_cutoff_local": "2026-12-17T15:45:00+08:00",
            "seal_cutoff_local": "2026-12-17T16:00:00+08:00",
        },
        "research_pool": {"symbols": list(FIXED_SYMBOLS)},
        "frozen_input_files": {
            "strategy_config": {"path": str(config), "sha256": _sha(config)},
            "pool": {"path": str(pool), "sha256": _sha(pool)},
            "calendar": {"path": str(calendar), "sha256": _sha(calendar)},
            "registration_document": {
                "path": "docs/lookback-10-vs-20-forward-registration.md",
                "sha256": _sha(registration_doc),
            },
        },
    }
    freeze = tmp_path / "registration-freeze.json"
    freeze.write_text(json.dumps(freeze_data, indent=2) + "\n", encoding="utf-8")
    freeze.with_suffix(".sha256").write_text(f"{_sha(freeze)}  {freeze.name}\n", encoding="ascii")
    paths = {"config": config, "pool": pool, "identity": identity, "calendar": calendar, "freeze": freeze}
    return paths, reference


class FakeBaoStockState:
    """测试辅助类“FakeBaoStockState”，用于承载模拟BaoStock股票状态相关的合成状态或模拟行为；对象仅在当前测试过程中使用，不代表真实供应商或账户对象。"""
    def __init__(
        self,
        reference: pd.DataFrame,
        *,
        fail_member_once: str | None = None,
        unknown_st: tuple[str, str] | None = None,
        missing_bar: tuple[str, str] | None = None,
        invalid_member_day: str | None = None,
        omitted_member: tuple[str, str] | None = None,
    ) -> None:
        """初始化测试辅助对象`FakeBaoStockState`的状态；参数提供模拟响应或夹具字段，不触发真实外部操作。"""
        self.reference = reference
        self.fail_member_once = fail_member_once
        self.unknown_st = unknown_st
        self.missing_bar = missing_bar
        self.invalid_member_day = invalid_member_day
        self.omitted_member = omitted_member
        self.calls: dict[str, int] = {}
        self.session_count = 0

    def session(self, **kwargs: Any) -> "FakeBaoStockSession":
        """实现测试替身FakeBaoStockState的交易日行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回：'FakeBaoStockSession'，供调用处构造夹具或断言复用。"""
        assert kwargs["max_retries"] == 0
        assert kwargs["minimum_interval_seconds"] == 1.0
        self.session_count += 1
        return FakeBaoStockSession(self)


class FakeBaoStockSession:
    """测试辅助类“FakeBaoStockSession”，用于承载模拟BaoStock股票交易日相关的合成状态或模拟行为；对象仅在当前测试过程中使用，不代表真实供应商或账户对象。"""
    package_version = "0.9.4-synthetic"

    def __init__(self, state: FakeBaoStockState) -> None:
        """初始化测试辅助对象`FakeBaoStockSession`的状态；参数提供模拟响应或夹具字段，不触发真实外部操作。"""
        self.state = state
        self.request_log: list[dict[str, Any]] = []

    def __enter__(self) -> "FakeBaoStockSession":
        """实现测试替身FakeBaoStockSession的进入行为；用于控制被测代码与断言，不访问真实服务。 返回：'FakeBaoStockSession'，供调用处构造夹具或断言复用。"""
        self._record("login")
        return self

    def __exit__(self, *_: object) -> None:
        """实现测试替身FakeBaoStockSession的退出行为；用于控制被测代码与断言，不访问真实服务。 参数：`*_`：场景输入。 返回：None，供调用处构造夹具或断言复用。"""
        return None

    def _record(self, endpoint: str) -> None:
        """实现测试替身FakeBaoStockSession的登记行为；用于控制被测代码与断言，不访问真实服务。 参数：`endpoint`：接口场景输入。 返回：None，供调用处构造夹具或断言复用。"""
        self.request_log.append({"endpoint": endpoint, "status": "success", "request_count": 1})
        self.state.calls[endpoint] = self.state.calls.get(endpoint, 0) + 1

    def trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        """实现测试替身FakeBaoStockSession的交易日历行为；用于控制被测代码与断言，不访问真实服务。 参数：`start`：代码标识“start”（含义待核实）场景输入；`end`：结束场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        self._record("query_trade_dates")
        return pd.DataFrame({
            "calendar_date": self.state.reference["trade_date"],
            "is_trading_day": self.state.reference["is_open"].map({True: "1", False: "0"}),
        })

    def stock_basic(self, symbol: str) -> pd.DataFrame:
        """实现测试替身FakeBaoStockSession的股票基础资料行为；用于控制被测代码与断言，不访问真实服务。 参数：`symbol`：证券代码场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        endpoint = f"query_stock_basic({_vendor_code(symbol)})"
        self._record(endpoint)
        return pd.DataFrame([{
            "code": _vendor_code(symbol), "code_name": f"synthetic-{symbol}",
            "ipoDate": "2000-01-01", "outDate": "", "type": "1",
        }])

    def all_stocks(self, day: str) -> pd.DataFrame:
        """实现测试替身FakeBaoStockSession的全部股票行为；用于控制被测代码与断言，不访问真实服务。 参数：`day`：日场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        endpoint = f"query_all_stock({day})"
        self._record(endpoint)
        if day == self.state.fail_member_once and self.state.calls[endpoint] == 1:
            raise TimeoutError("synthetic one-time timeout")
        trade_status = "" if day == self.state.invalid_member_day else "1"
        symbols = [
            symbol for symbol in FIXED_SYMBOLS
            if self.state.omitted_member != (symbol, day)
        ]
        return pd.DataFrame({"code": [_vendor_code(item) for item in symbols], "tradeStatus": [trade_status] * len(symbols)})

    def history(self, symbol: str, start: str, end: str, *, adjustflag: str, fields: str) -> pd.DataFrame:
        """实现测试替身FakeBaoStockSession的历史行为；用于控制被测代码与断言，不访问真实服务。 参数：`symbol`：证券代码场景输入；`start`：代码标识“start”（含义待核实）场景输入；`end`：结束场景输入；`adjustflag`：代码标识“adjustflag”（含义待核实）场景输入；`fields`：字段场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        endpoint = f"query_history_k_data_plus({_vendor_code(symbol)},adjustflag={adjustflag})"
        self._record(endpoint)
        open_dates = self.state.reference.loc[self.state.reference["is_open"], "trade_date"]
        dates = [value for value in open_dates if start <= value <= end]
        rows = []
        for index, day in enumerate(dates):
            if self.state.missing_bar == (symbol, day):
                continue
            close = 20.0 + index
            if adjustflag == "1":
                close *= 1.1
            st_value = "" if self.state.unknown_st == (symbol, day) else "0"
            rows.append({
                "date": day, "code": _vendor_code(symbol), "open": close,
                "high": close + 1, "low": close - 1, "close": close,
                "preclose": close - 0.1, "volume": "1000", "amount": "20000",
                "adjustflag": adjustflag, "turn": "0.01", "tradestatus": "1",
                "pctChg": "1", "isST": st_value,
            })
        return pd.DataFrame(rows)


def _vendor_code(symbol: str) -> str:
    """私有测试辅助函数，用于供应商代码。 参数：`symbol`：证券代码场景输入。 返回：str，供调用处构造夹具或断言复用。"""
    return f"{symbol[:2].lower()}.{symbol[2:]}"


def _clock(value: datetime):
    """私有测试辅助函数，用于时钟。 参数：`value`：值场景输入。 返回值供调用处构造夹具或断言复用。"""
    return lambda: value


def _capture(tmp_path: Path, paths: dict[str, Path], state: FakeBaoStockState, output: Path, at: datetime):
    """私有测试辅助函数，用于采集。 参数：`tmp_path`：pytest 提供的临时目录；`paths`：代码标识“paths”（含义待核实）场景输入；`state`：状态场景输入；`output`：输出场景输入；`at`：于场景输入。 返回值供调用处构造夹具或断言复用。"""
    return capture_lookback_pair_inputs(
        output_dir=output,
        registration_freeze_path=paths["freeze"],
        config_path=paths["config"],
        pool_path=paths["pool"],
        identity_events_path=paths["identity"],
        signal_date="2026-12-17",
        now=at,
        clock_fn=_clock(at),
        session_factory=state.session,
    )


def test_capture_writes_bao_stock_raw_and_normalized_inputs_with_hashes(tmp_path: Path) -> None:
    """验证采集写入BaoStock股票原始与规范化输入带有哈希。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    state = FakeBaoStockState(reference)
    at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    output = tmp_path / "capture"

    result = _capture(tmp_path, paths, state, output, at)

    assert result["manifest"]["status"] == "CAPTURED_INPUTS_READY"
    assert result["manifest"]["calendar_outcome_horizon_status"] == "NEEDS_DATA"
    assert result["manifest"]["rankings_generated"] is False
    assert result["manifest"]["returns_computed"] is False
    assert result["manifest"]["orders_generated"] is False
    assert result["manifest"]["fixed_symbols"] == list(FIXED_SYMBOLS)
    assert result["manifest"]["provider"] == "BaoStock"

    capture_manifest = json.loads((output / "capture-manifest.json").read_text(encoding="utf-8"))
    assert capture_manifest["status"] == "CAPTURED_INPUTS_READY"
    assert capture_manifest["provider_published_at_utc"] is None
    assert capture_manifest["provider_publication_evidence"] == "UNVERIFIED"
    assert capture_manifest["signal_price_adjustment"] == "adjustflag=1"
    assert capture_manifest["raw_price_adjustment"] == "adjustflag=3"
    assert len(capture_manifest["raw_responses"]) == 52
    for response in capture_manifest["raw_responses"]:
        path = output / response["path"]
        assert path.is_file()
        assert _sha(path) == response["sha256"]
    bars = pd.read_csv(output / "bars.csv")
    universe = pd.read_csv(output / "point-in-time-universe.csv")
    assert len(bars) == 21 * 10
    assert len(universe) == 21 * 10
    assert set(universe["pool_status"]) == {"STOCK_MEMBER"}
    assert len(capture_manifest["required_sessions"]) == 21
    assert capture_manifest["calendar_overlap"]["status"] == "MATCH"
    assert capture_manifest["session_events"]
    assert all(item["events_before_data_request"][0]["endpoint"] == "login" for item in capture_manifest["session_events"])


def test_capture_retries_only_failed_request_and_never_overwrites_attempts(tmp_path: Path) -> None:
    """验证采集重试仅失败请求与不会覆盖尝试。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    failed_day = "2026-12-16"
    state = FakeBaoStockState(reference, fail_member_once=failed_day)
    output = tmp_path / "retry-capture"
    first_at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    first = _capture(tmp_path, paths, state, output, first_at)
    assert first["manifest"]["status"] == "PARTIAL_CAPTURE_RETRY_2_PENDING"
    assert first["manifest"]["requests"][f"membership:{failed_day}"]["status"] == "FAILED"

    second_at = datetime(2026, 12, 17, 15, 21, tzinfo=SHANGHAI)
    resumed = capture_lookback_pair_inputs(
        output_dir=output, registration_freeze_path=paths["freeze"], config_path=paths["config"],
        pool_path=paths["pool"], identity_events_path=paths["identity"], signal_date="2026-12-17",
        resume=True, now=second_at, clock_fn=_clock(second_at), session_factory=state.session,
    )

    assert resumed["manifest"]["status"] == "CAPTURED_INPUTS_READY"
    assert len(resumed["manifest"]["requests"][f"membership:{failed_day}"]["attempts"]) == 2
    assert state.calls[f"query_all_stock({failed_day})"] == 2
    assert state.calls["query_stock_basic(sh.600000)"] == 1
    assert state.calls[f"query_history_k_data_plus(sh.600000,adjustflag=1)"] == 1
    assert (output / "stage-01-manifest.json").is_file()
    assert (output / "stage-02-manifest.json").is_file()
    raw_attempts = list((output / "raw/requests").glob(f"membership_{failed_day}-attempt-*.csv"))
    assert len(raw_attempts) == 1  # The failed timeout had no response body; success is retained once.
    with pytest.raises(FileExistsError):
        _capture(tmp_path, paths, state, output, second_at)


def test_unknown_st_state_is_preserved_as_unknown_and_blocks_capture_readiness(tmp_path: Path) -> None:
    """验证未知ST状态状态保留为未知与阻断采集就绪门控。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    unknown_day = "2026-12-16"
    state = FakeBaoStockState(reference, unknown_st=(FIXED_SYMBOLS[0], unknown_day))
    output = tmp_path / "unknown-state-capture"
    at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)

    result = _capture(tmp_path, paths, state, output, at)

    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert any(f"{unknown_day} {FIXED_SYMBOLS[0]}: ST status UNKNOWN" in item for item in result["manifest"]["point_in_time_status_failures"])
    universe = pd.read_csv(output / "point-in-time-universe.csv", dtype=str, keep_default_na=False)
    row = universe.loc[
        universe["symbol"].eq(FIXED_SYMBOLS[0]) & universe["trade_date"].eq(unknown_day),
    ].iloc[0]
    assert row["is_st"] == ""
    manifest = json.loads((output / "capture-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "NEEDS_DATA"
    assert len(manifest["raw_responses"]) == 52
    seal = json.loads((output / "capture-manifest.seal.json").read_text(encoding="utf-8"))
    assert seal["capture_manifest_sha256"] == _sha(output / "capture-manifest.json")


def test_nonempty_membership_response_missing_frozen_symbol_is_unknown_and_blocks(tmp_path: Path) -> None:
    """验证非空成员资格响应缺失冻结证券代码未知与阻断。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    missing_day = "2026-12-16"
    missing_symbol = FIXED_SYMBOLS[0]
    state = FakeBaoStockState(reference, omitted_member=(missing_symbol, missing_day))
    output = tmp_path / "truncated-membership-capture"

    first_at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    _capture(tmp_path, paths, state, output, first_at)
    second_at = datetime(2026, 12, 17, 15, 21, tzinfo=SHANGHAI)
    capture_lookback_pair_inputs(
        output_dir=output, registration_freeze_path=paths["freeze"], config_path=paths["config"],
        pool_path=paths["pool"], identity_events_path=paths["identity"], signal_date="2026-12-17",
        resume=True, now=second_at, clock_fn=_clock(second_at), session_factory=state.session,
    )
    third_at = datetime(2026, 12, 17, 15, 36, tzinfo=SHANGHAI)
    result = capture_lookback_pair_inputs(
        output_dir=output, registration_freeze_path=paths["freeze"], config_path=paths["config"],
        pool_path=paths["pool"], identity_events_path=paths["identity"], signal_date="2026-12-17",
        resume=True, now=third_at, clock_fn=_clock(third_at), session_factory=state.session,
    )

    key = f"membership:{missing_day}"
    request = result["manifest"]["requests"][key]
    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert request["status"] == "FAILED"
    assert len(request["attempts"]) == 3
    assert missing_symbol in request["attempts"][-1]["error"]["message"]
    assert "不能判为 NOT_STOCK_MEMBER" in request["attempts"][-1]["error"]["message"]

    sealed_manifest = json.loads((output / "capture-manifest.json").read_text(encoding="utf-8"))
    universe = pd.read_csv(output / "point-in-time-universe.csv", dtype=str, keep_default_na=False)
    missing = universe.loc[
        universe["trade_date"].eq(missing_day) & universe["symbol"].eq(missing_symbol),
    ].iloc[0]
    assert missing["pool_status"] == "UNKNOWN"
    assert missing["observed_in_response"] == "False"
    assert "缺少冻结证券" in missing["membership_response_error"]
    assert set(universe.loc[universe["trade_date"].eq(missing_day), "pool_status"]) == {"UNKNOWN"}
    assert "NOT_STOCK_MEMBER" not in set(universe.loc[universe["trade_date"].eq(missing_day), "pool_status"])
    assert _sha(output / "point-in-time-universe.csv") == sealed_manifest["point_in_time_universe_review"]["sha256"]
    assert sealed_manifest["status"] == "NEEDS_DATA"
    assert sealed_manifest["rankings_generated"] is False
    assert sealed_manifest["returns_computed"] is False
    assert sealed_manifest["orders_generated"] is False
    assert state.calls[f"query_all_stock({missing_day})"] == 3
    assert not (output / "ranking-20d.csv").exists()


def test_missing_member_bar_blocks_capture_without_inferring_suspension(tmp_path: Path) -> None:
    """验证缺失成员行情行阻断采集缺少推断停牌。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    missing_day = "2026-12-16"
    state = FakeBaoStockState(reference, missing_bar=(FIXED_SYMBOLS[0], missing_day))
    output = tmp_path / "missing-bar-capture"
    at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)

    result = _capture(tmp_path, paths, state, output, at)

    assert result["manifest"]["status"] == "NEEDS_DATA"
    assert result["manifest"]["bar_normalization_failures"][FIXED_SYMBOLS[0]] == (
        f"MISSING_BAR_WHEN_NOT_CONFIRMED_SUSPENDED:{missing_day}"
    )
    universe = pd.read_csv(output / "point-in-time-universe.csv", dtype=str, keep_default_na=False)
    state_row = universe.loc[
        universe["symbol"].eq(FIXED_SYMBOLS[0]) & universe["trade_date"].eq(missing_day),
    ].iloc[0]
    assert state_row["is_suspended"].lower() == "false"
    assert len(pd.read_csv(output / "bars.csv")) == 21 * 10 - 1


def test_retries_stop_after_registered_three_attempts_and_preserve_each_response(tmp_path: Path) -> None:
    """验证重试停止之后已登记三尝试与保留每项响应。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    invalid_day = "2026-12-16"
    state = FakeBaoStockState(reference, invalid_member_day=invalid_day)
    output = tmp_path / "bounded-retry-capture"

    first_at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    _capture(tmp_path, paths, state, output, first_at)
    second_at = datetime(2026, 12, 17, 15, 21, tzinfo=SHANGHAI)
    capture_lookback_pair_inputs(
        output_dir=output, registration_freeze_path=paths["freeze"], config_path=paths["config"],
        pool_path=paths["pool"], identity_events_path=paths["identity"], signal_date="2026-12-17",
        resume=True, now=second_at, clock_fn=_clock(second_at), session_factory=state.session,
    )
    third_at = datetime(2026, 12, 17, 15, 36, tzinfo=SHANGHAI)
    final = capture_lookback_pair_inputs(
        output_dir=output, registration_freeze_path=paths["freeze"], config_path=paths["config"],
        pool_path=paths["pool"], identity_events_path=paths["identity"], signal_date="2026-12-17",
        resume=True, now=third_at, clock_fn=_clock(third_at), session_factory=state.session,
    )

    key = f"membership:{invalid_day}"
    assert final["manifest"]["status"] == "NEEDS_DATA"
    assert len(final["manifest"]["requests"][key]["attempts"]) == 3
    manifest = json.loads((output / "capture-manifest.json").read_text(encoding="utf-8"))
    responses = [item for item in manifest["raw_responses"] if item["request_id"] == key]
    assert len(responses) == 3
    assert all(_sha(output / item["path"]) == item["sha256"] for item in responses)
    assert state.calls[f"query_all_stock({invalid_day})"] == 3
    assert not (output / "ranking-20d.csv").exists()


def test_capture_does_not_create_output_or_request_outside_first_window(tmp_path: Path) -> None:
    """验证采集不创建输出或请求范围外首次窗口。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    paths, reference = _fixture(tmp_path)
    state = FakeBaoStockState(reference)
    output = tmp_path / "too-early"
    too_early = datetime(2026, 12, 17, 15, 5, 59, tzinfo=SHANGHAI)

    with pytest.raises(ValueError, match="15:06"):
        _capture(tmp_path, paths, state, output, too_early)

    assert state.session_count == 0
    assert not output.exists()


def test_capture_response_validator_rejects_unknown_status_duplicate_and_empty_frames() -> None:
    """验证采集响应校验器拒绝未知状态重复与空数据表。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 无显式参数。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    from cehuan.research.lookback_pair_capture import _response_validation_error

    empty = pd.DataFrame(columns=["code", "tradeStatus"])
    unknown = pd.DataFrame({"code": ["sh.600000"], "tradeStatus": [""]})
    duplicate = pd.DataFrame({"code": ["sh.600000", "sh.600000"], "tradeStatus": ["1", "1"]})
    duplicate_history = pd.DataFrame({
        "date": ["2026-12-17", "2026-12-17"], "code": ["sh.600000", "sh.600000"],
        "open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1],
        "volume": [100, 100], "amount": [100, 100],
        "adjustflag": ["3", "3"], "tradestatus": ["1", "1"], "isST": ["0", "0"],
    })

    assert "0 行" in (_response_validation_error("membership:2026-12-17", empty) or "")
    assert "未知 tradeStatus" in (_response_validation_error("membership:2026-12-17", unknown) or "")
    assert "重复代码" in (_response_validation_error("membership:2026-12-17", duplicate) or "")
    assert "重复日期" in (_response_validation_error("bars_raw:SH600000", duplicate_history) or "")
