from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "examples" / "demo_daily.csv"


def main() -> None:
    rng = np.random.default_rng(20261025)
    dates = pd.bdate_range("2025-01-02", periods=180)
    securities = {
        "SIM001": ("合成示例甲", 20.0, 0.0012),
        "SIM002": ("合成示例乙", 35.0, 0.0003),
        "SIM003": ("合成示例丙", 48.0, -0.0002),
        "SIM004": ("合成示例丁", 16.0, 0.0006),
    }
    rows: list[dict[str, object]] = []
    for index, (symbol, (name, first, drift)) in enumerate(securities.items()):
        daily = rng.normal(drift, 0.009 + index * 0.001, len(dates))
        daily += 0.003 * np.sin(np.arange(len(dates)) / (9 + index))
        close = first * np.exp(np.cumsum(daily))
        previous = np.r_[first, close[:-1]]
        open_price = previous * (1 + rng.normal(0, 0.002, len(dates)))
        high = np.maximum(open_price, close) * (1 + rng.uniform(0.001, 0.012, len(dates)))
        low = np.minimum(open_price, close) * (1 - rng.uniform(0.001, 0.012, len(dates)))
        for i, date in enumerate(dates):
            rows.append({
                "trade_date": date.date().isoformat(),
                "symbol": symbol,
                "name": name,
                "open": round(float(open_price[i]), 2),
                "high": round(float(high[i]), 2),
                "low": round(float(low[i]), 2),
                "close": round(float(close[i]), 2),
                "adjusted_close": round(float(close[i]), 4),
                "volume_shares": int(rng.integers(800_000, 4_500_000)),
                "amount_cny": round(float(close[i] * rng.integers(80_000, 180_000)), 2),
                "is_listed": True,
                "is_suspended": False,
                "is_st": False,
                "has_price_limit": True,
                "price_limit_pct": 0.10,
                "buy_min_shares": 100,
                "buy_step_shares": 100,
                "sell_min_shares": 100,
                "sell_step_shares": 100,
                "board": "synthetic_main_board_assumption",
                "market_rule_source": "synthetic_demo_assumption",
                "market_rule_valid_from": "2025-01-02",
                "data_source": "synthetic_demo_generator",
                "retrieved_at_utc": "2026-10-25T00:00:00+00:00",
                "available_at": f"{date.date().isoformat()}T15:00:00+08:00",
            })
    pd.DataFrame(rows).sort_values(["trade_date", "symbol"]).to_csv(OUT, index=False)
    print(f"Created {OUT.relative_to(ROOT)}: {len(rows)} synthetic rows, {len(dates)} weekdays, {len(securities)} symbols")


if __name__ == "__main__":
    main()
