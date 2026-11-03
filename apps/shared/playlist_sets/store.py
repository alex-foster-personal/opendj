"""CRUD for playlist sets (SET-05).

A playlist set is a performance object with its own name, entry snapshot,
and play count. It is NOT a PLAY-01 play-order and NOT a SET-01 recorded
session.

Since SET-6586 a set may instead name a route: an ordered list of components
(``components.py``). Its entries then resolve live from those components, and
every run freezes what it resolved to, so history stays exact after an edit.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from apps.shared.play_orders import DEFAULT_ORDER_NAME, load_play_order

from .components import (
    find_set_for_route,
    load_component,
    resolve_parts,
    set_parts,
    validate_parts,
)
from .events import emit_playlist_set_changed
from .schema import apply_playlist_set_migrations, write_unit

RunKind = Literal["practice", "performance"]

_VALID_RUN_KINDS = frozenset({"practice", "performance"})

_PLAYLIST_SET_ROW_SELECT = (
    "SELECT id, playlist_id, name, play_count, source_play_order_id, "
    "       created_at, updated_at "
)


def _stable_position_pairs(rows: list[sqlite3.Row] | list[tuple]) -> list[tuple[str, int]]:
    return [(str(r[0]), int(r[1])) for r in rows]


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
    parts: list[int] = field(default_factory=list)
    """Component ids of this set's route (SET-6586); empty for a v1 snapshot set."""
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
    return _stable_position_pairs(rows)


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


def _insert_parts(conn: sqlite3.Connection, set_id: int, parts: list[int]) -> None:
    conn.executemany(
        "INSERT INTO playlist_set_parts(set_id, position, component_id) "
        "VALUES (?, ?, ?)",
        [(set_id, i, cid) for i, cid in enumerate(parts)],
    )


def create_playlist_set(
    conn: sqlite3.Connection,
    playlist_id: str,
    name: str,
    *,
    from_play_order: str | None = None,
    parts: Sequence[int] | None = None,
) -> int:
    """Create a set with a snapshot of playlist membership or play-order,
    or (SET-6586) with a route of components whose tracks resolve live."""
    if not playlist_id:
        raise ValueError("playlist_id must be a non-empty string")
    name = _normalize_name(name)
    if parts is not None and from_play_order:
        raise ValueError("give either parts or from_play_order, not both")

    apply_playlist_set_migrations(conn)
    now = _now_iso()

    with write_unit(conn):
        source_play_order_id: int | None = None
        route: list[int] = []
        if parts is not None:
            route = validate_parts(conn, playlist_id, parts)
            _refuse_saved_route(conn, playlist_id, route, None)
            entries: list[tuple[str, int]] = []
        elif from_play_order and from_play_order != DEFAULT_ORDER_NAME:
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
        _insert_parts(conn, set_id, route)
        emit_playlist_set_changed(
            conn, playlist_id=playlist_id, set_id=set_id, action="created"
        )
    return set_id


def _load_entries(conn: sqlite3.Connection, set_id: int) -> list[PlaylistSetEntry]:
    parts = set_parts(conn, set_id)
    if parts:
        return [
            PlaylistSetEntry(stable_id=sid, position=i)
            for i, (sid, _cid) in enumerate(resolve_parts(conn, parts))
        ]
    rows = conn.execute(
        "SELECT stable_id, position FROM playlist_set_entries "
        "WHERE set_id=? ORDER BY position ASC",
        (set_id,),
    ).fetchall()
    return [
        PlaylistSetEntry(stable_id=sid, position=pos)
        for sid, pos in _stable_position_pairs(rows)
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
    row: tuple,
    entries: list[PlaylistSetEntry],
    runs: list[PlaylistSetRun],
    parts: list[int],
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
        parts=parts,
        created_at=str(created_at),
        updated_at=str(updated_at),
    )


def load_playlist_set(
    conn: sqlite3.Connection, set_id: int, *, include_runs: bool = False
) -> PlaylistSet:
    apply_playlist_set_migrations(conn)
    row = conn.execute(
        f"{_PLAYLIST_SET_ROW_SELECT}FROM playlist_sets WHERE id=?",
        (set_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"playlist set {set_id} not found")
    entries = _load_entries(conn, set_id)
    runs = _load_runs(conn, set_id) if include_runs else []
    return _row_to_set(row, entries, runs, set_parts(conn, set_id))


def list_playlist_sets(conn: sqlite3.Connection, playlist_id: str) -> list[PlaylistSet]:
    apply_playlist_set_migrations(conn)
    rows = conn.execute(
        f"{_PLAYLIST_SET_ROW_SELECT}FROM playlist_sets WHERE playlist_id=? "
        "ORDER BY name ASC",
        (playlist_id,),
    ).fetchall()
    result: list[PlaylistSet] = []
    for row in rows:
        set_id = int(row[0])
        entries = _load_entries(conn, set_id)
        result.append(_row_to_set(row, entries, [], set_parts(conn, set_id)))
    return result


def _freeze_unfrozen_runs(conn: sqlite3.Connection, set_id: int) -> None:
    """Give runs recorded before SET-6586 their frozen entries.

    Such runs have no ``playlist_set_run_entries``; the v1 snapshot is the only
    record of what they played, so it is copied onto them before it goes.
    """
    conn.execute(
        "INSERT INTO playlist_set_run_entries(run_id, position, stable_id, component_id) "
        "SELECT r.id, e.position, e.stable_id, NULL "
        "FROM playlist_set_runs r JOIN playlist_set_entries e ON e.set_id = r.set_id "
        "WHERE r.set_id=? AND NOT EXISTS ("
        "  SELECT 1 FROM playlist_set_run_entries x WHERE x.run_id = r.id)",
        (set_id,),
    )


def update_set_parts(
    conn: sqlite3.Connection, set_id: int, parts: Sequence[int]
) -> PlaylistSet:
    """Replace a set's route (SET-6586). Past runs keep what they played."""
    with write_unit(conn):
        ps = load_playlist_set(conn, set_id)
        route = validate_parts(conn, ps.playlist_id, parts)
        _refuse_saved_route(conn, ps.playlist_id, route, set_id)
        _freeze_unfrozen_runs(conn, set_id)
        # The route now decides what the set plays, so a v1 snapshot is dead.
        conn.execute("DELETE FROM playlist_set_entries WHERE set_id=?", (set_id,))
        conn.execute("DELETE FROM playlist_set_parts WHERE set_id=?", (set_id,))
        _insert_parts(conn, set_id, route)
        conn.execute(
            "UPDATE playlist_sets SET updated_at=? WHERE id=?", (_now_iso(), set_id)
        )
    return load_playlist_set(conn, set_id)


def _check_live_pick(conn: sqlite3.Connection, ps: PlaylistSet, played: list[int]) -> None:
    """A live pick swaps components within the set's own slots, nothing else."""
    if not ps.parts:
        raise ValueError(
            f"set {ps.id} has no route, so there is no fork to pick at; "
            "record it without played parts"
        )
    route_slots = [load_component(conn, cid).slot for cid in ps.parts]
    played_slots = [load_component(conn, cid).slot for cid in played]
    if played_slots != route_slots:
        raise ValueError(
            f"played parts must fill set {ps.id}'s slots {route_slots}, "
            f"one component each; got {played_slots}"
        )


def _refuse_saved_route(
    conn: sqlite3.Connection, playlist_id: str, route: list[int], set_id: int | None
) -> None:
    existing = find_set_for_route(conn, playlist_id, route)
    if existing is not None and existing != set_id:
        raise ValueError(f"set {existing} on this playlist already plays that route")


def _played_tracks(
    conn: sqlite3.Connection, ps: PlaylistSet, played_parts: Sequence[int] | None
) -> list[tuple[str, int | None]]:
    if played_parts is not None:
        played = validate_parts(conn, ps.playlist_id, played_parts)
        _check_live_pick(conn, ps, played)
        return list(resolve_parts(conn, played))
    if ps.parts:
        return list(resolve_parts(conn, ps.parts))
    return [(e.stable_id, None) for e in ps.entries]


def load_run_entries(
    conn: sqlite3.Connection, run_id: int
) -> list[tuple[str, int | None]]:
    """What a run played, frozen at record time: (stable_id, component_id)."""
    apply_playlist_set_migrations(conn)
    return [
        (str(r[0]), None if r[1] is None else int(r[1]))
        for r in conn.execute(
            "SELECT stable_id, component_id FROM playlist_set_run_entries "
            "WHERE run_id=? ORDER BY position ASC",
            (run_id,),
        )
    ]


def record_run(
    conn: sqlite3.Connection,
    set_id: int,
    kind: RunKind,
    *,
    played_parts: Sequence[int] | None = None,
) -> int:
    """Record a practice or performance run; increment play_count on perform.

    The run freezes the tracks it played. ``played_parts`` names the route
    actually played when it differs from the set's own (an ending picked live).
    """
    if kind not in _VALID_RUN_KINDS:
        raise ValueError(f"invalid run kind: {kind!r}")

    apply_playlist_set_migrations(conn)
    now = _now_iso()
    with write_unit(conn):
        ps = load_playlist_set(conn, set_id)
        played = _played_tracks(conn, ps, played_parts)
        cur = conn.execute(
            "INSERT INTO playlist_set_runs(set_id, kind, created_at) VALUES (?, ?, ?)",
            (set_id, kind, now),
        )
        if cur.lastrowid is None:
            raise RuntimeError("run insert returned no row id")
        run_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO playlist_set_run_entries(run_id, position, stable_id, component_id) "
            "VALUES (?, ?, ?, ?)",
            [(run_id, i, sid, cid) for i, (sid, cid) in enumerate(played)],
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
