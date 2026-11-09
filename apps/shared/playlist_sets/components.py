"""Set components and routes (SET-6586).

A component is a named, ordered run of tracks inside a playlist, with a slot
(opener, main, ending...) and free tags. Components that share a slot are
alternatives, so one playlist can hold several endings. A route is one
component per slot, in slot order; a SET-05 set may name a route as its parts.

Component tags are not MyTag: MyTag labels a track and syncs to rekordbox,
while a component tag labels a run inside one playlist and stays here.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from ._time import _now_iso
from .events import emit_playlist_component_changed
from .schema import apply_playlist_set_migrations, write_unit

DEFAULT_SLOTS: tuple[str, ...] = ("opener", "build", "main", "peak", "ending", "encore")
"""The slot ladder. Users may add their own slot names; a custom slot is
placed after every slot the playlist already has."""

ROUTE_LIMIT_DEFAULT = 64


class ComponentInUseError(Exception):
    """A component that a set's route still names cannot be deleted."""

    def __init__(self, component_id: int, set_ids: list[int]) -> None:
        self.component_id = component_id
        self.set_ids = set_ids
        super().__init__(f"component {component_id} is used by sets {set_ids}; remove it from those sets first")


@dataclass(slots=True)
class Component:
    id: int
    playlist_id: str
    name: str
    slot: str
    stable_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Route:
    parts: list[int]
    stable_ids: list[str]
    set_id: int | None
    repeats: list[str] = field(default_factory=list)
    """stable_ids that appear more than once on this route."""


@dataclass(slots=True)
class RouteListing:
    routes: list[Route]
    total: int
    truncated: bool


def _clean(value: str, what: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{what} must be a non-empty string")
    return trimmed


def _clean_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        t = _clean(tag, "tag").lstrip("#")
        if not t:
            raise ValueError("tag must be a non-empty string")
        if t not in out:
            out.append(t)
    return out


def _clean_tracks(stable_ids: Sequence[str]) -> list[str]:
    tracks = [_clean(s, "stable_id") for s in stable_ids]
    if not tracks:
        raise ValueError("a component needs at least one track")
    return tracks


def _ensure_slot(conn: sqlite3.Connection, playlist_id: str, slot: str) -> None:
    if conn.execute(
        "SELECT 1 FROM playlist_component_slots WHERE playlist_id=? AND slot=?",
        (playlist_id, slot),
    ).fetchone():
        return
    if slot in DEFAULT_SLOTS:
        position = DEFAULT_SLOTS.index(slot) * 100
    else:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) FROM playlist_component_slots WHERE playlist_id=?",
            (playlist_id,),
        ).fetchone()
        position = max(int(row[0]) + 1, len(DEFAULT_SLOTS) * 100)
    conn.execute(
        "INSERT INTO playlist_component_slots(playlist_id, slot, position) VALUES (?, ?, ?)",
        (playlist_id, slot, position),
    )


def _write_tracks(conn: sqlite3.Connection, cid: int, tracks: list[str]) -> None:
    conn.execute("DELETE FROM playlist_component_entries WHERE component_id=?", (cid,))
    conn.executemany(
        "INSERT INTO playlist_component_entries(component_id, position, stable_id) VALUES (?, ?, ?)",
        [(cid, i, sid) for i, sid in enumerate(tracks)],
    )


def _write_tags(conn: sqlite3.Connection, cid: int, tags: list[str]) -> None:
    conn.execute("DELETE FROM playlist_component_tags WHERE component_id=?", (cid,))
    conn.executemany(
        "INSERT INTO playlist_component_tags(component_id, tag) VALUES (?, ?)",
        [(cid, t) for t in tags],
    )


def create_component(
    conn: sqlite3.Connection,
    playlist_id: str,
    name: str,
    slot: str,
    stable_ids: Sequence[str],
    *,
    tags: Iterable[str] = (),
) -> int:
    """Create a component; its slot joins the playlist's slot order if new."""
    playlist_id = _clean(playlist_id, "playlist_id")
    name = _clean(name, "component name")
    slot = _clean(slot, "slot").lower()
    tracks = _clean_tracks(stable_ids)
    clean_tags = _clean_tags(tags)

    apply_playlist_set_migrations(conn)
    now = _now_iso()
    with write_unit(conn):
        _ensure_slot(conn, playlist_id, slot)
        cur = conn.execute(
            "INSERT INTO playlist_components(playlist_id, name, slot, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (playlist_id, name, slot, now, now),
        )
        if cur.lastrowid is None:
            raise RuntimeError("component insert returned no row id")
        cid = cur.lastrowid
        _write_tracks(conn, cid, tracks)
        _write_tags(conn, cid, clean_tags)
        emit_playlist_component_changed(conn, playlist_id=playlist_id, component_id=cid, action="component_created")
    return cid


def load_component(conn: sqlite3.Connection, component_id: int) -> Component:
    apply_playlist_set_migrations(conn)
    row = conn.execute(
        "SELECT id, playlist_id, name, slot, created_at, updated_at FROM playlist_components WHERE id=?",
        (component_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"component {component_id} not found")
    return _hydrate(conn, row)


def _hydrate(conn: sqlite3.Connection, row: tuple) -> Component:
    cid = int(row[0])
    tracks = [
        str(r[0])
        for r in conn.execute(
            "SELECT stable_id FROM playlist_component_entries WHERE component_id=? ORDER BY position ASC",
            (cid,),
        )
    ]
    tags = [
        str(r[0])
        for r in conn.execute(
            "SELECT tag FROM playlist_component_tags WHERE component_id=? ORDER BY tag",
            (cid,),
        )
    ]
    return Component(
        id=cid,
        playlist_id=str(row[1]),
        name=str(row[2]),
        slot=str(row[3]),
        stable_ids=tracks,
        tags=tags,
        created_at=str(row[4]),
        updated_at=str(row[5]),
    )


def list_slots(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    """The playlist's slots in route order."""
    apply_playlist_set_migrations(conn)
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT slot FROM playlist_component_slots WHERE playlist_id=? ORDER BY position ASC, slot ASC",
            (playlist_id,),
        )
    ]


def list_components(conn: sqlite3.Connection, playlist_id: str) -> list[Component]:
    """Components in slot order, then in creation order within a slot."""
    apply_playlist_set_migrations(conn)
    rows = conn.execute(
        "SELECT c.id, c.playlist_id, c.name, c.slot, c.created_at, c.updated_at "
        "FROM playlist_components c "
        "LEFT JOIN playlist_component_slots s "
        "  ON s.playlist_id = c.playlist_id AND s.slot = c.slot "
        "WHERE c.playlist_id=? ORDER BY s.position ASC, c.id ASC",
        (playlist_id,),
    ).fetchall()
    return [_hydrate(conn, row) for row in rows]


def borrowing_playlists(conn: sqlite3.Connection, component_id: int) -> list[str]:
    """Every playlist whose saved set borrows this component, by playlist id."""
    apply_playlist_set_migrations(conn)
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT s.playlist_id FROM playlist_set_parts p "
            "JOIN playlist_sets s ON s.id = p.set_id WHERE p.component_id=? "
            "ORDER BY s.playlist_id",
            (component_id,),
        )
    ]


def list_borrowed_components(conn: sqlite3.Connection, playlist_id: str) -> list[Component]:
    """Components from other playlists that this playlist's saved sets borrow, by id.

    They are not the playlist's own components, so they are kept out of
    ``list_components``; the route view still needs them to draw a saved route.
    """
    local_ids = {c.id for c in list_components(conn, playlist_id)}
    borrowed = {cid for parts in _saved_parts(conn, playlist_id).values() for cid in parts}
    return [load_component(conn, cid) for cid in sorted(borrowed - local_ids)]


def update_component(
    conn: sqlite3.Connection,
    component_id: int,
    *,
    name: str | None = None,
    slot: str | None = None,
    stable_ids: Sequence[str] | None = None,
    tags: Iterable[str] | None = None,
) -> Component:
    """Edit a component in place. Every set whose route names it changes too."""
    clean_name = None if name is None else _clean(name, "component name")
    clean_slot = None if slot is None else _clean(slot, "slot").lower()
    new_tracks = None if stable_ids is None else _clean_tracks(stable_ids)
    new_tags = None if tags is None else _clean_tags(tags)
    with write_unit(conn):
        current = load_component(conn, component_id)
        new_name = current.name if clean_name is None else clean_name
        new_slot = current.slot if clean_slot is None else clean_slot
        if new_slot != current.slot:
            _ensure_slot(conn, current.playlist_id, new_slot)
        conn.execute(
            "UPDATE playlist_components SET name=?, slot=?, updated_at=? WHERE id=?",
            (new_name, new_slot, _now_iso(), component_id),
        )
        if new_tracks is not None:
            _write_tracks(conn, component_id, new_tracks)
        if new_tags is not None:
            _write_tags(conn, component_id, new_tags)
        if new_slot != current.slot:
            _recheck_routes_using(conn, component_id)
        emit_playlist_component_changed(
            conn,
            playlist_id=current.playlist_id,
            component_id=component_id,
            action="component_updated",
        )
    return load_component(conn, component_id)


def delete_component(conn: sqlite3.Connection, component_id: int) -> None:
    """Delete a component no set uses. Refuses, naming the sets, otherwise."""
    with write_unit(conn):
        current = load_component(conn, component_id)
        users = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT set_id FROM playlist_set_parts WHERE component_id=? ORDER BY set_id",
                (component_id,),
            )
        ]
        if users:
            raise ComponentInUseError(component_id, users)
        conn.execute("DELETE FROM playlist_component_entries WHERE component_id=?", (component_id,))
        conn.execute("DELETE FROM playlist_component_tags WHERE component_id=?", (component_id,))
        conn.execute("DELETE FROM playlist_components WHERE id=?", (component_id,))
        emit_playlist_component_changed(
            conn,
            playlist_id=current.playlist_id,
            component_id=component_id,
            action="component_deleted",
        )


def resolve_parts(conn: sqlite3.Connection, parts: Sequence[int]) -> list[tuple[str, int]]:
    """The tracks a route plays, as (stable_id, component_id) in order."""
    out: list[tuple[str, int]] = []
    for cid in parts:
        comp = load_component(conn, int(cid))
        out.extend((sid, comp.id) for sid in comp.stable_ids)
    return out


def slot_rank(conn: sqlite3.Connection, playlist_id: str, slot: str) -> int:
    """Where ``slot`` sits in ``playlist_id``'s slot order.

    A borrowed component's slot may be missing from this playlist: a ladder
    slot then takes its ladder place, and a custom one is refused, since
    nothing says where it belongs.
    """
    row = conn.execute(
        "SELECT position FROM playlist_component_slots WHERE playlist_id=? AND slot=?",
        (playlist_id, slot),
    ).fetchone()
    if row is not None:
        return int(row[0])
    if slot in DEFAULT_SLOTS:
        return DEFAULT_SLOTS.index(slot) * 100
    raise ValueError(
        f"slot {slot!r} is not in playlist {playlist_id!r}'s slot order; add a component in that slot there first"
    )


def validate_parts(conn: sqlite3.Connection, playlist_id: str, parts: Sequence[int]) -> list[int]:
    """Check a route for ``playlist_id``'s set.

    Non-empty, every component exists, and at most one component per slot,
    in the playlist's slot order. A route may skip a slot (no encore), and may
    borrow a component from another playlist (SET-6586 decision 3).
    """
    clean = [int(p) for p in parts]
    if not clean:
        raise ValueError("a route needs at least one component")
    if len(set(clean)) != len(clean):
        raise ValueError("a route names the same component twice")
    seen: dict[str, int] = {}
    last_rank = -1
    for cid in clean:
        comp = load_component(conn, cid)
        if comp.slot in seen:
            raise ValueError(
                f"a route takes one component per slot; components {seen[comp.slot]} and {cid} are both {comp.slot!r}"
            )
        seen[comp.slot] = cid
        rank = slot_rank(conn, playlist_id, comp.slot)
        if rank < last_rank:
            raise ValueError(f"a route follows the slot order; {comp.slot!r} (component {cid}) comes too late")
        last_rank = rank
    return clean


def _recheck_routes_using(conn: sqlite3.Connection, component_id: int) -> None:
    """Refuse an edit that leaves any set naming this component with a bad route."""
    bad: list[int] = []
    for set_id, playlist_id in conn.execute(
        "SELECT DISTINCT s.id, s.playlist_id FROM playlist_set_parts p "
        "JOIN playlist_sets s ON s.id = p.set_id WHERE p.component_id=? ORDER BY s.id",
        (component_id,),
    ).fetchall():
        try:
            validate_parts(conn, str(playlist_id), set_parts(conn, int(set_id)))
        except ValueError:
            bad.append(int(set_id))
    if bad:
        raise ValueError(
            f"moving component {component_id} to that slot would break the route "
            f"of sets {bad}; change those routes first"
        )


def _repeats(stable_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for sid in stable_ids:
        if sid in seen and sid not in dupes:
            dupes.append(sid)
        seen.add(sid)
    return dupes


def set_parts(conn: sqlite3.Connection, set_id: int) -> list[int]:
    apply_playlist_set_migrations(conn)
    return [
        int(r[0])
        for r in conn.execute(
            "SELECT component_id FROM playlist_set_parts WHERE set_id=? ORDER BY position ASC",
            (set_id,),
        )
    ]


def find_set_for_route(conn: sqlite3.Connection, playlist_id: str, parts: Sequence[int]) -> int | None:
    """The set on this playlist whose route is exactly ``parts``, if any."""
    wanted = [int(p) for p in parts]
    for (sid,) in conn.execute(
        "SELECT id FROM playlist_sets WHERE playlist_id=? ORDER BY id", (playlist_id,)
    ).fetchall():
        if set_parts(conn, int(sid)) == wanted:
            return int(sid)
    return None


def list_routes(
    conn: sqlite3.Connection,
    playlist_id: str,
    *,
    limit: int = ROUTE_LIMIT_DEFAULT,
    offset: int = 0,
) -> RouteListing:
    """Every route through the playlist: each full route, then each saved one.

    Full routes take one component from every slot in use, in slot order. A
    saved set may also skip a slot or borrow a component from another playlist;
    those routes follow, in set order, so every saved set shows up. Routes are
    counted, never stored; only a named set persists one. ``total`` is the full
    count and ``truncated`` says when the page stopped short of it. ``offset``
    starts a page partway through that one order, so paging with it lists every
    route exactly once.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset cannot be negative")
    comps = list_components(conn, playlist_id)
    local_ids = {c.id for c in comps}
    groups = _slot_groups(conn, playlist_id, comps)
    full_total = math.prod(len(group) for group in groups) if groups else 0

    route_to_set, extra = _split_saved(_saved_parts(conn, playlist_id), len(groups), local_ids)
    total = full_total + len(extra)

    routes = _full_routes(groups, limit, route_to_set, offset)
    start = max(0, offset - full_total)
    for sid, cids in extra[start : start + max(0, limit - len(routes))]:
        tracks = [t for t, _ in resolve_parts(conn, cids)]
        routes.append(_route(cids, tracks, sid))
    return RouteListing(routes=routes, total=total, truncated=offset + len(routes) < total)


def _slot_groups(conn: sqlite3.Connection, playlist_id: str, comps: list[Component]) -> list[list[Component]]:
    """The playlist's components grouped by slot, in slot order, empty slots dropped."""
    by_slot: dict[str, list[Component]] = {}
    for comp in comps:
        by_slot.setdefault(comp.slot, []).append(comp)
    return [by_slot[s] for s in list_slots(conn, playlist_id) if s in by_slot]


def _saved_parts(conn: sqlite3.Connection, playlist_id: str) -> dict[int, list[int]]:
    """Each saved set of the playlist mapped to its component ids, in part order."""
    saved: dict[int, list[int]] = {}
    for sid, cid in conn.execute(
        "SELECT p.set_id, p.component_id FROM playlist_set_parts p "
        "JOIN playlist_sets s ON s.id = p.set_id WHERE s.playlist_id=? "
        "ORDER BY p.set_id, p.position",
        (playlist_id,),
    ).fetchall():
        saved.setdefault(int(sid), []).append(int(cid))
    return saved


def _split_saved(
    saved: dict[int, list[int]], slot_count: int, local_ids: set[int]
) -> tuple[dict[tuple[int, ...], int], list[tuple[int, list[int]]]]:
    """Map each saved route to its first set, and list the sets that are not full routes.

    Saved routes are valid (one per slot, in order), so a full route is one
    that names every slot in use with this playlist's own components.
    """
    route_to_set: dict[tuple[int, ...], int] = {}
    for sid, cids in saved.items():
        route_to_set.setdefault(tuple(cids), sid)
    extra = [(sid, cids) for sid, cids in saved.items() if len(cids) != slot_count or not set(cids) <= local_ids]
    return route_to_set, extra


def _full_routes(
    groups: list[list[Component]],
    limit: int,
    route_to_set: dict[tuple[int, ...], int],
    offset: int = 0,
) -> list[Route]:
    """Up to ``limit`` routes from ``offset``, taking one component from every slot, in slot order."""
    # product() of no groups yields one empty tuple: a phantom route.
    if not groups:
        return []
    # Decode each index in the product order (first slot most significant), so a page
    # costs its own length, not the offset: walking the product would visit every route before it.
    sizes = [len(group) for group in groups]
    total = math.prod(sizes)
    routes: list[Route] = []
    for n in range(offset, min(offset + limit, total)):
        picks = [0] * len(groups)
        rest = n
        for k in range(len(groups) - 1, -1, -1):
            rest, picks[k] = divmod(rest, sizes[k])
        combo = [groups[k][picks[k]] for k in range(len(groups))]
        parts = [c.id for c in combo]
        tracks = [t for c in combo for t in c.stable_ids]
        routes.append(_route(parts, tracks, route_to_set.get(tuple(parts))))
    return routes


def _route(parts: list[int], tracks: list[str], set_id: int | None) -> Route:
    return Route(parts=parts, stable_ids=tracks, set_id=set_id, repeats=_repeats(tracks))
