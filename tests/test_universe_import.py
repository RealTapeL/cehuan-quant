import pandas as pd
import pytest

from cehuan.data.universe_import import import_historical_universe_csv


def test_import_preserves_unknown_state_and_requires_date_specific_source_rows(tmp_path):
    source = tmp_path / "external.csv"
    pd.DataFrame([
        {
            "trade_date": "2025-01-02",
            "symbol": "sh.600519",
            "source": "licensed-provider.daily_members",
            "source_record_date": "2025-01-02",
            "source_retrieved_at_utc": "2025-01-03T08:00:00Z",
            "security_type": "1",
            "daily_trade_status": "1",
            "is_st": "",
        },
        {
            "trade_date": "2025-01-02",
            "symbol": "sz.159001",
            "source": "licensed-provider.daily_members",
            "source_record_date": "2025-01-02",
            "source_retrieved_at_utc": "2025-01-03T08:00:00Z",
            "security_type": "5",
            "daily_trade_status": "1",
            "is_st": "",
        },
    ]).to_csv(source, index=False)

    result = import_historical_universe_csv(source, tmp_path / "out", "dated-import")
    members = pd.read_csv(tmp_path / "out" / "dated-import" / "securities_by_date.csv", dtype=str)

    assert result["coverage"]["membership_rows"] == 2
    assert set(members["symbol"]) == {"SH600519", "SZ159001"}
    stock = members.set_index("symbol").loc["SH600519"]
    assert stock["eligibility_status"] == "UNKNOWN_ST_STATUS"
    assert pd.isna(stock["is_st"])
    assert members.set_index("symbol").loc["SZ159001", "pool_status"] == "FILTERED_NON_STOCK"
    assert result["coverage"]["bars_attached"] is False


def test_import_rejects_current_list_backfill_and_duplicate_rows(tmp_path):
    source = tmp_path / "external.csv"
    row = {
        "trade_date": "2025-01-02",
        "symbol": "SH600519",
        "source": "provider",
        "source_record_date": "2026-10-27",
        "source_retrieved_at_utc": "2026-10-27T00:00:00Z",
    }
    pd.DataFrame([row]).to_csv(source, index=False)
    with pytest.raises(ValueError, match="source_record_date"):
        import_historical_universe_csv(source, tmp_path / "out", "bad-date")

    row["source_record_date"] = row["trade_date"]
    pd.DataFrame([row, row]).to_csv(source, index=False)
    with pytest.raises(ValueError, match="重复每日成员"):
        import_historical_universe_csv(source, tmp_path / "out", "duplicates")
