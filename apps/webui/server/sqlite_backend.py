"""SQLite-backed :class:`StateBackend` implementation (Phase 5 wiring).

Reads are served from Phase 5's shared-state DB (``data/state/state.db``)
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

Phase 5 does NOT (yet) model:

  * the webui ``pairings`` entity,
  * the triage ``queues`` (dedup / bad_beatgrid / auto_cue).

For those methods we delegate to an :class:`InMemoryBackend` companion and
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

import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.analysis.selection import EffectiveField
from apps.shared.state import db as _state_db
from apps.shared.state import queries as _state_queries
from apps.shared.state import schema as _state_schema
from apps.shared.state.writer import StateWriter

from .analysis_overlay import lane_owned_fields as _lane_owned_fields
from .analysis_overlay import selection_tag as _selection_tag
from .backend import (
    MAX_LIMIT,
    BackendError,
    BatchConflictError,
    ConflictError,
    InMemoryBackend,
    MyTagMergeConfirmationRequiredError,
    MyTagScopeConflictError,
    NotFoundError,
    Page,
    Pairing,
    Playlist,
    PlaylistPage,
    Provenance,
    QueueItem,
    QueueKind,
    Source,
    StateBackend,
    Track,
    TrackFilter,
    TrackPlaylistHit,
    TrackUpdate,
    compute_library_revision_summary,
    compute_mytag_catalog_revision,
    resolve_tempo_pref_write,
)
from .etag import compute_etag, strip_quotes
from .playlist_page import playlist_from_header, read_playlist_header, read_playlist_page

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
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
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
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone() is not None
        return has_tracks and not has_meta
    finally:
        conn.close()


# Track which fallback warnings we've already emitted, keyed by method name.
# Per-process singleton so the logs stay quiet after the first call.
_WARNED_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _warn_fallback_once(method: str, reason: str) -> None:
    """Emit a WARNING the first time ``method`` falls back; no-op thereafter."""
    with _WARNED_LOCK:
        if method in _WARNED:
            return
        _WARNED.add(method)
    log.warning(
        "SqliteBackend.%s: falling back to InMemoryBackend (%s)",
        method, reason,
    )


def _reset_warnings_for_tests() -> None:
    """Test hook: clear the once-per-process warning cache."""
    with _WARNED_LOCK:
        _WARNED.clear()


# --- field projection ----------------------------------------------------

# Fields the webui Track exposes that live in track_fields (EAV). Any field
# name listed here is JSON-decoded on the way out.
_EAV_FIELDS: tuple[str, ...] = (
    "bpm", "key", "rating", "tags", "notes", "last_played_at", "genre", "comments",
    "energy", "tempo_pref",
)

_TRACKS_PROJECTION = (
    "SELECT stable_id, title, artists_json, album, "
    "       duration_ms, file_path, created_at, updated_at "
    "FROM tracks WHERE deleted_at IS NULL"
)

_TRACKS_PROJECTION_INCLUDE_DELETED = (
    "SELECT stable_id, title, artists_json, album, "
    "       duration_ms, file_path, created_at, updated_at "
    "FROM tracks"
)


def _parse_rfc3339(ts: str) -> datetime:
    """Parse an RFC 3339 timestamp (``Z`` or ``+00:00`` form). Fail fast.

    Naive timestamps are treated as UTC (legacy fixture rows). Anything
    unparseable raises ``ValueError`` -- no silent clock guessing.
    """
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _effective_updated_at(
    row_updated_at: str,
    fields: dict[str, EffectiveField],
) -> str:
    """Latest of the row ``updated_at`` and every field ``modified_at``.

    This is the etag input (see module docstring): field-only writes via
    ``StateWriter.set_field`` must advance the etag even though they never
    touch the ``tracks`` identity row. Returns the original string of the
    winning stamp so the derivation is byte-stable across restarts.

    A field with an EMPTY stamp is skipped: an own lane with no analysis
    record yet reports ``status: missing`` and no time, and there is no
    stamp to compare. Parsing "" here would raise on every unanalyzed
    track the moment a lane is switched to own.
    """
    best = row_updated_at
    best_dt = _parse_rfc3339(row_updated_at)
    for view in fields.values():
        if not view.modified_at:
            continue
        dt = _parse_rfc3339(view.modified_at)
        if dt > best_dt:
            best, best_dt = view.modified_at, dt
    return best


def _row_to_track(
    row: sqlite3.Row,
    fields: dict[str, EffectiveField],
) -> Track:
    """Project a ``tracks`` row + its ``track_fields`` entries into ``Track``.

    ``fields`` maps ``field_name -> (value, source, confidence, modified_at)``.
    """
    artist: str | None = None
    artists_json = row["artists_json"]
    if artists_json:
        try:
            decoded = json.loads(artists_json)
            if isinstance(decoded, list) and decoded:
                # Join multi-artist values with ", " for the flat facade.
                artist = ", ".join(str(a) for a in decoded if a)
            elif isinstance(decoded, str):
                artist = decoded
        except (json.JSONDecodeError, TypeError):
            artist = None

    def _val(name: str) -> Any:
        view = fields.get(name)
        return view.value if view is not None else None

    bpm = _val("bpm")
    key = _val("key")
    rating = _val("rating")
    tags_val = _val("tags")
    notes = _val("notes")
    genre = _val("genre")
    comments = _val("comments")
    last_played_at = _val("last_played_at")
    tempo_pref_val = _val("tempo_pref")

    if isinstance(bpm, (int, float)):
        bpm = float(bpm)
    else:
        bpm = None
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            rating = None
    if isinstance(tags_val, list):
        tags = [str(t) for t in tags_val]
    else:
        tags = []
    if notes is not None and not isinstance(notes, str):
        notes = str(notes)
    if genre is not None and not isinstance(genre, str):
        genre = str(genre)
    if comments is not None and not isinstance(comments, str):
        comments = str(comments)
    if last_played_at is not None and not isinstance(last_played_at, str):
        last_played_at = str(last_played_at)
    if key is not None and not isinstance(key, str):
        key = str(key)
    if isinstance(tempo_pref_val, dict):
        tempo_pref: dict[str, float | None] | None = {
            "regular": tempo_pref_val.get("regular"),
            "min": tempo_pref_val.get("min"),
            "max": tempo_pref_val.get("max"),
        }
    else:
        tempo_pref = None

    provenance: dict[str, Provenance] = {}
    for fname, view in fields.items():
        provenance[fname] = Provenance(
            value=view.value, source=view.source, confidence=view.confidence,
            modified_at=view.modified_at, status=view.status, reason=view.reason,
        )

    return Track(
        stable_id=row["stable_id"],
        title=row["title"],
        artist=artist,
        album=row["album"],
        duration_ms=row["duration_ms"],
        bpm=bpm,
        key=key,
        rating=rating,
        tags=tags,
        notes=notes,
        genre=genre,
        comments=comments,
        last_played_at=last_played_at,
        tempo_pref=tempo_pref,
        file_path=row["file_path"],
        created_at=row["created_at"],
        updated_at=_effective_updated_at(row["updated_at"], fields),
        selection_tag=_selection_tag(fields),
        provenance=provenance,
    )


def _fetch_fields(
    conn: sqlite3.Connection, stable_ids: list[str],
) -> dict[str, dict[str, EffectiveField]]:
    """Fetch the effective projected fields for ``stable_ids``.

    Returns ``{stable_id: {field_name: EffectiveField}}``.

    Two sources, one function. The ordinary EAV fields
    (:data:`_EAV_FIELDS`) come from ``track_fields`` as they always have.
    The LANE-OWNED fields (``bpm``, ``key``, ``loudness_lufs``,
    ``loudness_dbtp``, ``key_change_count``, ``tempo_change_count``) come
    from :func:`apps.analysis.selection.effective_fields`, which reads
    ``track_fields`` for a lane on rbx and ``analysis_projection`` for a
    lane on own. Its answer OVERRIDES the EAV pass, so switching a lane to
    own cannot leave the rekordbox value showing, and an own lane with no
    record shows ``status: missing`` rather than silently falling back --
    which is the substitution native-analysis v1 exists to remove.

    Nothing here writes. An own value never reaches ``track_fields`` or
    ``track_field_history``.
    """
    if not stable_ids:
        return {}
    out: dict[str, dict[str, EffectiveField]] = {sid: {} for sid in stable_ids}
    # Use chunked IN (...) to avoid SQLite's default 999 variable cap.
    chunk = 500
    eav_placeholders = ",".join("?" * len(_EAV_FIELDS))
    for i in range(0, len(stable_ids), chunk):
        sub = stable_ids[i : i + chunk]
        placeholders = ",".join("?" * len(sub))
        sql = (
            f"SELECT stable_id, field_name, value_json, source, confidence, "
            f"       modified_at "
            f"FROM track_fields "
            f"WHERE stable_id IN ({placeholders}) "
            f"  AND field_name IN ({eav_placeholders}) "
            f"  AND deleted_at IS NULL"
        )
        for row in conn.execute(sql, (*sub, *_EAV_FIELDS)):
            sid = row["stable_id"]
            fname = row["field_name"]
            try:
                value = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = row["value_json"]
            out[sid][fname] = EffectiveField(
                value=value, source=row["source"], confidence=row["confidence"],
                modified_at=row["modified_at"], status="ok", reason=None,
            )

    for sid, lane_fields in _lane_owned_fields(conn, stable_ids).items():
        out[sid].update(lane_fields)
    return out


def _matches_track_filter(track: Track, flt: TrackFilter) -> bool:
    """Keep list-track filtering semantics independent of SQLite collation."""
    if flt.q:
        needle = flt.q.casefold()
        if not (
            (track.title and needle in track.title.casefold())
            or (track.artist and needle in track.artist.casefold())
        ):
            return False
    if flt.bpm_min is not None and not (
        track.bpm is not None and track.bpm >= flt.bpm_min
    ):
        return False
    if flt.bpm_max is not None and not (
        track.bpm is not None and track.bpm <= flt.bpm_max
    ):
        return False
    if flt.key and track.key != flt.key:
        return False
    if flt.rating_min is not None and not (
        track.rating is not None and track.rating >= flt.rating_min
    ):
        return False
    return not flt.tag or flt.tag in (track.tags or [])


def _field_writes(current: Track, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a patch and resolve tag deltas against the current track."""
    writes: dict[str, Any] = {}
    if "rating" in patch:
        rating = patch["rating"]
        if rating is not None and not (0 <= rating <= 5):
            raise BackendError("rating must be between 0 and 5")
        writes["rating"] = rating
    if "notes" in patch:
        writes["notes"] = patch["notes"]
    if "genre" in patch:
        writes["genre"] = patch["genre"]
    if "comments" in patch:
        writes["comments"] = patch["comments"]
    if "tempo_pref" in patch:
        writes["tempo_pref"] = resolve_tempo_pref_write(patch["tempo_pref"])
    if "tags_add" in patch or "tags_remove" in patch:
        tags = list(current.tags or [])
        for tag in patch.get("tags_add") or []:
            if tag and tag not in tags:
                tags.append(tag)
        if patch.get("tags_remove"):
            tags = [tag for tag in tags if tag not in patch["tags_remove"]]
        writes["tags"] = tags
    return writes


class SqliteBackend:
    """StateBackend backed by Phase 5's ``state.db``.

    Reads project the normalized schema; ``update_track`` writes through
    :class:`apps.shared.state.writer.StateWriter` (see module docstring).
    The constructor takes the path to ``state.db`` and a companion
    :class:`InMemoryBackend` used only for entities Phase 5 does not yet
    model (``pairings``, queues).

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
        return compute_library_revision_summary(
            int(track_summary[0]),
            str(track_summary[1]),
            str(field_summary[0]),
            int(changelog_summary[0]),
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

    def get_tracks_bulk(self, stable_ids: Sequence[str]) -> dict[str, Track]:
        """Chunked bulk fetch; missing ids are absent from the result.

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
                return self._fallback.get_tracks_bulk(ids)
            rows = _state_queries.bulk_select_by_stable_id(
                conn, "tracks",
                "stable_id, title, artists_json, album, duration_ms, "
                "file_path, created_at, updated_at",
                ids,
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

    def list_playlists(self) -> list[Playlist]:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("list_playlists", "no playlists table")
                return self._fallback.list_playlists()
            rows = list(
                conn.execute(
                    "SELECT playlist_id, name, vendor, vendor_pl_id, "
                    "       created_at, updated_at, forbid_duplicates "
                    "FROM playlists WHERE deleted_at IS NULL ORDER BY playlist_id"
                )
            )
            if not self._table_exists(conn, "playlist_memberships"):
                members: dict[str, list[str]] = {}
            else:
                members = {}
                for pid, sid in conn.execute(
                    "SELECT playlist_id, stable_id FROM playlist_memberships "
                    "WHERE deleted_at IS NULL "
                    "ORDER BY playlist_id, "
                    "COALESCE(order_key, printf('%08d', position)), position"
                ):
                    members.setdefault(pid, []).append(sid)
        return [
            Playlist(
                playlist_id=r["playlist_id"], name=r["name"],
                vendor=r["vendor"], vendor_pl_id=r["vendor_pl_id"],
                items=members.get(r["playlist_id"], []),
                created_at=r["created_at"], updated_at=r["updated_at"],
                forbid_duplicates=bool(r["forbid_duplicates"]),
            )
            for r in rows
        ]

    def get_playlist(self, playlist_id: str) -> Playlist:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("get_playlist", "no playlists table")
                return self._fallback.get_playlist(playlist_id)
            row = read_playlist_header(conn, playlist_id)
            member_rows = []
            if self._table_exists(conn, "playlist_memberships"):
                member_rows = conn.execute(
                    "SELECT stable_id, item_id FROM playlist_memberships "
                    "WHERE playlist_id = ? AND deleted_at IS NULL "
                    "ORDER BY COALESCE(order_key, printf('%08d', position)), "
                    "position",
                    (playlist_id,),
                ).fetchall()
        return playlist_from_header(
            row, [r[0] for r in member_rows], [r[1] or "" for r in member_rows],
        )

    def get_playlist_page(
        self, playlist_id: str, *, limit: int, offset: int,
    ) -> PlaylistPage:
        """One ordered window of the live membership (LIBM-133, playlist_page.py)."""
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("get_playlist_page", "no playlists table")
                return self._fallback.get_playlist_page(
                    playlist_id, limit=limit, offset=offset,
                )
            return read_playlist_page(conn, playlist_id, limit=limit, offset=offset)

    def list_track_playlists(self, stable_id: str) -> list[TrackPlaylistHit]:
        with self._ro() as conn:
            if (
                not self._table_exists(conn, "playlists")
                or not self._table_exists(conn, "playlist_memberships")
            ):
                return []
            rows = list(conn.execute(
                "SELECT p.playlist_id, p.name, p.vendor, m.position "
                "FROM playlist_memberships m "
                "JOIN playlists p ON p.playlist_id = m.playlist_id "
                "WHERE m.stable_id = ? "
                "  AND m.deleted_at IS NULL "
                "  AND p.deleted_at IS NULL "
                "ORDER BY p.name COLLATE NOCASE, p.playlist_id, m.position",
                (stable_id,),
            ))
        hits: list[TrackPlaylistHit] = []
        current: TrackPlaylistHit | None = None
        for row in rows:
            pid = row["playlist_id"]
            if current is not None and current.playlist_id == pid:
                current.positions.append(row["position"])
                continue
            current = TrackPlaylistHit(
                playlist_id=pid,
                name=row["name"],
                vendor=row["vendor"],
                positions=[row["position"]],
            )
            hits.append(current)
        return hits

    def list_pairings(
        self, *, from_stable_id: str | None = None,
        to_stable_id: str | None = None, source: str | None = None,
    ) -> list[Pairing]:
        # Phase 5 does not ship a pairings table yet.
        _warn_fallback_once(
            "list_pairings", "no pairings table in Phase 5 state.db",
        )
        return self._fallback.list_pairings(
            from_stable_id=from_stable_id,
            to_stable_id=to_stable_id, source=source,
        )

    def get_queue(
        self, kind: QueueKind,
    ) -> tuple[list[QueueItem], str | None]:
        # Triage queues come from Phase 6/7/12; not in state.db yet.
        _warn_fallback_once(
            "get_queue", "triage queues not in Phase 5 state.db",
        )
        return self._fallback.get_queue(kind)

    def stats(self) -> dict[str, Any]:
        with self._ro() as conn:
            tracks = 0
            playlists = 0
            if self._table_exists(conn, "tracks"):
                tracks = conn.execute(
                    "SELECT COUNT(*) FROM tracks"
                ).fetchone()[0]
            else:
                _warn_fallback_once("stats:tracks", "no tracks table")
            if self._table_exists(conn, "playlists"):
                playlists = conn.execute(
                    "SELECT COUNT(*) FROM playlists WHERE deleted_at IS NULL"
                ).fetchone()[0]
            else:
                _warn_fallback_once("stats:playlists", "no playlists table")
        # pairings always from fallback (no Phase 5 table).
        fb = self._fallback.stats()
        return {
            "tracks": tracks, "playlists": playlists,
            "pairings": fb.get("pairings", 0),
        }

    # --- writes -----------------------------------------------------------
    def update_track(
        self, stable_id: str, patch: dict[str, Any], *,
        expected_etag: str, source: Source = "webui",
        mutation_guard: Callable[[], None] | None = None,
    ) -> Track:
        """Persist a rating / notes / tags patch through ``StateWriter``.

        Contract (ratings-persistence-restart):

          * CAS: ``expected_etag`` must match the etag derived from the
            current effective ``updated_at`` or ``ConflictError`` raises.
          * Each changed field lands in ``track_fields`` with provenance
            ``source=<source>`` (webui for UI PATCHes), ``confidence=1.0``
            and one ``track.field.set`` event row -- via the Phase 5
            single-writer chokepoint, never direct SQL.
          * No in-memory fallback: a locked DB / writer conflict raises
            ``sqlite3.OperationalError`` after the 5s busy_timeout.
          * A fresh backend instance (daemon restart) re-reads the same
            values and re-derives the same etag.
        """
        try:
            return self.update_tracks(
                [TrackUpdate(stable_id, patch, expected_etag)], source=source,
                mutation_guard=mutation_guard,
            )[0]
        except BatchConflictError as exc:
            current = self.get_track(stable_id)
            raise ConflictError(current.to_dict(), exc.conflicts[0]["current_etag"]) from exc

    def update_tracks(
        self, updates: Sequence[TrackUpdate], *, source: Source = "webui",
        mutation_guard: Callable[[], None] | None = None,
    ) -> list[Track]:
        """Atomically compare-and-swap and persist every requested update.

        The complete ETag preflight and all mutations happen inside one
        SQLite writer transaction. ``StateWriter`` uses nested SAVEPOINTs,
        which compose with this outer transaction and therefore cannot leave
        earlier rows committed when a later write fails.
        """
        if not updates:
            return []
        stable_ids = [update.stable_id for update in updates]
        if len(stable_ids) != len(set(stable_ids)):
            raise BackendError("batch contains duplicate stable_ids")

        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                current_rows: list[Track] = []
                conflicts: list[dict[str, str]] = []
                for update in updates:
                    row = conn.execute(
                        "SELECT stable_id, stable_id_tier, title, artists_json, album, isrc, "
                        "duration_ms, file_path, content_hash, created_at, updated_at "
                        "FROM tracks WHERE stable_id = ?",
                        (update.stable_id,),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError(f"track not found: {update.stable_id}")
                    fields = _fetch_fields(conn, [update.stable_id])
                    current = _row_to_track(row, fields.get(update.stable_id, {}))
                    current_rows.append(current)
                    current_etag = compute_etag(current.stable_id, current.updated_at, current.selection_tag)
                    if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                        conflicts.append({
                            "stable_id": update.stable_id,
                            "current_etag": current_etag,
                        })
                if conflicts:
                    raise BatchConflictError(conflicts)

                writes = [
                    _field_writes(current, update.patch)
                    for current, update in zip(current_rows, updates, strict=False)
                ]
                file_paths: list[str | None] = []
                for update in updates:
                    if "file_path" not in update.patch:
                        file_paths.append(None)
                        continue
                    file_path = update.patch["file_path"]
                    if not isinstance(file_path, str) or not file_path:
                        raise BackendError("file_path must be a non-empty string")
                    file_paths.append(file_path)
                now = datetime.now(UTC).isoformat()
                if mutation_guard is not None:
                    mutation_guard()
                with StateWriter(conn, actor="webui") as writer:
                    for current, update, field_writes, file_path in zip(
                        current_rows, updates, writes, file_paths, strict=False,
                    ):
                        for field_name, value in field_writes.items():
                            writer.set_field(
                                update.stable_id, field_name, value,
                                source=source, modified_at=now, confidence=1.0,
                            )
                        if file_path is not None:
                            row = conn.execute(
                                "SELECT stable_id_tier, title, artists_json, album, isrc, "
                                "duration_ms, content_hash FROM tracks "
                                "WHERE stable_id = ? AND deleted_at IS NULL",
                                (current.stable_id,),
                            ).fetchone()
                            if row is None:
                                raise NotFoundError(
                                    f"track not found: {current.stable_id}"
                                )
                            writer.upsert_track(
                                stable_id=current.stable_id,
                                stable_id_tier=row["stable_id_tier"],
                                title=row["title"],
                                artists=(
                                    json.loads(row["artists_json"])
                                    if row["artists_json"]
                                    else []
                                ),
                                album=row["album"],
                                isrc=row["isrc"],
                                duration_ms=row["duration_ms"],
                                file_path=file_path,
                                content_hash=row["content_hash"],
                            )
                if mutation_guard is not None:
                    mutation_guard()
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

            self._sqlite_last_writer = (source, now)
            return [self.get_track(update.stable_id) for update in updates]

    def update_tag_members(
        self, old_name: str, new_name: str | None, *,
        expected_catalog_revision: str, expected_track_count: int,
        confirm_merge: bool = False, source: Source = "webui",
    ) -> int:
        """Atomically rename or delete every current member of a tag.

        Discovery occurs only after ``BEGIN IMMEDIATE`` has acquired the
        SQLite writer lock. The discovered ETags are then prechecked before
        any field write, so a tag added between an HTTP request arriving and
        this transaction acquiring the lock is part of the same sweep.
        """
        with self._write_lock:
            conn = _state_db.open_rw(self._path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = list(conn.execute(
                    "SELECT stable_id, title, artists_json, album, duration_ms, "
                    "file_path, created_at, updated_at FROM tracks "
                    "WHERE deleted_at IS NULL ORDER BY stable_id",
                ))
                fields_by_id = _fetch_fields(conn, [row["stable_id"] for row in rows])
                tracks = [
                    _row_to_track(row, fields_by_id.get(row["stable_id"], {}))
                    for row in rows
                ]
                catalog_revision = compute_mytag_catalog_revision(tracks)
                members = [track for track in tracks if old_name in track.tags]
                if (catalog_revision != expected_catalog_revision
                        or len(members) != expected_track_count):
                    raise MyTagScopeConflictError(catalog_revision, len(members))
                if (new_name is not None
                        and any(new_name in track.tags for track in tracks)
                        and not confirm_merge):
                    raise MyTagMergeConfirmationRequiredError(new_name)
                updates = [
                    TrackUpdate(
                        track.stable_id,
                        {"tags_remove": [old_name]} if new_name is None else {
                            "tags_add": [new_name], "tags_remove": [old_name],
                        },
                        compute_etag(track.stable_id, track.updated_at, track.selection_tag),
                    )
                    for track in members
                ]

                conflicts: list[dict[str, str]] = []
                for track, update in zip(members, updates, strict=False):
                    current_etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
                    if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                        conflicts.append({"stable_id": track.stable_id, "current_etag": current_etag})
                if conflicts:
                    raise BatchConflictError(conflicts)

                now = datetime.now(UTC).isoformat()
                with StateWriter(conn, actor="webui") as writer:
                    for track, update in zip(members, updates, strict=False):
                        for field_name, value in _field_writes(track, update.patch).items():
                            writer.set_field(
                                update.stable_id, field_name, value,
                                source=source, modified_at=now, confidence=1.0,
                            )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            if updates:
                self._sqlite_last_writer = (source, now)
            return len(updates)

    def create_pairing(self, pairing: Pairing) -> Pairing:
        _warn_fallback_once(
            "create_pairing", "no pairings table in Phase 5 state.db",
        )
        return self._fallback.create_pairing(pairing)

    def delete_pairing(self, pairing_id: str, *, expected_etag: str) -> None:
        _warn_fallback_once(
            "delete_pairing", "no pairings table in Phase 5 state.db",
        )
        self._fallback.delete_pairing(
            pairing_id, expected_etag=expected_etag,
        )

    def last_writer(self) -> tuple[str, str] | None:
        if self._sqlite_last_writer is not None:
            return self._sqlite_last_writer
        return self._fallback.last_writer()


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
