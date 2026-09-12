"""O(1) playlist membership remove (LIBM-21 DELETE by item_id)."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from apps.shared.state.writer import StateWriter

from .backend import BackendError, NotFoundError
from .playlist_add import (
    SmartlistImmutableError,
    _is_smartlist,
    _load_live_members,
    _reject_over_cap,
)
from .playlist_history import PlaylistSnapshot

if TYPE_CHECKING:
    from .playlist_store import PlaylistRow, PlaylistStore


def _snapshot_with_members(
    store: PlaylistStore, row: PlaylistRow,
) -> PlaylistSnapshot:
    snap = store._snapshot(row)
    snap.members = [
        {
            "item_id": m.item_id,
            "stable_id": m.stable_id,
            "order_key": m.order_key,
            "position": m.position,
        }
        for m in _load_live_members(store._conn, row.playlist_id)
    ]
    return snap


def apply_membership_snapshot(
    store: PlaylistStore,
    snap: PlaylistSnapshot,
    live: PlaylistRow | None,
) -> PlaylistRow:
    """Undo/redo inverse for membership edits with optional member identities."""
    if snap.members is None:
        if live is None:
            raise BackendError("memberships inverse is missing live row")
        return store.replace_memberships(
            live.playlist_id, list(snap.items),
            expected_etag=live.etag, record_edit=False,
        )
    if live is None:
        raise BackendError("memberships inverse is missing live row")
    desired = {m["item_id"] for m in snap.members}
    rows = store._conn.execute(
        "SELECT item_id, deleted_at FROM playlist_memberships "
        "WHERE playlist_id = ?",
        (snap.playlist_id,),
    ).fetchall()
    live_ids = {r[0] for r in rows if r[1] is None}
    tombstone_ids = {r[0] for r in rows if r[1] is not None}
    to_tombstone = list(live_ids - desired)
    to_restore = list(desired & tombstone_ids)
    writer: StateWriter = store._writer
    with writer.playlist_transaction():
        if to_tombstone:
            writer.tombstone_playlist_memberships(snap.playlist_id, to_tombstone)
        if to_restore:
            writer.restore_playlist_memberships(snap.playlist_id, to_restore)
        if snap.members is not None:
            live_by_id = {
                m.item_id: m
                for m in _load_live_members(store._conn, snap.playlist_id)
            }
            key_updates: list[tuple[str, str]] = []
            for member in snap.members:
                item_id = member["item_id"]
                desired_key = member["order_key"]
                live = live_by_id.get(item_id)
                if live is None or live.order_key == desired_key:
                    continue
                key_updates.append((item_id, desired_key))
            if key_updates:
                writer.update_playlist_membership_order_keys(
                    snap.playlist_id, key_updates,
                )
    return store._load(snap.playlist_id)


def remove_memberships(
    store: PlaylistStore,
    playlist_id: str,
    item_ids: list[str],
    *,
    record_edit: bool = True,
) -> PlaylistRow:
    """Tombstone membership rows without rewriting neighbors."""
    writer: StateWriter = store._writer
    conn: sqlite3.Connection = store._conn

    try:
        before_row = store._load(playlist_id)
    except NotFoundError:
        if _is_smartlist(conn, playlist_id):
            raise SmartlistImmutableError(
                "cannot remove tracks from a smartlist",
            ) from None
        raise

    _reject_over_cap(item_ids)

    members = _load_live_members(conn, playlist_id)
    live_by_id = {m.item_id: m for m in members}
    for item_id in item_ids:
        if item_id not in live_by_id:
            raise NotFoundError(
                f"playlist {playlist_id} has no membership {item_id}",
            )

    before_snap = _snapshot_with_members(store, before_row) if record_edit else None

    with writer.playlist_transaction():
        writer.tombstone_playlist_memberships(playlist_id, item_ids)

    new_row = store._load(playlist_id)
    if record_edit:
        store._record_edit(
            "memberships", playlist_id,
            before_snap, _snapshot_with_members(store, new_row),
        )
    return new_row


def remove_membership(
    store: PlaylistStore,
    playlist_id: str,
    item_id: str,
    *,
    record_edit: bool = True,
) -> PlaylistRow:
    """Tombstone one membership row without rewriting neighbors."""
    return remove_memberships(
        store, playlist_id, [item_id], record_edit=record_edit,
    )


__all__ = ["apply_membership_snapshot", "remove_membership", "remove_memberships"]
