"""策环测试模块：验证每日纸面流程的合成采集、排名、简报及缺数据阻断。 测试均应使用受控夹具，不访问真实券商；涉及网络的分支由测试替身隔离。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.security_identity import IDENTITY_EVENT_COLUMNS
from cehuan.research import daily_paper_run
from cehuan.research.lookback_pair import FIXED_SYMBOLS


SHANGHAI = ZoneInfo("Asia/Shanghai")
SIGNAL_DATE = "2026-11-20"


def _inputs(root: Path) -> tuple[Path, Path, Path]:
    """私有测试辅助函数，用于输入。 参数：`root`：代码标识“root”（含义待核实）场景输入。 返回：tuple[Path, Path, Path]，供调用处构造夹具或断言复用。"""
    pool = root / "selected_pool.csv"
    pool.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"symbol": FIXED_SYMBOLS}).to_csv(pool, index=False)
    config = Path(__file__).resolve().parents[1] / "configs" / "default.toml"
    identity = root / "security-identity-events.csv"
    pd.DataFrame(columns=IDENTITY_EVENT_COLUMNS).to_csv(identity, index=False)
    return pool, config, identity


class _SyntheticBaoStock:
    """测试辅助类“_SyntheticBaoStock”，用于承载合成BaoStock股票相关的合成状态或模拟行为；对象仅在当前测试过程中使用，不代表真实供应商或账户对象。"""
    package_version = "synthetic-test-only"

    def __init__(self, *, dates: list[str], omit_signal_member: bool = False, **kwargs: Any) -> None:
        """初始化测试辅助对象`_SyntheticBaoStock`的状态；参数提供模拟响应或夹具字段，不触发真实外部操作。"""
        self.dates = dates
        self.omit_signal_member = omit_signal_member
        self.request_log: list[dict[str, Any]] = []

    def __enter__(self) -> "_SyntheticBaoStock":
        """实现测试替身_SyntheticBaoStock的进入行为；用于控制被测代码与断言，不访问真实服务。 返回：'_SyntheticBaoStock'，供调用处构造夹具或断言复用。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """实现测试替身_SyntheticBaoStock的退出行为；用于控制被测代码与断言，不访问真实服务。 参数：`*args`：参数场景输入。 返回：None，供调用处构造夹具或断言复用。"""
        return None

    def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
        """实现测试替身_SyntheticBaoStock的交易日历行为；用于控制被测代码与断言，不访问真实服务。 参数：`start_date`：代码标识“start”（含义待核实）日期场景输入；`end_date`：结束日期场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        return pd.DataFrame({"calendar_date": self.dates, "is_trading_day": ["1"] * len(self.dates)})

    def stock_basic(self, symbol: str) -> pd.DataFrame:
        """实现测试替身_SyntheticBaoStock的股票基础资料行为；用于控制被测代码与断言，不访问真实服务。 参数：`symbol`：证券代码场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        code = f"{symbol[:2].lower()}.{symbol[2:]}"
        return pd.DataFrame([{
            "code": code, "code_name": symbol, "ipoDate": "2010-01-01", "outDate": "", "type": "1",
        }])

    def all_stocks(self, day: str) -> pd.DataFrame:
        """实现测试替身_SyntheticBaoStock的全部股票行为；用于控制被测代码与断言，不访问真实服务。 参数：`day`：日场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        symbols = list(FIXED_SYMBOLS)
        if self.omit_signal_member and day == SIGNAL_DATE:
            symbols = symbols[:-1]
        rows = []
        for symbol in symbols:
            status = "0" if symbol == FIXED_SYMBOLS[0] and day == self.dates[5] else "1"
            rows.append({"code": f"{symbol[:2].lower()}.{symbol[2:]}", "tradeStatus": status})
        return pd.DataFrame(rows)

    def history(
        self, symbol: str, start_date: str, end_date: str, adjustflag: str, fields: str,
    ) -> pd.DataFrame:
        """实现测试替身_SyntheticBaoStock的历史行为；用于控制被测代码与断言，不访问真实服务。 参数：`symbol`：证券代码场景输入；`start_date`：代码标识“start”（含义待核实）日期场景输入；`end_date`：结束日期场景输入；`adjustflag`：代码标识“adjustflag”（含义待核实）场景输入；`fields`：字段场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
        code = f"{symbol[:2].lower()}.{symbol[2:]}"
        slope = 0.08 + 0.005 * FIXED_SYMBOLS.index(symbol)
        rows = []
        prior_close = 10.0
        for index, day in enumerate(self.dates):
            close = 10.0 + slope * index
            suspended = symbol == FIXED_SYMBOLS[0] and day == self.dates[5]
            if suspended:
                close = prior_close
            status = "0" if suspended else "1"
            rows.append({
                "date": day, "code": code, "open": str(close), "high": str(close + 0.1),
                "low": str(close - 0.1), "close": str(close), "preclose": str(prior_close),
                "volume": "0" if suspended else "1000", "amount": "0" if suspended else "10000",
                "adjustflag": adjustflag, "turn": "0.1", "tradestatus": status,
                "pctChg": "0" if suspended else "1", "isST": "0",
            })
            prior_close = close
        return pd.DataFrame(rows)


def _clock(start: datetime):
    """私有测试辅助函数，用于时钟。 参数：`start`：代码标识“start”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
    tick = [0]

    def now() -> datetime:
        """实现测试替身_clock的当前行为；用于控制被测代码与断言，不访问真实服务。 返回：datetime，供调用处构造夹具或断言复用。"""
        value = start + timedelta(seconds=tick[0])
        tick[0] += 1
        return value

    return now


def _run(
    root: Path, monkeypatch, *, omit_signal_member: bool = False,
) -> tuple[Path, list[dict[str, Any]]]:
    """私有测试辅助函数，用于运行。 参数：`root`：代码标识“root”（含义待核实）场景输入；`monkeypatch`：pytest 提供的环境/属性替换工具；`omit_signal_member`：代码标识“omit”（含义待核实）信号成员场景输入。 返回：tuple[Path, list[dict[str, Any]]]，供调用处构造夹具或断言复用。"""
    monkeypatch.setattr(daily_paper_run, "REPO_ROOT", root)
    pool, config, identity = _inputs(root / "inputs")
    dates = pd.bdate_range(end=SIGNAL_DATE, periods=21).strftime("%Y-%m-%d").tolist()
    factory_arguments: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> _SyntheticBaoStock:
        """实现测试替身_run的工厂行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回：_SyntheticBaoStock，供调用处构造夹具或断言复用。"""
        factory_arguments.append(kwargs)
        return _SyntheticBaoStock(dates=dates, omit_signal_member=omit_signal_member, **kwargs)

    start = datetime(2026, 11, 20, 16, 0, tzinfo=SHANGHAI)
    run_dir = daily_paper_run.run_daily_paper_workflow(
        output_root=root / "data" / "private" / "daily-paper-runs",
        pool_path=pool,
        config_path=config,
        identity_events_path=identity,
        brief_output_root=root / "outputs" / "daily-briefs",
        model_mode="llm" if omit_signal_member else "stub",
        now=start,
        clock_fn=_clock(start),
        session_factory=factory,
    )
    return run_dir, factory_arguments


def test_daily_paper_workflow_synthetic_capture_rank_and_brief(tmp_path: Path, monkeypatch) -> None:
    """验证每日纸面流程合成采集排序与简报。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    run_dir, factory_arguments = _run(tmp_path, monkeypatch)

    manifest = json.loads((run_dir / "daily-run-manifest.json").read_text(encoding="utf-8"))
    capture = json.loads((run_dir / "capture-manifest.json").read_text(encoding="utf-8"))
    ranking_manifest = json.loads((run_dir / "ranking" / "paper-ranking-manifest.json").read_text(encoding="utf-8"))
    brief_manifest = json.loads((Path(manifest["brief_path"]).parent / "brief-manifest.json").read_text(encoding="utf-8"))

    assert factory_arguments[0]["max_retries"] == 0
    assert manifest["status"] == "COMPLETED_DAILY_PAPER_BRIEF"
    assert manifest["rankings_generated"] is True
    assert manifest["model_calls"] == 1
    assert manifest["llm_request_count"] == 0
    assert manifest["returns_computed"] is False
    assert manifest["orders_generated"] is False
    assert manifest["broker_connected"] is False
    assert manifest["formal_baseline"] == "NOT_READY"
    assert capture["state_window_row_count"] == len(FIXED_SYMBOLS) * 21
    assert len(capture["status_requests"]) == len(FIXED_SYMBOLS)
    assert len(capture["price_requests"]) == len(FIXED_SYMBOLS)
    assert ranking_manifest["status"] in {"CAPTURED", "CAPTURED_WITH_EXCLUSIONS"}
    assert ranking_manifest["window_scope"]["suspended_window_symbols"] == 1
    assert brief_manifest["model"]["selected_mode"] == "stub"
    assert brief_manifest["model"]["call_count"] == 1
    assert (run_dir / "capture-manifest.seal.json").is_file()
    assert (run_dir / "vendor-status.csv").is_file()


def test_incomplete_member_response_blocks_ranking_and_model(tmp_path: Path, monkeypatch) -> None:
    """验证不完整成员响应阻断排名与模型。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    run_dir, _ = _run(tmp_path, monkeypatch, omit_signal_member=True)

    manifest = json.loads((run_dir / "daily-run-manifest.json").read_text(encoding="utf-8"))
    capture = json.loads((run_dir / "capture-manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["rankings_generated"] is False
    assert manifest["model_calls"] == 0
    assert manifest["orders_generated"] is False
    assert capture["status"] == "NEEDS_DATA"
    assert capture["requests"][f"membership:{SIGNAL_DATE}"]["status"] == "FAILED_VALIDATION"
    assert "INCOMPLETE_MEMBER_RESPONSE_UNKNOWN" in capture["requests"][f"membership:{SIGNAL_DATE}"]["error_category"]
    assert not (run_dir / "ranking" / "paper-ranking-manifest.json").exists()
    assert not list((tmp_path / "outputs" / "daily-briefs").glob("*/brief-manifest.json"))


def test_empty_history_responses_are_sealed_as_needs_data(tmp_path: Path, monkeypatch) -> None:
    """验证所有行情响应为空时正常封存 NEEDS_DATA，且不进入排名或模型。"""
    monkeypatch.setattr(daily_paper_run, "REPO_ROOT", tmp_path)
    pool, config, identity = _inputs(tmp_path / "inputs")
    dates = pd.bdate_range(end=SIGNAL_DATE, periods=21).strftime("%Y-%m-%d").tolist()

    class EmptyHistoryBaoStock(_SyntheticBaoStock):
        """只返回带正确字段名的空行情响应，不模拟真实价格。"""

        def history(
            self, symbol: str, start_date: str, end_date: str, adjustflag: str, fields: str,
        ) -> pd.DataFrame:
            return pd.DataFrame(columns=fields.split(","))

    start = datetime(2026, 11, 20, 16, 0, tzinfo=SHANGHAI)
    run_dir = daily_paper_run.run_daily_paper_workflow(
        output_root=tmp_path / "data" / "private" / "daily-paper-runs",
        pool_path=pool,
        config_path=config,
        identity_events_path=identity,
        brief_output_root=tmp_path / "outputs" / "daily-briefs",
        model_mode="llm",
        now=start,
        clock_fn=_clock(start),
        session_factory=lambda **kwargs: EmptyHistoryBaoStock(dates=dates, **kwargs),
    )

    manifest_path = run_dir / "daily-run-manifest.json"
    capture_path = run_dir / "capture-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    capture_seal = json.loads((run_dir / "capture-manifest.seal.json").read_text(encoding="utf-8"))
    response_paths = [run_dir / item["path"] for item in capture["raw_responses"]]

    assert manifest["status"] == capture["status"] == capture["capture_manifest_status"] == "NEEDS_DATA"
    assert manifest["finished_at_utc"] == capture["finished_at_utc"]
    assert any("未形成任何有效规范行情" in item for item in manifest["blocking_reasons"])
    assert any(item["status"] == "FAILED_VALIDATION" for item in capture["requests"].values())
    assert capture_seal["capture_manifest_sha256"] == daily_paper_run._sha(capture_path)
    assert capture["raw_response_sha256"] == {
        item["path"]: item["sha256"] for item in capture["raw_responses"]
    }
    assert all(path.is_file() for path in response_paths)
    assert all(daily_paper_run._sha(path) == capture["raw_response_sha256"][str(path.relative_to(run_dir))] for path in response_paths)
    assert not (run_dir / "bars.csv").exists()
    assert not (run_dir / "ranking" / "paper-ranking-manifest.json").exists()
    assert manifest["rankings_generated"] is False
    assert manifest["model_calls"] == 0
    assert not list((tmp_path / "outputs" / "daily-briefs").glob("*/brief-manifest.json"))


def test_before_close_is_needs_data_without_provider_or_model_request(tmp_path: Path, monkeypatch) -> None:
    """验证之前收盘需要数据缺少供应商或模型请求。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    monkeypatch.setattr(daily_paper_run, "REPO_ROOT", tmp_path)
    pool, config, identity = _inputs(tmp_path / "inputs")
    called = []

    def forbidden_session(**kwargs: Any):
        """实现测试替身test_before_close_is_needs_data_without_provider_or_model_request的禁止交易日行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
        called.append(kwargs)
        raise AssertionError("pre-close gate must not open BaoStock")

    now = datetime(2026, 11, 20, 14, 59, tzinfo=SHANGHAI)
    run_dir = daily_paper_run.run_daily_paper_workflow(
        output_root=tmp_path / "data" / "private" / "daily-paper-runs",
        pool_path=pool,
        config_path=config,
        identity_events_path=identity,
        brief_output_root=tmp_path / "outputs" / "daily-briefs",
        model_mode="llm",
        now=now,
        session_factory=forbidden_session,
    )
    manifest = json.loads((run_dir / "daily-run-manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["capture_gate"] == "BLOCKED_BEFORE_CLOSE"
    assert manifest["requests"] == {}
    assert manifest["rankings_generated"] is False
    assert manifest["model_calls"] == 0
    assert not called


def test_calendar_not_confirming_signal_day_blocks_before_price_capture(tmp_path: Path, monkeypatch) -> None:
    """验证日历不确认信号日阻断之前价格采集。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    monkeypatch.setattr(daily_paper_run, "REPO_ROOT", tmp_path)
    pool, config, identity = _inputs(tmp_path / "inputs")
    sessions = pd.bdate_range(end=SIGNAL_DATE, periods=21).strftime("%Y-%m-%d").tolist()
    sessions = sessions[:-1]
    calls: list[str] = []

    class StaleCalendarBaoStock(_SyntheticBaoStock):
        """测试辅助类“StaleCalendarBaoStock”，用于承载过期日历BaoStock股票相关的合成状态或模拟行为；对象仅在当前测试过程中使用，不代表真实供应商或账户对象。"""
        def trade_calendar(self, start_date: str, end_date: str) -> pd.DataFrame:
            """实现测试替身StaleCalendarBaoStock的交易日历行为；用于控制被测代码与断言，不访问真实服务。 参数：`start_date`：代码标识“start”（含义待核实）日期场景输入；`end_date`：结束日期场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
            calls.append("calendar")
            return super().trade_calendar(start_date, end_date)

        def stock_basic(self, symbol: str) -> pd.DataFrame:
            """实现测试替身StaleCalendarBaoStock的股票基础资料行为；用于控制被测代码与断言，不访问真实服务。 参数：`symbol`：证券代码场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
            calls.append(f"basic:{symbol}")
            return super().stock_basic(symbol)

        def history(self, *args: Any, **kwargs: Any) -> pd.DataFrame:
            """实现测试替身StaleCalendarBaoStock的历史行为；用于控制被测代码与断言，不访问真实服务。 参数：`*args`：参数场景输入；`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回：pd.DataFrame，供调用处构造夹具或断言复用。"""
            calls.append("history")
            return super().history(*args, **kwargs)

    def factory(**kwargs: Any) -> StaleCalendarBaoStock:
        """实现测试替身test_calendar_not_confirming_signal_day_blocks_before_price_capture的工厂行为；用于控制被测代码与断言，不访问真实服务。 参数：`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回：StaleCalendarBaoStock，供调用处构造夹具或断言复用。"""
        return StaleCalendarBaoStock(dates=sessions, **kwargs)

    start = datetime(2026, 11, 20, 16, 0, tzinfo=SHANGHAI)
    run_dir = daily_paper_run.run_daily_paper_workflow(
        output_root=tmp_path / "data" / "private" / "daily-paper-runs",
        pool_path=pool,
        config_path=config,
        identity_events_path=identity,
        brief_output_root=tmp_path / "outputs" / "daily-briefs",
        model_mode="llm",
        now=start,
        clock_fn=_clock(start),
        session_factory=factory,
    )
    manifest = json.loads((run_dir / "daily-run-manifest.json").read_text(encoding="utf-8"))

    assert manifest["status"] == "NEEDS_DATA"
    assert list(manifest["requests"]) == ["calendar"]
    assert calls == ["calendar"]
    assert manifest["rankings_generated"] is False
    assert manifest["model_calls"] == 0
    assert any("未确认本机日期为当日开市日" in reason for reason in manifest["blocking_reasons"])


def test_modified_raw_response_hash_blocks_ranking_and_model(tmp_path: Path, monkeypatch) -> None:
    """验证修改原始响应哈希阻断排名与模型。本用例通过断言确认该边界；失败时由 pytest 报告具体断言位置。 参数：`tmp_path`：pytest 提供的临时目录；`monkeypatch`：pytest 提供的环境/属性替换工具。无业务返回值；涉及文件写入时限定在测试夹具目录。"""
    pool, config, identity = _inputs(tmp_path / "inputs")
    monkeypatch.setattr(daily_paper_run, "REPO_ROOT", tmp_path)
    original_request = daily_paper_run._request
    mutated = False

    def mutate_sealed_source(*args: Any, **kwargs: Any):
        """实现测试替身test_modified_raw_response_hash_blocks_ranking_and_model的修改已封存来源行为；用于控制被测代码与断言，不访问真实服务。 参数：`*args`：参数场景输入；`**kwargs`：代码标识“kwargs”（含义待核实）场景输入。 返回值供调用处构造夹具或断言复用。"""
        nonlocal mutated
        result = original_request(*args, **kwargs)
        request_id = args[4]
        if request_id == f"basic:{FIXED_SYMBOLS[0]}" and not mutated:
            raw_dir = args[2]
            (raw_dir / f"{request_id.replace(':', '_')}.csv").write_text("tampered\n", encoding="utf-8")
            mutated = True
        return result

    monkeypatch.setattr(daily_paper_run, "_request", mutate_sealed_source)
    dates = pd.bdate_range(end=SIGNAL_DATE, periods=21).strftime("%Y-%m-%d").tolist()
    start = datetime(2026, 11, 20, 16, 0, tzinfo=SHANGHAI)
    run_dir = daily_paper_run.run_daily_paper_workflow(
        output_root=tmp_path / "data" / "private" / "daily-paper-runs",
        pool_path=pool,
        config_path=config,
        identity_events_path=identity,
        brief_output_root=tmp_path / "outputs" / "daily-briefs",
        model_mode="llm",
        now=start,
        clock_fn=_clock(start),
        session_factory=lambda **kwargs: _SyntheticBaoStock(dates=dates, **kwargs),
    )
    manifest = json.loads((run_dir / "daily-run-manifest.json").read_text(encoding="utf-8"))
    capture = json.loads((run_dir / "capture-manifest.json").read_text(encoding="utf-8"))

    assert mutated
    assert manifest["status"] == "NEEDS_DATA"
    assert manifest["rankings_generated"] is False
    assert manifest["model_calls"] == 0
    assert capture["status"] == "NEEDS_DATA"
    assert any("原始响应缺失或哈希错误" in reason for reason in capture["blocking_reasons"])
    assert not (run_dir / "ranking" / "paper-ranking-manifest.json").exists()
    assert not list((tmp_path / "outputs" / "daily-briefs").glob("*/brief-manifest.json"))
