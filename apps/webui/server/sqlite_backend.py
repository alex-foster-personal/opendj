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
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

from apps.shared.state import db as _state_db
from apps.shared.state.writer import StateWriter

from .backend import (
    BackendError, BatchConflictError, ConflictError, InMemoryBackend,
    MAX_LIMIT, NotFoundError, Page, Pairing, Playlist, Provenance,
    QueueItem, QueueKind, Source, StateBackend, Track, TrackFilter,
    TrackUpdate,
    MyTagMergeConfirmationRequiredError, MyTagScopeConflictError,
    compute_mytag_catalog_revision,
)
from .etag import compute_etag, strip_quotes

log = logging.getLogger(__name__)


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
    "bpm", "key", "rating", "tags", "notes", "last_played_at",
)


def _parse_rfc3339(ts: str) -> datetime:
    """Parse an RFC 3339 timestamp (``Z`` or ``+00:00`` form). Fail fast.

    Naive timestamps are treated as UTC (legacy fixture rows). Anything
    unparseable raises ``ValueError`` -- no silent clock guessing.
    """
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _effective_updated_at(
    row_updated_at: str,
    fields: dict[str, tuple[Any, str, Optional[float], str]],
) -> str:
    """Latest of the row ``updated_at`` and every field ``modified_at``.

    This is the etag input (see module docstring): field-only writes via
    ``StateWriter.set_field`` must advance the etag even though they never
    touch the ``tracks`` identity row. Returns the original string of the
    winning stamp so the derivation is byte-stable across restarts.
    """
    best = row_updated_at
    best_dt = _parse_rfc3339(row_updated_at)
    for _value, _source, _confidence, modified_at in fields.values():
        dt = _parse_rfc3339(modified_at)
        if dt > best_dt:
            best, best_dt = modified_at, dt
    return best


def _row_to_track(
    row: sqlite3.Row,
    fields: dict[str, tuple[Any, str, Optional[float], str]],
) -> Track:
    """Project a ``tracks`` row + its ``track_fields`` entries into ``Track``.

    ``fields`` maps ``field_name -> (value, source, confidence, modified_at)``.
    """
    artist: Optional[str] = None
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

    bpm = fields.get("bpm", (None,))[0]
    key = fields.get("key", (None,))[0]
    rating = fields.get("rating", (None,))[0]
    tags_val = fields.get("tags", (None,))[0]
    notes = fields.get("notes", (None,))[0]
    last_played_at = fields.get("last_played_at", (None,))[0]

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
    if last_played_at is not None and not isinstance(last_played_at, str):
        last_played_at = str(last_played_at)
    if key is not None and not isinstance(key, str):
        key = str(key)

    provenance: dict[str, Provenance] = {}
    for fname, (value, source, confidence, modified_at) in fields.items():
        provenance[fname] = Provenance(
            value=value, source=source, confidence=confidence,  # type: ignore[arg-type]
            modified_at=modified_at,
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
        last_played_at=last_played_at,
        file_path=row["file_path"],
        created_at=row["created_at"],
        updated_at=_effective_updated_at(row["updated_at"], fields),
        provenance=provenance,
    )


def _fetch_fields(
    conn: sqlite3.Connection, stable_ids: list[str],
) -> dict[str, dict[str, tuple[Any, str, Optional[float], str]]]:
    """Fetch the EAV-projected fields for ``stable_ids``.

    Returns ``{stable_id: {field_name: (value, source, confidence, modified_at)}}``.
    Only fields in :data:`_EAV_FIELDS` are returned.
    """
    if not stable_ids:
        return {}
    out: dict[str, dict[str, tuple[Any, str, Optional[float], str]]] = {
        sid: {} for sid in stable_ids
    }
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
            f"  AND field_name IN ({eav_placeholders})"
        )
        for row in conn.execute(sql, (*sub, *_EAV_FIELDS)):
            sid = row["stable_id"]
            fname = row["field_name"]
            try:
                value = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = row["value_json"]
            out[sid][fname] = (
                value, row["source"], row["confidence"], row["modified_at"],
            )
    return out


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
        fallback: Optional[InMemoryBackend] = None,
    ) -> None:
        self._path = Path(state_db_path)
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

    # --- reads ------------------------------------------------------------
    def list_tracks(self, flt: TrackFilter) -> Page:
        with self._ro() as conn:
            if not self._table_exists(conn, "tracks"):
                _warn_fallback_once("list_tracks", "no tracks table")
                return self._fallback.list_tracks(flt)
            rows = list(
                conn.execute(
                    "SELECT stable_id, title, artists_json, album, "
                    "       duration_ms, file_path, created_at, updated_at "
                    "FROM tracks "
                    "ORDER BY stable_id"
                )
            )
            fields_map = _fetch_fields(
                conn, [r["stable_id"] for r in rows],
            )
        tracks = [
            _row_to_track(r, fields_map.get(r["stable_id"], {}))
            for r in rows
        ]
        if flt.q:
            needle = flt.q.casefold()
            tracks = [
                t for t in tracks
                if (t.title and needle in t.title.casefold())
                or (t.artist and needle in t.artist.casefold())
            ]
        if flt.bpm_min is not None:
            tracks = [
                t for t in tracks
                if t.bpm is not None and t.bpm >= flt.bpm_min
            ]
        if flt.bpm_max is not None:
            tracks = [
                t for t in tracks
                if t.bpm is not None and t.bpm <= flt.bpm_max
            ]
        if flt.key:
            tracks = [t for t in tracks if t.key == flt.key]
        if flt.rating_min is not None:
            tracks = [
                t for t in tracks
                if t.rating is not None and t.rating >= flt.rating_min
            ]
        if flt.tag:
            tracks = [t for t in tracks if flt.tag in (t.tags or [])]
        limit = max(1, min(flt.limit, MAX_LIMIT))
        start = 0
        if flt.cursor:
            for i, t in enumerate(tracks):
                if t.stable_id > flt.cursor:
                    start = i
                    break
            else:
                start = len(tracks)
        page = tracks[start : start + limit]
        next_cursor = page[-1].stable_id if len(page) == limit else None
        return Page(items=page, next_cursor=next_cursor)

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
            rows: list[sqlite3.Row] = []
            chunk = 500
            for i in range(0, len(ids), chunk):
                sub = ids[i : i + chunk]
                placeholders = ",".join("?" * len(sub))
                rows.extend(
                    conn.execute(
                        "SELECT stable_id, title, artists_json, album, "
                        "       duration_ms, file_path, created_at, updated_at "
                        f"FROM tracks WHERE stable_id IN ({placeholders})",
                        tuple(sub),
                    )
                )
            fields_map = _fetch_fields(conn, [r["stable_id"] for r in rows])
        return {
            r["stable_id"]: _row_to_track(r, fields_map.get(r["stable_id"], {}))
            for r in rows
        }

    def list_playlists(self) -> list[Playlist]:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("list_playlists", "no playlists table")
                return self._fallback.list_playlists()
            rows = list(
                conn.execute(
                    "SELECT playlist_id, name, vendor, vendor_pl_id, "
                    "       created_at, updated_at "
                    "FROM playlists ORDER BY playlist_id"
                )
            )
            if not self._table_exists(conn, "playlist_memberships"):
                members: dict[str, list[str]] = {}
            else:
                members = {}
                for pid, sid in conn.execute(
                    "SELECT playlist_id, stable_id FROM playlist_memberships "
                    "ORDER BY playlist_id, position"
                ):
                    members.setdefault(pid, []).append(sid)
        return [
            Playlist(
                playlist_id=r["playlist_id"], name=r["name"],
                vendor=r["vendor"], vendor_pl_id=r["vendor_pl_id"],
                items=members.get(r["playlist_id"], []),
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_playlist(self, playlist_id: str) -> Playlist:
        with self._ro() as conn:
            if not self._table_exists(conn, "playlists"):
                _warn_fallback_once("get_playlist", "no playlists table")
                return self._fallback.get_playlist(playlist_id)
            row = conn.execute(
                "SELECT playlist_id, name, vendor, vendor_pl_id, "
                "       created_at, updated_at "
                "FROM playlists WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"playlist not found: {playlist_id}")
            items: list[str] = []
            if self._table_exists(conn, "playlist_memberships"):
                items = [
                    r[0] for r in conn.execute(
                        "SELECT stable_id FROM playlist_memberships "
                        "WHERE playlist_id = ? ORDER BY position",
                        (playlist_id,),
                    )
                ]
        return Playlist(
            playlist_id=row["playlist_id"], name=row["name"],
            vendor=row["vendor"], vendor_pl_id=row["vendor_pl_id"], items=items,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

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
                    "SELECT COUNT(*) FROM playlists"
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
            )[0]
        except BatchConflictError as exc:
            current = self.get_track(stable_id)
            raise ConflictError(current.to_dict(), exc.conflicts[0]["current_etag"]) from exc

    def update_tracks(
        self, updates: Sequence[TrackUpdate], *, source: Source = "webui",
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
                        "SELECT stable_id, title, artists_json, album, duration_ms, "
                        "file_path, created_at, updated_at FROM tracks WHERE stable_id = ?",
                        (update.stable_id,),
                    ).fetchone()
                    if row is None:
                        raise NotFoundError(f"track not found: {update.stable_id}")
                    fields = _fetch_fields(conn, [update.stable_id])
                    current = _row_to_track(row, fields.get(update.stable_id, {}))
                    current_rows.append(current)
                    current_etag = compute_etag(current.stable_id, current.updated_at)
                    if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                        conflicts.append({
                            "stable_id": update.stable_id,
                            "current_etag": current_etag,
                        })
                if conflicts:
                    raise BatchConflictError(conflicts)

                writes = [
                    _field_writes(current, update.patch)
                    for current, update in zip(current_rows, updates)
                ]
                now = datetime.now(timezone.utc).isoformat()
                with StateWriter(conn, actor="webui") as writer:
                    for update, field_writes in zip(updates, writes):
                        for field_name, value in field_writes.items():
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
                    "file_path, created_at, updated_at FROM tracks ORDER BY stable_id",
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
                        compute_etag(track.stable_id, track.updated_at),
                    )
                    for track in members
                ]

                conflicts: list[dict[str, str]] = []
                for track, update in zip(members, updates):
                    current_etag = compute_etag(track.stable_id, track.updated_at)
                    if strip_quotes(current_etag) != strip_quotes(update.expected_etag):
                        conflicts.append({"stable_id": track.stable_id, "current_etag": current_etag})
                if conflicts:
                    raise BatchConflictError(conflicts)

                now = datetime.now(timezone.utc).isoformat()
                with StateWriter(conn, actor="webui") as writer:
                    for track, update in zip(members, updates):
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


def make_backend(
    state_db_path: str | Path | None = None,
) -> StateBackend:
    """Return the right backend based on whether ``state.db`` exists.

    If ``state_db_path`` resolves to an existing file, returns a
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
        log.info("webui backend: using SqliteBackend at %s", target)
        return SqliteBackend(target)
    log.info(
        "webui backend: %s not found; using InMemoryBackend", target,
    )
    return InMemoryBackend()


__all__ = [
    "SqliteBackend", "make_backend",
]
