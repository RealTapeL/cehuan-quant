from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from cehuan.research.lookback_pair import (
    EXPERIMENT_ID,
    FIXED_SYMBOLS,
    create_lookback_pair_ranking,
)
from cehuan.research.lookback_pair_capture import capture_lookback_pair_inputs


SHANGHAI = ZoneInfo("Asia/Shanghai")
REPO = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> tuple[dict[str, Path], pd.DataFrame]:
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
            "baostock_adapter": {
                "path": "src/cehuan/data/baostock_adapter.py",
                "sha256": _sha(REPO / "src/cehuan/data/baostock_adapter.py"),
            },
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
    def __init__(
        self,
        reference: pd.DataFrame,
        *,
        fail_member_once: str | None = None,
        unknown_st: tuple[str, str] | None = None,
        missing_bar: tuple[str, str] | None = None,
        invalid_member_day: str | None = None,
        omitted_member: tuple[str, str] | None = None,
        basic_issue: tuple[str, str] | None = None,
    ) -> None:
        self.reference = reference
        self.fail_member_once = fail_member_once
        self.unknown_st = unknown_st
        self.missing_bar = missing_bar
        self.invalid_member_day = invalid_member_day
        self.omitted_member = omitted_member
        self.basic_issue = basic_issue
        self.calls: dict[str, int] = {}
        self.session_count = 0

    def session(self, **kwargs: Any) -> "FakeBaoStockSession":
        assert kwargs["max_retries"] == 0
        assert kwargs["minimum_interval_seconds"] == 1.0
        self.session_count += 1
        return FakeBaoStockSession(self)


class FakeBaoStockSession:
    package_version = "0.9.4-synthetic"

    def __init__(self, state: FakeBaoStockState) -> None:
        self.state = state
        self.request_log: list[dict[str, Any]] = []
        self.bs = self

    def __enter__(self) -> "FakeBaoStockSession":
        self._record("login")
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _record(self, endpoint: str) -> None:
        self.request_log.append({"endpoint": endpoint, "status": "success", "request_count": 1})
        self.state.calls[endpoint] = self.state.calls.get(endpoint, 0) + 1

    def trade_calendar(self, start: str, end: str) -> pd.DataFrame:
        self._record("query_trade_dates")
        return pd.DataFrame({
            "calendar_date": self.state.reference["trade_date"],
            "is_trading_day": self.state.reference["is_open"].map({True: "1", False: "0"}),
        })

    def _query(self, endpoint: str, operation):
        self._record(endpoint)
        return operation()

    def query_stock_basic(self, *, code: str) -> pd.DataFrame:
        symbol = f"{code[:2].upper()}{code[3:]}"
        issue = self.state.basic_issue
        if issue == (symbol, "missing"):
            return pd.DataFrame(columns=["code", "code_name", "ipoDate", "outDate", "type"])
        security_type = "2" if issue == (symbol, "type") else "1"
        row = {
            "code": code, "code_name": f"synthetic-{symbol}",
            "ipoDate": "2000-01-01", "outDate": "", "type": security_type,
        }
        rows = [row, row.copy()] if issue == (symbol, "duplicate") else [row]
        return pd.DataFrame(rows)

    def all_stocks(self, day: str) -> pd.DataFrame:
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
    return f"{symbol[:2].lower()}.{symbol[2:]}"


def _clock(value: datetime):
    return lambda: value


def _capture(tmp_path: Path, paths: dict[str, Path], state: FakeBaoStockState, output: Path, at: datetime):
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
    adapter_hash = _sha(REPO / "src/cehuan/data/baostock_adapter.py")
    capture_layer_hash = _sha(REPO / "src/cehuan/research/lookback_pair_capture.py")
    assert capture_manifest["source_sha256"]["baostock_adapter"] == adapter_hash
    assert capture_manifest["source_sha256"]["lookback_pair_capture"] == capture_layer_hash
    assert capture_manifest["adapter_provenance"]["adapter_sha256"] == adapter_hash
    assert capture_manifest["adapter_provenance"]["compatibility_layer_sha256"] == capture_layer_hash
    assert capture_manifest["adapter_provenance"]["actual_query_interface"] == "query_stock_basic(code=<fixed symbol>)"
    assert all(state.calls[f"query_stock_basic({_vendor_code(symbol)})"] == 1 for symbol in FIXED_SYMBOLS)
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


def test_sealed_synthetic_capture_is_consumed_by_forward_pair_ranker(tmp_path: Path) -> None:
    paths, reference = _fixture(tmp_path)
    state = FakeBaoStockState(reference)
    capture_at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    capture_dir = tmp_path / "capture-then-rank"

    captured = _capture(tmp_path, paths, state, capture_dir, capture_at)
    assert captured["manifest"]["status"] == "CAPTURED_INPUTS_READY"

    ranking = create_lookback_pair_ranking(
        bars_path=capture_dir / "bars.csv",
        config_path=paths["config"],
        calendar_path=capture_dir / "trading-calendar.csv",
        universe_path=capture_dir / "point-in-time-universe.csv",
        pool_path=paths["pool"],
        identity_events_path=paths["identity"],
        registration_freeze_path=paths["freeze"],
        capture_manifest_path=capture_dir / "capture-manifest.json",
        signal_date="2026-12-17",
        output_dir=tmp_path / "paired-ranking",
        mode="FORWARD_PAPER",
        now_utc=datetime(2026, 12, 17, 8, 0, tzinfo=timezone.utc),
    )

    assert ranking["manifest"]["status"] == "CAPTURED_FORWARD_RANKING_ONLY"
    assert ranking["manifest"]["classification"] == "FORWARD_PAPER_CANDIDATE_ONLY"
    assert ranking["manifest"]["common_candidate_count"] == len(FIXED_SYMBOLS)
    assert ranking["manifest"]["input_sha256"]["capture_manifest"] == _sha(
        capture_dir / "capture-manifest.json",
    )
    assert ranking["manifest"]["capture_checks"]["source_baostock_adapter"] == "VERIFIED"
    assert ranking["manifest"]["capture_checks"]["source_lookback_pair_capture"] == "VERIFIED"
    assert (Path(ranking["output_dir"]) / "ranking-20d.csv").is_file()
    assert (Path(ranking["output_dir"]) / "ranking-10d.csv").is_file()

    capture_manifest_path = capture_dir / "capture-manifest.json"
    capture_manifest_path.write_text(
        capture_manifest_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    tampered = create_lookback_pair_ranking(
        bars_path=capture_dir / "bars.csv",
        config_path=paths["config"],
        calendar_path=capture_dir / "trading-calendar.csv",
        universe_path=capture_dir / "point-in-time-universe.csv",
        pool_path=paths["pool"],
        identity_events_path=paths["identity"],
        registration_freeze_path=paths["freeze"],
        capture_manifest_path=capture_manifest_path,
        signal_date="2026-12-17",
        output_dir=tmp_path / "tampered-paired-ranking",
        mode="FORWARD_PAPER",
        now_utc=datetime(2026, 12, 17, 8, 0, tzinfo=timezone.utc),
    )
    assert tampered["manifest"]["status"] == "NEEDS_DATA"
    assert any("封存 SHA-256 不匹配" in reason for reason in tampered["manifest"]["blocking_reasons"])
    assert not (Path(tampered["output_dir"]) / "ranking-20d.csv").exists()


@pytest.mark.parametrize(
    ("issue", "expected_error"),
    [
        ("missing", "必需响应返回 0 行"),
        ("duplicate", "证券基础资料含空代码或重复代码"),
        ("type", "含非股票或未知 type"),
    ],
)
def test_basic_response_missing_duplicate_or_wrong_type_blocks_capture(
    tmp_path: Path, issue: str, expected_error: str,
) -> None:
    paths, reference = _fixture(tmp_path)
    state = FakeBaoStockState(reference, basic_issue=(FIXED_SYMBOLS[0], issue))
    output = tmp_path / f"bad-basic-{issue}"
    at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)

    result = _capture(tmp_path, paths, state, output, at)

    request = result["manifest"]["requests"][f"stock_basic:{FIXED_SYMBOLS[0]}"]
    assert result["manifest"]["status"] == "PARTIAL_CAPTURE_RETRY_2_PENDING"
    assert request["status"] == "FAILED"
    assert expected_error in request["attempts"][0]["error"]["message"]
    assert result["manifest"]["rankings_generated"] is False


def test_adapter_hash_mismatch_blocks_before_provider_session(tmp_path: Path) -> None:
    paths, reference = _fixture(tmp_path)
    freeze = json.loads(paths["freeze"].read_text(encoding="utf-8"))
    freeze["frozen_input_files"]["baostock_adapter"]["sha256"] = "0" * 64
    paths["freeze"].write_text(json.dumps(freeze, indent=2) + "\n", encoding="utf-8")
    paths["freeze"].with_suffix(".sha256").write_text(
        f"{_sha(paths['freeze'])}  {paths['freeze'].name}\n", encoding="ascii",
    )
    sessions: list[object] = []

    def forbidden_session(**kwargs: Any):
        sessions.append(kwargs)
        raise AssertionError("adapter hash mismatch must block before provider connection")

    at = datetime(2026, 12, 17, 15, 10, tzinfo=SHANGHAI)
    with pytest.raises(ValueError, match="BaoStock 适配器路径或 SHA-256 与冻结登记不匹配"):
        capture_lookback_pair_inputs(
            output_dir=tmp_path / "adapter-mismatch",
            registration_freeze_path=paths["freeze"],
            config_path=paths["config"],
            pool_path=paths["pool"],
            identity_events_path=paths["identity"],
            signal_date="2026-12-17",
            now=at,
            clock_fn=_clock(at),
            session_factory=forbidden_session,
        )
    assert not sessions


def test_capture_retries_only_failed_request_and_never_overwrites_attempts(tmp_path: Path) -> None:
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
    paths, reference = _fixture(tmp_path)
    state = FakeBaoStockState(reference)
    output = tmp_path / "too-early"
    too_early = datetime(2026, 12, 17, 15, 5, 59, tzinfo=SHANGHAI)

    with pytest.raises(ValueError, match="15:06"):
        _capture(tmp_path, paths, state, output, too_early)

    assert state.session_count == 0
    assert not output.exists()


def test_capture_response_validator_rejects_unknown_status_duplicate_and_empty_frames() -> None:
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
