from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from cehuan.data.security_identity import sha256_file


UNKNOWN_TYPE_REVIEW_COLUMNS = [
    "symbol", "classification", "evidence_url", "source_sha256", "reviewed_by", "reviewed_at", "review_note",
]
ALLOWED_CLASSIFICATIONS = {"UNRESOLVED", "EXPLAINED_NON_STOCK", "ADDED_AS_STOCK"}


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


def audit_unknown_security_types(
    universe: str | Path,
    bars_path: str | Path,
    output_csv: str | Path,
) -> dict[str, Any]:
    """Create a per-code review register for rows excluded as UNKNOWN_TYPE.

    Existing classifications in output_csv are retained only when backed by a
    source URL, source-file digest, reviewer, and review date. No stock type is
    inferred from an exchange prefix or the presence of OHLC data.
    """
    universe_root = Path(universe).expanduser().resolve()
    membership_path = universe_root / "securities_by_date.csv" if universe_root.is_dir() else universe_root
    if not membership_path.is_file():
        raise FileNotFoundError(f"股票池成员文件不存在：{membership_path}")
    bars_source = Path(bars_path).expanduser().resolve()
    if not bars_source.is_file():
        raise FileNotFoundError(f"规范行情文件不存在：{bars_source}")
    output = Path(output_csv).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    required = {"trade_date", "symbol", "pool_status"}
    summary: dict[str, dict[str, Any]] = {}
    unknown_row_count = 0
    seen_unknown_keys: set[tuple[str, str]] = set()
    for chunk in pd.read_csv(membership_path, dtype=str, keep_default_na=False, chunksize=100_000):
        missing = required.difference(chunk.columns)
        if missing:
            raise ValueError("股票池成员文件缺少字段：" + ", ".join(sorted(missing)))
        unknown = chunk.loc[chunk["pool_status"].astype(str).str.strip().eq("UNKNOWN_TYPE")]
        for row in unknown.to_dict(orient="records"):
            day = pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d")
            symbol = str(row["symbol"]).strip().upper()
            if not symbol:
                raise ValueError("UNKNOWN_TYPE 股票池记录存在空代码")
            key = (day, symbol)
            if key in seen_unknown_keys:
                raise ValueError(f"UNKNOWN_TYPE 股票池存在重复日期/代码：{day} {symbol}")
            seen_unknown_keys.add(key)
            item = summary.setdefault(symbol, {
                "symbol": symbol,
                "exchange_prefix_observed": symbol[:2],
                "unknown_member_rows": 0,
                "first_seen_date": day,
                "last_seen_date": day,
                "appearance_dates": set(),
                "names": set(),
                "reported_security_types": set(),
                "reported_out_dates": set(),
            })
            item["unknown_member_rows"] += 1
            item["first_seen_date"] = min(item["first_seen_date"], day)
            item["last_seen_date"] = max(item["last_seen_date"], day)
            item["appearance_dates"].add(day)
            for field, target in (
                ("name_on_date_from_daily_list", "names"),
                ("type_from_latest_basic", "reported_security_types"),
                ("basic_out_date", "reported_out_dates"),
            ):
                value = str(row.get(field, "")).strip()
                if value:
                    item[target].add(value)
            unknown_row_count += 1

    known_unknown_symbols = set(summary)
    bar_row_counts: dict[str, int] = defaultdict(int)
    if known_unknown_symbols:
        for chunk in pd.read_csv(
            bars_source, usecols=["symbol", "trade_date"], dtype=str,
            keep_default_na=False, chunksize=250_000,
        ):
            chunk["symbol"] = chunk["symbol"].astype(str).str.strip().str.upper()
            chunk["trade_date"] = pd.to_datetime(chunk["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
            keys = list(zip(chunk["trade_date"], chunk["symbol"]))
            matched_symbols = [symbol for key, symbol in zip(keys, chunk["symbol"]) if key in seen_unknown_keys]
            counts = pd.Series(matched_symbols, dtype=str).value_counts()
            for symbol, count in counts.items():
                bar_row_counts[str(symbol)] += int(count)

    prior_review: dict[str, dict[str, str]] = {}
    if output.is_file():
        previous = pd.read_csv(output, dtype=str, keep_default_na=False)
        missing = set(UNKNOWN_TYPE_REVIEW_COLUMNS).difference(previous.columns)
        if missing:
            raise ValueError("已有未知类型审计表缺少复核字段：" + ", ".join(sorted(missing)))
        for row in previous[UNKNOWN_TYPE_REVIEW_COLUMNS].to_dict(orient="records"):
            symbol = str(row["symbol"]).strip().upper()
            if symbol in prior_review:
                raise ValueError(f"已有未知类型审计表代码重复：{symbol}")
            classification = str(row["classification"]).strip() or "UNRESOLVED"
            if classification not in ALLOWED_CLASSIFICATIONS:
                raise ValueError(f"{symbol} 的分类值无效：{classification}")
            if classification != "UNRESOLVED" and not (
                str(row["evidence_url"]).startswith("https://")
                and _valid_sha256(str(row["source_sha256"]))
                and str(row["reviewed_by"]).strip()
                and str(row["reviewed_at"]).strip()
            ):
                raise ValueError(f"{symbol} 已分类但缺少 HTTPS 来源、文件 SHA-256 或人工复核元数据")
            prior_review[symbol] = {**row, "classification": classification}

    stale_reviews = sorted(set(prior_review).difference(known_unknown_symbols))
    if stale_reviews:
        raise ValueError(
            "已有分类记录不属于当前快照的 UNKNOWN_TYPE 代码，请先人工复核：" + ", ".join(stale_reviews[:25])
        )

    records = []
    row_counts = {name: 0 for name in ALLOWED_CLASSIFICATIONS}
    for symbol, item in sorted(summary.items()):
        review = prior_review.get(symbol, {})
        classification = review.get("classification", "UNRESOLVED")
        row_counts[classification] += int(item["unknown_member_rows"])
        records.append({
            "symbol": symbol,
            "exchange_prefix_observed": item["exchange_prefix_observed"],
            "unknown_member_rows": item["unknown_member_rows"],
            "appearance_date_count": len(item["appearance_dates"]),
            "first_seen_date": item["first_seen_date"],
            "last_seen_date": item["last_seen_date"],
            "reported_names": "|".join(sorted(item["names"])),
            "reported_security_types": "|".join(sorted(item["reported_security_types"])),
            "reported_out_dates": "|".join(sorted(item["reported_out_dates"])),
            "bar_rows_in_supplied_file": int(bar_row_counts.get(symbol, 0)),
            "classification": classification,
            "evidence_url": review.get("evidence_url", ""),
            "source_sha256": review.get("source_sha256", ""),
            "reviewed_by": review.get("reviewed_by", ""),
            "reviewed_at": review.get("reviewed_at", ""),
            "review_note": review.get("review_note", ""),
        })
    columns = [
        "symbol", "exchange_prefix_observed", "unknown_member_rows", "appearance_date_count",
        "first_seen_date", "last_seen_date", "reported_names", "reported_security_types",
        "reported_out_dates", "bar_rows_in_supplied_file", *UNKNOWN_TYPE_REVIEW_COLUMNS[1:],
    ]
    pd.DataFrame(records, columns=columns).to_csv(output, index=False)
    explained_non_stock = row_counts["EXPLAINED_NON_STOCK"]
    added_as_stock = row_counts["ADDED_AS_STOCK"]
    unresolved = row_counts["UNRESOLVED"]
    report = {
        "schema_version": 1,
        "universe_membership_file": str(membership_path),
        "universe_membership_sha256": sha256_file(membership_path),
        "bars_file": str(bars_source),
        "bars_sha256": sha256_file(bars_source),
        "review_csv_sha256": sha256_file(output),
        "unknown_rows": unknown_row_count,
        "unknown_symbols": len(summary),
        "explained_non_stock_rows": explained_non_stock,
        "added_as_stock_rows": added_as_stock,
        "unresolved_rows": unresolved,
        "classification_partition_balances": (
            unknown_row_count == explained_non_stock + added_as_stock + unresolved
        ),
        "unresolved_symbol_count": sum(
            1 for row in records if row["classification"] == "UNRESOLVED"
        ),
        "scope_note": "Code prefix and OHLC presence are observations only; neither is used to infer security type.",
        "report_csv": str(output),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    report_path = output.with_suffix(output.suffix + ".summary.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
