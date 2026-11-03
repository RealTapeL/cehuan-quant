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
) -> dict[str, object]:
    """Record one source file hash, rejecting an exact replay before double count.

    The registry is local state. Its lock and atomic replacement make concurrent
    imports serialize on macOS/Linux; each successful import remains auditable.
    Changed exports naturally receive a different hash and can be imported.
    """
    source = Path(fills_csv).expanduser().resolve()
    registry = Path(registry_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"成交 CSV 不存在：{source}")
    if isinstance(row_count, bool) or not isinstance(row_count, Integral) or row_count < 0:
        raise ValueError("row_count 必须为非负整数")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
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
            "imported_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        document["imports"].append(receipt)
        _atomic_write(registry, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return receipt
