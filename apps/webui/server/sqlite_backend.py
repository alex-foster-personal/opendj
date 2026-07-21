"""SQLite-backed :class:`StateBackend` implementation (Phase 5 wiring).

Reads are served from Phase 5's shared-state DB (``data/state/state.db``)
via :func:`apps.shared.state.db.open_ro`. The webui's ``Track`` / ``Playlist``
shape is projected from the normalized Phase 5 schema:

  * flat columns (``title``, ``album``, ``duration_ms``, ``file_path``,
    ``artists_json``) come from the ``tracks`` table;
  * per-field values + provenance (``bpm``, ``key``, ``rating``, ``tags``,
    ``notes``, ``last_played_at``) come from the ``track_fields`` EAV
    table.

Phase 5 does NOT (yet) model:

  * the webui ``pairings`` entity,
  * the triage ``queues`` (dedup / bad_beatgrid / auto_cue),
  * writes with optimistic concurrency (If-Match / ETag).

For those methods we delegate to an :class:`InMemoryBackend` companion and
log a one-shot warning per process so deployments know they are on the
fallback path. When Phase 6/7/12 ship the missing tables, individual
methods here should switch to real SQL without the fallback.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Optional

from apps.shared.state import db as _state_db

from .backend import (
    BackendError, ConflictError, InMemoryBackend, MAX_LIMIT, NotFoundError,
    Page, Pairing, Playlist, Provenance, QueueItem, QueueKind, Source,
    StateBackend, Track, TrackFilter,
)

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
        updated_at=row["updated_at"],
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


class SqliteBackend:
    """Read-focused StateBackend backed by Phase 5's ``state.db``.

    The constructor takes the path to ``state.db`` and a companion
    :class:`InMemoryBackend` used for:

      * unimplemented writes,
      * entities Phase 5 does not yet model (``pairings``, queues).

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
        # Serialise compound read-then-write paths (e.g. update_track, which
        # reads via self.get_track / seeds the fallback / then hands off to
        # InMemoryBackend.update_track for the ETag CAS). Without this lock,
        # two concurrent writers race between the unlocked get_track read
        # and the fallback update, so a second caller reseeds the fallback
        # from a stale sqlite row and overwrites the first caller's write
        # with the old value (lost update). Adversarial R4 finding.
        self._write_lock = threading.RLock()

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

    # --- writes (delegated) ----------------------------------------------
    def update_track(
        self, stable_id: str, patch: dict[str, Any], *,
        expected_etag: str, source: Source = "webui",
    ) -> Track:
        _warn_fallback_once(
            "update_track",
            "webui writes not yet wired through apps.shared.state.writer",
        )
        # Hold the write lock across the read-then-write sequence so a second
        # caller cannot slip in between self.get_track (unlocked _ro read)
        # and self._fallback.update_track and overwrite the first caller's
        # commit with a stale seed. Also avoid reseeding the fallback from
        # sqlite when the fallback is already ahead: Phase 5 writes still
        # live in the fallback (sqlite is read-only from here), so if the
        # fallback carries a later ``updated_at`` it is the authoritative
        # post-write state; seeding from sqlite would silently clobber the
        # prior writer's commit and the next CAS would accept a stale
        # ``expected_etag``. Adversarial R4 finding (TOCTOU on update_track).
        with self._write_lock:
            try:
                current = self.get_track(stable_id)
            except NotFoundError:
                raise
            existing = self._fallback._tracks.get(stable_id)  # type: ignore[attr-defined]
            # Seed only on cold-start, i.e. fallback has never seen this
            # track. If ``existing`` is already present, trust it: the
            # fallback is the authoritative post-write state (sqlite is
            # read-only from this code path until Phase 5 ships real
            # writes). The CAS inside ``_fallback.update_track`` will
            # return ConflictError against a stale ``expected_etag``.
            if existing is None:
                self._fallback.seed_track(replace(current))
            return self._fallback.update_track(
                stable_id, patch,
                expected_etag=expected_etag, source=source,
            )

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
