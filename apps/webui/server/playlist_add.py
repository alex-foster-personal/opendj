"""Constant-time playlist membership insert (LIBM-20 ``:add``, LIBM-132).

An add never reads the whole membership (#3963): at 10,042 members the one
full ordered read, the response ``items`` and the undo snapshots cost 246 ms
against 5.3 ms at 50 members. Each read here is bounded by the request, not
the playlist:

* neighbors: ``DESC LIMIT 1`` for an append, ``LIMIT 2 OFFSET position - 1``
  for an insert, both seeks on ``idx_playlist_memberships_live_order``
  (migration v22). An insert walks ``position`` index entries in C and
  materializes at most two rows.
* duplicates (``forbid_duplicates``): one ``IN`` read of the requested ids
  on ``idx_playlist_memberships_live_stable_id``.
* response: the header plus the inserted rows (:class:`AddResult`), never
  the membership.
* undo: an ``add_items`` history command that records the inserted rows, so
  undo tombstones exactly those rows and redo restores them (ADR-NEW
  playlist-add-constant-time).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.shared.state.order_key import PrecisionExhausted, allocate_keys, renumbered_keys
from apps.shared.state.writer import StateWriter

from .backend import BackendError, NotFoundError
from .playlist_dupes import new_stable_ids

if TYPE_CHECKING:
    from .playlist_history import PlaylistEditCommand
    from .playlist_store import PlaylistRow, PlaylistStore

MEMBERSHIP_BATCH_LIMIT = 1000

MEMBERSHIP_ORDER_KEY = "COALESCE(order_key, printf('%08d', position))"
"""Effective order key; a legacy row with no order_key sorts by its padded position."""

MEMBERSHIP_ORDER_BY = f"{MEMBERSHIP_ORDER_KEY}, position"
"""Must match ``idx_playlist_memberships_live_order`` (migrations_v22) exactly."""

ADD_ITEMS_OP = "add_items"


@dataclass
class MembershipRow:
    item_id: str
    stable_id: str
    position: int
    order_key: str
    updated_at: str
    origin_device_id: str | None
    deleted_at: str | None


@dataclass(frozen=True)
class AddedMember:
    item_id: str
    stable_id: str
    order_key: str

    def to_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "stable_id": self.stable_id, "order_key": self.order_key}


@dataclass
class AddResult:
    """The playlist header (``items`` left EMPTY) plus the rows this add inserted."""

    row: PlaylistRow
    added: list[AddedMember]


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


# ---------------------------------------------------------------------------
# shared membership helpers (remove / move import these)


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
    """A legacy row with no order_key sorts by its zero-padded position.

    Only NULL falls back, exactly as SQL's COALESCE does: an empty key sorts
    first there, so treating it as missing here would misplace inserts.
    """
    return order_key if order_key is not None else f"{position:08d}"


def _reject_over_cap(items: list) -> None:
    if len(items) > MEMBERSHIP_BATCH_LIMIT:
        raise BulkLimitError(len(items))


# ---------------------------------------------------------------------------
# bounded reads


def _live_member_count(conn: sqlite3.Connection, playlist_id: str) -> int:
    """O(members); only the out-of-range error message pays for it."""
    return conn.execute(
        "SELECT COUNT(*) FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL",
        (playlist_id,),
    ).fetchone()[0]


def _neighbor_order_keys(
    conn: sqlite3.Connection,
    playlist_id: str,
    position: int | None,
) -> tuple[str | None, str | None]:
    """Order keys either side of the insert point, from at most two index rows."""
    select = (
        f"SELECT {MEMBERSHIP_ORDER_KEY} FROM playlist_memberships "
        f"WHERE playlist_id = ? AND deleted_at IS NULL "
    )
    if position is None:
        last = conn.execute(
            select + f"ORDER BY {MEMBERSHIP_ORDER_KEY} DESC, position DESC LIMIT 1",
            (playlist_id,),
        ).fetchone()
        return (None if last is None else last[0]), None
    if position == 0:
        first = conn.execute(
            select + f"ORDER BY {MEMBERSHIP_ORDER_BY} LIMIT 1",
            (playlist_id,),
        ).fetchone()
        return None, (None if first is None else first[0])
    if position > 0:
        pair = conn.execute(
            select + f"ORDER BY {MEMBERSHIP_ORDER_BY} LIMIT 2 OFFSET ?",
            (playlist_id, position - 1),
        ).fetchall()
        if not pair:
            raise BackendError(
                f"position out of range: {position} (playlist has "
                f"{_live_member_count(conn, playlist_id)} live members)",
            )
        return pair[0][0], (pair[1][0] if len(pair) == 2 else None)
    raise BackendError(f"position out of range: {position}")


def _already_present(
    conn: sqlite3.Connection,
    playlist_id: str,
    stable_ids: list[str],
) -> list[str]:
    """The requested ids that already have a live membership row."""
    unique = list(dict.fromkeys(stable_ids))
    placeholders = ",".join("?" * len(unique))
    return [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT stable_id FROM playlist_memberships "
            f"WHERE playlist_id = ? AND deleted_at IS NULL AND stable_id IN ({placeholders})",
            (playlist_id, *unique),
        )
    ]


def _new_members(stable_ids: list[str], keys: list[str]) -> list[AddedMember]:
    return [
        AddedMember(uuid.uuid4().hex, sid, key)
        for sid, key in zip(stable_ids, keys, strict=True)
    ]


def _renumber_around_insert(
    writer: StateWriter,
    conn: sqlite3.Connection,
    playlist_id: str,
    position: int | None,
    stable_ids: list[str],
) -> list[AddedMember]:
    """Rewrite every live key, leaving a gap for the new rows. O(members).

    Runs only when the gap has no key left (a legacy empty or overlong key, or
    a run of inserts at one point); the rewritten keys then leave room again.
    """
    # By position, the primary key: Spotify-imported rows have no item_id.
    positions = [
        row[0]
        for row in conn.execute(
            f"SELECT position FROM playlist_memberships "
            f"WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY {MEMBERSHIP_ORDER_BY}",
            (playlist_id,),
        )
    ]
    at = len(positions) if position is None else position
    keys = renumbered_keys(len(positions) + len(stable_ids))
    kept = keys[:at] + keys[at + len(stable_ids):]
    writer.renumber_playlist_membership_order_keys(
        playlist_id, list(zip(positions, kept, strict=True)),
    )
    return _new_members(stable_ids, keys[at:at + len(stable_ids)])


# ---------------------------------------------------------------------------
# add


def add_memberships(
    store: PlaylistStore,  # type: ignore[name-defined]
    playlist_id: str,
    stable_ids: list[str],
    *,
    position: int | None = None,
    record_edit: bool = True,
) -> AddResult:
    """Insert membership rows without reading or rewriting existing members.

    Every read and the history record run inside ONE writer transaction with
    the insert, as :meth:`StateWriter.playlist_transaction` requires.
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
            stable_ids = new_stable_ids(_already_present(conn, playlist_id, stable_ids), stable_ids)
            if not stable_ids:
                return AddResult(row=header, added=[])

        left_key, right_key = _neighbor_order_keys(conn, playlist_id, position)
        try:
            added = _new_members(stable_ids, allocate_keys(left_key, right_key, len(stable_ids)))
        except PrecisionExhausted:
            added = _renumber_around_insert(writer, conn, playlist_id, position, stable_ids)
        writer.insert_playlist_memberships(
            playlist_id,
            [(m.item_id, m.stable_id, m.order_key) for m in added],
        )
        new_header = store._load_header(playlist_id)
        if record_edit:
            store._record_add(new_header, [m.to_dict() for m in added])
        return AddResult(row=new_header, added=added)


# ---------------------------------------------------------------------------
# undo / redo of an add_items command: O(inserted rows), never O(members)


def _added_item_ids(command: PlaylistEditCommand) -> list[str]:
    if not command.added:
        raise BackendError(f"{ADD_ITEMS_OP} command {command.command_id} records no added rows")
    return [member["item_id"] for member in command.added]


def _count_added_rows(
    conn: sqlite3.Connection,
    playlist_id: str,
    item_ids: list[str],
    *,
    live: bool,
) -> int:
    placeholders = ",".join("?" * len(item_ids))
    deleted = "IS NULL" if live else "IS NOT NULL"
    return conn.execute(
        f"SELECT COUNT(*) FROM playlist_memberships WHERE playlist_id = ? "
        f"AND deleted_at {deleted} AND item_id IN ({placeholders})",
        (playlist_id, *item_ids),
    ).fetchone()[0]


def undo_add(store: PlaylistStore, command: PlaylistEditCommand) -> PlaylistRow:
    """Tombstone exactly the rows the add inserted; 409 if any is gone or the playlist is."""
    item_ids = _added_item_ids(command)
    with store._writer.playlist_transaction():
        live = store._try_load_header(command.playlist_id)
        if live is None or _count_added_rows(
            store._conn, command.playlist_id, item_ids, live=True,
        ) != len(item_ids):
            store._conflict(store._try_load(command.playlist_id))
        store._writer.tombstone_playlist_memberships(command.playlist_id, item_ids)
        return store._load(command.playlist_id)


def redo_add(store: PlaylistStore, command: PlaylistEditCommand) -> PlaylistRow:
    """Restore exactly the rows undo tombstoned; 409 if one is live or the playlist is gone."""
    item_ids = _added_item_ids(command)
    with store._writer.playlist_transaction():
        live = store._try_load_header(command.playlist_id)
        if live is None or _count_added_rows(
            store._conn, command.playlist_id, item_ids, live=False,
        ) != len(item_ids):
            store._conflict(store._try_load(command.playlist_id))
        store._writer.restore_playlist_memberships(command.playlist_id, item_ids)
        return store._load(command.playlist_id)


__all__ = [
    "ADD_ITEMS_OP",
    "MEMBERSHIP_BATCH_LIMIT",
    "MEMBERSHIP_ORDER_BY",
    "MEMBERSHIP_ORDER_KEY",
    "AddResult",
    "AddedMember",
    "AlreadyExistsError",
    "BulkLimitError",
    "MembershipRow",
    "SmartlistImmutableError",
    "_is_smartlist",
    "_load_live_members",
    "_neighbor_order_keys",
    "_reject_over_cap",
    "add_memberships",
    "redo_add",
    "undo_add",
]
