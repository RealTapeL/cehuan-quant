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
