from __future__ import annotations

import pandas as pd

from cehuan.data.adjustment import select_signal_prices
from cehuan.data.canonical import normalize_bar_frame


def order_constraints(
    row: pd.Series,
    side: str,
    market: dict[str, object],
    allow_fallback: bool = False,
) -> tuple[int, int]:
    prefix = "buy" if side.upper() == "BUY" else "sell"
    minimum = row.get(f"{prefix}_min_shares", pd.NA)
    step = row.get(f"{prefix}_step_shares", pd.NA)
    if pd.isna(minimum) or pd.isna(step):
        if not allow_fallback:
            raise ValueError(f"{side} 交易单位缺少点时数据")
        fallback = int(market.get(f"{prefix}_lot_shares", 1))
        minimum = fallback if pd.isna(minimum) else minimum
        step = fallback if pd.isna(step) else step
    if float(minimum) % 1 != 0 or float(step) % 1 != 0:
        raise ValueError(f"{side} 最小交易数量与增量必须是整数")
    minimum, step = int(minimum), int(step)
    if minimum < 1 or step < 1:
        raise ValueError(f"{side} 最小交易数量与增量必须为正整数")
    return minimum, step


def round_order_quantity(
    quantity: int,
    minimum: int,
    step: int,
    *,
    allow_full_liquidation: bool = False,
    full_position_shares: int | None = None,
) -> int:
    """Round down to a valid order size; allow an odd-lot final liquidation."""
    quantity = max(0, int(quantity))
    if allow_full_liquidation and full_position_shares is not None and quantity == full_position_shares:
        return quantity
    if quantity < minimum:
        return 0
    return minimum + ((quantity - minimum) // step) * step


def add_momentum(
    frame: pd.DataFrame,
    lookback_sessions: int,
    *,
    normalized: bool = False,
    copy_frame: bool = True,
    compact: bool = False,
    assume_sorted: bool = False,
) -> pd.DataFrame:
    """Calculate close-to-close momentum using adjusted close where available.

    Every value on date t uses observations up to and including the close on t.
    It can only be acted on at the next trading session's open.
    """
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    data = frame if normalized else normalize_bar_frame(
        frame,
        copy_frame=copy_frame,
        compact=compact,
        assume_sorted=assume_sorted,
    )
    data["signal_price"], _ = select_signal_prices(data)
    data["momentum"] = data.groupby("symbol", sort=False, observed=True)["signal_price"].transform(
        lambda values: values.div(values.shift(lookback_sessions)).sub(1.0)
    )
    return data


def _status_is_tradeable(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
    *,
    side: str,
) -> tuple[bool, str | None]:
    """Check common point-in-time status and side-specific trade rules."""
    listed = row.get("is_listed", pd.NA)
    suspended = row.get("is_suspended", pd.NA)
    st_flag = row.get("is_st", pd.NA)
    strict = bool(market.get("require_point_in_time_status", True))
    if not pd.isna(listed) and not bool(listed):
        return False, "该日期未上市或已退市"
    if pd.isna(listed) and strict:
        return False, "上市状态未知"
    if pd.isna(suspended) and strict:
        return False, "停牌状态未知"
    if not pd.isna(suspended) and bool(suspended):
        return False, "停牌"
    if side == "BUY" and bool(market.get("exclude_st", True)):
        if pd.isna(st_flag) and strict:
            return False, "ST 状态未知"
        if not pd.isna(st_flag) and bool(st_flag):
            return False, "ST 股票按配置排除"
    if require_order_rules:
        board = row.get("board", pd.NA)
        source = row.get("market_rule_source", pd.NA)
        valid_from = row.get("market_rule_valid_from", pd.NA)
        trade_date = row.get("trade_date", pd.NA)
        if pd.isna(board) or not str(board).strip():
            return False, "证券板块规则未知"
        if pd.isna(source) or not str(source).strip():
            return False, "交易规则来源未知"
        if pd.isna(valid_from):
            return False, "交易规则有效起始日未知"
        if not pd.isna(trade_date) and pd.Timestamp(valid_from).date() > pd.Timestamp(trade_date).date():
            return False, "交易规则尚未生效"
        valid_to = row.get("market_rule_valid_to", pd.NA)
        if not pd.isna(valid_to) and not pd.isna(trade_date) and pd.Timestamp(valid_to).date() < pd.Timestamp(trade_date).date():
            return False, "交易规则已失效"
        has_limit = row.get("has_price_limit", pd.NA)
        if pd.isna(has_limit):
            return False, "当日是否适用涨跌幅限制未知"
        if bool(has_limit) and pd.isna(row.get("price_limit_pct", pd.NA)):
            return False, "当日涨跌幅阈值未知"
        order_columns = (
            (("buy_min_shares", "买入最小数量未知"), ("buy_step_shares", "买入数量增量未知"))
            if side == "BUY"
            else (("sell_min_shares", "卖出最小数量未知"), ("sell_step_shares", "卖出数量增量未知"))
        )
        for column, label in order_columns:
            value = row.get(column, pd.NA)
            if pd.isna(value) or float(value) < 1 or float(value) % 1 != 0:
                return False, label
    return True, None


def status_is_buy_eligible(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
) -> tuple[bool, str | None]:
    """Whether a security may enter the new-buy candidate pool."""
    return _status_is_tradeable(
        row, market, require_order_rules, side="BUY",
    )


def status_is_sell_eligible(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
) -> tuple[bool, str | None]:
    """Whether an existing position may be sold under known constraints.

    The strategy's ST exclusion is an entry filter. It must not prevent exits;
    suspension, listing status, and order rules are still checked separately.
    """
    return _status_is_tradeable(
        row, market, require_order_rules, side="SELL",
    )


def status_is_eligible(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
) -> tuple[bool, str | None]:
    """Backward-compatible alias for buy-candidate eligibility."""
    return status_is_buy_eligible(row, market, require_order_rules)


def rank_at_close(
    frame: pd.DataFrame,
    as_of: object,
    strategy: dict[str, object],
    market: dict[str, object],
    require_order_rules: bool = True,
) -> pd.DataFrame:
    data = add_momentum(frame, int(strategy["lookback_sessions"]))
    day = data.loc[data["trade_date"].astype(str) == str(as_of)].copy()
    return rank_precomputed_day(day, strategy, market, require_order_rules)


def rank_precomputed_day(
    day: pd.DataFrame,
    strategy: dict[str, object],
    market: dict[str, object],
    require_order_rules: bool = True,
) -> pd.DataFrame:
    """Rank one day's rows when momentum has already been computed for history."""
    if day.empty:
        return day.assign(rank=pd.Series(dtype="Int64"))
    if "momentum" not in day:
        raise ValueError("rank_precomputed_day 需要预先计算的 momentum 列")
    day = day.reset_index(drop=True)
    eligibility = day.apply(lambda row: status_is_buy_eligible(row, market, require_order_rules)[0], axis=1)
    day = day.loc[eligibility & day["momentum"].notna()]
    day = day.loc[day["momentum"] >= float(strategy.get("min_momentum", 0.0))]
    day = day.sort_values(["momentum", "symbol"], ascending=[False, True]).reset_index(drop=True)
    day["rank"] = range(1, len(day) + 1)
    return day
