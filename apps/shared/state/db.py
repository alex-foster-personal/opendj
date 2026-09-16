"""Connection helpers for the state DB.

All callers MUST go through :func:`open_rw` or :func:`open_ro`. They set
PRAGMAs uniformly and (in the rw case) apply migrations on demand so the
DB is always usable immediately.

No wrapping around sqlite3.Connection -- callers use the stdlib object
directly. This keeps the surface small and matches the rest of the repo's
style (see ``apps/shared/djay_db.py``).
"""
from __future__ import annotations

import atexit
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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

# Matches PRAGMA busy_timeout above; passed to sqlite3.connect as well.
_RW_CONNECT_TIMEOUT_S: float = 5.0


class StateStoreBusyError(RuntimeError):
    """Raised when SQLite cannot acquire the writer lock after busy_timeout."""


def is_sqlite_busy(exc: BaseException) -> bool:
    """True for SQLITE_BUSY / ``database is locked`` :class:`OperationalError`."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, "sqlite_errorcode", None)
    if code == sqlite3.SQLITE_BUSY:
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database is busy" in message


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

    When the schema is at least v15,
    :func:`apps.shared.state.migrations_v15.backfill_track_fields_stamps`
    stamps legacy ``track_fields`` rows and appends the active-role changelog
    entry (issue #3136). A durable marker makes repeat opens constant-time.

    When the schema is at least v16,
    :func:`apps.shared.state.migrations_v16.repair_hub_changelog_stamps`
    scans for latest ``hub_changelog`` rows whose stamp disagrees with the
    live domain row, appends corrected entries at fresh sequences, and records
    completion in ``schema_meta_markers`` (issue #3171). The candidate scan
    runs before ``BEGIN IMMEDIATE``.

    After migrations and machine-id backfill,
    :func:`apps.database.regenerate_agents_md_if_writable` regenerates
    ``<state_dir>/AGENTS.md`` when the state directory is writable; a docs
    gap on an owned table
    (:class:`apps.database.generate_agents_md.MissingColumnDocsError`)
    still fails the open. Leftover tables that are not in
    :data:`apps.shared.state.schema.ALL_KNOWN_TABLES` are omitted from
    the sidecar rather than aborting the open -- they are one-shot
    conversion leftovers, not a forgotten schema column. Foreign-authority
    tables (pairings, play_orders, launcher_meta, FTS) stay in the sidecar
    when they are live. An
    unwritable directory is a spec-mandated skip, not a caught failure.
    ``MissingColumnDocsError`` is intentionally not caught here.
    """
    target = Path(path) if path is not None else state_paths.STATE_DB
    _ensure_parent(target)
    conn = sqlite3.connect(
        str(target),
        timeout=_RW_CONNECT_TIMEOUT_S,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    try:
        _apply_rw_pragmas(conn)
        if apply_schema:
            version = _schema.apply_migrations(conn)
            _sync_stamp.backfill_local_machine_id(conn)
            if version >= 15:
                from .migrations_v15 import backfill_track_fields_stamps

                backfill_track_fields_stamps(conn)
            if version >= 16:
                from .migrations_v16 import repair_hub_changelog_stamps

                repair_hub_changelog_stamps(conn)
            from apps.database import regenerate_agents_md_if_writable
            regenerate_agents_md_if_writable(
                conn,
                target.parent,
                owned_tables=_schema.ALL_KNOWN_TABLES,
            )
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


def open_dry_run(path: Path | None = None) -> sqlite3.Connection:
    """A migrated, disposable scratch copy of ``path``. Never touches the file.

    A dry-run command must be able to show real numbers -- including from a
    table a newer schema version added -- without ever migrating or writing
    to the live DB (``open_rw`` migrates ON OPEN, so pointing a dry run at it
    directly would silently upgrade a real database's schema just to preview
    an operation). This copies the on-disk bytes (via the sqlite backup API,
    so WAL frames not yet checkpointed are included) into a throwaway sibling
    file next to ``path``, migrates ONLY that copy, and returns a connection
    to it. A command built against this connection sees the same tables
    ``--live`` would after migrating (e.g. a v8-only table like
    ``track_availability`` on a live v7 database), so a dry run against an
    old database no longer raises ``no such table`` merely for previewing
    what ``--live`` would do.

    The copy is a real file rather than ``:memory:`` deliberately: it is
    named as a sibling of ``path`` (same ``state/`` directory), which is what
    lets :func:`apps.shared.state.sync_stamp.data_dir_for_connection` resolve
    it to the SAME data dir -- and therefore the same machine identity -- as
    the real database, so a dry run's ``track_locations`` alt-path fallback
    (:func:`apps.shared.state.locations.list_location_paths`) sees the same
    picture ``--live`` would. ``:memory:`` has no file path at all, which
    ``sync_stamp`` refuses outright rather than guessing a machine identity.

    Deleted on interpreter exit via :mod:`atexit` rather than tied to
    ``conn.close()``: this module deliberately does not subclass
    ``sqlite3.Connection`` (see the module docstring), and a dry-run CLI
    invocation is a one-shot process, so exit-time cleanup removes the
    sibling file before the process using it ends.
    """
    target = Path(path) if path is not None else state_paths.STATE_DB
    if not target.exists():
        raise FileNotFoundError(
            f"state DB not found at {target}; run "
            f"`python -m apps.shared.state.cli init` first."
        )
    dest = target.parent / f".dry-run-{uuid.uuid4().hex}.db"
    source = sqlite3.connect(
        f"file:{target}?mode=ro", uri=True, isolation_level=None
    )
    conn = sqlite3.connect(str(dest), isolation_level=None)
    try:
        source.backup(conn)
    finally:
        source.close()
    conn.execute("PRAGMA foreign_keys = ON")
    _schema.apply_migrations(conn)
    _sync_stamp.backfill_local_machine_id(conn)
    atexit.register(_cleanup_dry_run_copy, dest)
    return conn


def _cleanup_dry_run_copy(dest: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)


@contextmanager
def connect_dry_run(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context-managed :func:`open_dry_run`. Closes on exit."""
    conn = open_dry_run(path)
    try:
        yield conn
    finally:
        conn.close()


__all__ = [
    "StateStoreBusyError",
    "connect_dry_run",
    "connect_ro",
    "connect_rw",
    "is_sqlite_busy",
    "open_dry_run",
    "open_ro",
    "open_rw",
]
