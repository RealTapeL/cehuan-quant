"""Strict CSV adapter for the currently scheduled accounting event subset."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
from io import BytesIO
from pathlib import Path

import pandas as pd

from cehuan.accounting.events import ActionSource, CashDividendEvent, CodeChangeEvent, ShareDistributionEvent
from cehuan.accounting.models import decimal_value


ACCOUNTING_EVENT_CSV_COLUMNS = [
    "event_id", "event_type", "known_at_utc", "symbol", "record_date", "ex_date",
    "payable_date", "gross_per_share_cny", "old_symbol", "new_symbol", "effective_date",
    "registration_date", "tradable_date", "security_identity", "share_ratio",
    "fractional_share_method", "tax_cny", "source_id", "source_url",
    "source_sha256", "source_title", "source_page_reference", "source_archive_file",
    "review_status", "reviewed_by", "reviewed_at",
]
ACCOUNTING_EVENT_TYPES = {"CASH_DIVIDEND", "CODE_CHANGE_1_TO_1", "SHARE_DISTRIBUTION"}
SOURCE_REVIEW_COLUMNS = (
    "source_id", "source_url", "source_sha256", "source_title", "source_page_reference",
    "source_archive_file", "review_status", "reviewed_by", "reviewed_at",
)


@dataclass(frozen=True)
class AccountingEventBundle:
    events: tuple[CashDividendEvent | CodeChangeEvent | ShareDistributionEvent, ...]
    dividend_tax_cny_by_event: dict[str, Decimal | None]
    source_audit: pd.DataFrame
    source_csv_sha256: str


def _text(value: object) -> str:
    return "" if pd.isna(value) else str(value).strip()


def read_accounting_event_csv(path: str | Path) -> AccountingEventBundle:
    """Read reviewed supported event rows and verify each archived source.

    This checks event-key uniqueness, event-to-source row association, HTTPS
    metadata, reviewer fields, and the archived file's byte hash. It does not
    determine whether the source document's interpretation is correct.
    """
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"公司行动事件 CSV 不存在：{source_path}")
    raw_bytes = source_path.read_bytes()
    frame = pd.read_csv(BytesIO(raw_bytes), dtype=str, keep_default_na=False)
    missing = sorted(set(ACCOUNTING_EVENT_CSV_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError("公司行动事件 CSV 缺少字段：" + ", ".join(missing))
    frame = frame[ACCOUNTING_EVENT_CSV_COLUMNS].copy()
    for column in frame.columns:
        frame[column] = frame[column].map(_text)
    if frame.empty:
        return AccountingEventBundle((), {}, pd.DataFrame(columns=[
            "event_id", "event_type", "source_id", "source_archive_file",
            "declared_sha256", "observed_sha256", "review_status", "reviewed_by", "reviewed_at",
        ]), hashlib.sha256(raw_bytes).hexdigest())
    if frame["event_id"].eq("").any():
        raise ValueError("公司行动事件 event_id 不能为空")
    duplicate_ids = sorted(frame.loc[frame["event_id"].duplicated(keep=False), "event_id"].unique())
    if duplicate_ids:
        raise ValueError("公司行动事件 event_id 必须唯一：" + ", ".join(duplicate_ids))
    unknown_types = sorted(set(frame["event_type"]).difference(ACCOUNTING_EVENT_TYPES))
    if unknown_types:
        raise ValueError(
            "当前 CSV 适配器只支持 CASH_DIVIDEND/CODE_CHANGE_1_TO_1/SHARE_DISTRIBUTION："
            + ", ".join(unknown_types)
        )

    events: list[CashDividendEvent | CodeChangeEvent | ShareDistributionEvent] = []
    taxes: dict[str, Decimal | None] = {}
    audits: list[dict[str, object]] = []
    for row in frame.to_dict(orient="records"):
        event_id = row["event_id"]
        missing_source = [column for column in SOURCE_REVIEW_COLUMNS if not row[column]]
        if missing_source:
            raise ValueError(f"{event_id} 缺少来源/复核字段：" + ", ".join(missing_source))
        if row["review_status"] != "REVIEWED":
            raise ValueError(f"{event_id} 来源复核状态必须为 REVIEWED")

        archive_path = Path(row["source_archive_file"]).expanduser()
        if not archive_path.is_absolute():
            archive_path = source_path.parent / archive_path
        archive_path = archive_path.resolve()
        if not archive_path.is_file():
            raise FileNotFoundError(f"{event_id} 来源原件不存在：{archive_path}")
        observed_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        declared_sha256 = row["source_sha256"].lower()
        if observed_sha256 != declared_sha256:
            raise ValueError(
                f"{event_id} 的事件键与来源原件 SHA-256 不匹配："
                f"declared={declared_sha256}, observed={observed_sha256}"
            )
        action_source = ActionSource(
            source_id=row["source_id"],
            url=row["source_url"],
            sha256=declared_sha256,
            title=row["source_title"],
            page_reference=row["source_page_reference"],
            reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"],
        )
        if row["event_type"] == "CASH_DIVIDEND":
            if any(row[column] for column in (
                "old_symbol", "new_symbol", "effective_date", "registration_date", "tradable_date",
                "security_identity", "share_ratio", "fractional_share_method",
            )):
                raise ValueError(f"{event_id} 分红行包含代码变更专属字段")
            for column in ("symbol", "record_date", "ex_date", "payable_date", "gross_per_share_cny"):
                if not row[column]:
                    raise ValueError(f"{event_id} 分红行缺少 {column}")
            event = CashDividendEvent(
                event_id=event_id,
                symbol=row["symbol"],
                record_date=row["record_date"],
                ex_date=row["ex_date"],
                payable_date=row["payable_date"],
                gross_per_share_cny=Decimal(row["gross_per_share_cny"]),
                known_at_utc=row["known_at_utc"],
                source=action_source,
            )
            taxes[event_id] = (
                None if not row["tax_cny"]
                else decimal_value(row["tax_cny"], f"{event_id}.tax_cny", nonnegative=True)
            )
        elif row["event_type"] == "CODE_CHANGE_1_TO_1":
            if any(row[column] for column in (
                "symbol", "record_date", "ex_date", "payable_date", "gross_per_share_cny", "tax_cny",
            )):
                raise ValueError(f"{event_id} 代码变更行包含非代码变更字段")
            for column in ("old_symbol", "new_symbol", "effective_date", "security_identity", "share_ratio"):
                if not row[column]:
                    raise ValueError(f"{event_id} 代码变更行缺少 {column}")
            if any(row[column] for column in ("registration_date", "tradable_date", "fractional_share_method")):
                raise ValueError(f"{event_id} 代码变更行包含送转专属字段")
            event = CodeChangeEvent(
                event_id=event_id,
                old_symbol=row["old_symbol"],
                new_symbol=row["new_symbol"],
                effective_date=row["effective_date"],
                security_identity=row["security_identity"],
                share_ratio=Decimal(row["share_ratio"]),
                known_at_utc=row["known_at_utc"],
                source=action_source,
            )
        else:
            if any(row[column] for column in (
                "ex_date", "payable_date", "gross_per_share_cny", "old_symbol", "new_symbol",
                "security_identity", "tax_cny",
            )):
                raise ValueError(f"{event_id} 送转行包含分红或代码变更专属字段")
            for column in (
                "symbol", "record_date", "effective_date", "registration_date", "tradable_date",
                "share_ratio", "fractional_share_method",
            ):
                if not row[column]:
                    raise ValueError(f"{event_id} 送转行缺少 {column}")
            event = ShareDistributionEvent(
                event_id=event_id,
                symbol=row["symbol"],
                record_date=row["record_date"],
                effective_date=row["effective_date"],
                registration_date=row["registration_date"],
                tradable_date=row["tradable_date"],
                shares_per_old_share=Decimal(row["share_ratio"]),
                fractional_share_method=row["fractional_share_method"],
                known_at_utc=row["known_at_utc"],
                source=action_source,
            )
        events.append(event)
        audits.append({
            "event_id": event_id,
            "event_type": row["event_type"],
            "source_id": action_source.source_id,
            "source_url": action_source.url,
            "source_archive_file": str(archive_path),
            "declared_sha256": declared_sha256,
            "observed_sha256": observed_sha256,
            "source_hash_matches": True,
            "review_status": row["review_status"],
            "reviewed_by": action_source.reviewed_by,
            "reviewed_at": action_source.reviewed_at.isoformat(),
        })

    return AccountingEventBundle(
        events=tuple(events),
        dividend_tax_cny_by_event=taxes,
        source_audit=pd.DataFrame(audits),
        source_csv_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
