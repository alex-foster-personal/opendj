"""Shared SQLite verified-copy primitives for online backup and restore.

Used by :mod:`apps.sync_hub.hub_backup` and
:mod:`apps.shared.state_authoritative_backup`. A copy is taken through the
sqlite3 backup API inside a read transaction, then checked with
``PRAGMA integrity_check`` and equal per-table row counts before it is given
its final name.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


class VerifiedCopyCFG:
    BACKUP_SUFFIX: str = ".db"
    PARTIAL_PREFIX: str = ".partial-"
    #: ``%Z`` of an aware UTC datetime prints ``UTC`` itself, so no zone
    #: letter is ever hand-typed into a stamp (house rule, Mon 31 Aug 2026).
    STAMP_FORMAT: str = "%Y%m%dT%H%M%S%Z"
    SIDECAR_SUFFIXES: tuple[str, ...] = ("", "-wal", "-shm", "-journal")
    BACKUP_DIR_MODE: int = 0o700
    BACKUP_FILE_MODE: int = 0o600


class VerifiedCopyError(RuntimeError):
    """A verified copy could not be completed."""


def backup_file_name(*, prefix: str, taken_at: datetime) -> str:
    if taken_at.utcoffset() != timedelta(0):
        raise VerifiedCopyError(f"backup stamp must be UTC, got {taken_at.isoformat()}")
    stamp = taken_at.astimezone(UTC).strftime(VerifiedCopyCFG.STAMP_FORMAT)
    return f"{prefix}{stamp}{VerifiedCopyCFG.BACKUP_SUFFIX}"


def list_backups(dest_dir: Path, *, prefix: str) -> list[Path]:
    """Finished backups with ``prefix``, newest first. Stamps sort lexically by time."""
    pattern = f"{prefix}*{VerifiedCopyCFG.BACKUP_SUFFIX}"
    return sorted(Path(dest_dir).glob(pattern), reverse=True)


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables
    }


def _read_only_uri(path: Path) -> str:
    """Percent-encoded ``file:`` URI: a raw ``#`` or ``?`` would cut the path
    short and drop ``mode=ro``, so SQLite would open (and create) a different
    file read-write."""
    return f"{Path(path).resolve().as_uri()}?mode=ro"


def verify_backup(path: Path, *, empty_message: str | None = None) -> dict[str, int]:
    """``integrity_check`` must say ok; returns the per-table row counts."""
    if not Path(path).is_file():
        raise VerifiedCopyError(f"backup {path} does not exist")
    try:
        conn = sqlite3.connect(_read_only_uri(path), uri=True)
    except sqlite3.Error as exc:
        raise VerifiedCopyError(f"cannot open {path} read-only: {exc}") from exc
    try:
        verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        if verdict != ["ok"]:
            raise VerifiedCopyError(f"integrity_check failed for {path}: {verdict[:5]}")
        counts = _row_counts(conn)
    except sqlite3.DatabaseError as exc:
        raise VerifiedCopyError(f"{path} is not a readable sqlite database: {exc}") from exc
    finally:
        conn.close()
    if not counts:
        msg = empty_message or f"{path} holds no tables; that is not a sqlite DB"
        raise VerifiedCopyError(msg)
    return counts


def copy_snapshot(source: Path, partial: Path) -> dict[str, int]:
    """Copy ``source`` into ``partial`` and return the counts OF THAT SNAPSHOT."""
    src = sqlite3.connect(source, isolation_level=None)
    try:
        src.execute("PRAGMA query_only = ON")
        src.execute("BEGIN")
        counts = _row_counts(src)
        dst = sqlite3.connect(partial, isolation_level=None)
        try:
            src.backup(dst)
            # A standalone single file: no -wal sidecar to lose in transit.
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
        src.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        raise VerifiedCopyError(f"could not copy {source}: {exc}") from exc
    finally:
        src.close()
    return counts


def require_equal_counts(expected: dict[str, int], actual: dict[str, int], what: str) -> None:
    if actual != expected:
        raise VerifiedCopyError(f"{what}: row counts differ, expected {expected}, got {actual}")


def take_verified_backup(
    source: Path,
    dest_dir: Path,
    *,
    prefix: str,
    keep: int,
    now: datetime | None = None,
    missing_source_message: str | None = None,
    empty_backup_message: str | None = None,
) -> Path:
    """Take, verify and keep one online backup of ``source``; return the final path."""
    if keep < 1:
        raise VerifiedCopyError(f"--keep must be at least 1, got {keep}")
    if not source.is_file():
        msg = missing_source_message or f"no DB at {source}"
        raise VerifiedCopyError(msg)
    taken_at = now if now is not None else datetime.now(UTC)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True, mode=VerifiedCopyCFG.BACKUP_DIR_MODE)
    final = dest_dir / backup_file_name(prefix=prefix, taken_at=taken_at)
    partial = dest_dir / f"{VerifiedCopyCFG.PARTIAL_PREFIX}{final.name}"
    if final.exists() or partial.exists():
        raise VerifiedCopyError(f"{final} (or its partial) already exists; refusing to overwrite")
    partial.touch(mode=VerifiedCopyCFG.BACKUP_FILE_MODE, exist_ok=False)
    try:
        snapshot_counts = copy_snapshot(source, partial)
        copied_counts = verify_backup(partial, empty_message=empty_backup_message)
        require_equal_counts(snapshot_counts, copied_counts, f"backup copy of {source}")
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    prune_backups(dest_dir, prefix=prefix, keep=keep)
    return final


def prune_backups(dest_dir: Path, *, prefix: str, keep: int) -> list[Path]:
    """Delete finished backups beyond the newest ``keep``. Returns what went."""
    doomed = list_backups(dest_dir, prefix=prefix)[keep:]
    for path in doomed:
        path.unlink()
    return doomed
