"""SQLite-backed :class:`StateBackend` implementation (Phase 5 wiring).

Reads are served from Phase 5's shared-state DB (``(private path)``)
via :func:`apps.shared.state.db.open_ro`. The webui's ``Track`` / ``Playlist``
shape is projected from the normalized Phase 5 schema:

  * flat columns (``title``, ``album``, ``duration_ms``, ``file_path``,
    ``artists_json``) come from the ``tracks`` table;
  * per-field values + provenance (``bpm``, ``key``, ``rating``, ``tags``,
    ``notes``, ``last_played_at``) come from the ``track_fields`` EAV
    table.

Writes: ``update_track`` (PATCH /tracks/{stable_id} -- rating / notes /
tags) goes through Phase 5's single supported mutation surface,
:class:`apps.shared.state.writer.StateWriter`. Each accepted patch lands
in ``track_fields`` with provenance (``source='webui'``,
``confidence=1.0``) plus a ``track.field.set`` row in ``events``, so
edits survive a daemon restart. There is NO in-memory fallback on this
path: writer/lock failures raise.

Pairings read and write the durable ``pairings`` table owned by
:mod:`apps.shared.pairings` (the rows the ``apps.pairings`` CLI and the
suggester's stage-2 rerank read), so a pairing saved in the UI survives a
restart.

Phase 5 does NOT (yet) model the triage ``queues`` (dedup / bad_beatgrid /
auto_cue). For those methods we delegate to an :class:`InMemoryBackend` companion and
log a one-shot warning per process so deployments know they are on the
fallback path. When Phase 6/7/12 ship the missing tables, individual
methods here should switch to real SQL without the fallback.

ETag derivation: the projected ``Track.updated_at`` is the LATEST of the
``tracks`` row's ``updated_at`` and every ``track_fields.modified_at``
for that track ("effective updated_at"). ``StateWriter.set_field`` does
not touch the identity row, so deriving the etag input from the field
stamps is what makes a PATCH advance the etag -- and, because it is pure
projection, the same etag is re-derived after restart.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from apps.shared.state import db as _state_db
from apps.shared.state import queries as _state_queries
from apps.shared.state import schema as _state_schema
from apps.shared.state.writer import StateWriter  # noqa: F401 -- re-export (tests patch sb_mod.StateWriter)

from .analysis_overlay import lane_owned_fields as _lane_owned_fields  # noqa: F401 -- re-export
from .backend import (
    MAX_LIMIT,
    InMemoryBackend,
    NotFoundError,
    Page,
    StateBackend,
    Track,
    TrackFilter,
    compute_library_revision_summary,
)
from .sqlite_backend_catalog import _CatalogReadsMixin
from .sqlite_backend_rows import (
    _TRACKS_PROJECTION,
    _TRACKS_PROJECTION_INCLUDE_DELETED,
    _fetch_fields,
    _matches_track_filter,
    _pairing_id,  # noqa: F401 -- re-export
    _reset_warnings_for_tests,  # noqa: F401 -- re-export
    _row_to_track,
    _warn_fallback_once,
)
from .sqlite_backend_writes import _WritesMixin

log = logging.getLogger(__name__)


class StaleStateSchemaError(RuntimeError):
    """Raised when a state.db has a ``tracks`` table below SCHEMA_VERSION.

    ``SqliteBackend`` reads exclusively through ``open_ro`` (see ``_ro``
    below), which never migrates -- only ``open_rw``'s ``apply_migrations``
    side effect does. Without this guard a db imported by an older build
    serves confusing ``sqlite3.OperationalError: no such column`` crashes
    deep inside individual query methods instead of one clear refusal at
    construction (issue #762).
    """


def read_tracks_schema_version(
    path: Path, *, busy_timeout_s: float,
) -> tuple[bool, int]:
    """Return ``(has_tracks_table, schema_meta_version)`` read fresh off disk.

    Shared by :func:`_stale_tracks_schema_version` (the boot-time construction
    guard) and the PREFLIGHT-01 state-db check (``apps.webui.server.
    preflight_checks``), which polls the SAME question live after boot rather
    than trusting whatever an already-constructed backend decided once. A db
    with no ``tracks`` table at all (not yet a Phase 5 db) reads back version
    0 alongside ``has_tracks_table=False`` so a caller can tell "nothing here
    yet" from "here, but behind".

    ``busy_timeout_s`` is required so each caller states its own lock wait:
    boot passes :data:`apps.shared.state.db.BOOT_BUSY_TIMEOUT_S`. A lock held
    past it raises ``database is locked``; it is never read as "not stale".
    """
    conn = _state_db.open_ro(path, busy_timeout_s=busy_timeout_s)
    try:
        has_tracks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
        ).fetchone() is not None
        # Name-based presence: SQLite rewrites CREATE TABLE (drops IF NOT EXISTS);
        # stored-SQL whitespace must not hide a versioned schema_meta table.
        has_meta = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone() is not None
        version = (
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_meta"
            ).fetchone()[0]
            if has_meta else 0
        )
        return has_tracks, version
    finally:
        conn.close()


def _stale_tracks_schema_version(path: Path) -> int | None:
    """Return the on-disk ``schema_meta`` version iff migration was skipped.

    ``None`` means either "not a Phase 5 db at all" (no ``tracks`` table --
    the existing per-table InMemory-fallback tests deliberately construct
    dbs like this and must keep working) or "already current". A non-None
    result means a real Phase 5 db exists but predates SCHEMA_VERSION.
    """
    has_tracks, version = read_tracks_schema_version(
        path, busy_timeout_s=_state_db.BOOT_BUSY_TIMEOUT_S,
    )
    if not has_tracks:
        return None
    return version if version < _state_schema.SCHEMA_VERSION else None


def _tracks_table_missing_schema_meta(path: Path) -> bool:
    """Return whether a foreign ``tracks`` table lacks migration metadata.

    A genuine historical state DB has ``schema_meta`` and is safe for the
    migration ladder. Without it, migration starts at v0 and ``CREATE TABLE
    IF NOT EXISTS tracks`` retains an incompatible pre-existing table, so
    later v0 statements leak a low-level missing-column error.

    Boot-path only, so it waits :data:`apps.shared.state.db.BOOT_BUSY_TIMEOUT_S`
    for a peer boot's lock (see that constant) and raises past it.
    """
    conn = _state_db.open_ro(path, busy_timeout_s=_state_db.BOOT_BUSY_TIMEOUT_S)
    try:
        has_tracks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracks'"
        ).fetchone() is not None
        # Name-based presence: SQLite rewrites CREATE TABLE (drops IF NOT EXISTS);
        # stored-SQL whitespace must not hide a versioned schema_meta table.
        has_meta = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone() is not None
        return has_tracks and not has_meta
    finally:
        conn.close()




class SqliteBackend(_CatalogReadsMixin, _WritesMixin):
    """StateBackend backed by Phase 5's ``state.db``.

    Reads project the normalized schema; ``update_track`` writes through
    :class:`apps.shared.state.writer.StateWriter` (see module docstring).
    The constructor takes the path to ``state.db`` and a companion
    :class:`InMemoryBackend` used only for entities Phase 5 does not yet
    model (the triage queues).

    The companion can be pre-seeded in tests to exercise fallback paths.
    """

    def __init__(
        self,
        state_db_path: str | Path,
        *,
        fallback: InMemoryBackend | None = None,
    ) -> None:
        self._path = Path(state_db_path)
        # Skip the check for a path that does not exist yet (or whose parent
        # dir does not exist): callers may legitimately construct a
        # SqliteBackend before the db is created, deferring "not there" to
        # the normal open_ro-raises-FileNotFoundError path at query time.
        stale = (
            _stale_tracks_schema_version(self._path)
            if self._path.is_file() else None
        )
        if stale is not None:
            raise StaleStateSchemaError(
                f"{self._path} has a tracks table but schema_meta reports "
                f"version {stale}, below SCHEMA_VERSION "
                f"{_state_schema.SCHEMA_VERSION}. Migration was skipped "
                "somewhere in the boot path; refusing to serve a mismatched "
                "schema. Run `python -m apps.shared.state.cli init` (or "
                "apps.shared.state.schema.apply_migrations) on this DB "
                "before booting."
            )
        self._fallback = fallback if fallback is not None else InMemoryBackend()
        # Serialise the read-CAS-write sequence in update_track. Without
        # this lock two in-process callers could both pass the etag CAS
        # against the same snapshot and both commit (lost-update on the
        # second write's history ordering). Adversarial R4 finding kept
        # from the fallback era; cross-process CAS is still last-write-wins
        # (single-daemon deployment assumption; cloud lock gates peers).
        self._write_lock = threading.RLock()
        self._sqlite_last_writer: tuple[str, str] | None = None

    # --- connection helper ------------------------------------------------
    @contextmanager
    def _ro(self) -> Iterator[sqlite3.Connection]:
        conn = _state_db.open_ro(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _table_exists(self, conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    @property
    def writeback_state_db_path(self) -> Path:
        """Return the exact state database protected by the writeback lock."""
        return self._path

    @contextmanager
    def hold_writeback_source_lock(self) -> Iterator[None]:
        """Exclude source edits and vendor-ID remaps during a writeback apply.

        Live writeback always acquires this shared-state lock before its
        vendor-target lock. Keeping the transaction open through the vendor
        mutation makes the source membership and native-ID mapping snapshot
        authoritative instead of merely advisory.
        """
        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    # --- reads ------------------------------------------------------------
    def list_tracks(self, flt: TrackFilter) -> Page:
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("list_tracks", "no tracks table")
                return self._fallback.list_tracks(flt)
            limit = max(1, min(flt.limit, MAX_LIMIT))
            projection = (
                _TRACKS_PROJECTION_INCLUDE_DELETED
                if flt.show_deleted
                else _TRACKS_PROJECTION
            )
            page: list[Track] = []
            scan_cursor = flt.cursor
            while len(page) < limit:
                cursor_predicate = ""
                params: tuple[str | int, ...] = (limit,)
                if scan_cursor is not None:
                    cursor_predicate = " AND stable_id > ?"
                    params = (scan_cursor, limit)
                rows = list(conn.execute(
                    projection + cursor_predicate
                    + " ORDER BY stable_id LIMIT ?",
                    params,
                ))
                if not rows:
                    break
                fields_map = _fetch_fields(
                    conn, [row["stable_id"] for row in rows],
                )
                for row in rows:
                    track = _row_to_track(
                        row, fields_map.get(row["stable_id"], {}),
                    )
                    if _matches_track_filter(track, flt):
                        page.append(track)
                        if len(page) == limit:
                            break
                if len(page) == limit or len(rows) < limit:
                    break
                scan_cursor = rows[-1]["stable_id"]
            next_cursor = page[-1].stable_id if len(page) == limit else None
            return Page(items=page, next_cursor=next_cursor)

    def library_revision(self) -> str:
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("library_revision", "no tracks table")
                return self._fallback.library_revision()
            track_summary = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), '') "
                "FROM tracks WHERE deleted_at IS NULL"
            ).fetchone()
            field_summary = conn.execute(
                "SELECT COALESCE(MAX(modified_at), '') FROM track_fields "
                "WHERE deleted_at IS NULL"
            ).fetchone()
            changelog_summary = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM local_changelog "
                "WHERE table_name IN ('tracks', 'track_fields')"
            ).fetchone()
            edit_summary = (
                conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM track_edit "
                    "WHERE deleted_at IS NULL"
                ).fetchone()
                if self._table_exists(conn, "track_edit")
                else (0, "")
            )
        return compute_library_revision_summary(
            int(track_summary[0]),
            str(track_summary[1]),
            str(field_summary[0]),
            int(changelog_summary[0]),
            int(edit_summary[0]),
            str(edit_summary[1]),
        )

    def get_track(self, stable_id: str) -> Track:
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("get_track", "no tracks table")
                return self._fallback.get_track(stable_id)
            row = conn.execute(
                "SELECT stable_id, title, artists_json, album, duration_ms, "
                "       file_path, created_at, updated_at "
                "FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"track not found: {stable_id}")
            fields_map = _fetch_fields(conn, [stable_id])
        return _row_to_track(row, fields_map.get(stable_id, {}))

    def get_tracks_bulk(self, stable_ids: Sequence[str], *, live_only: bool = False) -> dict[str, Track]:
        """Chunked bulk fetch; missing ids are absent from the result.

        ``live_only`` leaves tombstoned tracks (``deleted_at`` set) out too, as
        the library listings do.

        One IN(...) pass over ``tracks`` plus the shared ``_fetch_fields``
        EAV pass -- powers playlist-row hydration without a per-member
        query fan-out.
        """
        ids = list(dict.fromkeys(stable_ids))
        if not ids:
            return {}
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("get_tracks_bulk", "no tracks table")
                return self._fallback.get_tracks_bulk(ids, live_only=live_only)
            rows = _state_queries.bulk_select_by_stable_id(
                conn, "tracks",
                "stable_id, title, artists_json, album, duration_ms, "
                "file_path, created_at, updated_at",
                ids,
                live_only=live_only,
            )
            fields_map = _fetch_fields(conn, [r["stable_id"] for r in rows])
        return {
            r["stable_id"]: _row_to_track(r, fields_map.get(r["stable_id"], {}))
            for r in rows
        }

    def get_file_paths_bulk(self, stable_ids: Sequence[str]) -> dict[str, str | None]:
        """``file_path`` only, skipping the ``_fetch_fields`` EAV pass -- see
        ``routes/playlists.py`` for why (pin e0f3a90652a9)."""
        ids = list(dict.fromkeys(stable_ids))
        if not ids:
            return {}
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("get_file_paths_bulk", "no tracks table")
                return self._fallback.get_file_paths_bulk(ids)
            rows = _state_queries.bulk_select_by_stable_id(
                conn, "tracks", "stable_id, file_path", ids,
            )
        return {r["stable_id"]: r["file_path"] for r in rows}

    def list_file_paths(self) -> dict[str, str | None]:
        """Every live track's ``file_path`` in one query, for the Explorer folder tree (LIBUX-9747)."""
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("list_file_paths", "no tracks table")
                return self._fallback.list_file_paths()
            rows = conn.execute("SELECT stable_id, file_path FROM tracks WHERE deleted_at IS NULL")
            return {r["stable_id"]: r["file_path"] for r in rows}



def _migrate_before_serving(target: Path) -> None:
    """Bring an existing state.db to SCHEMA_VERSION before boot serves it.

    Every ``SqliteBackend`` read goes through ``open_ro`` (see ``_ro``
    above), which never migrates -- only ``open_rw``'s ``apply_migrations``
    side effect does. Without this call, an install carrying a state.db from
    an older build (schema_meta below SCHEMA_VERSION) serves 500s on every
    query touching a column a later migration added, until some unrelated
    write path happens to ``open_rw`` it first (issue #762).

    Raise, don't swallow: a migration failure must stop the boot loudly, not
    fall back to InMemoryBackend and quietly serve an empty library in place
    of a real one.
    """
    try:
        _state_db.open_rw(
            target, busy_timeout_s=_state_db.BOOT_BUSY_TIMEOUT_S,
        ).close()
    except Exception:
        log.error(
            "state DB migration failed for %s; refusing to boot against a "
            "mismatched schema",
            target,
        )
        raise


def make_backend(
    state_db_path: str | Path | None = None,
) -> StateBackend:
    """Return the right backend based on whether ``state.db`` exists.

    If ``state_db_path`` resolves to an existing file, migrates it to
    SCHEMA_VERSION (see :func:`_migrate_before_serving`) and returns a
    :class:`SqliteBackend`; otherwise returns an :class:`InMemoryBackend`
    (matches the contract of the webui's default factory).
    """
    if state_db_path is None:
        # Late import so tests don't need the shared paths module wired up.
        from apps.shared.paths import STATE_DB
        target = STATE_DB
    else:
        target = Path(state_db_path)
    if Path(target).is_file():
        if _tracks_table_missing_schema_meta(target):
            raise StaleStateSchemaError(
                f"{target} has a tracks table but schema_meta reports "
                f"version 0, below SCHEMA_VERSION "
                f"{_state_schema.SCHEMA_VERSION}. Refusing to migrate an "
                "unversioned tracks table. Migration was skipped somewhere "
                "in the boot path; refusing to serve a mismatched schema. "
                "Run `python -m "
                "apps.shared.state.cli init` (or "
                "apps.shared.state.schema.apply_migrations) on this DB "
                "before booting."
            )
        _migrate_before_serving(target)
        log.info("webui backend: using SqliteBackend at %s", target)
        return SqliteBackend(target)
    log.info(
        "webui backend: %s not found; using InMemoryBackend", target,
    )
    return InMemoryBackend()


__all__ = [
    "SqliteBackend", "StaleStateSchemaError", "make_backend",
    "read_tracks_schema_version",
]
