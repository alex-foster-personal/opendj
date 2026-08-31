"""Connection helpers for the state DB.

All callers MUST go through :func:`open_rw` or :func:`open_ro`. They set
PRAGMAs uniformly and (in the rw case) apply migrations on demand so the
DB is always usable immediately.

No wrapping around sqlite3.Connection -- callers use the stdlib object
directly. This keeps the surface small and matches the rest of the repo's
style (see ``apps/shared/djay_db.py``).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import paths as state_paths
from . import schema as _schema
from . import sync_stamp as _sync_stamp

# PRAGMA values used for every writable handle. WAL + NORMAL is the usual
# recommendation for OLTP-ish workloads; foreign_keys enforces our
# REFERENCES clauses; busy_timeout rides out a few hundred ms of lock
# contention without the caller seeing sqlite3.OperationalError.
_RW_PRAGMAS: tuple[tuple[str, object], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", 5000),
)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _apply_rw_pragmas(conn: sqlite3.Connection) -> None:
    for key, value in _RW_PRAGMAS:
        conn.execute(f"PRAGMA {key} = {value}")


def open_rw(
    path: Path | None = None,
    *,
    apply_schema: bool = True,
    check_same_thread: bool = True,
) -> sqlite3.Connection:
    """Open ``path`` read-write; optionally apply migrations.

    The parent directory is created if missing so ``init`` can be called on
    a fresh checkout.

    ``check_same_thread=False`` is for callers that own a single connection
    behind their own lock but are invoked from a thread pool (e.g. the webui
    ``PlaylistStore`` under FastAPI's sync-endpoint executor). Such callers
    MUST serialise all access themselves.

    When migrations run, the post-migration hook
    :func:`apps.shared.state.sync_stamp.backfill_local_machine_id` claims any
    ``track_locations`` row that migration v6 could not stamp (schema.py
    reading 3). It is a no-op on a DB with nothing to claim, so an ordinary
    open still mints no identity file and writes no ``machines`` row.
    """
    target = Path(path) if path is not None else state_paths.STATE_DB
    _ensure_parent(target)
    conn = sqlite3.connect(
        str(target), isolation_level=None,
        check_same_thread=check_same_thread,
    )
    try:
        _apply_rw_pragmas(conn)
        if apply_schema:
            _schema.apply_migrations(conn)
            _sync_stamp.backfill_local_machine_id(conn)
    except Exception:
        conn.close()
        raise
    return conn


def open_ro(path: Path | None = None) -> sqlite3.Connection:
    """Open ``path`` read-only with a query_only guard.

    Uses the SQLite URI form ``mode=ro`` so write attempts raise
    :class:`sqlite3.OperationalError` at execute time.
    """
    target = Path(path) if path is not None else state_paths.STATE_DB
    if not target.exists():
        raise FileNotFoundError(
            f"state DB not found at {target}; run "
            f"`python -m apps.shared.state.cli init` first."
        )
    uri = f"file:{target}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect_rw(
    path: Path | None = None,
    *,
    apply_schema: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Context-managed :func:`open_rw`. Closes on exit."""
    conn = open_rw(path, apply_schema=apply_schema)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_ro(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed :func:`open_ro`. Closes on exit."""
    conn = open_ro(path)
    try:
        yield conn
    finally:
        conn.close()


__all__ = ["open_rw", "open_ro", "connect_rw", "connect_ro"]
