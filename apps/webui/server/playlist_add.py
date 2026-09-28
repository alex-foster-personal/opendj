"""O(1) playlist membership insert (LIBM-20 ``:add`` algorithm).

An add reads each existing member once (LIBM-129, #3963): the neighbors'
order_keys, duplicate check, response ``items``, and undo snapshots all come
from one ordered read of the whole membership. The response inserts the new
ids into that in-memory order, since an insert between two neighbors reorders
nothing already present.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from apps.shared.state.order_key import between
from apps.shared.state.writer import StateWriter

from .backend import BackendError, NotFoundError
from .playlist_dupes import new_stable_ids

if TYPE_CHECKING:
    from .playlist_store import PlaylistRow, PlaylistStore

MEMBERSHIP_BATCH_LIMIT = 1000

MEMBERSHIP_ORDER_BY = "COALESCE(order_key, printf('%08d', position)), position"


@dataclass
class MembershipRow:
    item_id: str
    stable_id: str
    position: int
    order_key: str
    updated_at: str
    origin_device_id: str | None
    deleted_at: str | None


class AlreadyExistsError(BackendError):
    def __init__(self, stable_id: str, playlist_id: str) -> None:
        self.stable_id = stable_id
        self.playlist_id = playlist_id
        super().__init__(f"track {stable_id} is already a member of playlist {playlist_id}")


class BulkLimitError(BackendError):
    def __init__(self, got: int) -> None:
        super().__init__(f"batch exceeds limit of {MEMBERSHIP_BATCH_LIMIT} items (got {got})")


class SmartlistImmutableError(BackendError):
    pass


def _is_smartlist(conn: sqlite3.Connection, playlist_id: str) -> bool:
    if not _table_exists(conn, "smartlists"):
        return False
    row = conn.execute(
        "SELECT 1 FROM smartlists WHERE id = ?",
        (playlist_id,),
    ).fetchone()
    return row is not None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _load_live_members(
    conn: sqlite3.Connection,
    playlist_id: str,
) -> list[MembershipRow]:
    rows = conn.execute(
        f"SELECT item_id, stable_id, position, order_key, updated_at, "
        f"origin_device_id, deleted_at FROM playlist_memberships "
        f"WHERE playlist_id = ? AND deleted_at IS NULL "
        f"ORDER BY {MEMBERSHIP_ORDER_BY}",
        (playlist_id,),
    ).fetchall()
    return [
        MembershipRow(
            item_id=row[0] or "",
            stable_id=row[1],
            position=row[2],
            order_key=_effective_order_key(row[3], row[2]),
            updated_at=row[4],
            origin_device_id=row[5],
            deleted_at=row[6],
        )
        for row in rows
    ]


def _effective_order_key(order_key: str | None, position: int) -> str:
    """A legacy row with no order_key sorts by its zero-padded position."""
    return order_key or f"{position:08d}"


def _neighbor_order_keys(
    members: list[MembershipRow],
    insert_index: int,
) -> tuple[str | None, str | None]:
    """Order keys around ``insert_index`` from the one full member read."""
    left_member = members[insert_index - 1] if insert_index > 0 else None
    right_member = members[insert_index] if insert_index < len(members) else None
    left = (
        _effective_order_key(left_member.order_key, left_member.position)
        if left_member is not None
        else None
    )
    right = (
        _effective_order_key(right_member.order_key, right_member.position)
        if right_member is not None
        else None
    )
    return left, right


def _reject_over_cap(items: list) -> None:
    if len(items) > MEMBERSHIP_BATCH_LIMIT:
        raise BulkLimitError(len(items))


def _insert_index(member_count: int, position: int | None) -> int:
    insert_index = member_count if position is None else position
    if insert_index < 0 or insert_index > member_count:
        raise BackendError(
            f"position out of range: {insert_index} (playlist has {member_count} live members)",
        )
    return insert_index


def _membership_insert_rows(
    stable_ids: list[str],
    left_key: str | None,
    right_key: str | None,
) -> list[tuple[str, str, str]]:
    insert_rows: list[tuple[str, str, str]] = []
    prev = left_key
    for sid in stable_ids:
        order_key = between(prev, right_key)
        insert_rows.append((uuid.uuid4().hex, sid, order_key))
        prev = order_key
    return insert_rows


def add_memberships(
    store: PlaylistStore,  # type: ignore[name-defined]
    playlist_id: str,
    stable_ids: list[str],
    *,
    position: int | None = None,
    record_edit: bool = True,
) -> PlaylistRow:  # type: ignore[name-defined]
    """Insert membership rows without rewriting existing neighbors.

    The one ordered membership read and the edit record run inside ONE writer
    transaction with the insert, as :meth:`StateWriter.playlist_transaction`
    requires. Duplicate filtering, neighbor selection, and before/after
    snapshots reuse that read and execute no second membership scan.
    """
    writer: StateWriter = store._writer
    conn: sqlite3.Connection = store._conn

    with writer.playlist_transaction():
        try:
            header = store._load_header(playlist_id)
        except NotFoundError:
            if _is_smartlist(conn, playlist_id):
                raise SmartlistImmutableError(
                    "cannot add tracks to a smartlist",
                ) from None
            raise

        store._require_known_tracks(stable_ids)
        _reject_over_cap(stable_ids)
        members = _load_live_members(conn, playlist_id)
        before_items = [member.stable_id for member in members]

        if header.forbid_duplicates:
            stable_ids = new_stable_ids(before_items, stable_ids)
            if not stable_ids:
                header.items = before_items
                return header

        insert_index = _insert_index(len(members), position)
        left_key, right_key = _neighbor_order_keys(members, insert_index)
        insert_rows = _membership_insert_rows(stable_ids, left_key, right_key)
        writer.insert_playlist_memberships(playlist_id, insert_rows)

        after_items = [
            *before_items[:insert_index],
            *stable_ids,
            *before_items[insert_index:],
        ]
        new_row = store._load_header(playlist_id)
        new_row.items = after_items
        if record_edit:
            store._record_edit(
                "memberships",
                playlist_id,
                store._snapshot(replace(header, items=before_items)),
                store._snapshot(new_row),
            )
        return new_row


__all__ = [
    "MEMBERSHIP_BATCH_LIMIT",
    "MEMBERSHIP_ORDER_BY",
    "AlreadyExistsError",
    "BulkLimitError",
    "MembershipRow",
    "SmartlistImmutableError",
    "_is_smartlist",
    "_load_live_members",
    "_neighbor_order_keys",
    "_reject_over_cap",
    "add_memberships",
]
