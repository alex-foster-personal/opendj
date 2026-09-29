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
import math
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
# REFERENCES clauses. busy_timeout is set per handle from the caller's
# ``busy_timeout_s`` (see :func:`open_rw`).
_RW_PRAGMAS: tuple[tuple[str, object], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
)

# Default busy wait for request-time handles: rides out a few hundred ms of
# lock contention without the caller seeing sqlite3.OperationalError, and is
# the ceiling after which a contended API write surfaces as STATE_STORE_BUSY.
# It equals Python's own ``sqlite3.connect`` default, stated here so no handle
# relies on that implicit value.
DEFAULT_BUSY_TIMEOUT_S: float = 5.0

# Busy wait for the one-shot BOOT path (``make_backend``'s read-only schema
# pre-checks and its migrate-on-open). In WAL mode a reader is blocked only
# while a peer holds the database file EXCLUSIVE (the last connection to close
# checkpoints and deletes -wal/-shm under it) or rebuilds the wal-index
# (recovery). SQLite's busy handler waits out both -- measured: SQLITE_BUSY
# and SQLITE_BUSY_RECOVERY raise at the timeout, not before -- and both take
# milliseconds on an idle host. Under a loaded CI runner (load ~24, fast lane
# shard 4 on nucbox-wsl-16, Sat 26 Sep 2026) a peer boot held one past the
# implicit 5 s default and a pre-check raised ``database is locked``. Boot
# runs once, so the longer ceiling costs nothing on the happy path; past it
# the error still propagates and the boot fails loud.
BOOT_BUSY_TIMEOUT_S: float = 30.0


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


def _checked_busy_timeout_s(busy_timeout_s: float) -> float:
    """Return ``busy_timeout_s`` if it is a finite positive wait; raise otherwise.

    Every handle's lock wait must be BOUNDED: an infinite or non-positive
    value is a configuration bug, never a request for "wait forever" or
    "fail instantly".
    """
    if not math.isfinite(busy_timeout_s) or busy_timeout_s <= 0:
        raise ValueError(
            "busy_timeout_s must be a finite number of seconds above zero, "
            f"got {busy_timeout_s!r}"
        )
    return busy_timeout_s


def _apply_rw_pragmas(conn: sqlite3.Connection, busy_timeout_s: float) -> None:
    for key, value in _RW_PRAGMAS:
        conn.execute(f"PRAGMA {key} = {value}")
    conn.execute(f"PRAGMA busy_timeout = {round(busy_timeout_s * 1000)}")


def open_rw(
    path: Path | None = None,
    *,
    apply_schema: bool = True,
    check_same_thread: bool = True,
    busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
) -> sqlite3.Connection:
    """Open ``path`` read-write; optionally apply migrations.

    ``busy_timeout_s`` bounds how long any statement on this handle waits for
    a lock held by another connection (:data:`BOOT_BUSY_TIMEOUT_S` on the boot
    path); past it SQLite raises ``database is locked``.

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

    :func:`apps.shared.state.schema.apply_migrations` runs the v15
    ``track_fields`` stamp backfill when schema is at least v15 (issue #3136).
    A durable ``schema_meta_markers`` row (v16, issue #3165) records
    completion so repeat opens issue only a constant-time marker check.

    At v17 it also runs
    :func:`apps.shared.state.migrations_v17.repair_hub_changelog_stamps`
    (issue #3171): on a hub, latest ``hub_changelog`` rows whose stamp
    disagrees with the live domain row are re-offered at a fresh sequence.
    Candidate discovery runs before ``BEGIN IMMEDIATE`` and a marker makes
    later opens constant-time.

    After migrations and machine-id backfill,
    :func:`apps.shared.state.agents_md_cache.regenerate_agents_md_cached`
    regenerates ``<state_dir>/AGENTS.md`` when the state directory is
    writable and the sidecar is not already current for this schema; a docs
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
    busy_timeout_s = _checked_busy_timeout_s(busy_timeout_s)
    _ensure_parent(target)
    conn = sqlite3.connect(
        str(target),
        timeout=busy_timeout_s,
        isolation_level=None,
        check_same_thread=check_same_thread,
    )
    try:
        _apply_rw_pragmas(conn, busy_timeout_s)
        if apply_schema:
            _schema.apply_migrations(conn)
            _sync_stamp.backfill_local_machine_id(conn)
            from apps.shared.state.agents_md_cache import regenerate_agents_md_cached

            regenerate_agents_md_cached(
                conn,
                target.parent,
                owned_tables=_schema.ALL_KNOWN_TABLES,
            )
    except Exception:
        conn.close()
        raise
    return conn


def open_ro(
    path: Path | None = None,
    *,
    busy_timeout_s: float = DEFAULT_BUSY_TIMEOUT_S,
) -> sqlite3.Connection:
    """Open ``path`` read-only with a query_only guard.

    Uses the SQLite URI form ``mode=ro`` so write attempts raise
    :class:`sqlite3.OperationalError` at execute time. ``busy_timeout_s``
    bounds the wait for a peer's lock, as in :func:`open_rw`.
    """
    target = Path(path) if path is not None else state_paths.STATE_DB
    busy_timeout_s = _checked_busy_timeout_s(busy_timeout_s)
    if not target.exists():
        raise FileNotFoundError(
            f"state DB not found at {target}; run "
            f"`python -m apps.shared.state.cli init` first."
        )
    uri = f"file:{target}?mode=ro"
    conn = sqlite3.connect(
        uri, uri=True, isolation_level=None, timeout=busy_timeout_s,
    )
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
