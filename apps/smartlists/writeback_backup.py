"""SQLite-consistent writeback snapshots.

``Connection.backup`` copies the logical database through SQLite itself, so it
includes committed WAL pages.  Never use ``shutil.copy2`` for a live SQLite
writeback target.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from apps.shared import paths

WRITEBACK_BACKUP_DIR = paths.DATA_DIR / "writeback-backups"


def backup_path(vendor: str, backup_id: str) -> Path:
    return WRITEBACK_BACKUP_DIR / f"{vendor}.{backup_id}.db"


def reversal_path(vendor: str, backup_id: str) -> Path:
    return WRITEBACK_BACKUP_DIR / f"{vendor}.{backup_id}.reversal.json"


def write_reversal(
    vendor: str, backup_id: str, target_path: Path, target_id: str,
    stable_preimage: list[str], native_preimage: list[str], post_apply_revision: str,
) -> None:
    """Persist stable and native preimages for one exact reversible target."""
    canonical_target = target_path.resolve(strict=True)
    destination = reversal_path(vendor, backup_id)
    temporary = destination.with_name(f".{destination.name}.tmp")
    payload = json.dumps({
        "backup_id": backup_id, "vendor": vendor, "target_path": str(canonical_target), "target_id": target_id,
        "stable_preimage": stable_preimage, "native_preimage": native_preimage,
        "post_apply_revision": post_apply_revision,
    }, sort_keys=True)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def read_reversal(
    vendor: str, backup_id: str, target_path: Path, target_id: str, expected_revision: str,
) -> tuple[list[str], list[str]]:
    """Load one exact reversal record or reject it before a vendor mutation."""
    path = reversal_path(vendor, backup_id)
    if not path.exists():
        raise RuntimeError(f"{vendor}: reversal ID {backup_id!r} not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    expected = {"backup_id": backup_id, "vendor": vendor, "target_path": str(target_path.resolve(strict=True)), "target_id": target_id, "post_apply_revision": expected_revision}
    if any(data.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"{vendor}: reversal metadata does not bind this exact target and revision")
    stable_preimage = data.get("stable_preimage")
    native_preimage = data.get("native_preimage")
    if not isinstance(stable_preimage, list) or not all(isinstance(item, str) for item in stable_preimage):
        raise RuntimeError(f"{vendor}: stable reversal preimage is malformed")
    if not isinstance(native_preimage, list) or not all(isinstance(item, str) for item in native_preimage):
        raise RuntimeError(f"{vendor}: native reversal preimage is malformed")
    return stable_preimage, native_preimage


def online_backup(source: sqlite3.Connection, vendor: str) -> str:
    """Copy the exact source connection snapshot, including committed WAL data."""
    WRITEBACK_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_id = uuid.uuid4().hex
    destination = backup_path(vendor, backup_id)
    with sqlite3.connect(destination) as target:
        source.backup(target)
    if destination.stat().st_size <= 0:
        raise RuntimeError(f"{vendor}: online backup is empty: {destination}")
    return backup_id


@contextmanager
def exclusive_target_lock(target: Path):
    """Serialize writeback ownership across CAS, backup, mutation, and restore."""
    lock = target.with_name(f".{target.name}.writeback.lock")
    try:
        descriptor = lock.open("x")
    except FileExistsError as exc:
        raise RuntimeError(f"writeback target is already owned: {target}") from exc
    try:
        yield
    finally:
        descriptor.close()
        lock.unlink(missing_ok=True)


__all__ = ["WRITEBACK_BACKUP_DIR", "backup_path", "exclusive_target_lock", "online_backup", "read_reversal", "reversal_path", "write_reversal"]
