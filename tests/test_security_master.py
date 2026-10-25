from __future__ import annotations

import pandas as pd

from cehuan.data.security_master import apply_security_master


def test_security_master_only_applies_inside_effective_date_window() -> None:
    bars = pd.DataFrame([
        {"trade_date": "2025-01-01", "symbol": "600519", "open": 10, "high": 11, "low": 9, "close": 10, "volume_shares": 100},
        {"trade_date": "2025-01-02", "symbol": "600519", "open": 10, "high": 11, "low": 9, "close": 10, "volume_shares": 100},
    ])
    master = pd.DataFrame([{
        "symbol": "600519", "name": "测试标的", "valid_from": "2025-01-02", "valid_to": "",
        "is_listed": True, "is_suspended": False, "is_st": False,
        "board": "主板", "has_price_limit": True, "price_limit_pct": 0.10,
        "buy_min_shares": 100, "buy_step_shares": 100,
        "sell_min_shares": 100, "sell_step_shares": 100,
        "source": "测试规则来源",
    }])
    result = apply_security_master(bars, master)
    assert pd.isna(result.loc[0, "is_st"])
    assert result.loc[1, "is_st"] == False
    assert result.loc[1, "name"] == "测试标的"
    assert result.loc[1, "symbol"] == "SH600519"
    assert result.loc[1, "market_rule_source"] == "测试规则来源"
    assert result.loc[1, "buy_step_shares"] == 100
    assert result.loc[1, "board"] == "主板"
