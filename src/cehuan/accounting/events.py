from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from cehuan.accounting.models import calendar_date, decimal_value, security_symbol


def _aware_instant(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} 必须是带时区的 ISO 时间") from exc
    else:
        raise ValueError(f"{field} 必须是带时区的 ISO 时间")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    return instant.astimezone(timezone.utc)


def _require_known_by(known_at_utc: datetime, day: date, local_time: time, field: str) -> None:
    shanghai = timezone(timedelta(hours=8))
    cutoff = datetime.combine(day, local_time, tzinfo=shanghai).astimezone(timezone.utc)
    if known_at_utc > cutoff:
        raise ValueError(f"{field} 在上海本地允许时点之后才可得，不能用于该事件的点时账务")


@dataclass(frozen=True)
class ActionSource:
    source_id: str
    url: str
    sha256: str
    title: str
    page_reference: str
    reviewed_by: str
    reviewed_at: date | str

    def __post_init__(self) -> None:
        for field in ("source_id", "title", "page_reference", "reviewed_by"):
            value = str(getattr(self, field)).strip()
            if not value:
                raise ValueError(f"公司行动来源 {field} 不能为空")
            object.__setattr__(self, field, value)
        url = str(self.url).strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError("公司行动来源 URL 必须使用 HTTPS")
        digest = str(self.sha256).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("公司行动来源必须提供有效的 SHA-256")
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "reviewed_at", calendar_date(self.reviewed_at, "reviewed_at"))


@dataclass(frozen=True)
class CashDividendEvent:
    event_id: str
    symbol: str
    record_date: date | str
    ex_date: date | str
    payable_date: date | str
    gross_per_share_cny: Decimal
    known_at_utc: datetime | str
    source: ActionSource

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        if not event_id:
            raise ValueError("分红 event_id 不能为空")
        record = calendar_date(self.record_date, "record_date")
        ex_date = calendar_date(self.ex_date, "ex_date")
        payable = calendar_date(self.payable_date, "payable_date")
        if not record <= ex_date <= payable:
            raise ValueError("分红日期必须满足 record_date <= ex_date <= payable_date")
        known_at = _aware_instant(self.known_at_utc, "known_at_utc")
        _require_known_by(known_at, record, time(15, 0), "分红方案")
        if not isinstance(self.source, ActionSource):
            raise ValueError("分红事件必须绑定 ActionSource")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "symbol"))
        object.__setattr__(self, "record_date", record)
        object.__setattr__(self, "ex_date", ex_date)
        object.__setattr__(self, "payable_date", payable)
        object.__setattr__(self, "gross_per_share_cny", decimal_value(
            self.gross_per_share_cny, "gross_per_share_cny", nonnegative=True
        ))
        object.__setattr__(self, "known_at_utc", known_at)


@dataclass(frozen=True)
class ShareDistributionEvent:
    event_id: str
    symbol: str
    record_date: date | str
    effective_date: date | str
    tradable_date: date | str
    shares_per_old_share: Decimal
    known_at_utc: datetime | str
    source: ActionSource

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        if not event_id:
            raise ValueError("送转 event_id 不能为空")
        record = calendar_date(self.record_date, "record_date")
        effective = calendar_date(self.effective_date, "effective_date")
        tradable = calendar_date(self.tradable_date, "tradable_date")
        if not record <= effective <= tradable:
            raise ValueError("送转日期必须满足 record_date <= effective_date <= tradable_date")
        known_at = _aware_instant(self.known_at_utc, "known_at_utc")
        _require_known_by(known_at, record, time(15, 0), "送转方案")
        if not isinstance(self.source, ActionSource):
            raise ValueError("送转事件必须绑定 ActionSource")
        ratio = decimal_value(self.shares_per_old_share, "shares_per_old_share", nonnegative=True)
        if ratio <= 0:
            raise ValueError("shares_per_old_share 必须大于 0")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "symbol"))
        object.__setattr__(self, "record_date", record)
        object.__setattr__(self, "effective_date", effective)
        object.__setattr__(self, "tradable_date", tradable)
        object.__setattr__(self, "shares_per_old_share", ratio)
        object.__setattr__(self, "known_at_utc", known_at)


@dataclass(frozen=True)
class CodeChangeEvent:
    event_id: str
    old_symbol: str
    new_symbol: str
    effective_date: date | str
    security_identity: str
    share_ratio: Decimal
    known_at_utc: datetime | str
    source: ActionSource
    event_type: Literal["CODE_CHANGE_1_TO_1"] = "CODE_CHANGE_1_TO_1"

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        identity = str(self.security_identity).strip()
        if not event_id or not identity:
            raise ValueError("代码变更 event_id/security_identity 不能为空")
        if self.event_type != "CODE_CHANGE_1_TO_1":
            raise ValueError("身份迁移只接受 CODE_CHANGE_1_TO_1；并购换股须用独立公司行动")
        old = security_symbol(self.old_symbol, "old_symbol")
        new = security_symbol(self.new_symbol, "new_symbol")
        if old == new:
            raise ValueError("代码变更前后代码不能相同")
        ratio = decimal_value(self.share_ratio, "share_ratio", nonnegative=True)
        if ratio != Decimal("1"):
            raise ValueError("CODE_CHANGE_1_TO_1 的 share_ratio 必须严格等于 1")
        effective = calendar_date(self.effective_date, "effective_date")
        known_at = _aware_instant(self.known_at_utc, "known_at_utc")
        _require_known_by(known_at, effective, time(9, 15), "代码变更信息")
        if not isinstance(self.source, ActionSource):
            raise ValueError("代码变更事件必须绑定 ActionSource")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "old_symbol", old)
        object.__setattr__(self, "new_symbol", new)
        object.__setattr__(self, "effective_date", effective)
        object.__setattr__(self, "security_identity", identity)
        object.__setattr__(self, "share_ratio", ratio)
        object.__setattr__(self, "known_at_utc", known_at)


@dataclass(frozen=True)
class RightsOfferingEvent:
    event_id: str
    symbol: str
    record_date: date | str
    subscription_start_date: date | str
    subscription_end_date: date | str
    tradable_date: date | str
    rights_per_old_share: Decimal
    subscription_price_cny: Decimal
    known_at_utc: datetime | str
    source: ActionSource

    def __post_init__(self) -> None:
        event_id = str(self.event_id).strip()
        if not event_id:
            raise ValueError("配股 event_id 不能为空")
        record = calendar_date(self.record_date, "record_date")
        start = calendar_date(self.subscription_start_date, "subscription_start_date")
        end = calendar_date(self.subscription_end_date, "subscription_end_date")
        tradable = calendar_date(self.tradable_date, "tradable_date")
        if not record <= start <= end <= tradable:
            raise ValueError("配股日期必须满足 record <= subscription_start <= subscription_end <= tradable")
        known_at = _aware_instant(self.known_at_utc, "known_at_utc")
        _require_known_by(known_at, record, time(15, 0), "配股方案")
        if not isinstance(self.source, ActionSource):
            raise ValueError("配股事件必须绑定 ActionSource")
        ratio = decimal_value(self.rights_per_old_share, "rights_per_old_share", nonnegative=True)
        price = decimal_value(self.subscription_price_cny, "subscription_price_cny", nonnegative=True)
        if ratio <= 0 or price <= 0:
            raise ValueError("配股比例与认购价格必须大于 0")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "symbol", security_symbol(self.symbol, "symbol"))
        object.__setattr__(self, "record_date", record)
        object.__setattr__(self, "subscription_start_date", start)
        object.__setattr__(self, "subscription_end_date", end)
        object.__setattr__(self, "tradable_date", tradable)
        object.__setattr__(self, "rights_per_old_share", ratio)
        object.__setattr__(self, "subscription_price_cny", price)
        object.__setattr__(self, "known_at_utc", known_at)


RightsDisposition = Literal["SUBSCRIBE", "DECLINE"]
