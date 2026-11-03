"""Idempotency ledger for imported fill CSV files."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from numbers import Integral
import os
from pathlib import Path
import tempfile
from typing import Iterator


class DuplicateFillImportError(ValueError):
    """Raised when the exact same fill export is imported into a registry twice."""


def find_fill_import_record(
    fills_csv: str | Path,
    registry_path: str | Path,
) -> dict[str, object] | None:
    """Return the existing row for an exact source hash under the registry lock."""
    source = Path(fills_csv).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"成交 CSV 不存在：{source}")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    lock_path = registry.with_suffix(registry.suffix + ".lock")
    with _exclusive_lock(lock_path):
        if not registry.exists():
            return None
        document = json.loads(registry.read_text(encoding="utf-8"))
        if document.get("schema_version") != 1 or not isinstance(document.get("imports"), list):
            raise ValueError("成交导入登记簿 schema 无效，拒绝继续读取")
        prior = next((item for item in document["imports"] if item.get("source_sha256") == digest), None)
        return dict(prior) if prior is not None else None


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def register_fill_import(
    fills_csv: str | Path,
    registry_path: str | Path,
    *,
    row_count: int,
    cycle_run_id: str | None = None,
    output_manifest_path: str | Path | None = None,
) -> dict[str, object]:
    """Record one source file hash, rejecting an exact replay before double count.

    The registry is local state. Its lock and atomic replacement make concurrent
    imports serialize on macOS/Linux; each successful import remains auditable.
    When supplied, the completed review manifest must already bind the same fill
    hash and import ID; the registry then stores that manifest path and hash.
    Changed exports naturally receive a different hash and can be imported.
    """
    source = Path(fills_csv).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"成交 CSV 不存在：{source}")
    if isinstance(row_count, bool) or not isinstance(row_count, Integral) or row_count < 0:
        raise ValueError("row_count 必须为非负整数")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = Path(output_manifest_path).expanduser().resolve() if output_manifest_path else None
    manifest_digest = None
    if manifest_path is not None:
        if not manifest_path.is_file():
            raise FileNotFoundError(f"复盘 manifest 必须先完整生成才能登记成交：{manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        reference = manifest.get("fill_import_reference")
        input_hashes = manifest.get("input_sha256") or {}
        if manifest.get("result_type") != "manual_account_reconciliation_csv_not_broker_connected":
            raise ValueError("导入登记只能绑定完整的人工复盘 manifest")
        if not isinstance(input_hashes, dict) or input_hashes.get("fills") != digest:
            raise ValueError("复盘 manifest 的 fills 输入散列与成交文件不匹配")
        if not (manifest_path.parent / "review.csv").is_file() or not (manifest_path.parent / "review.md").is_file():
            raise ValueError("复盘 manifest 对应的 review.csv/review.md 尚未完整发布")
        if not isinstance(reference, dict) or reference.get("source_sha256") != digest:
            raise ValueError("复盘 manifest 未绑定本次成交 CSV 散列，拒绝登记")
        if reference.get("import_id") != f"fill-import-{digest[:16]}":
            raise ValueError("复盘 manifest 的 import_id 与成交文件散列不一致，拒绝登记")
        if cycle_run_id and manifest.get("cycle_run_id") != cycle_run_id:
            raise ValueError("复盘 manifest 的 cycle_run_id 与成交登记参数不一致，拒绝登记")
        manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lock_path = registry.with_suffix(registry.suffix + ".lock")
    with _exclusive_lock(lock_path):
        if registry.exists():
            document = json.loads(registry.read_text(encoding="utf-8"))
            if document.get("schema_version") != 1 or not isinstance(document.get("imports"), list):
                raise ValueError("成交导入登记簿 schema 无效，拒绝继续追加")
        else:
            document = {"schema_version": 1, "imports": []}
        prior = next((item for item in document["imports"] if item.get("source_sha256") == digest), None)
        if prior is not None:
            raise DuplicateFillImportError(
                f"同一成交 CSV 已导入：{digest}；首次导入时间 {prior.get('imported_at_utc')}，"
                "本次未重复计入"
            )
        receipt = {
            "import_id": f"fill-import-{digest[:16]}",
            "source_path": str(source),
            "source_sha256": digest,
            "row_count": int(row_count),
            "cycle_run_id": cycle_run_id,
            "output_manifest_path": str(manifest_path) if manifest_path else None,
            "output_manifest_sha256": manifest_digest,
            "imported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        document["imports"].append(receipt)
        _atomic_write(registry, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return receipt
