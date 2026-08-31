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

from .writer_common import (
    MEMBERSHIPS_TABLE,
    PLAYLISTS_TABLE,
    immediate_transaction,
    next_playlist_revision,
)


class _PlaylistWriterMixin:
    """Playlist mutation methods, mixed into ``StateWriter``."""

    # --- playlists ------------------------------------------------------

    def insert_playlist(
        self,
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
        self, playlist_id: str, stable_ids: list[str]
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
            for position, sid in enumerate(stable_ids):
                member_stamp = self._stamp(
                    MEMBERSHIPS_TABLE, (playlist_id, position), now,
                )
                conn.execute(
                    "INSERT INTO playlist_memberships(playlist_id, stable_id, "
                    "position, updated_at, origin_device_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        playlist_id,
                        sid,
                        position,
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

    def delete_playlist(self, playlist_id: str) -> bool:
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
                        member_stamp.updated_at,
                        member_stamp.updated_at,
                        member_stamp.origin_device_id,
                        playlist_id,
                        position,
                    ),
                )
            stamp = self._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
            conn.execute(
                "UPDATE playlists SET updated_at=?, origin_device_id=?, "
                "deleted_at=? WHERE playlist_id=?",
                (stamp.updated_at, stamp.origin_device_id, stamp.updated_at, playlist_id),
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


__all__ = ["_PlaylistWriterMixin"]
