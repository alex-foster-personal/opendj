"""O(1) playlist membership insert (LIBM-20 ``:add`` algorithm).

An add reads each existing member once (LIBM-129, #3963): the neighbors'
order_keys and the duplicate check are bounded queries, and the single
ordered read of the whole membership is the one the response ``items`` and
the undo snapshots need. The before-snapshot is that same read minus the rows
this call inserted, since an insert between two neighbors reorders nothing.
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

MEMBERSHIP_ORDER_BY = (
    "COALESCE(order_key, printf('%08d', position)), position"
)
_MEMBERSHIP_ORDER_BY_DESC = (
    "COALESCE(order_key, printf('%08d', position)) DESC, position DESC"
)
_LIVE_MEMBERS_OF = (
    "FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL"
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


def _live_member_count(conn: sqlite3.Connection, playlist_id: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) {_LIVE_MEMBERS_OF}", (playlist_id,)).fetchone()[0])


def _neighbor_order_keys(
    conn: sqlite3.Connection, playlist_id: str, insert_index: int, member_count: int,
) -> tuple[str | None, str | None]:
    """Order keys either side of ``insert_index`` (None past an end).

    At most two rows reach Python, where :func:`_load_live_members` used to
    bring every member back just to index into the list twice.
    """
    if member_count == 0:
        return None, None
    if insert_index == member_count:
        order_key, position = conn.execute(
            f"SELECT order_key, position {_LIVE_MEMBERS_OF} "
            f"ORDER BY {_MEMBERSHIP_ORDER_BY_DESC} LIMIT 1",
            (playlist_id,),
        ).fetchone()
        return _effective_order_key(order_key, position), None
    first = max(insert_index - 1, 0)
    keys = [
        _effective_order_key(order_key, position)
        for order_key, position in conn.execute(
            f"SELECT order_key, position {_LIVE_MEMBERS_OF} "
            f"ORDER BY {MEMBERSHIP_ORDER_BY} LIMIT ? OFFSET ?",
            (playlist_id, insert_index + 1 - first, first),
        )
    ]
    if insert_index == 0:
        return None, keys[0]
    return keys[0], keys[1]


#: Ids bound per duplicate-check statement; under SQLite's oldest 999 cap.
_PRESENT_BATCH = 500


def _members_already_present(
    conn: sqlite3.Connection, playlist_id: str, stable_ids: list[str],
) -> list[str]:
    """The requested ids that are already live members (duplicate policy)."""
    present: list[str] = []
    unique = list(dict.fromkeys(stable_ids))
    for start in range(0, len(unique), _PRESENT_BATCH):
        batch = unique[start : start + _PRESENT_BATCH]
        placeholders = ",".join("?" * len(batch))
        present.extend(
            row[0] for row in conn.execute(
                f"SELECT DISTINCT stable_id {_LIVE_MEMBERS_OF} "
                f"AND stable_id IN ({placeholders})",
                (playlist_id, *batch),
            )
        )
    return present


def _reject_over_cap(items: list) -> None:
    if len(items) > MEMBERSHIP_BATCH_LIMIT:
        raise BulkLimitError(len(items))


def _insert_index(member_count: int, position: int | None) -> int:
    insert_index = member_count if position is None else position
    if insert_index < 0 or insert_index > member_count:
        raise BackendError(
            f"position out of range: {insert_index} (playlist has "
            f"{member_count} live members)",
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


def _items_after_and_before(
    conn: sqlite3.Connection, playlist_id: str, inserted_item_ids: set[str],
) -> tuple[list[str], list[str]]:
    """The ONE full ordered membership read: items after the insert, and the
    items before it (the same list without the rows this call inserted)."""
    after: list[str] = []
    before: list[str] = []
    for item_id, stable_id in conn.execute(
        f"SELECT item_id, stable_id {_LIVE_MEMBERS_OF} ORDER BY {MEMBERSHIP_ORDER_BY}",
        (playlist_id,),
    ):
        after.append(stable_id)
        if item_id not in inserted_item_ids:
            before.append(stable_id)
    return after, before


def add_memberships(
    store: PlaylistStore,  # type: ignore[name-defined]
    playlist_id: str,
    stable_ids: list[str],
    *,
    position: int | None = None,
    record_edit: bool = True,
) -> PlaylistRow:  # type: ignore[name-defined]
    """Insert membership rows without rewriting existing neighbors.

    Every membership read (the duplicate check, the count, the neighbor
    lookup, the response / undo read) and the edit record run inside ONE
    writer transaction with the insert, as :meth:`StateWriter.playlist_transaction`
    requires: the count and the neighbor lookup are separate statements, so a
    concurrent remove landing between them would otherwise break the insert.
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

        if header.forbid_duplicates:
            stable_ids = new_stable_ids(
                _members_already_present(conn, playlist_id, stable_ids), stable_ids,
            )
            if not stable_ids:
                return store._load(playlist_id)

        member_count = _live_member_count(conn, playlist_id)
        insert_index = _insert_index(member_count, position)
        left_key, right_key = _neighbor_order_keys(conn, playlist_id, insert_index, member_count)
        insert_rows = _membership_insert_rows(stable_ids, left_key, right_key)
        writer.insert_playlist_memberships(playlist_id, insert_rows)

        after_items, before_items = _items_after_and_before(
            conn, playlist_id, {item_id for item_id, _sid, _key in insert_rows},
        )
        new_row = store._load_header(playlist_id)
        new_row.items = after_items
        if record_edit:
            store._record_edit(
                "memberships", playlist_id,
                store._snapshot(replace(header, items=before_items)),
                store._snapshot(new_row),
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
    "_neighbor_order_keys",
    "_reject_over_cap",
    "add_memberships",
]
