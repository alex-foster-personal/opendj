"""O(1) playlist membership insert (LIBM-20 ``:add`` algorithm)."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.shared.state.order_key import between
from apps.shared.state.writer import StateWriter

from .backend import BackendError, NotFoundError
from .playlist_dupes import new_stable_ids

if TYPE_CHECKING:
    from .playlist_store import PlaylistRow, PlaylistStore

MEMBERSHIP_BATCH_LIMIT = 1000

MEMBERSHIP_ORDER_BY = (
    "COALESCE(order_key, printf('%08d', position)), position"
)


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
        super().__init__(
            f"track {stable_id} is already a member of playlist {playlist_id}"
        )


class BulkLimitError(BackendError):
    def __init__(self, got: int) -> None:
        super().__init__(
            f"batch exceeds limit of {MEMBERSHIP_BATCH_LIMIT} items (got {got})"
        )


class SmartlistImmutableError(BackendError):
    pass


def _is_smartlist(conn: sqlite3.Connection, playlist_id: str) -> bool:
    if not _table_exists(conn, "smartlists"):
        return False
    row = conn.execute(
        "SELECT 1 FROM smartlists WHERE id = ?", (playlist_id,),
    ).fetchone()
    return row is not None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    return row is not None


def _load_live_members(
    conn: sqlite3.Connection, playlist_id: str,
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
            order_key=row[3] or f"{row[2]:08d}",
            updated_at=row[4],
            origin_device_id=row[5],
            deleted_at=row[6],
        )
        for row in rows
    ]


def _reject_over_cap(items: list) -> None:
    if len(items) > MEMBERSHIP_BATCH_LIMIT:
        raise BulkLimitError(len(items))


def _insert_index(members: list[MembershipRow], position: int | None) -> int:
    insert_index = len(members) if position is None else position
    if insert_index < 0 or insert_index > len(members):
        raise BackendError(
            f"position out of range: {insert_index} (playlist has "
            f"{len(members)} live members)",
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
    """Insert membership rows without rewriting existing neighbors."""
    writer: StateWriter = store._writer
    conn: sqlite3.Connection = store._conn

    try:
        before_row = store._load(playlist_id)
    except NotFoundError:
        if _is_smartlist(conn, playlist_id):
            raise SmartlistImmutableError(
                "cannot add tracks to a smartlist",
            ) from None
        raise

    store._require_known_tracks(stable_ids)
    _reject_over_cap(stable_ids)

    if before_row.forbid_duplicates:
        stable_ids = new_stable_ids(before_row.items, stable_ids)
        if not stable_ids:
            return before_row

    members = _load_live_members(conn, playlist_id)
    insert_index = _insert_index(members, position)
    left_key = members[insert_index - 1].order_key if insert_index > 0 else None
    right_key = (
        members[insert_index].order_key if insert_index < len(members) else None
    )
    insert_rows = _membership_insert_rows(stable_ids, left_key, right_key)

    before_snap = store._snapshot(before_row) if record_edit else None

    with writer.playlist_transaction():
        writer.insert_playlist_memberships(playlist_id, insert_rows)

    new_row = store._load(playlist_id)
    if record_edit:
        store._record_edit(
            "memberships", playlist_id, before_snap, store._snapshot(new_row),
        )
    return new_row


__all__ = [
    "AlreadyExistsError",
    "BulkLimitError",
    "MEMBERSHIP_BATCH_LIMIT",
    "MEMBERSHIP_ORDER_BY",
    "MembershipRow",
    "SmartlistImmutableError",
    "_is_smartlist",
    "_load_live_members",
    "_reject_over_cap",
    "add_memberships",
]
