"""Conservative cross-file fill overlap classification.

This module works on explicitly normalized CSV columns. It does not assume a
broker-specific execution-id column and never drops or merges rows.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
from typing import Sequence

import pandas as pd

from cehuan.data.canonical import canonical_symbol


CORE_COLUMNS = ("trade_date", "symbol", "side", "quantity", "price")
OUTPUT_COLUMNS = (
    "classification", "source_file", "source_sha256", "source_row_number",
    "matched_source_file", "matched_row_number", "execution_id", "correction_link",
    "record_status", "order_id", *CORE_COLUMNS, "trade_time", "reason",
)


def _decimal_text(value: str, field: str, *, positive: bool = True) -> str:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是有效十进制数") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ValueError(f"{field} 必须为有限正数")
    return format(parsed.normalize(), "f")


def _load_export(
    path: str | Path,
    *,
    execution_id_column: str | None,
    correction_link_column: str | None,
    status_column: str | None,
) -> list[dict[str, str | int]]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"成交导出文件不存在：{source}")
    raw = source.read_bytes()
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    required = set(CORE_COLUMNS)
    optional = {column for column in (execution_id_column, correction_link_column, status_column) if column}
    optional.add("trade_time")
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(
            "跨文件成交去重原型要求规范列 " + ", ".join(CORE_COLUMNS)
            + "；缺少：" + ", ".join(missing)
        )
    missing_optional = sorted((optional - {"trade_time"}).difference(frame.columns))
    if missing_optional:
        raise ValueError("指定的可选字段不存在：" + ", ".join(missing_optional))
    digest = hashlib.sha256(raw).hexdigest()
    rows: list[dict[str, str | int]] = []
    for index, row in frame.iterrows():
        trade_date = str(row["trade_date"]).strip()
        try:
            parsed_date = pd.Timestamp(trade_date)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{source.name} 第 {index + 2} 行 trade_date 无效") from exc
        if pd.isna(parsed_date) or parsed_date.strftime("%Y-%m-%d") != trade_date[:10]:
            raise ValueError(f"{source.name} 第 {index + 2} 行 trade_date 必须以 YYYY-MM-DD 开始")
        symbol = canonical_symbol(row["symbol"])
        side = str(row["side"]).strip().upper()
        side = {"买入": "BUY", "买": "BUY", "B": "BUY", "卖出": "SELL", "卖": "SELL", "S": "SELL"}.get(side, side)
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"{source.name} 第 {index + 2} 行 side 只能是 BUY/SELL")
        quantity_text = _decimal_text(str(row["quantity"]), "quantity")
        quantity_decimal = Decimal(quantity_text)
        if quantity_decimal != quantity_decimal.to_integral_value():
            raise ValueError(f"{source.name} 第 {index + 2} 行 quantity 必须是整数股数")
        item: dict[str, str | int] = {
            "source_file": str(source),
            "source_sha256": digest,
            "source_row_number": int(index) + 2,
            "trade_date": trade_date[:10],
            "symbol": symbol,
            "side": side,
            "quantity": format(quantity_decimal.quantize(Decimal("1")), "f"),
            "price": _decimal_text(str(row["price"]), "price"),
            "trade_time": str(row.get("trade_time", "")).strip(),
            "execution_id": str(row.get(execution_id_column, "")).strip() if execution_id_column else "",
            "correction_link": str(row.get(correction_link_column, "")).strip() if correction_link_column else "",
            "record_status": str(row.get(status_column, "")).strip().upper() if status_column else "",
            "order_id": str(row.get("order_id", "")).strip(),
        }
        rows.append(item)
    return rows


def audit_cross_file_fills(
    previous_files: Sequence[str | Path],
    current_file: str | Path,
    *,
    execution_id_column: str | None = None,
    correction_link_column: str | None = None,
    status_column: str | None = None,
) -> pd.DataFrame:
    """Classify current rows against prior exports without merging or dropping.

    The normalized columns `trade_date,symbol,side,quantity,price` are required.
    A stable execution identifier is used only when the caller names its column
    explicitly. Without one, matching trade details are only
    `POSSIBLE_DUPLICATE` and must be reviewed by a person.
    """
    prior_rows: list[dict[str, str | int]] = []
    for path in previous_files:
        prior_rows.extend(_load_export(
            path,
            execution_id_column=execution_id_column,
            correction_link_column=correction_link_column,
            status_column=status_column,
        ))
    current_rows = _load_export(
        current_file,
        execution_id_column=execution_id_column,
        correction_link_column=correction_link_column,
        status_column=status_column,
    )

    def signature(item: dict[str, str | int]) -> tuple[str, ...]:
        return tuple(str(item[key]) for key in (*CORE_COLUMNS, "trade_time"))

    prior_by_id: dict[str, list[dict[str, str | int]]] = {}
    prior_by_signature: dict[tuple[str, ...], list[dict[str, str | int]]] = {}
    for item in prior_rows:
        identifier = str(item["execution_id"])
        if identifier:
            prior_by_id.setdefault(identifier, []).append(item)
        prior_by_signature.setdefault(signature(item), []).append(item)

    current_by_id: dict[str, list[dict[str, str | int]]] = {}
    current_by_signature: dict[tuple[str, ...], list[dict[str, str | int]]] = {}
    for item in current_rows:
        identifier = str(item["execution_id"])
        if identifier:
            current_by_id.setdefault(identifier, []).append(item)
        current_by_signature.setdefault(signature(item), []).append(item)

    output: list[dict[str, object]] = []
    for item in current_rows:
        identifier = str(item["execution_id"])
        link = str(item["correction_link"])
        candidates: list[dict[str, str | int]] = []
        classification = "NEW"
        reason = "未发现稳定标识冲突或字段相同的既有记录"

        if link and link in prior_by_id:
            classification = "CORRECTION_OR_REVERSAL"
            candidates = prior_by_id[link]
            reason = "显式 correction_link 指向既有 execution id；需按原件核对更正/冲正语义"
        elif str(item["record_status"]) in {"CORRECTION", "REVERSAL", "CANCEL", "更正", "冲正", "撤销"}:
            classification = "POSSIBLE_DUPLICATE"
            reason = "来源状态标为更正/冲正，但缺少可核实的原成交关联编号"
        elif identifier:
            matches = prior_by_id.get(identifier, [])
            if not matches:
                matches = [
                    other for other in current_by_id.get(identifier, [])
                    if int(other["source_row_number"]) < int(item["source_row_number"])
                ]
            if matches:
                candidates = matches
                if all(signature(other) == signature(item) for other in matches):
                    classification = "EXACT_DUPLICATE"
                    reason = "显式 execution id 与成交核心字段均一致；本报告不自动删除记录"
                else:
                    classification = "CONFLICT"
                    reason = "相同 execution id 对应不同日期、证券、方向、数量、价格或时间"
        else:
            matches = prior_by_signature.get(signature(item), [])
            if matches:
                classification = "POSSIBLE_DUPLICATE"
                candidates = matches
                reason = "没有显式稳定 execution id，成交字段相同只能标疑似重复"
            elif len(current_by_signature.get(signature(item), [])) > 1:
                classification = "POSSIBLE_DUPLICATE"
                candidates = [other for other in current_by_signature[signature(item)] if other is not item]
                reason = "当前导出内存在字段相同记录；可能是重复，也可能是两笔真实成交"

        matched = candidates[0] if candidates else {}
        output.append({
            "classification": classification,
            "source_file": item["source_file"],
            "source_sha256": item["source_sha256"],
            "source_row_number": item["source_row_number"],
            "matched_source_file": matched.get("source_file", ""),
            "matched_row_number": matched.get("source_row_number", ""),
            "execution_id": item["execution_id"],
            "correction_link": item["correction_link"],
            "record_status": item["record_status"],
            "order_id": item["order_id"],
            **{column: item[column] for column in CORE_COLUMNS},
            "trade_time": item["trade_time"],
            "reason": reason,
        })
    return pd.DataFrame(output, columns=OUTPUT_COLUMNS)
