"""Verify archived corporate-action sample files against a field-audit manifest.

The verifier proves only byte-hash and extraction-token matches. It cannot
attest that a human interpreted an announcement correctly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MANIFEST_COLUMNS = {
    "sample_id", "event_type", "symbol", "source_url", "archive_path", "source_sha256",
    "extracted_text_path", "required_text_tokens", "machine_extracted_values", "review_status",
    "reviewed_by", "reviewed_at", "manual_review_notes",
}


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def verify_accounting_sample_manifest(manifest_path: str | Path) -> dict[str, Any]:
    path = Path(manifest_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"公司行动样本清单不存在：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("公司行动样本清单缺少表头")
        missing = MANIFEST_COLUMNS.difference(reader.fieldnames)
        if missing:
            raise ValueError("公司行动样本清单缺少字段：" + ", ".join(sorted(missing)))
        rows = list(reader)
    if not rows:
        raise ValueError("公司行动样本清单不能为空")
    ids = [row["sample_id"].strip() for row in rows]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("sample_id 必须非空且唯一")

    root = path.parents[2] if len(path.parents) >= 3 else path.parent
    results: list[dict[str, Any]] = []
    for row in rows:
        source_url = row["source_url"].strip()
        if urlparse(source_url).scheme != "https":
            raise ValueError(f"{row['sample_id']} source_url 必须使用 HTTPS")
        archive = (root / row["archive_path"].strip()).resolve()
        extracted = (root / row["extracted_text_path"].strip()).resolve()
        expected_hash = row["source_sha256"].strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"{row['sample_id']} source_sha256 格式错误")

        archive_exists = archive.is_file()
        observed_hash = hashlib.sha256(archive.read_bytes()).hexdigest() if archive_exists else None
        hash_matches = observed_hash == expected_hash
        text_exists = extracted.is_file()
        extracted_text = extracted.read_text(encoding="utf-8") if text_exists else ""
        normalized = _normalized_text(extracted_text)
        required_tokens = [token.strip() for token in row["required_text_tokens"].split("|") if token.strip()]
        missing_tokens = [token for token in required_tokens if _normalized_text(token) not in normalized]
        tokens_match = text_exists and bool(required_tokens) and not missing_tokens
        automated_status = "PASS" if hash_matches and tokens_match else "FAIL"
        results.append({
            "sample_id": row["sample_id"].strip(),
            "event_type": row["event_type"].strip(),
            "symbol": row["symbol"].strip(),
            "source_url": source_url,
            "archive_path": str(archive),
            "archive_exists": archive_exists,
            "declared_sha256": expected_hash,
            "observed_sha256": observed_hash,
            "source_hash_matches": hash_matches,
            "extracted_text_exists": text_exists,
            "required_token_count": len(required_tokens),
            "missing_tokens": missing_tokens,
            "token_match": tokens_match,
            "automated_status": automated_status,
            "manual_review_status": row["review_status"].strip(),
            "reviewed_by": row["reviewed_by"].strip(),
            "reviewed_at": row["reviewed_at"].strip(),
            "manual_review_notes": row["manual_review_notes"].strip(),
            "machine_extracted_values": row["machine_extracted_values"].strip(),
            "interpretation_limit": "Hash and text-token checks do not establish legal meaning or account-level allocation.",
        })
    return {
        "schema_version": 1,
        "manifest_path": str(path),
        "sample_count": len(results),
        "automated_pass_count": sum(row["automated_status"] == "PASS" for row in results),
        "manual_signoff_pending_count": sum(
            "PENDING" in row["manual_review_status"].upper() for row in results
        ),
        "result_type": "source_archive_integrity_and_extraction_tokens_only_not_manual_legal_review",
        "samples": results,
    }


_FIELD_TERMS = {
    "symbol": ("证券代码", "股票代码"),
    "old_symbol": ("证券代码", "原证券代码"),
    "new_symbol": ("变更后的证券代码", "新证券代码"),
    "record_date": ("股权登记日",),
    "ex_date": ("除权（息）日", "除权除息日", "除权日"),
    "payable_date": ("现金红利发放日", "红利发放日"),
    "effective_date": ("启用日期", "生效日", "除权（息）日"),
    "registration_date": ("新增无限售条件流通股", "登记日"),
    "tradable_date": ("上市日", "可流通日", "新增无限售条件流通股"),
    "gross_cash_cny_per_share": ("每股现金红利", "每股派发现金红利"),
    "shares_per_old_share": ("每股转增股份", "每股送股"),
    "share_ratio": ("公司股份数量不变", "股份数量不变", "换股比例"),
    "individual_payment_withholding": ("暂不扣缴个人所得税", "待实际转让股票时"),
    "fractional_allocation_rule": ("零碎股", "尾数", "整数股"),
    "security_identity": ("公司股份数量不变", "持股期限"),
}


def _referenced_pages(reference: str, page_count: int) -> list[int]:
    requested: set[int] = set()
    for match in re.finditer(r"第\s*(\d+)\s*(?:[-–]\s*(\d+)\s*)?页", reference):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        requested.update(range(start, end + 1))
    return sorted(page for page in requested if 1 <= page <= page_count)


def _candidate_page_excerpts(
    *, field_name: str,
    official_value: str,
    machine_value: str,
    page_reference: str,
    extracted_text: str,
) -> tuple[str, str]:
    pages = extracted_text.split("\f")
    page_numbers = _referenced_pages(page_reference, len(pages))
    if not page_numbers:
        page_numbers = list(range(1, len(pages) + 1))
    if any(marker in official_value.upper() for marker in ("NOT IN ANNOUNCEMENT", "REQUIRES", "NOT STATED")):
        return ",".join(map(str, page_numbers)), "NOT_STATED_OR_ACCOUNT_EVIDENCE_REQUIRED"
    terms = list(_FIELD_TERMS.get(field_name, ()))
    for token in re.split(r"[|;；,，]", machine_value):
        token = token.strip()
        if len(token) >= 3 and not token.lower().startswith(("record=", "ex=", "cash=", "ratio=")):
            terms.append(token)
    excerpts: list[str] = []
    for page_number in page_numbers:
        lines = [line.strip() for line in pages[page_number - 1].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if any(term and term in line for term in terms):
                excerpt = " ".join(lines[max(0, index - 1):min(len(lines), index + 2)])
                if excerpt not in excerpts:
                    excerpts.append(excerpt[:600])
                if len(excerpts) >= 3:
                    break
        if len(excerpts) >= 3:
            break
    if excerpts:
        return ",".join(map(str, page_numbers)), "CANDIDATE_EXCERPT_FOUND: " + " || ".join(excerpts)
    return ",".join(map(str, page_numbers)), "NO_AUTOMATIC_EXCERPT: REVIEW_PAGE_MANUALLY"


def build_accounting_sample_review_pack(
    sample_manifest_path: str | Path,
    field_review_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Build a private, copyable human review packet without changing signoff state."""
    sample_path = Path(sample_manifest_path).expanduser().resolve()
    fields_path = Path(field_review_path).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"审阅包输出目录已存在，拒绝覆盖：{output}")
    sample_audit = verify_accounting_sample_manifest(sample_path)
    sample_root = sample_path.parents[2]
    with sample_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample_rows = list(csv.DictReader(handle))
    with fields_path.open("r", encoding="utf-8-sig", newline="") as handle:
        field_reader = csv.DictReader(handle)
        field_rows = list(field_reader)
        field_names = list(field_reader.fieldnames or [])
    sample_by_id = {row["sample_id"].strip(): row for row in sample_rows}
    if not field_rows:
        raise ValueError("字段审核表不能为空")
    for required in ("sample_id", "field_name", "official_value", "page_reference", "machine_extracted_value", "review_status"):
        if required not in field_names:
            raise ValueError(f"字段审核表缺少列：{required}")

    output.mkdir(parents=True)
    originals_dir = output / "originals"
    extracted_dir = output / "extracted-text"
    originals_dir.mkdir()
    extracted_dir.mkdir()
    extracted_by_sample: dict[str, str] = {}
    copy_by_sample: dict[str, dict[str, str]] = {}
    for sample_id, row in sample_by_id.items():
        archive = (sample_root / row["archive_path"].strip()).resolve()
        extracted = (sample_root / row["extracted_text_path"].strip()).resolve()
        if not archive.is_file() or not extracted.is_file():
            raise FileNotFoundError(f"{sample_id}: 审阅包要求原件 PDF 和提取文本均存在")
        observed_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        if observed_hash != row["source_sha256"].strip().lower():
            raise ValueError(f"{sample_id}: 原件 SHA-256 不匹配，拒绝生成审阅包")
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_id)
        pdf_copy = originals_dir / f"{safe_id}.pdf"
        text_copy = extracted_dir / f"{safe_id}.txt"
        shutil.copyfile(archive, pdf_copy)
        shutil.copyfile(extracted, text_copy)
        extracted_by_sample[sample_id] = extracted.read_text(encoding="utf-8")
        copy_by_sample[sample_id] = {
            "pdf": str(pdf_copy.relative_to(output)),
            "text": str(text_copy.relative_to(output)),
            "sha256": observed_hash,
        }

    enriched_rows: list[dict[str, str]] = []
    for row in field_rows:
        sample_id = row["sample_id"].strip()
        if sample_id not in sample_by_id:
            raise ValueError(f"字段审核行引用未登记的 sample_id：{sample_id}")
        source = sample_by_id[sample_id]
        if row.get("source_sha256", "").strip().lower() not in {"", source["source_sha256"].strip().lower()}:
            raise ValueError(f"{sample_id}/{row['field_name']}: 字段表来源哈希与样本清单不一致")
        if row.get("archive_path", "").strip() not in {"", source["archive_path"].strip()}:
            raise ValueError(f"{sample_id}/{row['field_name']}: 字段表原件路径与样本清单不一致")
        pages, locator = _candidate_page_excerpts(
            field_name=row["field_name"].strip(),
            official_value=row["official_value"].strip(),
            machine_value=row["machine_extracted_value"].strip(),
            page_reference=row["page_reference"].strip(),
            extracted_text=extracted_by_sample[sample_id],
        )
        enriched_rows.append({
            **row,
            "source_title": source["source_title"],
            "source_url": source["source_url"],
            "archive_sha256_verified": copy_by_sample[sample_id]["sha256"],
            "local_pdf_copy": copy_by_sample[sample_id]["pdf"],
            "local_text_copy": copy_by_sample[sample_id]["text"],
            "candidate_page_numbers": pages,
            "candidate_excerpt_or_locator_status": locator,
            "field_scope": (
                "ACCOUNT_EVIDENCE_REQUIRED" if "ACCOUNT" in row["official_value"].upper()
                or "NOT IN ANNOUNCEMENT" in row["official_value"].upper()
                else "ANNOUNCEMENT_CANDIDATE_REQUIRES_HUMAN_CHECK"
            ),
            "review_status": row["review_status"].strip() or "PENDING",
        })
    review_csv = output / "review_fields.csv"
    with review_csv.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(enriched_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(enriched_rows)

    pending_rows = sum("PENDING" in row["review_status"].upper() for row in enriched_rows)
    source_manifest = {
        "schema_version": 1,
        "result_type": "human_review_packet_candidate_excerpts_not_signed_off",
        "source_manifest": str(sample_path),
        "field_review_source": str(fields_path),
        "sample_count": len(sample_rows),
        "field_count": len(enriched_rows),
        "source_archive_hashes_verified": all(item["source_hash_matches"] for item in sample_audit["samples"]),
        "manual_signoff_pending_fields": pending_rows,
        "source_copies": copy_by_sample,
        "signoff_state_changed": False,
    }
    (output / "package_manifest.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (output / "README.md").write_text(
        "# 公司行动公告人工审阅包\n\n"
        f"- 样本事件：{len(sample_rows)} 条；字段：{len(enriched_rows)} 项。\n"
        f"- 原件哈希与提取文本自动检查：{'通过' if source_manifest['source_archive_hashes_verified'] else '未通过'}。\n"
        f"- 待人工签认字段：{pending_rows} 项。\n"
        "- 本包只提供机器候选摘录和页码定位，不代替逐页阅读公告；哈希不证明解释正确。\n"
        "- 所有签认状态保持输入值；请人工核对后在项目审核表中填写审核人、日期和结论。\n"
        "- `ACCOUNT_EVIDENCE_REQUIRED` 表示发行人公告不能提供个人账户到账、最终获配或实际税款等事实。\n\n"
        "文件：`review_fields.csv` 为逐字段审阅表；`originals/` 与 `extracted-text/` 为本地审阅副本；"
        "`package_manifest.json` 记录输入和哈希状态。该目录建议放在 `outputs/` 下并保持本机私有。\n",
        encoding="utf-8",
    )
    return source_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="核验公司行动公告小样的私有原件散列与文本字段")
    parser.add_argument("--manifest", required=True, help="样本字段对照 CSV")
    parser.add_argument("--output", help="可选 JSON 输出路径；不提供时打印到标准输出")
    args = parser.parse_args()
    result = verify_accounting_sample_manifest(args.manifest)
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = Path.cwd() / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if result["automated_pass_count"] == result["sample_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
