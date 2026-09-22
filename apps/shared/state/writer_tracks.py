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
from typing import Any, Protocol

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


class TrackNotFoundError(LookupError):
    """Raised when ``stable_id`` has no ``tracks`` row."""


class TrackAlreadyRemovedError(RuntimeError):
    """Raised when ``remove_from_library`` targets an already-tombstoned row."""


class TrackNotRemovedError(RuntimeError):
    """Raised when ``undelete_track`` targets a live row."""


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
    ) -> bool:
        """Insert/update ``tracks``. Returns True on change, False on no-op.

        A byte-equal repeat is a no-op (no history, no event) -- unless the
        row is tombstoned, in which case it is reactivated the same way
        ``insert_playlist`` reactivates a playlist: a stable_id is
        deterministic, so a re-ingest (round 2 finding 4b,
        apps/shared/state/ingest/rekordbox.py) can land on a row this
        machine previously soft-deleted. Reactivation clears ``deleted_at``
        and is stamped through ``sync_stamp`` like any other write, so the
        undelete itself propagates instead of leaving a tombstoned row
        silently refreshed underneath its own dead marker.
        """
        artists_json = json.dumps(
            list(artists), sort_keys=False, separators=(",", ":"), ensure_ascii=False
        )
        now = self._now_iso()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT stable_id_tier, title, artists_json, album, isrc, "
                "duration_ms, file_path, content_hash, deleted_at FROM tracks "
                "WHERE stable_id = ?",
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
            )
            existing_deleted_at = existing[-1] if existing is not None else None
            if (
                existing is not None
                and tuple(existing[:-1]) == new_row
                and existing_deleted_at is None
            ):
                return False
            stamp = self._stamp(TRACKS_TABLE, (stable_id,), now)
            if existing is None:
                conn.execute(
                    "INSERT INTO tracks(stable_id, stable_id_tier, title, "
                    "artists_json, album, isrc, duration_ms, file_path, "
                    "content_hash, created_at, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        now,
                        stamp.updated_at,
                        stamp.origin_device_id,
                    ),
                )
                kind = "track.insert"
            else:
                reactivated = existing_deleted_at is not None
                conn.execute(
                    "UPDATE tracks SET stable_id_tier=?, title=?, artists_json=?, "
                    "album=?, isrc=?, duration_ms=?, file_path=?, content_hash=?, "
                    "updated_at=?, origin_device_id=?, deleted_at=NULL "
                    "WHERE stable_id=?",
                    (*new_row, stamp.updated_at, stamp.origin_device_id, stable_id),
                )
                kind = "track.undelete" if reactivated else "track.update"
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

    def remove_from_library(self: _WriterHost, stable_id: str) -> TrackLifecycleResult:
        """Soft-delete a track and its live playlist memberships.

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
                "UPDATE tracks SET deleted_at=?, updated_at=?, origin_device_id=? "
                "WHERE stable_id=?",
                (
                    tombstone_ts,
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
        """Clear a track tombstone and restore memberships from this remove."""
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
                "UPDATE tracks SET deleted_at=NULL, updated_at=?, origin_device_id=? "
                "WHERE stable_id=?",
                (track_stamp.updated_at, track_stamp.origin_device_id, stable_id),
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


__all__ = [
    "_TrackWriterMixin",
    "TrackAlreadyRemovedError",
    "TrackLifecycleResult",
    "TrackMembershipRef",
    "TrackNotFoundError",
    "TrackNotRemovedError",
]
