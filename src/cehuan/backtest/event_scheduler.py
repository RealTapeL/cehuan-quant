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
    dividend_tax_cny: Mapping[str, Decimal | int | str | None]

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
        event_ids: set[str] = set()
        dividend_ids: set[str] = set()

        for event in events:
            if not isinstance(event, (CashDividendEvent, CodeChangeEvent, ShareDistributionEvent)):
                raise TypeError(
                    "当前 ledger-v2 事件调度器只接受 CashDividendEvent/CodeChangeEvent/"
                    f"ShareDistributionEvent；收到 {type(event).__name__}"
                )
            if event.event_id in event_ids:
                raise ValueError(f"账务事件 event_id 重复：{event.event_id}")
            event_ids.add(event.event_id)
            if isinstance(event, CodeChangeEvent):
                if event.effective_date not in sessions:
                    raise ValueError(f"代码变更 {event.event_id} 生效日不在输入交易日中：{event.effective_date}")
                code_changes[event.effective_date].append(event)
                continue

            if isinstance(event, ShareDistributionEvent):
                for label, day in (
                    ("登记日", event.record_date),
                    ("生效日", event.effective_date),
                    ("新增股份登记日", event.registration_date),
                    ("可流通日", event.tradable_date),
                ):
                    if day not in sessions:
                        raise ValueError(f"送转 {event.event_id} 的{label}不在输入交易日中：{day}")
                distribution_records[event.record_date].append(event)
                distribution_effective[event.effective_date].append(event)
                distribution_registrations[event.registration_date].append(event)
                continue

            dividend_ids.add(event.event_id)
            for label, day in (
                ("登记日", event.record_date),
                ("除息日", event.ex_date),
                ("派息日", event.payable_date),
            ):
                if day not in sessions:
                    raise ValueError(f"现金分红 {event.event_id} 的{label}不在输入交易日中：{day}")
            dividend_records[event.record_date].append(event)
            dividend_accruals[event.ex_date].append(event)
            dividend_payments[event.payable_date].append(event)

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
            dividend_tax_cny=taxes,
        )
