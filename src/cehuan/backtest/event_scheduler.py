"""Date-indexed scheduling for a deliberately small ledger-v2 event subset."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Mapping

from cehuan.accounting.events import (
    CashDividendEvent,
    CodeChangeEvent,
    RightsOfferingDecision,
    RightsOfferingEvent,
    ShareDistributionEvent,
)
from cehuan.accounting.models import decimal_value


SupportedAccountingEvent = CashDividendEvent | CodeChangeEvent | ShareDistributionEvent | RightsOfferingEvent


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
    rights_records: Mapping[date, tuple[RightsOfferingEvent, ...]]
    rights_registrations: Mapping[date, tuple[RightsOfferingEvent, ...]]
    rights_tradabilities: Mapping[date, tuple[RightsOfferingEvent, ...]]
    rights_decisions: Mapping[date, tuple[tuple[RightsOfferingEvent, RightsOfferingDecision], ...]]
    rights_decisions_by_event: Mapping[str, RightsOfferingDecision]
    dividend_tax_cny: Mapping[str, Decimal | int | str | None]
    dividend_tax_treatment: Mapping[str, str]
    dividend_payment_withholding_per_share_cny: Mapping[str, Decimal | int | str | None]
    events: tuple[SupportedAccountingEvent, ...]
    window_start: date
    window_end: date

    @classmethod
    def build(
        cls,
        events: Iterable[SupportedAccountingEvent],
        session_dates: Iterable[date],
        dividend_tax_cny: Mapping[str, Decimal | int | str | None] | None = None,
        dividend_tax_treatment: Mapping[str, str] | None = None,
        dividend_payment_withholding_per_share_cny: Mapping[str, Decimal | int | str | None] | None = None,
        rights_offering_decisions: Mapping[str, RightsOfferingDecision] | None = None,
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
        rights_records: dict[date, list[RightsOfferingEvent]] = defaultdict(list)
        rights_registrations: dict[date, list[RightsOfferingEvent]] = defaultdict(list)
        rights_tradabilities: dict[date, list[RightsOfferingEvent]] = defaultdict(list)
        rights_decisions: dict[date, list[tuple[RightsOfferingEvent, RightsOfferingDecision]]] = defaultdict(list)
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
                if isinstance(event, RightsOfferingEvent):
                    pass
                else:
                    raise TypeError(
                        "当前 ledger-v2 事件调度器只接受 CashDividendEvent/CodeChangeEvent/"
                        f"ShareDistributionEvent/RightsOfferingEvent；收到 {type(event).__name__}"
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

            if isinstance(event, RightsOfferingEvent):
                schedule_phase(rights_records, event.record_date, "配股登记日", event)
                schedule_phase(rights_registrations, event.registration_date, "配股股份登记日", event)
                schedule_phase(rights_tradabilities, event.tradable_date, "配股可流通日", event)
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
        treatments = {
            str(event_id): str(value).strip().upper()
            for event_id, value in (dividend_tax_treatment or {}).items()
        }
        unknown_treatments = set(treatments).difference(dividend_ids)
        if unknown_treatments:
            raise ValueError("分红税务处理表含未知 event_id：" + ", ".join(sorted(unknown_treatments)))
        payment_withholdings = dict(dividend_payment_withholding_per_share_cny or {})
        unknown_withholdings = set(payment_withholdings).difference(dividend_ids)
        if unknown_withholdings:
            raise ValueError("发放日预扣规则表含未知 event_id：" + ", ".join(sorted(unknown_withholdings)))
        for event_id, amount in list(payment_withholdings.items()):
            if amount is not None:
                payment_withholdings[event_id] = decimal_value(
                    amount, f"{event_id}.payment_withholding_per_share_cny", nonnegative=True
                )
        for event_id in dividend_ids:
            treatment = treatments.get(event_id)
            if treatment is None:
                treatment = "FINAL_TAX_KNOWN" if taxes.get(event_id) is not None else "ACCOUNT_EVIDENCE_REQUIRED"
            if treatment not in {"FINAL_TAX_KNOWN", "DEFERRED_BY_HOLDING_PERIOD", "ACCOUNT_EVIDENCE_REQUIRED"}:
                raise ValueError(f"{event_id} 分红税务处理状态未知：{treatment}")
            if treatment == "FINAL_TAX_KNOWN" and taxes.get(event_id) is None:
                raise ValueError(f"{event_id} 采用 FINAL_TAX_KNOWN 时必须提供 tax_cny")
            if treatment == "DEFERRED_BY_HOLDING_PERIOD":
                if taxes.get(event_id) is not None:
                    raise ValueError(f"{event_id} 递延税务不能把最终税额提前写入 tax_cny")
                if event_id not in payment_withholdings or payment_withholdings[event_id] is None:
                    raise ValueError(f"{event_id} 递延税务必须提供发放日每股预扣规则")
            elif payment_withholdings.get(event_id) is not None:
                raise ValueError(
                    f"{event_id} 发放日每股预扣规则只允许用于 DEFERRED_BY_HOLDING_PERIOD"
                )
            if treatment == "ACCOUNT_EVIDENCE_REQUIRED" and taxes.get(event_id) is not None:
                raise ValueError(f"{event_id} 需要账户证据时不能同时声明最终税额")
            treatments[event_id] = treatment

        rights_event_by_id = {
            event.event_id: event for event in event_rows if isinstance(event, RightsOfferingEvent)
        }
        decision_rows = dict(rights_offering_decisions or {})
        unknown_decisions = set(decision_rows).difference(rights_event_by_id)
        if unknown_decisions:
            raise ValueError("配股决策表含未知 event_id：" + ", ".join(sorted(unknown_decisions)))
        for event_id, decision in decision_rows.items():
            event = rights_event_by_id[event_id]
            if not isinstance(decision, RightsOfferingDecision) or decision.event_id != event_id:
                raise ValueError(f"配股 {event_id} 决策必须是匹配 event_id 的 RightsOfferingDecision")
            if decision.disposition == "SUBSCRIBE":
                if not event.subscription_start_date <= decision.decision_date <= event.subscription_end_date:
                    raise ValueError(f"配股 {event_id} SUBSCRIBE 决策日期不在认购期内")
            elif decision.disposition == "DECLINE":
                if decision.decision_date < event.subscription_end_date:
                    raise ValueError(f"配股 {event_id} 只能在认购期结束日或之后确认放弃")
            elif decision.decision_date is not None:
                raise ValueError(f"配股 {event_id} MANUAL_REVIEW 不应自动带有执行日期")
            if decision.decision_date is not None:
                if window_start <= decision.decision_date <= window_end and decision.decision_date not in sessions:
                    raise ValueError(
                        f"账务事件 {event_id} 的配股决策日落在输入区间内但缺少交易日：{decision.decision_date}"
                    )
                rights_decisions[decision.decision_date].append((event, decision))

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
            rights_records=freeze(rights_records),
            rights_registrations=freeze(rights_registrations),
            rights_tradabilities=freeze(rights_tradabilities),
            rights_decisions={
                day: tuple(sorted(rows, key=lambda pair: pair[0].event_id))
                for day, rows in rights_decisions.items()
            },
            rights_decisions_by_event=decision_rows,
            dividend_tax_cny=taxes,
            dividend_tax_treatment=treatments,
            dividend_payment_withholding_per_share_cny=payment_withholdings,
            events=tuple(sorted(event_rows, key=lambda event: event.event_id)),
            window_start=window_start,
            window_end=window_end,
        )
