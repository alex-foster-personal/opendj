"""Playlist WRITE store over the shared state DB (``data/state/state.db``).

CONTRACT -- playlists-router lane owner (downstream agents code against this)
=============================================================================
This module + :mod:`apps.webui.server.routes.playlist_write` define the write
contract for playlists. Reads stay in :mod:`.routes.playlists` /
:class:`.sqlite_backend.SqliteBackend`; every mutation goes through
:class:`PlaylistStore`, which writes exclusively via the shared-state
chokepoint :class:`apps.shared.state.writer.StateWriter` (durable ``events``
rows + in-process bus fanout on every change -- never raw INSERTs).

Entity shape (also the 409 ``current`` body and the route response body):

    PlaylistRow {
        playlist_id: str      # sha1("<vendor>:<vendor_pl_id>") -- stable
        name: str
        vendor: str           # "webui" for rows created here
        vendor_pl_id: str     # uuid4 hex for rows created here
        items: list[str]      # member stable_ids in position order
        created_at: str       # ISO-8601 UTC
        updated_at: str       # ISO-8601 UTC; bumps on rename AND membership change
    }

ETag / optimistic concurrency (identical to the tracks PATCH semantics):

  * etag = ``compute_etag(playlist_id, updated_at)`` -- quoted sha1.
  * Mutating an existing row (rename / delete / membership replace) requires
    ``If-Match``; a missing header is 428, a stale one raises
    :class:`ConflictError` -> HTTP 409 with ``{current, etag}`` so the caller
    can rebase (this is the undo/redo + write-back building block).
  * ``updated_at`` bumps on membership-only changes too (StateWriter bumps it
    inside the same transaction), so the etag always observes reorders.

Operations (routes in ``routes/playlist_write.py`` map 1:1):

  * ``create_playlist(name)``                          POST   /playlists
  * ``rename_playlist(id, name, expected_etag=...)``   PATCH  /playlists/{id}
  * ``delete_playlist(id, expected_etag=...)``         DELETE /playlists/{id}
  * ``duplicate_playlist(id, name=None, expected_etag=None)``
                                                       POST   /playlists/{id}/duplicate
  * ``replace_memberships(id, stable_ids, expected_etag=...)``
                                                       PUT    /playlists/{id}/tracks
  * ``transfer_memberships(dest_id, stable_ids, dest_etag, mode, ...)``
                                                       POST   /playlists/{id}/tracks/transfer

``replace_memberships`` is the single membership primitive: add / remove /
reorder / move / copy are all expressed as one full-list replace. It is
idempotent -- replaying the same list is a no-op (no event, same etag).
Duplicate stable_ids are permitted (a track may appear at several positions,
matching Rekordbox). Unknown stable_ids fail fast with
:class:`BackendError` -> HTTP 422; nothing is partially written.

Events appended (kind -> payload keys):

  * ``playlist.insert`` / ``playlist.update``  {playlist_id, vendor, vendor_pl_id, name}
  * ``playlist.memberships.set``               {playlist_id, count}
  * ``playlist.delete``                        {playlist_id, name, vendor, vendor_pl_id}

Provenance: playlists carry no per-field provenance envelope (unlike
track_fields); provenance is the ``events`` log itself -- every row records
``actor`` ("webui" here) plus the vendor identity columns on the playlist.

Concurrency: one store per process, one rw connection, one lock. Membership
replacement also acquires SQLite's cross-process writer lock before loading
the authoritative row, checking its ETag, or validating requested tracks.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from apps.shared.state import db as _state_db
from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id

from .backend import BackendError, ConflictError, NotFoundError
from .etag import compute_etag, strip_quotes

WEBUI_VENDOR: str = "webui"
"""Vendor tag for playlists created through the web UI / agent API."""


@dataclass
class PlaylistRow:
    """Post-write read model returned by every mutating call."""
    playlist_id: str
    name: str
    vendor: str
    vendor_pl_id: str
    items: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def etag(self) -> str:
        return compute_etag(self.playlist_id, self.updated_at)


class PlaylistStore:
    """Serialised playlist mutation facade. See module docstring for contract."""

    def __init__(
        self,
        state_db_path: str | Path,
        *,
        bus: EventBus | FakeEventBus | None = None,
        actor: str = "webui",
        clock: Optional[Callable[..., Any]] = None,
    ) -> None:
        path = Path(state_db_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"state DB not found at {path}; playlist writes require an "
                f"initialised state.db (python -m apps.shared.state.cli init)"
            )
        # Single connection, serialised by self._lock; FastAPI's sync
        # executor calls in from varying threadpool threads, hence the
        # check_same_thread opt-out (safe because every public method
        # takes the lock).
        self._conn: sqlite3.Connection = _state_db.open_rw(
            path, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._writer = StateWriter(self._conn, bus, clock=clock, actor=actor)
        self._lock = threading.RLock()
        self._closed = False

    # --- lifecycle --------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._writer.close()
            self._conn.close()

    def __enter__(self) -> "PlaylistStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- internal helpers -------------------------------------------------
    def _load(self, playlist_id: str) -> PlaylistRow:
        row = self._conn.execute(
            "SELECT playlist_id, name, vendor, vendor_pl_id, "
            "       created_at, updated_at "
            "FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
            (playlist_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"playlist not found: {playlist_id}")
        items = [
            r[0] for r in self._conn.execute(
                "SELECT stable_id FROM playlist_memberships "
                "WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
                (playlist_id,),
            )
        ]
        return PlaylistRow(
            playlist_id=row["playlist_id"], name=row["name"],
            vendor=row["vendor"], vendor_pl_id=row["vendor_pl_id"],
            items=items, created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _check_etag(self, row: PlaylistRow, expected_etag: str) -> None:
        if strip_quotes(row.etag) != strip_quotes(expected_etag):
            raise ConflictError(current=row.to_dict(), etag=row.etag)

    def _validated_name(self, name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise BackendError("playlist name must be a non-empty string")
        return cleaned

    def _require_known_tracks(self, stable_ids: list[str]) -> None:
        """Fail fast (422) when any stable_id has no ``tracks`` row."""
        unique = list(dict.fromkeys(stable_ids))
        known: set[str] = set()
        chunk = 500
        for i in range(0, len(unique), chunk):
            sub = unique[i : i + chunk]
            placeholders = ",".join("?" * len(sub))
            known.update(
                r[0] for r in self._conn.execute(
                    f"SELECT stable_id FROM tracks "
                    f"WHERE stable_id IN ({placeholders}) AND deleted_at IS NULL",
                    tuple(sub),
                )
            )
        missing = [sid for sid in unique if sid not in known]
        if missing:
            raise BackendError(
                f"unknown stable_ids ({len(missing)}): {missing[:5]}"
            )

    # --- mutations --------------------------------------------------------
    def create_playlist(self, name: str) -> PlaylistRow:
        """Create an empty playlist owned by the ``webui`` vendor."""
        cleaned = self._validated_name(name)
        with self._lock:
            vendor_pl_id = uuid.uuid4().hex
            playlist_id = compute_playlist_id(WEBUI_VENDOR, vendor_pl_id)
            self._writer.insert_playlist(
                playlist_id=playlist_id, name=cleaned,
                vendor=WEBUI_VENDOR, vendor_pl_id=vendor_pl_id,
            )
            return self._load(playlist_id)

    def rename_playlist(
        self, playlist_id: str, name: str, *, expected_etag: str,
    ) -> PlaylistRow:
        """Rename; renaming to the current name is a no-op (same etag)."""
        cleaned = self._validated_name(name)
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                changed = self._writer.insert_playlist(
                    playlist_id=playlist_id, name=cleaned,
                    vendor=row.vendor, vendor_pl_id=row.vendor_pl_id,
                )
                if not changed:
                    return row
                return self._load(playlist_id)

    def delete_playlist(
        self, playlist_id: str, *, expected_etag: str,
    ) -> None:
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                deleted = self._writer.delete_playlist(playlist_id)
                if not deleted:  # pragma: no cover - guarded by the lock
                    raise NotFoundError(f"playlist not found: {playlist_id}")

    def duplicate_playlist(
        self,
        playlist_id: str,
        *,
        name: Optional[str] = None,
        expected_etag: Optional[str] = None,
    ) -> PlaylistRow:
        """Copy name (or ``name``) + full membership into a new webui playlist.

        ``expected_etag`` is optional (POST creates a new resource); when
        provided it is CAS-checked against the SOURCE playlist so callers
        can guarantee they duplicated the version they were looking at.
        """
        with self._lock:
            with self._writer.playlist_transaction():
                source = self._load(playlist_id)
                if expected_etag is not None:
                    self._check_etag(source, expected_etag)
                new_name = self._validated_name(
                    name if name is not None else f"{source.name} (copy)"
                )
                vendor_pl_id = uuid.uuid4().hex
                new_id = compute_playlist_id(WEBUI_VENDOR, vendor_pl_id)
                self._writer.insert_playlist(
                    playlist_id=new_id, name=new_name,
                    vendor=WEBUI_VENDOR, vendor_pl_id=vendor_pl_id,
                )
                if source.items:
                    self._writer.set_playlist_memberships(new_id, list(source.items))
                return self._load(new_id)

    def replace_memberships(
        self, playlist_id: str, stable_ids: list[str], *, expected_etag: str,
    ) -> PlaylistRow:
        """Full membership replace = add/remove/reorder in one idempotent op.

        Replaying the identical list is a true no-op: no event, no
        ``updated_at`` bump, same etag back.
        """
        with self._lock:
            with self._writer.playlist_transaction():
                row = self._load(playlist_id)
                self._check_etag(row, expected_etag)
                self._require_known_tracks(stable_ids)
                if list(row.items) == list(stable_ids):
                    return row
                self._writer.set_playlist_memberships(playlist_id, list(stable_ids))
                return self._load(playlist_id)

    def transfer_memberships(
        self,
        dest_id: str,
        stable_ids: list[str],
        *,
        dest_etag: str,
        mode: Literal["add", "move"],
        source_id: str | None = None,
        source_etag: str | None = None,
    ) -> tuple[PlaylistRow, PlaylistRow | None]:
        """Atomic cross-playlist add (copy) or move of track identities.

        Add appends missing stable_ids to dest (set-union, request-order).
        Move also removes every matching stable_id from source. Both writes
        happen inside one ``playlist_transaction`` so an interrupt rolls back.
        """
        if not stable_ids:
            raise BackendError("stable_ids must be a non-empty list")
        if mode == "move" and (source_id is None or source_etag is None):
            raise BackendError(
                "mode=move requires source_playlist_id and source_etag"
            )
        if source_id is not None and source_id == dest_id:
            raise BackendError("cannot transfer a playlist onto itself")

        with self._lock:
            with self._writer.playlist_transaction():
                self._require_known_tracks(stable_ids)
                dest = self._load(dest_id)
                self._check_etag(dest, dest_etag)

                source: PlaylistRow | None = None
                source_next: list[str] | None = None
                if source_id is not None:
                    source = self._load(source_id)
                    if source_etag is not None:
                        self._check_etag(source, source_etag)

                dest_set = set(dest.items)
                dest_next = list(dest.items)
                pending: set[str] = set()
                for sid in stable_ids:
                    if sid in dest_set:
                        continue
                    if sid not in pending:
                        dest_next.append(sid)
                        pending.add(sid)

                if mode == "move" and source is not None:
                    drop = set(stable_ids)
                    source_next = [sid for sid in source.items if sid not in drop]
                else:
                    source_next = source.items if source is not None else None

                dest_unchanged = dest_next == list(dest.items)
                source_unchanged = (
                    source is None
                    or source_next == list(source.items)
                )
                if dest_unchanged and (mode == "add" or source_unchanged):
                    return dest, source if mode == "move" else None

                if not dest_unchanged:
                    self._writer.set_playlist_memberships(dest_id, dest_next)

                if mode == "move" and source is not None and not source_unchanged:
                    self._writer.set_playlist_memberships(source_id, source_next)

                dest_row = self._load(dest_id)
                source_row = (
                    self._load(source_id) if mode == "move" and source_id else None
                )
                return dest_row, source_row

    # --- reads (for router symmetry) --------------------------------------
    def get_playlist_row(self, playlist_id: str) -> PlaylistRow:
        with self._lock:
            return self._load(playlist_id)

    def verify_etag(self, playlist_id: str, expected_etag: str) -> PlaylistRow:
        """CAS-check without writing; raises ConflictError on mismatch."""
        with self._lock:
            row = self._load(playlist_id)
            self._check_etag(row, expected_etag)
            return row


__all__ = ["PlaylistRow", "PlaylistStore", "WEBUI_VENDOR"]
