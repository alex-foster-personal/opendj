"""CRUD for playlist sets (SET-05).

A playlist set is a performance object with its own name, entry snapshot,
and play count. It is NOT a PLAY-01 play-order and NOT a SET-01 recorded
session.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from apps.shared.play_orders import DEFAULT_ORDER_NAME, load_play_order

from .events import emit_playlist_set_changed
from .schema import apply_playlist_set_migrations

RunKind = Literal["practice", "performance"]


@dataclass(slots=True)
class PlaylistSetEntry:
    stable_id: str
    position: int


@dataclass(slots=True)
class PlaylistSetRun:
    id: int
    kind: RunKind
    created_at: str


@dataclass(slots=True)
class PlaylistSet:
    id: int
    playlist_id: str
    name: str
    play_count: int
    entries: list[PlaylistSetEntry] = field(default_factory=list)
    runs: list[PlaylistSetRun] = field(default_factory=list)
    source_play_order_id: int | None = None
    created_at: str = ""
    updated_at: str = ""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("set name must be a non-empty string")
    return trimmed


def _snapshot_membership(
    conn: sqlite3.Connection, playlist_id: str
) -> list[tuple[str, int]]:
    try:
        rows = conn.execute(
            "SELECT stable_id, position FROM playlist_memberships "
            "WHERE playlist_id=? AND deleted_at IS NULL ORDER BY position ASC",
            (playlist_id,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            # A schema mismatch (e.g. a pre-migration DB missing deleted_at)
            # must fail loudly, not be swallowed into an empty snapshot --
            # only a genuinely absent playlist_memberships table is expected.
            raise
        return []
    return [(str(r[0]), int(r[1])) for r in rows]


def _snapshot_play_order(
    conn: sqlite3.Connection, playlist_id: str, play_order_name: str
) -> tuple[list[tuple[str, int]], int | None]:
    po = load_play_order(conn, playlist_id, play_order_name)
    if po.id is None:
        raise LookupError(
            f"play-order (playlist_id={playlist_id!r}, "
            f"name={play_order_name!r}) does not exist"
        )
    entries = [(e.stable_id, e.position) for e in po.entries]
    return entries, po.id


def _insert_entries(
    conn: sqlite3.Connection, set_id: int, entries: list[tuple[str, int]]
) -> None:
    for stable_id, position in entries:
        conn.execute(
            "INSERT INTO playlist_set_entries(set_id, stable_id, position) "
            "VALUES (?, ?, ?)",
            (set_id, stable_id, position),
        )


def create_playlist_set(
    conn: sqlite3.Connection,
    playlist_id: str,
    name: str,
    *,
    from_play_order: str | None = None,
) -> int:
    """Create a set with a snapshot of playlist membership or play-order."""
    if not playlist_id:
        raise ValueError("playlist_id must be a non-empty string")
    name = _normalize_name(name)

    apply_playlist_set_migrations(conn)
    now = _now_iso()

    source_play_order_id: int | None = None
    if from_play_order and from_play_order != DEFAULT_ORDER_NAME:
        entries, source_play_order_id = _snapshot_play_order(
            conn, playlist_id, from_play_order
        )
    else:
        entries = _snapshot_membership(conn, playlist_id)

    cur = conn.execute(
        "INSERT INTO playlist_sets"
        "(playlist_id, name, play_count, source_play_order_id, "
        " created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (playlist_id, name, source_play_order_id, now, now),
    )
    set_id = int(cur.lastrowid)
    _insert_entries(conn, set_id, entries)
    emit_playlist_set_changed(
        conn, playlist_id=playlist_id, set_id=set_id, action="created"
    )
    return set_id


def _load_entries(conn: sqlite3.Connection, set_id: int) -> list[PlaylistSetEntry]:
    rows = conn.execute(
        "SELECT stable_id, position FROM playlist_set_entries "
        "WHERE set_id=? ORDER BY position ASC",
        (set_id,),
    ).fetchall()
    return [
        PlaylistSetEntry(stable_id=str(r[0]), position=int(r[1])) for r in rows
    ]


def _load_runs(conn: sqlite3.Connection, set_id: int) -> list[PlaylistSetRun]:
    rows = conn.execute(
        "SELECT id, kind, created_at FROM playlist_set_runs "
        "WHERE set_id=? ORDER BY id ASC",
        (set_id,),
    ).fetchall()
    return [
        PlaylistSetRun(id=int(r[0]), kind=str(r[1]), created_at=str(r[2]))
        for r in rows
    ]


def _row_to_set(
    row: tuple, entries: list[PlaylistSetEntry], runs: list[PlaylistSetRun]
) -> PlaylistSet:
    set_id, playlist_id, name, play_count, source_po_id, created_at, updated_at = row
    return PlaylistSet(
        id=int(set_id),
        playlist_id=str(playlist_id),
        name=str(name),
        play_count=int(play_count),
        entries=entries,
        runs=runs,
        source_play_order_id=(
            None if source_po_id is None else int(source_po_id)
        ),
        created_at=str(created_at),
        updated_at=str(updated_at),
    )


def load_playlist_set(
    conn: sqlite3.Connection, set_id: int, *, include_runs: bool = False
) -> PlaylistSet:
    apply_playlist_set_migrations(conn)
    row = conn.execute(
        "SELECT id, playlist_id, name, play_count, source_play_order_id, "
        "       created_at, updated_at "
        "FROM playlist_sets WHERE id=?",
        (set_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"playlist set {set_id} not found")
    entries = _load_entries(conn, set_id)
    runs = _load_runs(conn, set_id) if include_runs else []
    return _row_to_set(row, entries, runs)


def list_playlist_sets(conn: sqlite3.Connection, playlist_id: str) -> list[PlaylistSet]:
    apply_playlist_set_migrations(conn)
    rows = conn.execute(
        "SELECT id, playlist_id, name, play_count, source_play_order_id, "
        "       created_at, updated_at "
        "FROM playlist_sets WHERE playlist_id=? ORDER BY name ASC",
        (playlist_id,),
    ).fetchall()
    result: list[PlaylistSet] = []
    for row in rows:
        set_id = int(row[0])
        entries = _load_entries(conn, set_id)
        result.append(_row_to_set(row, entries, []))
    return result


def record_run(conn: sqlite3.Connection, set_id: int, kind: RunKind) -> int:
    """Record a practice or performance run; increment play_count on perform."""
    if kind not in ("practice", "performance"):
        raise ValueError(f"invalid run kind: {kind!r}")

    apply_playlist_set_migrations(conn)
    ps = load_playlist_set(conn, set_id)
    now = _now_iso()
    conn.execute(
        "INSERT INTO playlist_set_runs(set_id, kind, created_at) VALUES (?, ?, ?)",
        (set_id, kind, now),
    )
    if kind == "performance":
        conn.execute(
            "UPDATE playlist_sets SET play_count = play_count + 1, "
            "updated_at=? WHERE id=?",
            (now, set_id),
        )
        action = "performed"
    else:
        conn.execute(
            "UPDATE playlist_sets SET updated_at=? WHERE id=?",
            (now, set_id),
        )
        action = "practiced"
    emit_playlist_set_changed(
        conn,
        playlist_id=ps.playlist_id,
        set_id=set_id,
        action=action,
    )
    updated = load_playlist_set(conn, set_id)
    return updated.play_count
