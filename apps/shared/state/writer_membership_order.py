"""``StateWriter``'s membership order_key writes, shared by move and renumber.

Split out of :mod:`apps.shared.state.writer_playlists` to keep that module
under the quality gate's 600-line file ratchet. The host is the playlist
mixin's ``StateWriter`` surface (:class:`~apps.shared.state.writer_playlists._WriterHost`).
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from .writer_common import (
    MEMBERSHIPS_TABLE,
    PLAYLISTS_TABLE,
    immediate_transaction,
    next_playlist_revision,
)

if TYPE_CHECKING:
    from .writer_playlists import _WriterHost


def write_membership_order_keys(
    host: _WriterHost,
    playlist_id: str,
    address: Literal["item_id", "position"],
    rows: Sequence[tuple[str | int, str]],
    *,
    renumbered: bool,
) -> None:
    """UPDATE order_key for live membership rows named by ``address``."""
    if not rows:
        return
    transaction = (
        host._tx() if host._conn.in_transaction
        else immediate_transaction(host._conn)
    )
    with transaction as conn:
        now = next_playlist_revision(conn, playlist_id, host._now_iso())
        changed = 0
        for address_value, new_key in rows:
            row = conn.execute(
                f"SELECT position, order_key FROM playlist_memberships "
                f"WHERE playlist_id = ? AND {address} = ? AND deleted_at IS NULL",
                (playlist_id, address_value),
            ).fetchone()
            if row is None or row[1] == new_key:
                continue
            position = row[0]
            member_stamp = host._stamp(MEMBERSHIPS_TABLE, (playlist_id, position), now)
            conn.execute(
                "UPDATE playlist_memberships SET order_key=?, updated_at=?, "
                "origin_device_id=? WHERE playlist_id=? AND position=?",
                (
                    new_key,
                    member_stamp.updated_at,
                    member_stamp.origin_device_id,
                    playlist_id,
                    position,
                ),
            )
            changed += 1
        if changed == 0:
            return
        stamp = host._stamp(PLAYLISTS_TABLE, (playlist_id,), now)
        conn.execute(
            "UPDATE playlists SET updated_at = ?, origin_device_id = ? "
            "WHERE playlist_id = ?",
            (stamp.updated_at, stamp.origin_device_id, playlist_id),
        )
        ev = host._append_event(
            kind="playlist.memberships.move",
            stable_id=None,
            payload={
                "playlist_id": playlist_id,
                "count": changed,
                "renumbered": renumbered,
            },
            ts=now,
        )
        host.bus.publish(ev)


__all__ = ["write_membership_order_keys"]
