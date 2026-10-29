from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.canonical import canonical_symbol, normalize_bar_frame
from cehuan.data.calendar import next_open_session, validate_bars_against_calendar
from cehuan.signals.strategy import (
    add_momentum,
    order_constraints,
    round_order_quantity,
    status_is_buy_eligible,
    status_is_sell_eligible,
)


PLAN_COLUMNS = [
    "plan_id", "generated_at", "data_cutoff_time", "effective_time", "calendar_source", "symbol", "name",
    "action", "current_shares", "target_shares", "planned_shares", "target_weight",
    "reference_price", "price_condition", "trigger_basis", "major_risks", "manual_review_required",
    "board", "market_rule_source", "market_rule_valid_from", "market_rule_valid_to",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares",
]


def generate_plan(
    frame: pd.DataFrame,
    cash_cny: float,
    config: dict[str, object],
    positions: pd.DataFrame | None = None,
    now: datetime | None = None,
    calendar: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build a separate plan artifact for a person to review and place manually."""
    if cash_cny <= 0:
        raise ValueError("生成计划前请明确输入可用现金；系统不会读取或猜测账户资金")
    data = add_momentum(frame, int(config["strategy"]["lookback_sessions"]))
    if data.empty:
        raise ValueError("没有可用于计划的行情数据")
    current_time = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    cutoff_values = data["available_at"].dropna()
    if not cutoff_values.empty:
        latest_cutoff = pd.Timestamp(pd.to_datetime(cutoff_values, errors="coerce").max())
        if latest_cutoff.tzinfo is None:
            latest_cutoff = latest_cutoff.tz_localize(ZoneInfo("Asia/Shanghai"))
        else:
            latest_cutoff = latest_cutoff.tz_convert(ZoneInfo("Asia/Shanghai"))
        if latest_cutoff.to_pydatetime() > current_time:
            raise ValueError(f"最新日线标记为 {latest_cutoff.isoformat()} 才可用；此时尚不能生成计划")
    as_of = data["trade_date"].max()
    effective_time = "next_trading_session_open; verify exchange calendar manually"
    calendar_source = ""
    if calendar is not None:
        calendar_validation = validate_bars_against_calendar(data, calendar)
        if not calendar_validation["healthy"]:
            raise ValueError("交易日历校验未通过：" + "; ".join(calendar_validation["errors"]))
        next_session = next_open_session(calendar, as_of)
        if next_session is None:
            raise ValueError("交易日历未包含最新数据日之后的下一个开市日")
        next_date, calendar_source = next_session
        effective_time = f"{next_date} 开盘前；下一个开市日来自交易日历"
    current = data.loc[data["trade_date"] == as_of].copy()
    latest_available = pd.to_datetime(current["available_at"], errors="coerce", utc=True)
    latest_retrieved = pd.to_datetime(current["retrieved_at_utc"], errors="coerce", utc=True)
    fetched_too_early = (
        latest_available.notna() & latest_retrieved.notna() & (latest_retrieved < latest_available)
    )
    if fetched_too_early.any():
        raise ValueError("最新交易日行情快照在标记的收盘可用时间前获取；请在收盘后重新拉取数据再生成计划")
    current = current.sort_values(["momentum", "symbol"], ascending=[False, True])
    current_by_symbol = {str(row["symbol"]): row for _, row in current.iterrows()}
    existing: dict[str, dict[str, float]] = {}
    if positions is not None and not positions.empty:
        for _, row in positions.iterrows():
            symbol = canonical_symbol(row["symbol"])
            shares_value = row.get("shares", 0)
            available_value = row.get("available_to_sell", pd.NA)
            cost_value = row.get("average_cost", 0)
            existing[symbol] = {
                "shares": 0 if pd.isna(shares_value) else int(float(shares_value)),
                "available_to_sell": None if pd.isna(available_value) else int(float(available_value)),
                "average_cost": 0.0 if pd.isna(cost_value) else float(cost_value),
            }

    strategy = config["strategy"]
    market = config["market"]
    if not bool(market.get("require_point_in_time_status", True)):
        raise ValueError("交易计划不能关闭点时上市、停牌、ST 与交易规则校验")
    eligible_rows: list[pd.Series] = []
    unknown_rows: list[tuple[pd.Series, str]] = []
    for _, row in current.iterrows():
        if pd.isna(row["momentum"]):
            continue
        if float(row["momentum"]) < float(strategy.get("min_momentum", 0.0)):
            continue
        eligible, reason = status_is_buy_eligible(row, market)
        if eligible:
            eligible_rows.append(row)
        elif reason:
            unknown_rows.append((row, reason))
    eligible_rows.sort(key=lambda row: (-float(row["momentum"]), str(row["symbol"])))
    for rank, row in enumerate(eligible_rows, start=1):
        row["rank"] = rank
    selected = eligible_rows[: int(strategy["max_positions"])]
    if selected:
        weight_each = min(
            float(strategy["max_position_weight"]),
            float(strategy["max_total_exposure"]) / len(selected),
        )
    else:
        weight_each = 0.0

    latest_prices = {
        symbol: float(row["close"])
        for symbol, row in current_by_symbol.items()
        if pd.notna(row["close"]) and float(row["close"]) > 0
    }
    account_value = float(cash_cny) + sum(
        holding["shares"] * latest_prices.get(symbol, holding["average_cost"])
        for symbol, holding in existing.items()
    )
    target_by_symbol = {str(row["symbol"]): row for row in selected}
    desired_shares: dict[str, int] = {}
    for symbol, row in target_by_symbol.items():
        price = latest_prices.get(symbol)
        if price:
            target_value = account_value * weight_each
            minimum, step = order_constraints(row, "BUY", market)
            desired_shares[symbol] = round_order_quantity(int(target_value / price), minimum, step)

    generated = current_time.isoformat(timespec="seconds")
    plan_id = f"chq-plan-{current_time:%Y%m%d-%H%M%S}"
    cutoff_values = current["available_at"].dropna()
    data_cutoff = str(cutoff_values.max()) if not cutoff_values.empty else f"{as_of} 15:00:00+08:00 (按日线收盘后假设)"
    rows: list[dict[str, object]] = []

    def add_row(symbol: str, action: str, current_shares: int, target_shares: int, weight: float | None,
                price: float | None, basis: str, risks: str, price_condition: str) -> None:
        row = current_by_symbol.get(symbol, {})
        rule_values = {
            column: row.get(column, pd.NA) if hasattr(row, "get") else pd.NA
            for column in (
                "board", "market_rule_source", "market_rule_valid_from", "market_rule_valid_to",
                "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
                "sell_min_shares", "sell_step_shares",
            )
        }
        rows.append({
            "plan_id": plan_id,
            "generated_at": generated,
            "data_cutoff_time": data_cutoff,
            "effective_time": effective_time,
            "calendar_source": calendar_source,
            "symbol": symbol,
            "name": row.get("name", "") if hasattr(row, "get") else "",
            "action": action,
            "current_shares": current_shares,
            "target_shares": target_shares,
            "planned_shares": abs(target_shares - current_shares) if action in {"BUY", "SELL"} else 0,
            "target_weight": weight,
            "reference_price": price,
            "price_condition": price_condition,
            "trigger_basis": basis,
            "major_risks": risks,
            "manual_review_required": True,
            **rule_values,
        })

    for symbol, holding in existing.items():
        current_shares = holding["shares"]
        row = current_by_symbol.get(symbol)
        if row is None:
            add_row(symbol, "HOLD", current_shares, current_shares, None, None,
                    "最新数据中没有该证券的行情行", "缺失日线不能据此认定停牌或可卖；人工核验", "未生成委托价格；先核对持仓与停牌状态")
            continue
        eligible, reason = status_is_sell_eligible(row, market)
        if not eligible:
            add_row(symbol, "HOLD", current_shares, current_shares, None, latest_prices.get(symbol),
                    "持仓状态无法通过数据校验", f"{reason}；核对可卖数量及交易状态", "不建议据本计划卖出；先人工核验")
            continue
        if symbol in desired_shares:
            target = desired_shares[symbol]
            if target < current_shares and holding["available_to_sell"] is None:
                add_row(symbol, "HOLD", current_shares, current_shares, weight_each, latest_prices.get(symbol),
                        "目標持倉需要調整，但持倉快照沒有可賣數量",
                        "T+1 可賣數量未知；補充持倉快照或人工核對後再決定",
                        "不生成賣出數量；不可超過華泰顯示的可賣數量")
                continue
            if target < current_shares:
                sell_quantity = min(current_shares - target, int(holding["available_to_sell"] or 0))
                minimum, step = order_constraints(row, "SELL", market)
                sell_quantity = round_order_quantity(
                    sell_quantity, minimum, step,
                    allow_full_liquidation=(sell_quantity == current_shares),
                    full_position_shares=current_shares,
                )
                target = current_shares - sell_quantity
            action = "BUY" if target > current_shares else "SELL" if target < current_shares else "HOLD"
            add_row(symbol, action, current_shares, target, weight_each, latest_prices.get(symbol),
                    f"{strategy['lookback_sessions']} 日后复权动量排名入选；按目标权重计算",
                    "未纳入逐笔盘口、滑点和公司行动；需核对 T+1 可卖数量",
                    "参考最近收盘价；下一交易日开盘前复核，涨跌停或停牌时不执行")
        else:
            if holding["available_to_sell"] is None:
                add_row(symbol, "HOLD", current_shares, current_shares, 0.0, latest_prices.get(symbol),
                        "当前不在符合规则的目标排名内，但可卖数量未提供",
                        "T+1 可卖数量未知；需导入持仓快照并人工核对",
                        "不生成卖出数量；不可超出华泰显示的可卖数量")
                continue
            sell_quantity = min(current_shares, int(holding["available_to_sell"] or 0))
            minimum, step = order_constraints(row, "SELL", market)
            sell_quantity = round_order_quantity(
                sell_quantity, minimum, step,
                allow_full_liquidation=(sell_quantity == current_shares),
                full_position_shares=current_shares,
            )
            action = "SELL" if sell_quantity else "HOLD"
            add_row(symbol, "SELL", current_shares, 0, 0.0, latest_prices.get(symbol),
                    "当前不在符合规则的目标排名内",
                    "ST 等状态可按卖出规则退出；仍须核对 T+1 可卖数量、停牌/跌停和华泰费用",
                    "参考最近收盘价；下一交易日开盘前复核，跌停或停牌时不执行")
            rows[-1]["target_shares"] = current_shares - sell_quantity
            rows[-1]["planned_shares"] = sell_quantity
            rows[-1]["action"] = action

    for symbol, row in target_by_symbol.items():
        if symbol in existing:
            continue
        target = desired_shares.get(symbol, 0)
        if target <= 0:
            add_row(symbol, "HOLD", 0, 0, weight_each, latest_prices.get(symbol),
                    "目标权重低于一手可买数量", "未形成整手买入数量", "不操作")
            continue
        add_row(symbol, "BUY", 0, target, weight_each, latest_prices.get(symbol),
                f"{strategy['lookback_sessions']} 日后复权动量排名第 {int(row['rank'])}；按目标权重计算",
                "须复核点时 ST/上市状态、涨停、停牌、可用现金及费用",
                "参考最近收盘价；下一交易日开盘前复核，不追涨停；手动限价委托")

    # Show highly ranked names whose point-in-time trading status is missing.
    for row, reason in unknown_rows[: int(strategy["max_positions"])]:
        symbol = str(row["symbol"])
        if symbol in existing or symbol in target_by_symbol:
            continue
        add_row(symbol, "VERIFY", 0, 0, None, latest_prices.get(symbol),
                f"{strategy['lookback_sessions']} 日动量为 {float(row['momentum']):.2%}，但状态数据不足",
                f"{reason}；补齐带生效日期的证券主数据后重新生成计划",
                "不生成委托数量；完成人工核验前不考虑下单")

    return pd.DataFrame(rows, columns=PLAN_COLUMNS).sort_values(["action", "symbol"]).reset_index(drop=True)


def plan_markdown(plan: pd.DataFrame) -> str:
    if plan.empty:
        return "# 策环交易计划\n\n本次没有生成候选。请检查数据窗口、状态字段和现金输入。\n"
    first = plan.iloc[0]
    lines = [
        "# 策环交易计划（人工审核）",
        "",
        f"- 计划编号：{first['plan_id']}",
        f"- 生成时间：{first['generated_at']}",
        f"- 数据截止：{first['data_cutoff_time']}",
        f"- 计划生效：{first['effective_time']}",
        "- 性质：研究输出；须人工核验并在华泰软件手动操作；本文件不会下单。",
        "",
        "| 代码 | 名称 | 动作 | 计划数量（股） | 目标权重 | 参考价 | 触发依据 | 主要风险 |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for _, row in plan.iterrows():
        weight = "" if pd.isna(row["target_weight"]) else f"{float(row['target_weight']):.1%}"
        price = "" if pd.isna(row["reference_price"]) else f"¥{float(row['reference_price']):.2f}"
        name = "" if pd.isna(row["name"]) else str(row["name"])
        lines.append(f"| {row['symbol']} | {name} | {row['action']} | {row['planned_shares']} | {weight} | {price} | {row['trigger_basis']} | {row['major_risks']} |")
    lines += ["", "日线信号使用 t 日收盘后可得数据，最早在下一交易日开盘前审核执行。交易日历需人工确认。"]
    return "\n".join(lines) + "\n"
