"""``StateWriter``'s playlist writes: insert, membership replace, tombstone.

Split out of :mod:`apps.shared.state.writer` (quality-gate file_size
ratchet, round 4) as a mixin. ``_PlaylistWriterMixin`` assumes the host
class provides ``self._conn``, ``self._tx()``, ``self._stamp()``,
``self._now_iso()``, ``self.machine_id()`` and ``self.bus`` -- the same
infrastructure :class:`~apps.shared.state.writer.StateWriter` already
exposes to every write method. It is not usable on its own. Imports its
constants and helpers from :mod:`apps.shared.state.writer_common`, never
from ``writer`` itself -- see that module's docstring for why.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from typing import Any, Protocol

from .events import EventBus, FakeEventBus
from .order_key import renumbered_keys
from .sync_stamp import Stamp
from .types import Event
from .writer_common import (
    MEMBERSHIPS_TABLE,
    PLAYLISTS_TABLE,
    immediate_transaction,
    next_playlist_revision,
)
from .writer_membership_order import write_membership_order_keys


class _WriterHost(Protocol):
    """StateWriter surface this mixin uses. Copy the real types; do not guess."""

    bus: EventBus | FakeEventBus
    _conn: sqlite3.Connection

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


class PlaylistNotFoundError(LookupError):
    """Raised when playlist_id has no playlists row."""


class PlaylistNotDeletedError(RuntimeError):
    """Raised when undelete_playlist targets a live row."""


class _PlaylistWriterMixin:
    """Playlist mutation methods, mixed into ``StateWriter``."""

    # --- playlists ------------------------------------------------------

    def insert_playlist(
        self: _WriterHost,
        *,
        playlist_id: str,
        name: str,
        vendor: str,
        vendor_pl_id: str,
    ) -> bool:
        """Insert, rename, or reactivate a playlist. Returns True on change.

        A ``playlist_id`` is deterministic (``compute_playlist_id``), so a
        vendor re-ingest can land on a row this machine previously
        soft-deleted (ADR 08 point 5): a rekordbox playlist deleted locally
        and still present on the next scan must come back, not stay a
        permanent tombstone. Reactivation is therefore keyed on
        ``deleted_at`` being set, not only on the name having changed --
        replaying an identical name onto a tombstoned row must still clear
        it.
        """
        now = self._now_iso()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT name, deleted_at FROM playlists WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if existing is None:
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
                conn.execute(
                    "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
                    "created_at, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        name,
                        vendor,
                        vendor_pl_id,
                        now,
                        stamp.updated_at,
                        stamp.origin_device_id,
                    ),
                )
                kind = "playlist.insert"
            elif existing[0] != name or existing[1] is not None:
                reactivated = existing[1] is not None
                revision = next_playlist_revision(conn, playlist_id, now)
                stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), revision)
                conn.execute(
                    "UPDATE playlists SET name=?, updated_at=?, "
                    "origin_device_id=?, deleted_at=NULL WHERE playlist_id=?",
                    (name, stamp.updated_at, stamp.origin_device_id, playlist_id),
                )
                kind = "playlist.undelete" if reactivated else "playlist.update"
            else:
                return False
            ev = self._append_event(
                kind=kind,
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "vendor": vendor,
                    "vendor_pl_id": vendor_pl_id,
                    "name": name,
                },
                ts=now,
            )
            self.bus.publish(ev)
        return True

    def set_playlist_memberships(
        self: _WriterHost, playlist_id: str, stable_ids: list[str]
    ) -> None:
        """Full-replace playlist memberships. Positions become 0..N-1.

        Also bumps ``playlists.updated_at`` so row-version etags derived
        from it (webui optimistic concurrency) observe membership-only
        changes, not just renames. That bump is load-bearing for sync too:
        membership rows travel attached to their playlist row (ADR 04 c5), so
        a membership edit that left ``playlists.updated_at`` alone would
        never propagate and would strand the fleet on a permanent digest
        mismatch (round 1 finding 5b).
        """
        transaction = self._tx() if self._conn.in_transaction else immediate_transaction(self._conn)
        with transaction as conn:
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            conn.execute(
                "DELETE FROM playlist_memberships WHERE playlist_id = ?",
                (playlist_id,),
            )
            keys = renumbered_keys(len(stable_ids))
            for position, sid in enumerate(stable_ids):
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "INSERT INTO playlist_memberships(playlist_id, stable_id, "
                    "position, item_id, order_key, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        sid,
                        position,
                        uuid.uuid4().hex,
                        keys[position],
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.memberships.set",
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "count": len(stable_ids),
                },
                ts=now,
            )
            self.bus.publish(ev)

    def insert_playlist_memberships(
        self: _WriterHost,
        playlist_id: str,
        rows: list[tuple[str, str, str]],
    ) -> None:
        """O(1) insert of new membership rows (item_id, stable_id, order_key)."""
        transaction = (
            self._tx() if self._conn.in_transaction
            else immediate_transaction(self._conn)
        )
        with transaction as conn:
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            for item_id, stable_id, order_key in rows:
                max_pos = conn.execute(
                    "SELECT COALESCE(MAX(position), -1) FROM playlist_memberships "
                    "WHERE playlist_id = ?",
                    (playlist_id,),
                ).fetchone()[0]
                slot = int(max_pos) + 1
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, slot), now,
                )
                conn.execute(
                    "INSERT INTO playlist_memberships(playlist_id, stable_id, "
                    "position, item_id, order_key, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        stable_id,
                        slot,
                        item_id,
                        order_key,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.memberships.add",
                stable_id=None,
                payload={"playlist_id": playlist_id, "count": len(rows)},
                ts=now,
            )
            self.bus.publish(ev)

    def tombstone_playlist_memberships(
        self: _WriterHost, playlist_id: str, item_ids: list[str],
    ) -> None:
        """Soft-delete membership rows by item_id without touching neighbors."""
        if not item_ids:
            return
        transaction = (
            self._tx() if self._conn.in_transaction
            else immediate_transaction(self._conn)
        )
        with transaction as conn:
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            for item_id in item_ids:
                row = conn.execute(
                    "SELECT position FROM playlist_memberships "
                    "WHERE playlist_id = ? AND item_id = ? AND deleted_at IS NULL",
                    (playlist_id, item_id),
                ).fetchone()
                if row is None:
                    continue
                position = row[0]
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=?, updated_at=?, "
                    "origin_device_id=? WHERE playlist_id=? AND position=?",
                    (
                        member_stamp.updated_at,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.memberships.remove",
                stable_id=None,
                payload={"playlist_id": playlist_id, "count": len(item_ids)},
                ts=now,
            )
            self.bus.publish(ev)

    def restore_playlist_memberships(
        self: _WriterHost, playlist_id: str, item_ids: list[str],
    ) -> None:
        """Undelete tombstoned membership rows, preserving item_id/order_key."""
        if not item_ids:
            return
        transaction = (
            self._tx() if self._conn.in_transaction
            else immediate_transaction(self._conn)
        )
        with transaction as conn:
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            for item_id in item_ids:
                row = conn.execute(
                    "SELECT position FROM playlist_memberships "
                    "WHERE playlist_id = ? AND item_id = ? AND deleted_at IS NOT NULL",
                    (playlist_id, item_id),
                ).fetchone()
                if row is None:
                    continue
                position = row[0]
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=NULL, "
                    "updated_at=?, origin_device_id=? "
                    "WHERE playlist_id=? AND item_id=?",
                    (
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        item_id,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
                "WHERE playlist_id = ?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.memberships.add",
                stable_id=None,
                payload={"playlist_id": playlist_id, "count": len(item_ids)},
                ts=now,
            )
            self.bus.publish(ev)

    def delete_playlist(self: _WriterHost, playlist_id: str) -> bool:
        """Tombstone a playlist and its memberships. Returns True when a live row existed.

        ADR 08 point 5 (round 1 finding 4a): a hard ``DELETE`` on a synced
        table has no way to tell a peer a row is gone -- a spoke that had
        already pushed the row finds nothing left to offer, and a hub that
        gets hard-deleted under it 409s every future pull permanently
        (``hub_changes_since`` refuses to serve a changelog entry for a row
        that no longer exists). Stamping ``deleted_at`` instead keeps the row
        alive for the merge and lets deletion itself propagate as an
        ordinary LWW-losable write.

        Memberships are stamped explicitly rather than left for an
        ``ON DELETE CASCADE`` (there is no cascade to leave -- this is an
        ``UPDATE``, not a ``DELETE``) so each membership row's tombstone gets
        its own ``local_changelog`` entry and travels with the playlist
        bundle (``apps.sync_hub.engine_apply._replace_members`` reads whatever
        currently sits in the table, deleted or not). Already-tombstoned
        memberships are left alone -- re-stamping them would move their
        ``updated_at`` without a real change. Appends one ``playlist.delete``
        event on success.
        """
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT name, vendor, vendor_pl_id FROM playlists "
                "WHERE playlist_id = ? AND deleted_at IS NULL",
                (playlist_id,),
            ).fetchone()
            if existing is None:
                return False
            # Advance past the row's own stored updated_at, the same guard
            # insert_playlist and set_playlist_memberships use: a frozen test
            # clock (or two writes in the same wall-clock tick) must not
            # stamp the tombstone with a sort key equal to what is already
            # stored, which the strict LWW "<=" comparison would then never
            # let this delete overwrite on its own hub.
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            tombstone_ts = stamp.updated_at
            member_positions = [
                row[0]
                for row in conn.execute(
                    "SELECT position FROM playlist_memberships "
                    "WHERE playlist_id = ? AND deleted_at IS NULL",
                    (playlist_id,),
                )
            ]
            for position in member_positions:
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
            conn.execute(
                "UPDATE playlists SET updated_at=?, origin_device_id=?, "
                "deleted_at=? WHERE playlist_id=?",
                (stamp.updated_at, stamp.origin_device_id, tombstone_ts, playlist_id),
            )
            ev = self._append_event(
                kind="playlist.delete",
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "name": existing[0],
                    "vendor": existing[1],
                    "vendor_pl_id": existing[2],
                },
                ts=now,
            )
            self.bus.publish(ev)
        return True

    def undelete_playlist(self: _WriterHost, playlist_id: str) -> bool:
        """Clear a playlist tombstone and restore memberships from this delete."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT name, vendor, vendor_pl_id, deleted_at FROM playlists "
                "WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()
            if row is None:
                raise PlaylistNotFoundError(playlist_id)
            if row[3] is None:
                raise PlaylistNotDeletedError(playlist_id)
            tombstone_ts = row[3]
            name, vendor, vendor_pl_id = row[0], row[1], row[2]
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET deleted_at=NULL, updated_at=?, origin_device_id=? "
                "WHERE playlist_id=?",
                (stamp.updated_at, stamp.origin_device_id, playlist_id),
            )
            restored = conn.execute(
                "SELECT position FROM playlist_memberships "
                "WHERE playlist_id = ? AND deleted_at = ?",
                (playlist_id, tombstone_ts),
            ).fetchall()
            for (position,) in restored:
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "UPDATE playlist_memberships SET deleted_at=NULL, updated_at=?, "
                    "origin_device_id=? WHERE playlist_id=? AND position=? "
                    "AND deleted_at=?",
                    (
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                        tombstone_ts,
                    ),
                )
            ev = self._append_event(
                kind="playlist.undelete",
                stable_id=None,
                payload={
                    "playlist_id": playlist_id,
                    "vendor": vendor,
                    "vendor_pl_id": vendor_pl_id,
                    "name": name,
                },
                ts=now,
            )
            self.bus.publish(ev)
        return True

    def set_playlist_forbid_duplicates(
        self: _WriterHost, playlist_id: str, value: bool,
    ) -> bool:
        """Set forbid_duplicates on a live playlist. Returns True on change."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT forbid_duplicates FROM playlists "
                "WHERE playlist_id = ? AND deleted_at IS NULL",
                (playlist_id,),
            ).fetchone()
            if row is None:
                return False
            current = bool(row[0] or 0)
            if current == value:
                return False
            now = next_playlist_revision(conn, playlist_id, self._now_iso())
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET forbid_duplicates=?, updated_at=?, "
                "origin_device_id=? WHERE playlist_id=?",
                (
                    1 if value else 0,
                    stamp.updated_at,
                    stamp.origin_device_id,
                    playlist_id,
                ),
            )
            ev = self._append_event(
                kind="playlist.update",
                stable_id=None,
                payload={"playlist_id": playlist_id, "forbid_duplicates": value},
                ts=now,
            )
            self.bus.publish(ev)
        return True

    def update_playlist_membership_order_keys(
        self: _WriterHost,
        playlist_id: str,
        rows: list[tuple[str, str]],
        *,
        renumbered: bool = False,
    ) -> None:
        """UPDATE order_key for named live membership rows only, by item_id."""
        write_membership_order_keys(self, playlist_id, "item_id", rows, renumbered=renumbered)

    def renumber_playlist_membership_order_keys(
        self: _WriterHost,
        playlist_id: str,
        rows: list[tuple[int, str]],
    ) -> None:
        """Rewrite live order_keys by position, the primary key.

        A renumber must reach every live row, and the Spotify importer writes
        rows with no item_id, so addressing them by item_id would skip them.
        """
        write_membership_order_keys(self, playlist_id, "position", rows, renumbered=True)

    def append_playlist_history(
        self: _WriterHost, kind: str, payload: dict[str, Any]
    ) -> object:
        """Append a playlist.edit / undo / redo row on the events log.

        Does not change insert / memberships.set / delete payloads. Callers
        must already be inside the mutation's playlist_transaction (or accept
        a nested SAVEPOINT that commits on its own).
        """
        now = self._now_iso()
        with self._tx():
            ev = self._append_event(
                kind=kind,
                stable_id=None,
                payload=payload,
                ts=now,
            )
            self.bus.publish(ev)
        return ev


__all__ = [
    "_PlaylistWriterMixin",
    "PlaylistNotDeletedError",
    "PlaylistNotFoundError",
]

