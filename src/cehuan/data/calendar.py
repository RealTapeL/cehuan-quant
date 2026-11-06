from __future__ import annotations

import pandas as pd

from cehuan.data.adjustment import INVALID_SOURCE, signal_price_sources
from cehuan.data.canonical import canonical_symbol, normalize_bar_frame, parse_optional_bool


CALENDAR_REQUIRED = ["trade_date", "is_open", "source"]
CALENDAR_COLUMNS = ["trade_date", "is_open", "previous_trade_date", "source", "retrieved_at_utc"]


def _parse_calendar_date(series: pd.Series) -> pd.Series:
    values = series.astype("string").str.strip()
    compact = values.str.fullmatch(r"\d{8}").fillna(False)
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    parsed.loc[compact] = pd.to_datetime(values.loc[compact], format="%Y%m%d", errors="coerce")
    parsed.loc[~compact] = pd.to_datetime(values.loc[~compact], errors="coerce")
    return parsed.dt.date


def normalize_trading_calendar(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize an imported exchange calendar while retaining provenance."""
    data = frame.copy()
    data.columns = [str(column).strip() for column in data.columns]
    aliases = {"cal_date": "trade_date", "pretrade_date": "previous_trade_date"}
    for source, target in aliases.items():
        if source in data.columns and target not in data.columns:
            data = data.rename(columns={source: target})
    missing = [column for column in CALENDAR_REQUIRED if column not in data.columns]
    if missing:
        raise ValueError("交易日历缺少字段：" + ", ".join(missing))
    for column in CALENDAR_COLUMNS:
        if column not in data.columns:
            data[column] = pd.NA
    data["trade_date"] = _parse_calendar_date(data["trade_date"])
    data["previous_trade_date"] = _parse_calendar_date(data["previous_trade_date"])
    data["is_open"] = parse_optional_bool(data["is_open"])
    data["source"] = data["source"].astype("string").str.strip()
    if data["trade_date"].isna().any():
        raise ValueError("交易日历 trade_date 存在空值或无效日期")
    if data["is_open"].isna().any():
        raise ValueError("交易日历 is_open 只能是 0/1、true/false 或 是/否")
    if data["source"].isna().any() or data["source"].eq("").any():
        raise ValueError("交易日历每行都需要填写 source")
    if data["trade_date"].duplicated().any():
        raise ValueError("交易日历存在重复日期")
    return data[CALENDAR_COLUMNS].sort_values("trade_date").reset_index(drop=True)


def validate_bars_against_calendar(bars: pd.DataFrame, calendar: pd.DataFrame) -> dict[str, object]:
    data = normalize_bar_frame(bars)
    sessions = normalize_trading_calendar(calendar)
    lookup = sessions.set_index("trade_date")["is_open"]
    bar_dates = set(data["trade_date"].dropna())
    missing = sorted(bar_dates.difference(set(lookup.index)))
    closed = sorted(day for day in bar_dates.intersection(set(lookup.index)) if not bool(lookup.loc[day]))
    errors = []
    if missing:
        errors.append("行情日期不在导入的交易日历中：" + ", ".join(day.isoformat() for day in missing[:10]))
    if closed:
        errors.append("行情出现在日历标记的休市日：" + ", ".join(day.isoformat() for day in closed[:10]))
    return {
        "healthy": not errors,
        "calendar_source": ", ".join(sorted(set(sessions["source"].astype(str)))),
        "calendar_start": sessions["trade_date"].min().isoformat() if not sessions.empty else None,
        "calendar_end": sessions["trade_date"].max().isoformat() if not sessions.empty else None,
        "bar_date_count": len(bar_dates),
        "missing_calendar_dates": [day.isoformat() for day in missing],
        "bars_on_closed_dates": [day.isoformat() for day in closed],
        "errors": errors,
    }


def next_open_session(calendar: pd.DataFrame, after: object) -> tuple[str, str] | None:
    sessions = normalize_trading_calendar(calendar)
    cutoff = pd.Timestamp(after).date()
    next_rows = sessions.loc[(sessions["trade_date"] > cutoff) & sessions["is_open"].fillna(False)]
    if next_rows.empty:
        return None
    row = next_rows.iloc[0]
    return row["trade_date"].isoformat(), str(row["source"])


def audit_security_windows(
    bars: pd.DataFrame,
    calendar: pd.DataFrame,
    symbols: list[str] | set[str] | tuple[str, ...],
    *,
    signal_date: object,
    lookback_sessions: int,
) -> pd.DataFrame:
    """Audit a per-security calendar window without inferring missing bars as suspensions."""
    if isinstance(lookback_sessions, bool) or lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须是正整数")
    data = normalize_bar_frame(bars)
    sessions = normalize_trading_calendar(calendar)
    signal_day = pd.Timestamp(signal_date).date()
    opens = sessions.loc[sessions["is_open"].fillna(False), "trade_date"].tolist()
    if signal_day not in opens:
        raise ValueError(f"signal_date {signal_day} 不在交易日历开市日中")
    end = opens.index(signal_day)
    required = opens[max(0, end - lookback_sessions):end + 1]
    window_length = lookback_sessions + 1
    normalized_symbols = sorted({canonical_symbol(symbol) for symbol in symbols})
    duplicate = data.duplicated(["trade_date", "symbol"], keep=False)
    if duplicate.any():
        duplicate_rows = data.loc[duplicate, ["symbol", "trade_date"]].to_dict(orient="records")
        raise ValueError(f"证券日行情有重复行，不能做窗口审计: {duplicate_rows[:10]}")

    date_text = data["trade_date"].astype(str)
    source_by_index = signal_price_sources(data)
    records: list[dict[str, object]] = []
    for symbol in normalized_symbols:
        symbol_mask = data["symbol"].astype(str).eq(symbol)
        history = data.loc[symbol_mask & date_text.le(signal_day.isoformat())].copy()
        history_dates = set(pd.to_datetime(history["trade_date"]).dt.date.tolist())
        actual_dates = history_dates.intersection(set(required))
        missing = [day.isoformat() for day in required if day not in actual_dates]
        # Missing rows inside a complete calendar window are data gaps, not a
        # shorter history. Reserve INSUFFICIENT_HISTORY for the calendar edge.
        insufficient_history = len(required) < window_length
        rows = history.loc[pd.to_datetime(history["trade_date"]).dt.date.isin(required)].copy()
        status_fields = [field for field in ("is_listed", "is_suspended", "is_st") if field not in rows.columns]
        if status_fields:
            unknown_status_dates = ["ALL_REQUIRED_SESSIONS"]
            unknown_status_fields = list(status_fields)
        else:
            unknown_mask = rows[["is_listed", "is_suspended", "is_st"]].isna().any(axis=1)
            unknown_status_dates = sorted({
                *pd.to_datetime(rows.loc[unknown_mask, "trade_date"]).dt.strftime("%Y-%m-%d").tolist(),
                *missing,
            })
            unknown_status_fields = [
                field for field in ("is_listed", "is_suspended", "is_st")
                if missing or rows[field].isna().any()
            ]
        suspended_dates = sorted(pd.to_datetime(rows.loc[rows.get("is_suspended", pd.Series(pd.NA, index=rows.index)).fillna(False).astype(bool), "trade_date"]).dt.strftime("%Y-%m-%d").tolist()) if "is_suspended" in rows else []
        source_rows = source_by_index.loc[rows.index].astype(str)
        invalid_prices = (
            pd.to_numeric(rows["close"], errors="coerce").isna()
            | pd.to_numeric(rows["close"], errors="coerce").le(0)
            | source_rows.eq(INVALID_SOURCE)
        )
        mixed_basis = source_rows.nunique(dropna=False) > 1
        if insufficient_history:
            status = "INSUFFICIENT_HISTORY"
        elif missing:
            status = "MISSING_BAR"
        elif unknown_status_dates:
            status = "UNKNOWN_STATUS"
        elif invalid_prices.any() or mixed_basis:
            status = "INVALID_OR_MIXED_PRICE_WINDOW"
        elif suspended_dates:
            status = "COMPLETE_WITH_SUSPENSION"
        else:
            status = "COMPLETE"
        records.append({
            "symbol": symbol,
            "signal_date": signal_day.isoformat(),
            "expected_session_count": len(required),
            "required_sessions": ";".join(day.isoformat() for day in required),
            "actual_row_count": len(actual_dates),
            "missing_sessions": ";".join(missing),
            "unknown_status_sessions": ";".join(unknown_status_dates),
            "unknown_status_fields": ";".join(unknown_status_fields),
            "suspended_sessions": ";".join(suspended_dates),
            "price_basis_status": "MIXED" if mixed_basis else "INVALID" if invalid_prices.any() else "CONSISTENT",
            "window_status": status,
            "included_in_ranking_scope": status in {"COMPLETE", "COMPLETE_WITH_SUSPENSION"},
        })
    return pd.DataFrame(records)
