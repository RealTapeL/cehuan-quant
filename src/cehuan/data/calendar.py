from __future__ import annotations

import pandas as pd

from cehuan.data.canonical import normalize_bar_frame, parse_optional_bool


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
