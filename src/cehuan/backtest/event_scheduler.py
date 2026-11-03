"""Date-indexed scheduling for a deliberately small ledger-v2 event subset."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping

from cehuan.accounting.events import CashDividendEvent, CodeChangeEvent, ShareDistributionEvent
from cehuan.accounting.models import decimal_value


SupportedAccountingEvent = CashDividendEvent | CodeChangeEvent | ShareDistributionEvent


@dataclass(frozen=True)
class AccountingEventSchedule:
    """Validate event/session alignment and expose explicit ledger phases.

    Supported events are cash dividends, 1:1 code changes, and share
    distributions. Other action types fail closed.
    """

    sessions: frozenset[date]
    code_changes: Mapping[date, tuple[CodeChangeEvent, ...]]
    dividend_records: Mapping[date, tuple[CashDividendEvent, ...]]
    dividend_accruals: Mapping[date, tuple[CashDividendEvent, ...]]
    dividend_payments: Mapping[date, tuple[CashDividendEvent, ...]]
    distribution_records: Mapping[date, tuple[ShareDistributionEvent, ...]]
    distribution_effective: Mapping[date, tuple[ShareDistributionEvent, ...]]
    distribution_registrations: Mapping[date, tuple[ShareDistributionEvent, ...]]
    distribution_tradabilities: Mapping[date, tuple[ShareDistributionEvent, ...]]
    dividend_tax_cny: Mapping[str, Decimal | int | str | None]
    events: tuple[SupportedAccountingEvent, ...]
    window_start: date
    window_end: date

    @classmethod
    def build(
        cls,
        events: Iterable[SupportedAccountingEvent],
        session_dates: Iterable[date],
        dividend_tax_cny: Mapping[str, Decimal | int | str | None] | None = None,
    ) -> "AccountingEventSchedule":
        sessions = frozenset(session_dates)
        code_changes: dict[date, list[CodeChangeEvent]] = defaultdict(list)
        dividend_records: dict[date, list[CashDividendEvent]] = defaultdict(list)
        dividend_accruals: dict[date, list[CashDividendEvent]] = defaultdict(list)
        dividend_payments: dict[date, list[CashDividendEvent]] = defaultdict(list)
        distribution_records: dict[date, list[ShareDistributionEvent]] = defaultdict(list)
        distribution_effective: dict[date, list[ShareDistributionEvent]] = defaultdict(list)
        distribution_registrations: dict[date, list[ShareDistributionEvent]] = defaultdict(list)
        distribution_tradabilities: dict[date, list[ShareDistributionEvent]] = defaultdict(list)
        if not sessions:
            raise ValueError("公司行动调度至少需要一个输入交易日")
        window_start, window_end = min(sessions), max(sessions)
        event_ids: set[str] = set()
        dividend_ids: set[str] = set()
        event_rows: list[SupportedAccountingEvent] = []

        def schedule_phase(mapping: dict[date, list[object]], day: date, label: str, event: object) -> None:
            if window_start <= day <= window_end and day not in sessions:
                event_id = getattr(event, "event_id", "unknown")
                raise ValueError(f"账务事件 {event_id} 的{label}落在输入区间内但缺少交易日：{day}")
            if day in sessions:
                mapping[day].append(event)

        for event in events:
            if not isinstance(event, (CashDividendEvent, CodeChangeEvent, ShareDistributionEvent)):
                raise TypeError(
                    "当前 ledger-v2 事件调度器只接受 CashDividendEvent/CodeChangeEvent/"
                    f"ShareDistributionEvent；收到 {type(event).__name__}"
                )
            if event.event_id in event_ids:
                raise ValueError(f"账务事件 event_id 重复：{event.event_id}")
            event_ids.add(event.event_id)
            event_rows.append(event)
            if isinstance(event, CodeChangeEvent):
                schedule_phase(code_changes, event.effective_date, "生效日", event)
                continue

            if isinstance(event, ShareDistributionEvent):
                for label, day in (
                    ("登记日", event.record_date),
                    ("生效日", event.effective_date),
                    ("新增股份登记日", event.registration_date),
                    ("可流通日", event.tradable_date),
                ):
                    schedule_phase(
                        {
                            "登记日": distribution_records,
                            "生效日": distribution_effective,
                            "新增股份登记日": distribution_registrations,
                            "可流通日": distribution_tradabilities,
                        }[label],
                        day,
                        label,
                        event,
                    )
                continue

            dividend_ids.add(event.event_id)
            schedule_phase(dividend_records, event.record_date, "登记日", event)
            schedule_phase(dividend_accruals, event.ex_date, "除息日", event)
            schedule_phase(dividend_payments, event.payable_date, "派息日", event)

        taxes = dict(dividend_tax_cny or {})
        unknown_taxes = set(taxes).difference(dividend_ids)
        if unknown_taxes:
            raise ValueError("分红税费表含未知 event_id：" + ", ".join(sorted(unknown_taxes)))
        for event_id, amount in list(taxes.items()):
            if amount is None:
                continue
            taxes[event_id] = decimal_value(amount, f"{event_id}.tax_cny", nonnegative=True)

        def freeze(mapping: dict[date, list[object]]) -> dict[date, tuple]:
            return {day: tuple(sorted(rows, key=lambda event: event.event_id)) for day, rows in mapping.items()}

        return cls(
            sessions=sessions,
            code_changes=freeze(code_changes),
            dividend_records=freeze(dividend_records),
            dividend_accruals=freeze(dividend_accruals),
            dividend_payments=freeze(dividend_payments),
            distribution_records=freeze(distribution_records),
            distribution_effective=freeze(distribution_effective),
            distribution_registrations=freeze(distribution_registrations),
            distribution_tradabilities=freeze(distribution_tradabilities),
            dividend_tax_cny=taxes,
            events=tuple(sorted(event_rows, key=lambda event: event.event_id)),
            window_start=window_start,
            window_end=window_end,
        )
