from __future__ import annotations

import math
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from cehuan.data.canonical import canonical_symbol, normalize_bar_frame
from cehuan.data.calendar import (
    next_open_session,
    normalize_trading_calendar,
    validate_bars_against_calendar,
)
from cehuan.research.baseline_readiness import require_baseline_ready
from cehuan.signals.strategy import (
    add_momentum,
    order_constraints,
    round_order_quantity,
    status_is_buy_eligible,
    status_is_sell_eligible,
)


PLAN_COLUMNS = [
    "plan_id", "generated_at", "data_cutoff_time", "effective_time", "calendar_source", "symbol", "name",
    "plan_purpose", "plan_readiness", "executable_quantity_permitted", "action", "current_shares", "target_shares", "planned_shares", "target_weight",
    "reference_price", "price_condition", "trigger_basis", "major_risks", "manual_review_required",
    "board", "market_rule_source", "market_rule_valid_from", "market_rule_valid_to",
    "has_price_limit", "price_limit_pct", "buy_min_shares", "buy_step_shares",
    "sell_min_shares", "sell_step_shares",
]


class PlanReadinessError(ValueError):
    def __init__(self, report: dict[str, object]):
        self.report = report
        super().__init__("人工审核计划未通过准入：" + "; ".join(report["blocking_reasons"]))


def _manual_plan_readiness(
    data: pd.DataFrame,
    calendar: pd.DataFrame | None,
    universe: pd.DataFrame | None,
    positions: pd.DataFrame | None,
    positions_as_of: datetime | None,
    formal_readiness: dict[str, object] | None,
    now: datetime,
) -> dict[str, object]:
    local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    blockers: list[str] = []
    checks: dict[str, str] = {}
    if formal_readiness is None:
        checks["formal_baseline_readiness"] = "MISSING"
        blockers.append("缺少正式基线 readiness 报告")
    elif (
        formal_readiness.get("schema") != "cehuan.baseline_readiness.v1"
        or formal_readiness.get("ready") is not True
        or formal_readiness.get("decision") != "READY"
        or formal_readiness.get("blockers")
        or not isinstance(formal_readiness.get("checks"), dict)
        or not isinstance(formal_readiness.get("checks", {}).get("execution_engine"), dict)
        or formal_readiness.get("checks", {}).get("execution_engine", {}).get("complete") is not True
    ):
        checks["formal_baseline_readiness"] = "NOT_READY"
        blockers.append("正式基线 readiness 未达到 READY")
    else:
        copy = dict(formal_readiness)
        supplied = copy.pop("report_sha256", None)
        digest = hashlib.sha256(
            json.dumps(copy, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if supplied != digest:
            checks["formal_baseline_readiness"] = "INVALID_REPORT_HASH"
            blockers.append("正式基线 readiness 报告散列校验失败")
        else:
            try:
                window = formal_readiness["evaluation_window"]
                root = Path(__file__).resolve().parents[3]
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                    text=True, check=True,
                ).stdout.strip()
                require_baseline_ready(
                    formal_readiness,
                    expected_input_hashes=formal_readiness["input_hashes"],
                    evaluation_start=window["start"], evaluation_end=window["end"],
                    current_git_commit=commit,
                )
                checks["formal_baseline_readiness"] = "READY"
            except Exception as exc:
                checks["formal_baseline_readiness"] = "NOT_READY"
                blockers.append(f"正式基线 readiness 深度校验失败：{exc}")

    if calendar is None:
        checks["calendar"] = "MISSING"
        blockers.append("人工审核计划必须提供交易日历")
        sessions = None
    else:
        try:
            sessions = normalize_trading_calendar(calendar)
            if not validate_bars_against_calendar(data, sessions)["healthy"]:
                raise ValueError("行情日期与交易日历不一致")
            opens = sessions.loc[sessions["is_open"].fillna(False), "trade_date"]
            past_opens = opens.loc[opens <= local_now.date()]
            as_of = pd.Timestamp(data["trade_date"].max()).date()
            if past_opens.empty or past_opens.iloc[-1] != as_of:
                raise ValueError("行情日不是截至当前的最近一个交易日，数据可能过期")
            next_session = next_open_session(sessions, as_of)
            if next_session is None:
                raise ValueError("日历缺少行情日之后的下一个开市日")
            checks["calendar"] = "VALIDATED"
        except Exception as exc:
            checks["calendar"] = "FAILED"
            blockers.append(f"交易日历校验失败：{exc}")
            sessions = None

    if universe is None:
        checks["point_in_time_universe"] = "MISSING"
        blockers.append("人工审核计划必须提供当日点时股票池")
    elif not {"trade_date", "symbol", "pool_status"}.issubset(universe.columns):
        checks["point_in_time_universe"] = "INVALID_SCHEMA"
        blockers.append("点时股票池缺少 trade_date/symbol/pool_status")
    else:
        membership = universe.copy()
        membership["trade_date"] = pd.to_datetime(membership["trade_date"], errors="coerce").dt.date
        membership["symbol"] = membership["symbol"].map(canonical_symbol)
        as_of = pd.Timestamp(data["trade_date"].max()).date()
        day_members = membership.loc[
            membership["trade_date"].eq(as_of)
            & membership["pool_status"].astype(str).eq("STOCK_MEMBER"), "symbol",
        ]
        if day_members.empty:
            checks["point_in_time_universe"] = "MISSING_AS_OF_DATE"
            blockers.append(f"点时股票池在 {as_of} 没有 STOCK_MEMBER")
        elif day_members.duplicated().any():
            checks["point_in_time_universe"] = "DUPLICATE_MEMBERS"
            blockers.append("点时股票池存在重复证券成员")
        else:
            latest_symbols = set(data.loc[data["trade_date"] == data["trade_date"].max(), "symbol"].astype(str))
            missing_symbols = sorted(set(day_members.astype(str)).difference(latest_symbols))
            if missing_symbols:
                checks["point_in_time_universe"] = "MEMBERS_WITHOUT_LATEST_BAR"
                blockers.append("点时股票池成员缺少最新日线，状态保持未知：" + ", ".join(missing_symbols[:20]))
            else:
                checks["point_in_time_universe"] = "VALIDATED"

    if local_now.time() < datetime.strptime("15:05", "%H:%M").time():
        checks["after_close_stability"] = "NOT_READY"
        blockers.append("人工审核计划只允许在上海时间 15:05 后生成")
    else:
        checks["after_close_stability"] = "READY"

    latest = data.loc[data["trade_date"] == data["trade_date"].max()].copy()
    available = pd.to_datetime(latest["available_at"], errors="coerce", utc=True)
    retrieved = pd.to_datetime(latest["retrieved_at_utc"], errors="coerce", utc=True)
    now_utc = pd.Timestamp(now.astimezone(ZoneInfo("UTC")))
    if available.isna().any() or retrieved.isna().any():
        checks["latest_bar_timestamps"] = "MISSING"
        blockers.append("最新行情行缺少可用时间或采集时间")
    elif (available > now_utc).any() or (retrieved > now_utc).any() or (retrieved < available).any():
        checks["latest_bar_timestamps"] = "FAILED"
        blockers.append("最新行情的可用/采集时间与当前时点冲突")
    else:
        local_available = available.dt.tz_convert(ZoneInfo("Asia/Shanghai"))
        row_dates = pd.to_datetime(latest["trade_date"], errors="coerce").dt.date.reset_index(drop=True)
        available_dates = local_available.dt.date.reset_index(drop=True)
        after_close = local_available.dt.time.reset_index(drop=True).map(
            lambda value: value >= datetime.strptime("15:00", "%H:%M").time()
        )
        if row_dates.isna().any() or not row_dates.equals(available_dates) or not after_close.all():
            checks["latest_bar_timestamps"] = "DATE_OR_CLOSE_MISMATCH"
            blockers.append("最新日线 available_at 必须对应该交易日 15:00 收盘或之后")
        else:
            checks["latest_bar_timestamps"] = "VALIDATED"
    if "data_source" not in latest.columns or latest["data_source"].isna().any() or latest["data_source"].astype(str).str.strip().eq("").any():
        checks["latest_bar_source"] = "MISSING"
        blockers.append("最新行情缺少逐行 data_source 标识")
    else:
        checks["latest_bar_source"] = "RECORDED_SOURCE_UNVERIFIED"
    latest_close = pd.to_numeric(latest["close"], errors="coerce")
    invalid_quote_symbols = set(latest.loc[latest_close.isna() | latest_close.le(0), "symbol"].astype(str))
    held_symbols: set[str] = set()
    if positions is not None and {"symbol", "shares"}.issubset(positions.columns):
        shares = pd.to_numeric(positions["shares"], errors="coerce")
        held_symbols = set(positions.loc[shares.gt(0), "symbol"].map(canonical_symbol).astype(str))
    missing_held_quotes = sorted(invalid_quote_symbols.intersection(held_symbols))
    if missing_held_quotes:
        blockers.append("持仓缺少有效现价，禁止用历史成本估算组合市值：" + ", ".join(missing_held_quotes))
    elif invalid_quote_symbols:
        blockers.append("最新行情存在缺失或无效收盘价：" + ", ".join(sorted(invalid_quote_symbols)[:20]))

    if positions is None:
        checks["positions_snapshot"] = "MISSING"
        blockers.append("缺少持仓快照")
    elif positions_as_of is None or positions_as_of.tzinfo is None or positions_as_of.utcoffset() is None:
        checks["positions_snapshot"] = "MISSING_AS_OF"
        blockers.append("持仓快照必须提供带时区的 positions_as_of 时间")
    elif positions_as_of.astimezone(ZoneInfo("UTC")) > now.astimezone(ZoneInfo("UTC")):
        checks["positions_snapshot"] = "FUTURE_TIMESTAMP"
        blockers.append("持仓快照时间晚于计划生成时间")
    elif positions_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date() < pd.Timestamp(data["trade_date"].max()).date():
        checks["positions_snapshot"] = "STALE"
        blockers.append("持仓快照日期早于最新行情日")
    elif (
        positions_as_of.astimezone(ZoneInfo("Asia/Shanghai")).date() == pd.Timestamp(data["trade_date"].max()).date()
        and positions_as_of.astimezone(ZoneInfo("Asia/Shanghai")).time() < datetime.strptime("15:00", "%H:%M").time()
    ):
        checks["positions_snapshot"] = "PRE_CLOSE_SNAPSHOT"
        blockers.append("持仓快照早于最新交易日收盘，不能作为收盘后账户状态")
    else:
        checks["positions_snapshot"] = "VALIDATED"
        if "symbol" not in positions.columns or "shares" not in positions.columns:
            checks["positions_schema"] = "INVALID"
            blockers.append("持仓快照缺少 symbol 或 shares")
        else:
            normalized_position_symbols = positions["symbol"].map(canonical_symbol)
            if normalized_position_symbols.duplicated().any():
                checks["positions_schema"] = "DUPLICATE_SYMBOLS"
                blockers.append("持仓快照同一证券出现多行")
            else:
                checks["positions_schema"] = "VALIDATED"
            share_counts = pd.to_numeric(positions["shares"], errors="coerce")
            if share_counts.isna().any() or share_counts.lt(0).any() or share_counts.mod(1).ne(0).any():
                checks["positions_share_counts"] = "INVALID"
                blockers.append("持仓股数必须是非负整数")

    return {
        "schema": "cehuan.plan_readiness.v1",
        "status": "READY_FOR_MANUAL_REVIEW" if not blockers else "NOT_READY",
        "checks": checks,
        "blocking_reasons": blockers,
        "plan_is_order": False,
        "orders_generated": False,
        "formal_baseline_readiness": checks.get("formal_baseline_readiness", "MISSING"),
        "meaning": "仅决定是否允许生成供人工审核的计划文件；不连接券商、不保证成交。",
    }


def generate_plan(
    frame: pd.DataFrame,
    cash_cny: float,
    config: dict[str, object],
    positions: pd.DataFrame | None = None,
    now: datetime | None = None,
    calendar: pd.DataFrame | None = None,
    *,
    universe: pd.DataFrame | None = None,
    purpose: str = "RESEARCH_ONLY",
    positions_as_of: datetime | None = None,
    formal_readiness: dict[str, object] | None = None,
) -> pd.DataFrame:
    """Create a non-actionable research candidate or a gated manual-review plan."""
    if purpose not in {"RESEARCH_ONLY", "SYNTHETIC_DIAGNOSTIC", "MANUAL_REVIEW"}:
        raise ValueError("purpose 只能是 RESEARCH_ONLY、SYNTHETIC_DIAGNOSTIC 或 MANUAL_REVIEW")
    if cash_cny <= 0:
        raise ValueError("生成计划前请明确输入可用现金；系统不会读取或猜测账户资金")
    if not math.isfinite(float(cash_cny)):
        raise ValueError("输入现金必须是有限数值")
    data = add_momentum(frame, int(config["strategy"]["lookback_sessions"]))
    if data.empty:
        raise ValueError("没有可用于计划的行情数据")
    if purpose == "SYNTHETIC_DIAGNOSTIC":
        if "data_source" not in data.columns or data["data_source"].isna().any() or not data["data_source"].astype(str).str.lower().str.contains("synthetic").all():
            raise ValueError("SYNTHETIC_DIAGNOSTIC 只接受明确标记为 synthetic 的测试行情")
    current_time = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    readiness = _manual_plan_readiness(
        data, calendar, universe, positions, positions_as_of, formal_readiness, current_time,
    ) if purpose == "MANUAL_REVIEW" else {
        "schema": "cehuan.plan_readiness.v1", "status": "RESEARCH_ONLY",
        "checks": {}, "blocking_reasons": [], "plan_is_order": False,
        "orders_generated": False,
        "formal_baseline_readiness": "NOT_CHECKED",
        "meaning": "研究候选不具备账户级准入，不可作为委托数量使用。",
    }
    if purpose == "MANUAL_REVIEW" and readiness["status"] != "READY_FOR_MANUAL_REVIEW":
        raise PlanReadinessError(readiness)
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

    if purpose == "MANUAL_REVIEW":
        gate = dict(readiness)
        errors: list[str] = []
        current_rows = data.loc[data["trade_date"] == data["trade_date"].max()].set_index("symbol", drop=False)
        required_symbols = set(existing) | {
            str(row["symbol"]) for _, row in current_rows.iterrows()
            if pd.notna(row.get("momentum")) and float(row["momentum"]) >= float(config["strategy"].get("min_momentum", 0.0))
        }
        rule_fields = (
            "board", "market_rule_source", "market_rule_valid_from", "has_price_limit",
            "buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares",
        )
        for symbol in sorted(required_symbols):
            if symbol not in current_rows.index:
                errors.append(f"{symbol} 缺少最新行情，无法确认状态和估值")
                continue
            row = current_rows.loc[symbol]
            for field in ("is_listed", "is_suspended", "is_st"):
                if pd.isna(row.get(field)):
                    errors.append(f"{symbol} 的 {field} 未知")
            for field in rule_fields:
                value = row.get(field)
                if pd.isna(value) or (isinstance(value, str) and not value.strip()):
                    errors.append(f"{symbol} 的交易规则字段 {field} 缺失")
            source = row.get("market_rule_source")
            if pd.notna(source) and not str(source).strip():
                errors.append(f"{symbol} 的市场规则来源为空")
            valid_from = pd.to_datetime(row.get("market_rule_valid_from"), errors="coerce")
            valid_to = pd.to_datetime(row.get("market_rule_valid_to"), errors="coerce")
            as_of = pd.Timestamp(data["trade_date"].max())
            if pd.isna(valid_from) or valid_from.normalize() > as_of.normalize():
                errors.append(f"{symbol} 的市场规则有效期不覆盖行情日")
            elif pd.notna(row.get("market_rule_valid_to")) and (pd.isna(valid_to) or valid_to.normalize() < as_of.normalize()):
                errors.append(f"{symbol} 的市场规则已过期")
            has_limit = row.get("has_price_limit")
            if not pd.isna(has_limit) and bool(has_limit):
                limit = pd.to_numeric(pd.Series([row.get("price_limit_pct")]), errors="coerce").iloc[0]
                if pd.isna(limit) or float(limit) <= 0:
                    errors.append(f"{symbol} 的涨跌幅限制无效")
            for field in ("buy_min_shares", "buy_step_shares", "sell_min_shares", "sell_step_shares"):
                value = pd.to_numeric(pd.Series([row.get(field)]), errors="coerce").iloc[0]
                if pd.isna(value) or float(value) <= 0 or not float(value).is_integer():
                    errors.append(f"{symbol} 的 {field} 必须是正整数")
            if symbol in existing:
                holding = existing[symbol]
                if holding["shares"] > 0 and holding["available_to_sell"] is None:
                    errors.append(f"{symbol} 持仓快照缺少可卖数量")
                if holding["available_to_sell"] is not None and not 0 <= holding["available_to_sell"] <= holding["shares"]:
                    errors.append(f"{symbol} 可卖数量超出持仓范围")
        if errors:
            gate["status"] = "NOT_READY"
            gate["blocking_reasons"] = [*gate["blocking_reasons"], *sorted(set(errors))]
            raise PlanReadinessError(gate)

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
    missing_holding_prices = sorted(
        symbol for symbol, holding in existing.items()
        if holding["shares"] > 0 and symbol not in latest_prices
    )
    if purpose == "MANUAL_REVIEW" and missing_holding_prices:
        report = dict(readiness)
        report["status"] = "NOT_READY"
        report["blocking_reasons"] = [
            *report["blocking_reasons"],
            "持仓缺少有效现价，禁止用历史成本估算组合市值：" + ", ".join(missing_holding_prices),
        ]
        raise PlanReadinessError(report)
    account_value = float(cash_cny) + sum(
        holding["shares"] * latest_prices[symbol]
        for symbol, holding in existing.items()
        if symbol in latest_prices
    )
    target_by_symbol = {str(row["symbol"]): row for row in selected}
    desired_shares: dict[str, int] = {}
    for symbol, row in target_by_symbol.items():
        price = latest_prices.get(symbol)
        if price and purpose in {"SYNTHETIC_DIAGNOSTIC", "MANUAL_REVIEW"}:
            target_value = account_value * weight_each
            minimum, step = order_constraints(row, "BUY", market)
            desired_shares[symbol] = round_order_quantity(int(target_value / price), minimum, step)

    generated = current_time.isoformat(timespec="seconds")
    plan_id = f"chq-plan-{current_time:%Y%m%d-%H%M%S-%f}"
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
            "plan_purpose": purpose,
            "plan_readiness": readiness["status"],
            "executable_quantity_permitted": purpose == "MANUAL_REVIEW",
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
            "manual_review_required": purpose == "MANUAL_REVIEW",
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

    result = pd.DataFrame(rows, columns=PLAN_COLUMNS)
    if purpose == "MANUAL_REVIEW" and not result.empty:
        buy_notional = 0.0
        for _, row in result.loc[result["action"].eq("BUY")].iterrows():
            price = pd.to_numeric(pd.Series([row["reference_price"]]), errors="coerce").iloc[0]
            if pd.isna(price) or float(price) <= 0:
                raise PlanReadinessError({
                    **readiness,
                    "status": "NOT_READY",
                    "blocking_reasons": [*readiness["blocking_reasons"], f"{row['symbol']} 买入参考价无效"],
                })
            buy_notional += float(row["planned_shares"]) * float(price)
        if buy_notional > float(cash_cny):
            raise PlanReadinessError({
                **readiness,
                "status": "NOT_READY",
                "blocking_reasons": [
                    *readiness["blocking_reasons"],
                    f"目标买入参考金额 {buy_notional:.2f} 元超过输入可用现金 {float(cash_cny):.2f} 元；未计费用和价格变动",
                ],
            })
    if purpose == "RESEARCH_ONLY":
        result["action"] = result["action"].map({"BUY": "CANDIDATE_BUY", "SELL": "CANDIDATE_EXIT", "VERIFY": "VERIFY", "HOLD": "RESEARCH_HOLD"}).fillna("RESEARCH_HOLD")
        result["target_shares"] = pd.NA
        result["planned_shares"] = 0
        result["executable_quantity_permitted"] = False
        result["major_risks"] = result["major_risks"].astype(str) + "；仅研究候选，不是可执行数量"
    result = result.sort_values(["action", "symbol"]).reset_index(drop=True)
    result.attrs["readiness_report"] = readiness
    return result


def plan_markdown(plan: pd.DataFrame) -> str:
    if plan.empty:
        return "# 策环交易计划\n\n本次没有生成候选。请检查数据窗口、状态字段和现金输入。\n"
    first = plan.iloc[0]
    lines = [
        f"# 策环计划（{first['plan_purpose']}）",
        "",
        f"- 计划编号：{first['plan_id']}",
        f"- 生成时间：{first['generated_at']}",
        f"- 数据截止：{first['data_cutoff_time']}",
        f"- 计划生效：{first['effective_time']}",
        f"- 准入状态：{first['plan_readiness']}",
        f"- 性质：{_plan_purpose_description(str(first['plan_purpose']))}",
        "",
        "| 代码 | 名称 | 动作 | 计划数量（股） | 目标权重 | 参考价 | 触发依据 | 主要风险 |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for _, row in plan.iterrows():
        weight = "" if pd.isna(row["target_weight"]) else f"{float(row['target_weight']):.1%}"
        price = "" if pd.isna(row["reference_price"]) else f"¥{float(row['reference_price']):.2f}"
        name = "" if pd.isna(row["name"]) else str(row["name"])
        quantity = "" if pd.isna(row["planned_shares"]) else str(int(row["planned_shares"]))
        lines.append(f"| {row['symbol']} | {name} | {row['action']} | {quantity} | {weight} | {price} | {row['trigger_basis']} | {row['major_risks']} |")
    lines += ["", "日线信号使用 t 日收盘后可得数据，最早用于下一开市日审核。该文件不会创建委托；MANUAL_REVIEW 仍须由用户在华泰手动核对和下单。"]
    return "\n".join(lines) + "\n"


def _plan_purpose_description(purpose: str) -> str:
    if purpose == "MANUAL_REVIEW":
        return "准入检查允许输出人工审核数量；仍需用户复核并在华泰手动操作，本文件不会下单。"
    if purpose == "SYNTHETIC_DIAGNOSTIC":
        return "数量仅用于合成流程诊断，不代表真实持仓或交易建议，本文件不会下单。"
    return "研究候选不含可执行数量；不可用于真实账户下单，本文件不会下单。"
