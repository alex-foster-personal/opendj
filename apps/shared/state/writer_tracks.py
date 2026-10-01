"""``StateWriter``'s track-level writes: tracks, locations, vendor ids, fields.

Split out of :mod:`apps.shared.state.writer` (quality-gate file_size
ratchet, round 4) as a mixin. ``_TrackWriterMixin`` assumes the host class
provides ``self._conn``, ``self._tx()``, ``self._stamp()``,
``self._now_iso()``, ``self.machine_id()``, ``self.bus`` and
``self._actor`` -- the same infrastructure :class:`~apps.shared.state.writer.StateWriter`
already exposes to every write method. It is not usable on its own.
Imports its constants from :mod:`apps.shared.state.writer_common`, never
from ``writer`` itself -- see that module's docstring for why.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from . import locations as _locations
from . import provenance as _prov
from .events import EventBus, FakeEventBus
from .sync_stamp import Stamp
from .types import Event, Source
from .writer_common import (
    MEMBERSHIPS_TABLE,
    PLAYLISTS_TABLE,
    TRACKS_TABLE,
    VENDOR_IDS_TABLE,
    next_playlist_revision,
)


class _WriterHost(Protocol):
    """StateWriter surface this mixin uses. Copy the real types; do not guess."""

    bus: EventBus | FakeEventBus
    _conn: sqlite3.Connection
    _actor: str | None

    def _tx(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def _stamp(self, table: str, row_pk: tuple[Any, ...], now: str) -> Stamp: ...

    def _now_iso(self) -> str: ...

    def _append_event(
        self,
        *,
        kind: str,
        stable_id: str | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> Event: ...

    def machine_id(self) -> str: ...


#: Why a ``tracks`` tombstone exists (``tracks.deleted_reason``, schema v23).
#: ``user``: Remove from library; only an explicit undelete lifts it.
#: ``missing``: a watched-folder rescan found the file gone; an ingest that
#: finds the file again lifts it. NULL on a tombstone predates the column and
#: is read as ``user``, so an old delete stays deleted.
DeleteReason = Literal["user", "missing"]
DELETED_BY_USER: DeleteReason = "user"
DELETED_FILE_MISSING: DeleteReason = "missing"


class TrackNotFoundError(LookupError):
    """Raised when ``stable_id`` has no ``tracks`` row."""


class TrackAlreadyRemovedError(RuntimeError):
    """Raised when ``remove_from_library`` targets an already-tombstoned row."""


class TrackNotRemovedError(RuntimeError):
    """Raised when ``undelete_track`` targets a live row."""


class TrackRemovedError(RuntimeError):
    """Raised when ``upsert_track`` or ``claim_track`` targets a removed row.

    A removed track comes back through ``undelete_track`` and nothing else
    (LIBM-140). An ingest asks ``deleted_tracks.find_deleted_match`` first and
    counts the row as skipped; any other caller reaching a tombstone has a
    defect, and overwriting the tombstone would hide it.
    """


@dataclass(frozen=True)
class TrackMembershipRef:
    playlist_id: str
    position: int


@dataclass(frozen=True)
class TrackLifecycleResult:
    stable_id: str
    deleted_at: str | None
    memberships: list[TrackMembershipRef]


class _TrackWriterMixin:
    """Track-level mutation methods, mixed into ``StateWriter``."""

    # --- tracks -------------------------------------------------------

    def upsert_track(
        self: _WriterHost,
        *,
        stable_id: str,
        stable_id_tier: str,
        title: str | None,
        artists: Iterable[str],
        album: str | None,
        isrc: str | None,
        duration_ms: int | None,
        file_path: str | None,
        content_hash: str | None = None,
        audio_hash: str | None = None,
    ) -> bool:
        """Insert/update ``tracks``. Returns True on change, False on no-op.

        A byte-equal repeat is a no-op (no history, no event).

        A row the user removed is NEVER written here, changed or not: this
        raises :class:`TrackRemovedError` and leaves the tombstone
        byte-identical (LIBM-140). A ``stable_id`` is deterministic, so a
        vendor re-ingest lands on the row this machine soft-deleted; before
        this rule that cleared ``deleted_at`` and stamped the row, which
        undid every delete on the next rekordbox import and synced the
        resurrection to the fleet. ``undelete_track`` is the one way back.

        A row tombstoned because its FILE went missing
        (``deleted_reason='missing'``, a watched-folder rescan) is the one
        tombstone an upsert lifts: the file is back, which is exactly what the
        tombstone was waiting for. That lift stamps ``restored_at`` like an
        explicit restore, so it outranks the tombstone on every machine.
        """
        artists_json = json.dumps(
            list(artists), sort_keys=False, separators=(",", ":"), ensure_ascii=False
        )
        now = self._now_iso()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT stable_id_tier, title, artists_json, album, isrc, "
                "duration_ms, file_path, content_hash, audio_hash, deleted_at, "
                "deleted_reason FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            new_row = (
                stable_id_tier,
                title,
                artists_json,
                album,
                isrc,
                duration_ms,
                file_path,
                content_hash,
                audio_hash,
            )
            deleted_at, deleted_reason = existing[-2:] if existing is not None else (None, None)
            file_is_back = deleted_at is not None and deleted_reason == DELETED_FILE_MISSING
            if deleted_at is not None and not file_is_back:
                raise TrackRemovedError(
                    f"{stable_id} was removed from the library at {deleted_at}; "
                    "restore it with undelete_track before writing to it"
                )
            if existing is not None and tuple(existing[:-2]) == new_row and not file_is_back:
                return False
            stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            if existing is None:
                conn.execute(
                    "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                    "artists_json, album, isrc, duration_ms, file_path, "
                    "content_hash, audio_hash, created_at, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stable_id,
                        stable_id_tier,
                        title,
                        artists_json,
                        album,
                        isrc,
                        duration_ms,
                        file_path,
                        content_hash,
                        audio_hash,
                        now,
                        stamp.updated_at,
                        stamp.origin_device_id,
                    ),
                )
                kind = "track.insert"
            elif file_is_back:
                conn.execute(
                    "UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?, "
                    "album=?, isrc=?, duration_ms=?, file_path=?, content_hash=?, audio_hash=?, "
                    "updated_at=?, origin_device_id=?, deleted_at=NULL, "
                    "deleted_reason=NULL, restored_at=? "
                    "WHERE stable_id=? AND deleted_reason=?",
                    (
                        *new_row,
                        stamp.updated_at,
                        stamp.origin_device_id,
                        stamp.updated_at,
                        stable_id,
                        DELETED_FILE_MISSING,
                    ),
                )
                kind = "track.undelete"
            else:
                conn.execute(
                    "UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?, "
                    "album=?, isrc=?, duration_ms=?, file_path=?, content_hash=?, audio_hash=?, "
                    "updated_at=?, origin_device_id=? "
                    "WHERE stable_id=? AND deleted_at IS NULL",
                    (*new_row, stamp.updated_at, stamp.origin_device_id, stable_id),
                )
                kind = "track.update"
            ev = self._append_event(
                kind=kind,
                stable_id=stable_id,
                payload={"tier": stable_id_tier, "had_isrc": bool(isrc)},
                ts=now,
            )
            self.bus.publish(ev)
            _locations.sync_primary_local(
                conn,
                stable_id=stable_id,
                file_path=file_path,
                now=now,
                machine_id=self.machine_id(),
            )
        return True

    def upsert_track_location(
        self: _WriterHost,
        *,
        stable_id: str,
        kind: _locations.Kind,
        file_path: str | None = None,
        remote_url: str | None = None,
        role: _locations.Role = "alternate",
        content_hash: str | None = None,
    ) -> str:
        """Record an extra playable copy on THIS machine.

        Does not change ``tracks.file_path``. Returns the ``location_id``
        (uuid4 hex): schema v6 dropped the INTEGER rowid key because it
        collided across machines.
        """
        now = self._now_iso()
        with self._tx() as conn:
            loc_id = _locations.upsert_location(
                conn,
                stable_id=stable_id,
                kind=kind,
                file_path=file_path,
                remote_url=remote_url,
                role=role,
                content_hash=content_hash,
                now=now,
                machine_id=self.machine_id(),
            )
            ev = self._append_event(
                kind="track.location.upsert",
                stable_id=stable_id,
                payload={"location_id": loc_id, "kind": kind, "role": role},
                ts=now,
            )
            self.bus.publish(ev)
        return loc_id

    # --- vendor ids -----------------------------------------------------

    def set_vendor_id(
        self: _WriterHost, stable_id: str, vendor: str, vendor_id: str
    ) -> None:
        now = self._now_iso()
        with self._tx() as conn:
            stamp = self._stamp(VENDOR_IDS_TABLE, (stable_id, vendor), now)
            # Upsert rather than INSERT OR REPLACE: the latter is a
            # delete-then-insert, which would silently drop ``deleted_at``
            # (and fire ON DELETE cascades) on every vendor-id refresh.
            conn.execute(
                "INSERT INTO track_vendor_ids(stable_id, vendor, vendor_id, "
                "updated_at, origin_device_id) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(stable_id, vendor) DO UPDATE SET "
                "vendor_id=excluded.vendor_id, "
                "updated_at=excluded.updated_at, "
                "origin_device_id=excluded.origin_device_id",
                (
                    stable_id,
                    vendor,
                    vendor_id,
                    stamp.updated_at,
                    stamp.origin_device_id,
                ),
            )
            ev = self._append_event(
                kind="track.vendor_id.set",
                stable_id=stable_id,
                payload={"vendor": vendor, "vendor_id": vendor_id},
                ts=now,
            )
            self.bus.publish(ev)

    # --- wrapped fields ---------------------------------------------------

    def set_field(
        self: _WriterHost,
        stable_id: str,
        field_name: str,
        value: Any,
        *,
        source: Source,
        modified_at: str,
        confidence: float | None = None,
    ) -> bool:
        """Set a provenance-wrapped field. Returns True on change.

        The DB mutation and the bus publish are ordered inside one outer
        SAVEPOINT: if ``bus.publish`` raises, the mutation is rolled back so
        subscribers and the ``track_fields`` row can never drift. This keeps
        the durable log + in-process fanout invariant the module docstring
        promises -- addresses Codex P05 finding on mutation/publish ordering.
        """
        with self._tx():
            changed = _prov.write_field(
                self._conn,
                stable_id=stable_id,
                field_name=field_name,
                value=value,
                source=source,
                modified_at=modified_at,
                confidence=confidence,
                actor=self._actor,
                now=self._now_iso(),
                machine_id=self.machine_id(),
            )
            if changed:
                payload: dict[str, Any] = {
                    "field_name": field_name,
                    "value": value,
                    "source": source,
                    "modified_at": modified_at,
                }
                if confidence is not None:
                    payload["confidence"] = confidence
                # Publish inside the SAVEPOINT: a raising bus propagates out
                # of ``_tx`` and triggers ROLLBACK TO, reverting write_field's
                # already-RELEASEd inner SAVEPOINT.
                self.bus.publish(
                    Event(
                        ts=self._now_iso(),
                        kind="track.field.set",
                        stable_id=stable_id,
                        payload=payload,
                        actor=self._actor,
                    )
                )
        return changed

    def remove_from_library(
        self: _WriterHost, stable_id: str, *, reason: DeleteReason = DELETED_BY_USER
    ) -> TrackLifecycleResult:
        """Soft-delete a track and its live playlist memberships.

        ``reason`` is recorded in ``tracks.deleted_reason``. The default is the
        user's own removal, which no ingest undoes. A scan that tombstones a
        row because its file vanished passes ``DELETED_FILE_MISSING``, the one
        tombstone ``upsert_track`` lifts when the file is found again.

        The audio file on disk is never touched. Membership tombstones use the
        track stamp's ``updated_at`` as ``deleted_at`` so ``undelete_track``
        can restore exactly this remove's rows.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT deleted_at FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise TrackNotFoundError(stable_id)
            if row[0] is not None:
                raise TrackAlreadyRemovedError(stable_id)
            now = self._now_iso()
            track_stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            tombstone_ts = track_stamp.updated_at
            live_memberships = conn.execute(
                "SELECT playlist_id, position FROM playlist_memberships "
                "WHERE stable_id = ? AND deleted_at IS NULL",
                (stable_id,),
            ).fetchall()
            memberships: list[TrackMembershipRef] = []
            playlist_ids: set[str] = set()
            for playlist_id, position in live_memberships:
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=?, updated_at=?, "
                    "origin_device_id=? WHERE playlist_id=? AND position=?",
                    (
                        tombstone_ts,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                    ),
                )
                memberships.append(TrackMembershipRef(playlist_id, position))
                playlist_ids.add(playlist_id)
            for playlist_id in sorted(playlist_ids):
                revision = next_playlist_revision(conn, playlist_id, now)
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), revision)
                conn.execute(
                    "UPDATE playlists SET updated_at=?, origin_device_id=? "
                    "WHERE playlist_id=?",
                    (stamp.updated_at, stamp.origin_device_id, playlist_id),
                )
            conn.execute(
                "UPDATE tracks SET deleted_at=?, deleted_reason=?, updated_at=?, "
                "origin_device_id=? WHERE stable_id=?",
                (
                    tombstone_ts,
                    reason,
                    track_stamp.updated_at,
                    track_stamp.origin_device_id,
                    stable_id,
                ),
            )
            ev = self._append_event(
                kind="track.delete",
                stable_id=stable_id,
                payload={
                    "memberships": [
                        {"playlist_id": m.playlist_id, "position": m.position}
                        for m in memberships
                    ],
                },
                ts=now,
            )
            self.bus.publish(ev)
        return TrackLifecycleResult(stable_id, tombstone_ts, memberships)

    def undelete_track(self: _WriterHost, stable_id: str) -> TrackLifecycleResult:
        """Clear a track tombstone and restore memberships from this remove.

        Stamps ``restored_at``: the record that this live row is a deliberate
        restore, which is what lets it outrank the tombstone on every other
        machine (``apps.sync_hub.protocol_common.lifecycle_key``). The only
        other writer of it is ``upsert_track`` lifting a ``missing`` tombstone.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT deleted_at FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise TrackNotFoundError(stable_id)
            tombstone_ts = row[0]
            if tombstone_ts is None:
                raise TrackNotRemovedError(stable_id)
            now = self._now_iso()
            track_stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            conn.execute(
                "UPDATE tracks SET deleted_at=NULL, deleted_reason=NULL, restored_at=?, "
                "updated_at=?, origin_device_id=? WHERE stable_id=?",
                (
                    track_stamp.updated_at,
                    track_stamp.updated_at,
                    track_stamp.origin_device_id,
                    stable_id,
                ),
            )
            restored_rows = conn.execute(
                "SELECT playlist_id, position FROM playlist_memberships "
                "WHERE stable_id = ? AND deleted_at = ?",
                (stable_id, tombstone_ts),
            ).fetchall()
            memberships: list[TrackMembershipRef] = []
            playlist_ids: set[str] = set()
            for playlist_id, position in restored_rows:
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=NULL, updated_at=?, "
                    "origin_device_id=? WHERE playlist_id=? AND position=? "
                    "AND stable_id=? AND deleted_at=?",
                    (
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                        stable_id,
                        tombstone_ts,
                    ),
                )
                memberships.append(TrackMembershipRef(playlist_id, position))
                playlist_ids.add(playlist_id)
            for playlist_id in sorted(playlist_ids):
                revision = next_playlist_revision(conn, playlist_id, now)
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), revision)
                conn.execute(
                    "UPDATE playlists SET updated_at=?, origin_device_id=? "
                    "WHERE playlist_id=?",
                    (stamp.updated_at, stamp.origin_device_id, playlist_id),
                )
            ev = self._append_event(
                kind="track.undelete",
                stable_id=stable_id,
                payload={
                    "memberships": [
                        {"playlist_id": m.playlist_id, "position": m.position}
                        for m in memberships
                    ],
                },
                ts=now,
            )
            self.bus.publish(ev)
        return TrackLifecycleResult(stable_id, None, memberships)

    def claim_track(self: _WriterHost, stable_id: str) -> str:
        """Re-stamp a live track as this machine's own write. Returns the stamp.

        The ``stale-tracks --keep`` remedy (CLOUDSYNC-30, issue #4628): a
        track another machine wrote, which the fleet has since dropped, is
        refused by the stale-copy guard (``apps.sync_hub.stale_copy``) until
        a machine claims it. Claiming changes no value but the stamp, so the
        next sync offers it as this machine's write and it returns to the
        fleet. A removed track is not claimed; restore it with
        ``undelete_track``.
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT deleted_at FROM tracks WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if row is None:
                raise TrackNotFoundError(stable_id)
            if row[0] is not None:
                raise TrackRemovedError(stable_id)
            now = self._now_iso()
            stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            conn.execute(
                "UPDATE tracks SET updated_at=?, origin_device_id=? WHERE stable_id=?",
                (stamp.updated_at, stamp.origin_device_id, stable_id),
            )
            ev = self._append_event(
                kind="track.claim", stable_id=stable_id, payload={}, ts=now
            )
            self.bus.publish(ev)
        return stamp.updated_at


__all__ = [
    "DELETED_BY_USER",
    "DELETED_FILE_MISSING",
    "DeleteReason",
    "TrackAlreadyRemovedError",
    "TrackLifecycleResult",
    "TrackMembershipRef",
    "TrackNotFoundError",
    "TrackNotRemovedError",
    "TrackRemovedError",
    "_TrackWriterMixin",
]
