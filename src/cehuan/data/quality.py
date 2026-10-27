from __future__ import annotations

import pandas as pd
import re

from cehuan.data.adjustment import select_signal_prices
from cehuan.data.calendar import validate_bars_against_calendar
from cehuan.data.canonical import PRICE_COLUMNS, REQUIRED_BAR_COLUMNS, normalize_bar_frame


def check_data_quality(
    frame: pd.DataFrame,
    max_age_days: int | None = None,
    large_change_warning: float = 0.50,
    calendar: pd.DataFrame | None = None,
) -> dict[str, object]:
    data = normalize_bar_frame(frame)
    errors: list[str] = []
    warnings: list[str] = []
    missing_counts = {
        column: int(data[column].isna().sum())
        for column in REQUIRED_BAR_COLUMNS
        if column in data
    }
    if data.empty:
        errors.append("数据集为空")
    if data["trade_date"].isna().any():
        errors.append("存在无法解析的交易日")
    if data["symbol"].isna().any() or (data["symbol"].str.len() == 0).any():
        errors.append("存在空证券代码")
    synthetic = data["data_source"].eq("synthetic_demo_generator").fillna(False)
    invalid_symbols = data.loc[~synthetic, "symbol"].dropna().loc[
        lambda values: ~values.str.fullmatch(r"(?:SH|SZ|BJ)\d{6}")
    ].unique()
    if len(invalid_symbols):
        errors.append("证券代码未能映射为交易所前缀格式：" + ", ".join(map(str, invalid_symbols[:10])))
    duplicate_count = int(data.duplicated(["trade_date", "symbol"]).sum())
    if duplicate_count:
        errors.append(f"交易日与证券代码重复 {duplicate_count} 行")
    if data["data_source"].notna().any():
        mixed_sources: list[str] = []
        for symbol, group in data.groupby("symbol", sort=False):
            sources = sorted(set(group["data_source"].dropna().astype(str)))
            if len(sources) > 1:
                mixed_sources.append(f"{symbol} ({', '.join(sources)})")
        if mixed_sources:
            errors.append(
                "同一证券混用了多个行情来源，禁止直接拼接价格/复权序列："
                + "; ".join(mixed_sources[:10])
            )
    null_required = sum(missing_counts.values())
    if null_required:
        errors.append(f"必需行情字段存在 {null_required} 个空值")
    valid_prices = data[PRICE_COLUMNS[:4]].dropna(how="all")
    if (valid_prices <= 0).any().any():
        errors.append("存在非正价格")
    bad_ohlc = (
        (data["high"] < data[["open", "close", "low"]].max(axis=1))
        | (data["low"] > data[["open", "close", "high"]].min(axis=1))
    )
    if bad_ohlc.any():
        errors.append(f"OHLC 关系异常 {int(bad_ohlc.sum())} 行")
    if (data["volume_shares"] < 0).any():
        errors.append("存在负成交量")
    raw_return = data.sort_values(["symbol", "trade_date"]).groupby("symbol")["close"].pct_change()
    large_changes = int((raw_return.abs() > large_change_warning).sum())
    if large_changes:
        warnings.append(f"发现 {large_changes} 个单日原始收盘价变化超过 {large_change_warning:.0%}；请核对复权、公司行动和源数据")
    if data["is_st"].isna().any():
        warnings.append("历史 ST 状态不完整；未知状态不能形成可执行交易计划")
    if data["is_listed"].isna().any():
        warnings.append("历史上市状态不完整；未知状态不能形成可执行交易计划")
    if data["is_suspended"].isna().any():
        warnings.append("停牌状态未由行情表证明；缺失日线不等同于已识别停牌")
    missing_limited_pct = data["has_price_limit"].fillna(True) & data["price_limit_pct"].isna()
    if missing_limited_pct.any():
        warnings.append("涨跌幅阈值缺失；须按证券类别、无涨跌幅限制日期与规则生效日期核验")
    if data["has_price_limit"].isna().any():
        warnings.append("是否适用涨跌幅限制未知；执行计划需要带有效日期的证券主数据")
    if data["board"].isna().any() or data["market_rule_source"].isna().any() or data["market_rule_valid_from"].isna().any():
        warnings.append("证券板块、规则来源或有效起始日不完整；不能证明交易规则对该日期有效")
    for column, label in (
        ("buy_min_shares", "买入最小数量"), ("buy_step_shares", "买入数量增量"),
        ("sell_min_shares", "卖出最小数量"), ("sell_step_shares", "卖出数量增量"),
    ):
        if data[column].isna().any():
            warnings.append(f"{label}缺失；不能生成可执行计划，回测仅可按配置假设")
    if data["adjusted_close"].isna().all():
        warnings.append("没有后复权收盘价；动量信号将回退到原始收盘价，需检查公司行动影响")
    else:
        _, adjustment_modes = select_signal_prices(data)
        unusable_adjustment = [
            symbol for symbol, mode in adjustment_modes.items()
            if mode == "unusable_mixed_or_nonpositive_adjustment"
        ]
        if unusable_adjustment:
            warnings.append(
                "部分股票的后复权收盘价有缺失或非正值；这些股票的信号暂不计算，须核对复权数据："
                + ", ".join(unusable_adjustment[:10])
            )
    available_at = pd.to_datetime(data["available_at"], errors="coerce", utc=True)
    retrieved_at = pd.to_datetime(data["retrieved_at_utc"], errors="coerce", utc=True)
    retrieved_before_available = available_at.notna() & retrieved_at.notna() & (retrieved_at < available_at)
    if retrieved_before_available.any():
        warnings.append(
            f"有 {int(retrieved_before_available.sum())} 行在标记的日线可用时间前已拉取；最新交易日数据需收盘后重新获取"
        )
    as_of = data["trade_date"].max().isoformat() if not data.empty and data["trade_date"].notna().any() else None
    if max_age_days is not None and as_of:
        age = (pd.Timestamp.now(tz="Asia/Shanghai").date() - pd.Timestamp(as_of).date()).days
        if age > max_age_days:
            warnings.append(f"最新行情日期距今天数为 {age}，请确认数据更新")
    retrieved = data["retrieved_at_utc"].dropna()
    calendar_result = validate_bars_against_calendar(data, calendar) if calendar is not None else None
    if calendar_result is not None:
        errors.extend(calendar_result["errors"])
    return {
        "healthy": not errors,
        "row_count": int(len(data)),
        "symbol_count": int(data["symbol"].nunique()),
        "duplicate_count": duplicate_count,
        "missing_required": missing_counts,
        "as_of": as_of,
        "retrieved_at_utc": str(retrieved.max()) if not retrieved.empty else None,
        "retrieved_before_available_count": int(retrieved_before_available.sum()),
        "errors": errors,
        "warnings": warnings,
        "calendar_note": "未提供交易所交易日历时，不对非交易日缺口作结论。",
        "calendar_validation": calendar_result,
        "large_daily_price_change_warning": large_change_warning,
    }
