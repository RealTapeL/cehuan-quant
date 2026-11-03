"""Versioned checkpoints for deterministic rule-based backtest continuation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from cehuan.accounting.models import LedgerSnapshot, calendar_date
from cehuan.accounting.snapshot_io import ledger_snapshot_from_dict


@dataclass(frozen=True)
class SignalHistoryBar:
    symbol: str
    trade_date: date
    close_cny: Decimal
    signal_price_cny: Decimal | None

    def __post_init__(self) -> None:
        if not str(self.symbol).strip():
            raise ValueError("checkpoint signal history symbol 不能为空")
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "trade_date", calendar_date(self.trade_date, "signal_history.trade_date"))
        close = Decimal(str(self.close_cny))
        if not close.is_finite() or close <= 0:
            raise ValueError("checkpoint signal history close 必须是有限正数")
        object.__setattr__(self, "close_cny", close)
        signal = None if self.signal_price_cny is None else Decimal(str(self.signal_price_cny))
        if signal is not None and (not signal.is_finite() or signal <= 0):
            raise ValueError("checkpoint signal_price 必须是有限正数或空值")
        object.__setattr__(self, "signal_price_cny", signal)


@dataclass(frozen=True)
class CheckpointPrice:
    symbol: str
    price_cny: Decimal
    price_date: date
    global_session_index: int
    source: str

    def __post_init__(self) -> None:
        if not str(self.symbol).strip() or not str(self.source).strip():
            raise ValueError("checkpoint price 必须包含 symbol/source")
        price = Decimal(str(self.price_cny))
        if not price.is_finite() or price <= 0:
            raise ValueError("checkpoint price 必须是有限正数")
        if isinstance(self.global_session_index, bool) or self.global_session_index < 0:
            raise ValueError("checkpoint global_session_index 必须是非负整数")
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        object.__setattr__(self, "price_cny", price)
        object.__setattr__(self, "price_date", calendar_date(self.price_date, "last_price.price_date"))
        object.__setattr__(self, "source", str(self.source).strip())


@dataclass(frozen=True)
class BacktestCheckpoint:
    schema_version: int
    strategy_id: str
    as_of_date: date
    next_expected_session: date
    global_session_index: int
    trade_count: int
    pending_targets: tuple[tuple[str, float], ...]
    transition_signal_ready_index: tuple[tuple[str, int], ...]
    signal_history: tuple[SignalHistoryBar, ...]
    signal_price_modes: tuple[tuple[str, str], ...]
    last_prices: tuple[CheckpointPrice, ...]
    previous_equity_cny: Decimal
    ledger_snapshot: LedgerSnapshot
    config_sha256: str
    market_rules_sha256: str | None
    fee_schedule_sha256: str | None
    accounting_events_sha256: str
    identity_events_sha256: str
    corporate_actions_sha256: str
    calendar_sha256: str
    calendar_prefix_sha256: str
    last_window_data_sha256: str
    last_window_bars_sha256: str
    data_chain_sha256: str
    source_data_path: str | None = None
    benchmark_symbol: str | None = None
    benchmark_previous_close_cny: Decimal | None = None
    state_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"不支持的 backtest checkpoint schema_version={self.schema_version}")
        if not str(self.strategy_id).strip():
            raise ValueError("backtest checkpoint strategy_id 不能为空")
        as_of = calendar_date(self.as_of_date, "checkpoint.as_of_date")
        next_session = calendar_date(self.next_expected_session, "checkpoint.next_expected_session")
        if next_session <= as_of:
            raise ValueError("checkpoint.next_expected_session 必须晚于 as_of_date")
        if isinstance(self.global_session_index, bool) or self.global_session_index < 0:
            raise ValueError("checkpoint.global_session_index 必须是非负整数")
        if isinstance(self.trade_count, bool) or self.trade_count < 0:
            raise ValueError("checkpoint.trade_count 必须是非负整数")
        if self.ledger_snapshot.as_of_date != as_of:
            raise ValueError("checkpoint 与账本快照 as_of_date 不一致")
        pending = tuple(sorted((str(symbol).strip().upper(), float(weight)) for symbol, weight in self.pending_targets))
        if any(not symbol or not 0 <= weight <= 1 for symbol, weight in pending):
            raise ValueError("checkpoint pending_targets 包含无效标的或权重")
        if len({symbol for symbol, _ in pending}) != len(pending):
            raise ValueError("checkpoint pending_targets 存在重复证券")
        transition = tuple(sorted((str(symbol).strip().upper(), int(index)) for symbol, index in self.transition_signal_ready_index))
        if any(not symbol or index < 0 for symbol, index in transition):
            raise ValueError("checkpoint transition signal state 无效")
        modes = tuple(sorted((str(symbol).strip().upper(), str(mode).strip()) for symbol, mode in self.signal_price_modes))
        if len({symbol for symbol, _ in modes}) != len(modes):
            raise ValueError("checkpoint signal price modes 存在重复证券")
        if any(mode not in {"adjusted_close", "raw_close_no_adjustment", "unusable_mixed_or_nonpositive_adjustment"} for _, mode in modes):
            raise ValueError("checkpoint signal price mode 未知")
        if len({(item.trade_date, item.symbol) for item in self.signal_history}) != len(self.signal_history):
            raise ValueError("checkpoint signal history 存在重复证券日")
        if any(item.trade_date > as_of for item in self.signal_history):
            raise ValueError("checkpoint signal history 含未来数据")
        equity = Decimal(str(self.previous_equity_cny))
        if not equity.is_finite() or equity <= 0:
            raise ValueError("checkpoint previous_equity_cny 必须是有限正数")
        benchmark_close = (
            None if self.benchmark_previous_close_cny is None
            else Decimal(str(self.benchmark_previous_close_cny))
        )
        if benchmark_close is not None and (not benchmark_close.is_finite() or benchmark_close <= 0):
            raise ValueError("checkpoint benchmark_previous_close_cny 必须是有限正数")
        for name in (
            "config_sha256", "accounting_events_sha256", "identity_events_sha256",
            "corporate_actions_sha256", "calendar_sha256", "calendar_prefix_sha256", "last_window_data_sha256",
            "last_window_bars_sha256",
            "data_chain_sha256",
        ):
            value = str(getattr(self, name)).strip().lower()
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"checkpoint {name} 必须是 SHA-256")
            object.__setattr__(self, name, value)
        for name in ("market_rules_sha256", "fee_schedule_sha256"):
            value = getattr(self, name)
            if value is not None:
                value = str(value).strip().lower()
                if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                    raise ValueError(f"checkpoint {name} 必须是 SHA-256 或空")
                object.__setattr__(self, name, value)
        object.__setattr__(self, "as_of_date", as_of)
        object.__setattr__(self, "next_expected_session", next_session)
        object.__setattr__(self, "pending_targets", pending)
        object.__setattr__(self, "transition_signal_ready_index", transition)
        object.__setattr__(self, "signal_price_modes", modes)
        object.__setattr__(self, "signal_history", tuple(self.signal_history))
        object.__setattr__(self, "last_prices", tuple(self.last_prices))
        object.__setattr__(self, "previous_equity_cny", equity)
        object.__setattr__(self, "benchmark_symbol", self.benchmark_symbol.strip().upper() if self.benchmark_symbol else None)
        object.__setattr__(self, "benchmark_previous_close_cny", benchmark_close)
        object.__setattr__(self, "source_data_path", str(Path(self.source_data_path).expanduser().resolve()) if self.source_data_path else None)
        if self.state_sha256 and self.state_sha256 != self.calculate_state_sha256():
            raise ValueError("backtest checkpoint 状态哈希不匹配；文件可能被改写")

    def _payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("state_sha256", None)
        return payload

    def calculate_state_sha256(self) -> str:
        data = json.dumps(self._payload(), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def sealed(self) -> "BacktestCheckpoint":
        return replace(self, state_sha256=self.calculate_state_sha256())

    def to_dict(self) -> dict[str, Any]:
        sealed = self if self.state_sha256 else self.sealed()
        payload = asdict(sealed)
        payload["as_of_date"] = sealed.as_of_date.isoformat()
        payload["next_expected_session"] = sealed.next_expected_session.isoformat()
        payload["previous_equity_cny"] = str(sealed.previous_equity_cny)
        if sealed.benchmark_previous_close_cny is not None:
            payload["benchmark_previous_close_cny"] = str(sealed.benchmark_previous_close_cny)
        for row in payload["signal_history"]:
            row["trade_date"] = row["trade_date"].isoformat()
            row["close_cny"] = str(row["close_cny"])
            row["signal_price_cny"] = None if row["signal_price_cny"] is None else str(row["signal_price_cny"])
        for row in payload["last_prices"]:
            row["price_date"] = row["price_date"].isoformat()
            row["price_cny"] = str(row["price_cny"])
        payload["ledger_snapshot"] = asdict(sealed.ledger_snapshot)
        return payload


def backtest_checkpoint_from_dict(value: Mapping[str, Any]) -> BacktestCheckpoint:
    if not isinstance(value, Mapping):
        raise ValueError("backtest checkpoint JSON 顶层必须是对象")
    required = {
        "schema_version", "strategy_id", "as_of_date", "next_expected_session", "global_session_index", "trade_count",
        "pending_targets", "transition_signal_ready_index", "signal_history", "signal_price_modes",
        "last_prices", "previous_equity_cny", "ledger_snapshot", "config_sha256", "market_rules_sha256",
        "fee_schedule_sha256", "accounting_events_sha256", "identity_events_sha256", "corporate_actions_sha256",
        "calendar_sha256", "calendar_prefix_sha256", "last_window_data_sha256", "last_window_bars_sha256",
        "data_chain_sha256", "state_sha256",
    }
    missing = required.difference(value)
    if missing:
        raise ValueError("backtest checkpoint 缺少字段：" + ", ".join(sorted(missing)))
    history = tuple(SignalHistoryBar(
        symbol=str(row["symbol"]), trade_date=row["trade_date"],
        close_cny=Decimal(str(row["close_cny"])),
        signal_price_cny=None if row.get("signal_price_cny") is None else Decimal(str(row["signal_price_cny"])),
    ) for row in value["signal_history"])
    last_prices = tuple(CheckpointPrice(
        symbol=str(row["symbol"]), price_cny=Decimal(str(row["price_cny"])),
        price_date=row["price_date"], global_session_index=int(row["global_session_index"]),
        source=str(row["source"]),
    ) for row in value["last_prices"])
    snapshot = ledger_snapshot_from_dict(value["ledger_snapshot"])
    return BacktestCheckpoint(
        schema_version=int(value["schema_version"]),
        strategy_id=str(value["strategy_id"]),
        as_of_date=value["as_of_date"],
        next_expected_session=value["next_expected_session"],
        global_session_index=int(value["global_session_index"]),
        trade_count=int(value["trade_count"]),
        pending_targets=tuple((str(row[0]), float(row[1])) for row in value["pending_targets"]),
        transition_signal_ready_index=tuple((str(row[0]), int(row[1])) for row in value["transition_signal_ready_index"]),
        signal_history=history,
        signal_price_modes=tuple((str(row[0]), str(row[1])) for row in value["signal_price_modes"]),
        last_prices=last_prices,
        previous_equity_cny=Decimal(str(value["previous_equity_cny"])),
        ledger_snapshot=snapshot,
        config_sha256=str(value["config_sha256"]),
        market_rules_sha256=value.get("market_rules_sha256"),
        fee_schedule_sha256=value.get("fee_schedule_sha256"),
        accounting_events_sha256=str(value["accounting_events_sha256"]),
        identity_events_sha256=str(value["identity_events_sha256"]),
        corporate_actions_sha256=str(value["corporate_actions_sha256"]),
        calendar_sha256=str(value["calendar_sha256"]),
        calendar_prefix_sha256=str(value["calendar_prefix_sha256"]),
        last_window_data_sha256=str(value["last_window_data_sha256"]),
        last_window_bars_sha256=str(value["last_window_bars_sha256"]),
        data_chain_sha256=str(value["data_chain_sha256"]),
        source_data_path=value.get("source_data_path"),
        benchmark_symbol=value.get("benchmark_symbol"),
        benchmark_previous_close_cny=(
            None if value.get("benchmark_previous_close_cny") is None
            else Decimal(str(value["benchmark_previous_close_cny"]))
        ),
        state_sha256=str(value["state_sha256"]),
    )


def read_backtest_checkpoint(path: str | Path) -> BacktestCheckpoint:
    checkpoint_path = Path(path).expanduser().resolve()
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"backtest checkpoint 不存在：{checkpoint_path}")
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"backtest checkpoint JSON 无法解析：{checkpoint_path}") from exc
    checkpoint = backtest_checkpoint_from_dict(payload)
    if checkpoint.source_data_path:
        bound_path = Path(checkpoint.source_data_path)
        if not bound_path.is_file():
            raise FileNotFoundError(f"checkpoint 绑定的历史行情文件不存在：{bound_path}")
        with bound_path.open("rb") as handle:
            observed = hashlib.file_digest(handle, "sha256").hexdigest()
        if observed != checkpoint.last_window_data_sha256:
            raise ValueError("checkpoint 绑定的上一窗口历史行情 SHA-256 不匹配；请从受影响日期重算")
    return checkpoint
