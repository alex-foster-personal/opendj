"""CRUD for play-orders (PLAY-01).

All helpers take an open ``sqlite3.Connection``. Callers own the
transaction boundary; the helpers rely on the connection's autocommit /
isolation setting to flush writes. Every mutation fires a
``play_order_changed`` event via :mod:`events`.

The virtual ``"default"`` play-order is not persisted -- it is a read-
through alias for the playlist's native ``TrackNo`` sequence, materialised
on demand. :func:`list_play_orders` always returns it in the response.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .events import emit_play_order_changed
from .schema import apply_play_order_migrations
from .validation import (
    validate_key_sync,
    validate_target_key,
    validate_target_tempo,
)

DEFAULT_ORDER_NAME: str = "default"


@dataclass(slots=True)
class PlayOrderEntry:
    """One slot in a named play-order. Mirrors open-dj v0 §4.6."""

    stable_id: str
    position: int
    target_key: str | None = None
    target_tempo: float | None = None
    key_sync: bool | None = None
    transition_hint: str | None = None


@dataclass(slots=True)
class PlayOrder:
    """A complete named ordering plus metadata."""

    id: int | None
    playlist_id: str
    name: str
    entries: list[PlayOrderEntry] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    generated_by: str | None = None
    goal_json: str | None = None
    schema_version: int = 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def create_play_order(
    conn: sqlite3.Connection,
    playlist_id: str,
    name: str,
    *,
    generated_by: str | None = None,
    goal_json: str | None = None,
) -> int:
    """Create an empty play-order; return its autoincrement id.

    The caller-supplied ``name`` must not be the reserved string
    :data:`DEFAULT_ORDER_NAME`; that alias is reserved for the virtual
    read-through order. ``(playlist_id, name)`` must be unique -- the
    underlying UNIQUE index surfaces a :class:`sqlite3.IntegrityError`.
    """
    if name == DEFAULT_ORDER_NAME:
        raise ValueError(
            f"play-order name {name!r} is reserved (use a different name; "
            "the default ordering is virtual and not persisted)."
        )
    if not playlist_id or not name:
        raise ValueError("playlist_id and name must be non-empty strings")

    apply_play_order_migrations(conn)
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO play_orders"
        "(playlist_id, name, created_at, updated_at, generated_by, "
        " goal_json, schema_version) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (playlist_id, name, now, now, generated_by, goal_json),
    )
    po_id = int(cur.lastrowid)
    emit_play_order_changed(
        conn, playlist_id=playlist_id, name=name, action="created"
    )
    return po_id


def add_entry(
    conn: sqlite3.Connection,
    play_order_id: int,
    stable_id: str,
    position: int,
    *,
    target_key: str | None = None,
    target_tempo: float | None = None,
    key_sync: bool | None = None,
    transition_hint: str | None = None,
) -> None:
    """Append one entry to an existing play-order.

    All PLAY-03 override fields are validated here; rejections raise
    ``ValueError`` with the offending field in the message. The parent
    play-order's ``updated_at`` is refreshed and a
    ``play_order_changed`` event is emitted with ``action="updated"``.
    """
    validate_target_key(target_key)
    validate_target_tempo(target_tempo)
    validate_key_sync(key_sync)
    if not isinstance(position, int) or position < 0:
        raise ValueError(
            f"position must be a non-negative int, got {position!r}"
        )
    if not stable_id:
        raise ValueError("stable_id must be a non-empty string")

    conn.execute(
        "INSERT INTO play_order_entries"
        "(play_order_id, stable_id, position, target_key, target_tempo, "
        " key_sync, transition_hint) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            play_order_id,
            stable_id,
            position,
            target_key,
            target_tempo,
            None if key_sync is None else int(bool(key_sync)),
            transition_hint,
        ),
    )
    row = conn.execute(
        "SELECT playlist_id, name FROM play_orders WHERE id=?",
        (play_order_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"play_order_id {play_order_id} not found")
    playlist_id, name = row
    conn.execute(
        "UPDATE play_orders SET updated_at=? WHERE id=?",
        (_now_iso(), play_order_id),
    )
    emit_play_order_changed(
        conn, playlist_id=playlist_id, name=name, action="updated"
    )


def load_play_order(
    conn: sqlite3.Connection, playlist_id: str, name: str
) -> PlayOrder:
    """Fetch a named play-order + its entries (ordered by position).

    When ``name == DEFAULT_ORDER_NAME``, the read-through virtual order
    is materialised from ``playlist_memberships`` if present; otherwise
    an empty ``PlayOrder`` is returned so callers do not have to special-
    case the unconfigured path.
    """
    if name == DEFAULT_ORDER_NAME:
        return _load_default_order(conn, playlist_id)

    apply_play_order_migrations(conn)
    row = conn.execute(
        "SELECT id, created_at, updated_at, generated_by, goal_json, "
        "       schema_version "
        "FROM play_orders WHERE playlist_id=? AND name=?",
        (playlist_id, name),
    ).fetchone()
    if row is None:
        raise LookupError(
            f"play-order (playlist_id={playlist_id!r}, name={name!r}) "
            "does not exist"
        )
    po_id, created_at, updated_at, generated_by, goal_json, schema_version = row
    entry_rows = conn.execute(
        "SELECT stable_id, position, target_key, target_tempo, key_sync, "
        "       transition_hint "
        "FROM play_order_entries WHERE play_order_id=? "
        "ORDER BY position ASC",
        (po_id,),
    ).fetchall()
    entries = [
        PlayOrderEntry(
            stable_id=r[0],
            position=int(r[1]),
            target_key=r[2],
            target_tempo=(None if r[3] is None else float(r[3])),
            key_sync=(None if r[4] is None else bool(r[4])),
            transition_hint=r[5],
        )
        for r in entry_rows
    ]
    return PlayOrder(
        id=int(po_id),
        playlist_id=playlist_id,
        name=name,
        entries=entries,
        created_at=created_at,
        updated_at=updated_at,
        generated_by=generated_by,
        goal_json=goal_json,
        schema_version=int(schema_version),
    )


def _load_default_order(
    conn: sqlite3.Connection, playlist_id: str
) -> PlayOrder:
    """Virtual read-through for the native TrackNo sequence.

    Reads ``playlist_memberships`` ordered by ``position``. When the
    table is missing (Phase 5 not yet landed) or the playlist has no
    rows, returns an empty order -- callers can still rely on the
    non-null dataclass.
    """
    entries: list[PlayOrderEntry] = []
    try:
        rows = conn.execute(
            "SELECT stable_id, position FROM playlist_memberships "
            "WHERE playlist_id=? ORDER BY position ASC",
            (playlist_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for stable_id, position in rows:
        entries.append(
            PlayOrderEntry(stable_id=stable_id, position=int(position))
        )
    return PlayOrder(
        id=None,
        playlist_id=playlist_id,
        name=DEFAULT_ORDER_NAME,
        entries=entries,
        generated_by=None,
        goal_json=None,
    )


def list_play_orders(
    conn: sqlite3.Connection, playlist_id: str
) -> list[str]:
    """Return names of all play-orders for ``playlist_id``, starting with
    the virtual ``"default"`` alias.
    """
    apply_play_order_migrations(conn)
    rows = conn.execute(
        "SELECT name FROM play_orders WHERE playlist_id=? ORDER BY name ASC",
        (playlist_id,),
    ).fetchall()
    return [DEFAULT_ORDER_NAME, *[r[0] for r in rows]]


def delete_play_order(
    conn: sqlite3.Connection, playlist_id: str, name: str
) -> None:
    """Delete a named play-order + its entries (CASCADE).

    Deleting the virtual ``DEFAULT_ORDER_NAME`` is a no-op (raises no
    error: the default is not persisted).
    """
    if name == DEFAULT_ORDER_NAME:
        return
    apply_play_order_migrations(conn)
    cur = conn.execute(
        "DELETE FROM play_orders WHERE playlist_id=? AND name=?",
        (playlist_id, name),
    )
    if cur.rowcount > 0:
        emit_play_order_changed(
            conn, playlist_id=playlist_id, name=name, action="deleted"
        )
