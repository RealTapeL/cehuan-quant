"""动量信号、候选资格和交易数量约束。

日线动量使用一致口径的信号价格，信号在收盘后形成、最早下一开市日执行；
买入资格与已有持仓卖出资格分开检查，市场规则未知时由调用方的严格门控阻断。
"""

from __future__ import annotations

import pandas as pd

from cehuan.data.adjustment import select_signal_prices, signal_price_sources
from cehuan.data.canonical import normalize_bar_frame


def order_constraints(
    row: pd.Series,
    side: str,
    market: dict[str, object],
    allow_fallback: bool = False,
) -> tuple[int, int]:
    """读取指定买卖方向的最小数量与数量增量。

    ``row`` 是证券日规则，``market`` 提供显式回退配置。仅在
    ``allow_fallback=True`` 时缺值才可回退到配置手数；返回整数股数
    ``(minimum, step)``，无效或缺失规则默认抛出 ``ValueError``。
    """
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
    """将请求股数向下调整为符合最小数量和增量的整数股数。

    完全清仓且显式允许时保留尾数卖出；其他低于最小数量的请求返回 0，
    不会把回退交易单位自动解释为已核实市场规则。
    """
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
    """计算收盘到收盘的 lookback 动量并保留每行价格口径标签。

    ``frame`` 为行情，``lookback_sessions`` 按证券交易记录数定义窗口；
    ``normalized``/``assume_sorted`` 用于复用已规范且排序的数据，``copy_frame``
    控制是否复制，``compact`` 启用节省内存的类别 dtype。返回带 signal_price、
    来源标签和 momentum 的行情表。t 日信号只用截至 t 日收盘的数据，最早
    下一开市日执行；混合价格口径的窗口由下游标记为无效。
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
    data["signal_price_source"] = signal_price_sources(data)
    return add_momentum_from_signal_prices(data, lookback_sessions)


def add_momentum_from_signal_prices(frame: pd.DataFrame, lookback_sessions: int) -> pd.DataFrame:
    """仅在完整窗口价格口径一致时计算动量。

    此函数也供 checkpoint 续跑使用，以保证分段运行与连续运行采用相同的逐行价格标签。
    ``frame`` 必须已有信号价格及其来源；返回增加动量列的副本，窗口缺值或混合口径时
    动量保留为空。
    """
    if lookback_sessions < 1:
        raise ValueError("lookback_sessions 必须大于 0")
    required = {"symbol", "close", "signal_price", "signal_price_source"}
    if not required.issubset(frame.columns):
        raise ValueError("窗口动量计算需要 symbol/close/signal_price/signal_price_source 字段")
    data = frame.copy()

    def calculate(group: pd.DataFrame) -> pd.Series:
        """对单证券序列计算仅同口径且窗口完整的收益率。"""
        price = pd.to_numeric(group["signal_price"], errors="coerce")
        source = group["signal_price_source"].astype("string")
        consistent = pd.Series(True, index=group.index, dtype="bool")
        for offset in range(lookback_sessions + 1):
            consistent &= source.eq(source.shift(offset)).fillna(False)
        valid_window = price.notna().rolling(
            lookback_sessions + 1, min_periods=lookback_sessions + 1,
        ).sum().eq(lookback_sessions + 1)
        result = price.div(price.shift(lookback_sessions)).sub(1.0)
        return result.where(consistent & valid_window)

    momentum = pd.Series(float("nan"), index=data.index, dtype="float64", name="momentum")
    for _, group in data.groupby("symbol", sort=False, observed=True):
        momentum.loc[group.index] = calculate(group)
    data["momentum"] = momentum
    return data


def _status_is_tradeable(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
    *,
    side: str,
) -> tuple[bool, str | None]:
    """检查点时证券状态及买卖方向对应的市场规则。

    ``row`` 是证券日状态与规则，``market`` 提供严格程度和回退配置，``side`` 决定
    ST 与交易单位检查口径。返回是否可交易及阻断原因；未知字段在严格模式下拒绝。
    """
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
    """判断证券能否进入新买候选池；不负责已有持仓退出判定。"""
    return _status_is_tradeable(
        row, market, require_order_rules, side="BUY",
    )


def status_is_sell_eligible(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
) -> tuple[bool, str | None]:
    """按已知约束判断已有持仓是否可以卖出。

    策略的 ST 排除仅限制新买，不锁住退出；停牌、上市状态和卖出交易规则仍分别检查。
    返回可卖标记与阻断原因。
    """
    return _status_is_tradeable(
        row, market, require_order_rules, side="SELL",
    )


def status_is_eligible(
    row: pd.Series,
    market: dict[str, object],
    require_order_rules: bool = True,
) -> tuple[bool, str | None]:
    """兼容旧调用方的买入资格别名；语义等同于新买候选检查。"""
    return status_is_buy_eligible(row, market, require_order_rules)


def rank_at_close(
    frame: pd.DataFrame,
    as_of: object,
    strategy: dict[str, object],
    market: dict[str, object],
    require_order_rules: bool = True,
) -> pd.DataFrame:
    """为截至 ``as_of`` 收盘可见的行情计算动量并生成当日排名。

    参数为历史行情、信号日期、策略与市场配置及是否要求完整交易规则；
    返回当日符合买入资格和最低动量条件的排序表。它只生成候选排名，
    不创建委托或证明下一交易日可成交。
    """
    data = add_momentum(frame, int(strategy["lookback_sessions"]))
    day = data.loc[data["trade_date"].astype(str) == str(as_of)].copy()
    return rank_precomputed_day(day, strategy, market, require_order_rules)


def rank_precomputed_day(
    day: pd.DataFrame,
    strategy: dict[str, object],
    market: dict[str, object],
    require_order_rules: bool = True,
) -> pd.DataFrame:
    """对已预先计算历史动量的单日行执行候选筛选和排序。

    ``day`` 是同一信号日的证券行，策略和市场配置控制阈值、资格及排序；返回带名次
    的候选表。该函数不计算成交，也不证明下一交易日可执行。
    """
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
