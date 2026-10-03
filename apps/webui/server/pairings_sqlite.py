"""Durable HTTP ``Pairing`` entities in state.db (PAIR-04).

Phase 8 ``pairings`` edges use ``into|out_of|either``; the webui HTTP model uses
``->`` and ``<->`` with a UUID ``pairing_id`` and optional open-time snapshot
JSON. This module owns a dedicated ``http_pairings`` table so neither shape is
lossy, and mirrors edges into the graph table for CAT-03 tooling.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from typing import Any

from apps.shared.pairings.repo import PairingsRepo

from .backend import BackendError, ConflictError, NotFoundError, Pairing
from .playlist_add import AlreadyExistsError

_HTTP_PAIRINGS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS http_pairings (
        pairing_id     TEXT PRIMARY KEY,
        from_stable_id TEXT NOT NULL,
        to_stable_id   TEXT NOT NULL,
        direction      TEXT NOT NULL CHECK (direction IN ('->', '<->')),
        source         TEXT NOT NULL CHECK (source IN ('manual', 'learned', 'ai')),
        notes          TEXT,
        snapshot_json  TEXT,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL,
        graph_owner_stamp TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_from ON http_pairings(from_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_to ON http_pairings(to_stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_http_pairings_source ON http_pairings(source)",
)

_SELECT_HTTP = (
    "pairing_id, from_stable_id, to_stable_id, direction, source, notes, "
    "snapshot_json, created_at, updated_at"
)


class PairingIdConflictError(AlreadyExistsError):
    """A create reused a pairing_id stored for different endpoints or direction (409).

    Sol P1 on PR #4014: the upsert would otherwise overwrite that pairing and
    its snapshot while the graph mirror kept the old edge beside the new one.
    """

    def __init__(self, pairing_id: str) -> None:
        self.pairing_id = pairing_id
        BackendError.__init__(
            self, f"pairing_id {pairing_id} already names a different pairing"
        )


def raise_on_pairing_id_collision(existing: Pairing | None, pairing: Pairing) -> None:
    """Refuse ``pairing`` when ``existing`` holds its id for another edge."""
    if existing is not None and (
        existing.from_stable_id, existing.to_stable_id, existing.direction
    ) != (pairing.from_stable_id, pairing.to_stable_id, pairing.direction):
        raise PairingIdConflictError(pairing.pairing_id)


def _http_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='http_pairings'"
    ).fetchone()
    return row is not None


def ensure_http_pairings_table(conn: sqlite3.Connection) -> None:
    for stmt in _HTTP_PAIRINGS_DDL:
        conn.execute(stmt)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(http_pairings)")}
    if "graph_owner_stamp" not in columns:
        # Rows from before the marker own no graph edge: delete leaves the
        # edge in place, the direction that never loses someone else's data.
        conn.execute("ALTER TABLE http_pairings ADD COLUMN graph_owner_stamp TEXT")


def _graph_direction(http_dir: str) -> str:
    return "either" if http_dir == "<->" else "into"


def _graph_edge_candidates(pairing: Pairing) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """Direct graph key, then the reverse form that reads as the same wire edge.

    ``a -> b`` is stored as ``(a, b, into)`` or, from the CLI, ``(b, a, out_of)``.
    ``a <-> b`` is ``(a, b, either)`` or the swapped ``(b, a, either)``. The
    direct key wins when both rows exist.
    """
    direct = (pairing.from_stable_id, pairing.to_stable_id, _graph_direction(pairing.direction))
    if pairing.direction == "<->":
        reverse = (pairing.to_stable_id, pairing.from_stable_id, "either")
    else:
        reverse = (pairing.to_stable_id, pairing.from_stable_id, "out_of")
    return direct, reverse


def _stored_graph_pairing_id(from_id: str, to_id: str, direction: str) -> str:
    """Same derivation as ``sqlite_backend._pairing_id`` for a stored graph key."""
    digest = hashlib.sha1(
        f"{from_id}\x1f{to_id}\x1f{direction}".encode()
    ).hexdigest()
    return f"pair-{digest[:24]}"


_GRAPH_EDGE_SELECT = (
    "from_stable_id, to_stable_id, direction, source, notes, "
    "snapshot_json, created_at, modified_at"
)


def _graph_edge_row(conn: sqlite3.Connection, pairing: Pairing) -> sqlite3.Row | None:
    """First existing candidate: the direct key, else the reverse-stored form."""
    PairingsRepo(conn, ensure_schema=True)
    for from_id, to_id, direction in _graph_edge_candidates(pairing):
        row = conn.execute(
            f"SELECT {_GRAPH_EDGE_SELECT} FROM pairings "
            "WHERE from_stable_id=? AND to_stable_id=? AND direction=?",
            (from_id, to_id, direction),
        ).fetchone()
        if row is not None:
            return row
    return None


def _is_reverse_stored(pairing: Pairing, row: sqlite3.Row) -> bool:
    direct = _graph_edge_candidates(pairing)[0]
    return (row[0], row[1], row[2]) != direct


def _graph_edge_stamp(conn: sqlite3.Connection, pairing: Pairing) -> str | None:
    """``created_at|modified_at`` of the CAT-03 edge for ``pairing``, or None when absent.

    The lookup is the direct key, then the reverse-stored form (``out_of`` /
    swapped ``either``). A reverse row is the edge a capture must update.
    """
    row = _graph_edge_row(conn, pairing)
    return None if row is None else f"{row[6]}|{row[7]}"


def _merge_reverse_stored_edge(
    conn: sqlite3.Connection, pairing: Pairing, row: sqlite3.Row,
) -> Pairing:
    """Update the reverse-stored primary key in place and return its wire view.

    Notes append. A snapshot already on the row is kept. ``PairingsRepo.add``
    is not used: that would insert a second ``into`` / ``either`` row beside
    this key. The returned id is the stored key's id, not the capture's wire id.
    """
    from_id, to_id, direction = row[0], row[1], row[2]
    source, notes, snap_raw, created_at, modified_at = (
        row[3], row[4], row[5], row[6], row[7],
    )
    existing_snap = json.loads(snap_raw) if snap_raw else None
    new_notes = notes
    if pairing.notes and pairing.notes != notes:
        new_notes = f"{notes or ''}\n{pairing.notes}".strip()
    new_snap = existing_snap if existing_snap is not None else pairing.snapshot
    updated_at = modified_at
    if new_notes != notes or new_snap != existing_snap:
        updated_at = pairing.updated_at
        snap_json = None if new_snap is None else json.dumps(
            new_snap, separators=(",", ":"),
        )
        conn.execute(
            "UPDATE pairings SET notes=?, snapshot_json=?, modified_at=? "
            "WHERE from_stable_id=? AND to_stable_id=? AND direction=?",
            (new_notes, snap_json, updated_at, from_id, to_id, direction),
        )
    wire_from, wire_to = (
        (to_id, from_id) if direction == "out_of" else (from_id, to_id)
    )
    return Pairing(
        pairing_id=_stored_graph_pairing_id(from_id, to_id, direction),
        from_stable_id=wire_from,
        to_stable_id=wire_to,
        direction="<->" if direction == "either" else "->",
        source=source,
        notes=new_notes,
        snapshot=new_snap,
        created_at=created_at,
        updated_at=updated_at,
    )


def _owner_stamp(conn: sqlite3.Connection, pairing_id: str) -> str | None:
    row = conn.execute(
        "SELECT graph_owner_stamp FROM http_pairings WHERE pairing_id=?", (pairing_id,)
    ).fetchone()
    return None if row is None else row[0]


def _http_owns_graph_edge(conn: sqlite3.Connection, pairing: Pairing) -> bool:
    """True when ``pairing`` holds the ownership marker of its CAT-03 graph edge.

    PR #4014 review (Codex P2 BLOCKING, then a Silver P0): an edge authored on
    its own, through the pairing CLI or other graph tooling, must survive
    capturing and deleting an HTTP pairing on the same endpoints, including
    an edge that tooling recreated or rewrote after the capture. The HTTP
    pairing records the edge's ``created_at|modified_at`` in
    ``graph_owner_stamp`` each time it writes the edge, and owns it only while
    the edge still carries exactly that stamp. Any other write to the edge
    changes the stamp and hands the edge back to whoever wrote it.
    """
    marker = _owner_stamp(conn, pairing.pairing_id)
    return marker is not None and marker == _graph_edge_stamp(conn, pairing)


def _row_to_pairing(row: sqlite3.Row) -> Pairing:
    snap_raw = row["snapshot_json"]
    snapshot: dict[str, Any] | None = None
    if snap_raw is not None:
        snapshot = json.loads(snap_raw)
    return Pairing(
        pairing_id=row["pairing_id"],
        from_stable_id=row["from_stable_id"],
        to_stable_id=row["to_stable_id"],
        direction=row["direction"],
        source=row["source"],
        notes=row["notes"],
        snapshot=snapshot,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_http_pairings(
    conn: sqlite3.Connection,
    *,
    from_stable_id: str | None = None,
    to_stable_id: str | None = None,
    source: str | None = None,
) -> list[Pairing]:
    if not _http_table_exists(conn):
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if from_stable_id is not None:
        clauses.append("from_stable_id = ?")
        params.append(from_stable_id)
    if to_stable_id is not None:
        clauses.append("to_stable_id = ?")
        params.append(to_stable_id)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings{where} ORDER BY created_at",
        params,
    ).fetchall()
    return [_row_to_pairing(r) for r in rows]


def count_http_pairings(conn: sqlite3.Connection) -> int:
    if not _http_table_exists(conn):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM http_pairings").fetchone()
    return int(row[0]) if row is not None else 0


def _find_existing(
    conn: sqlite3.Connection, pairing: Pairing,
) -> Pairing | None:
    row = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings "
        "WHERE from_stable_id=? AND to_stable_id=? AND direction=?",
        (pairing.from_stable_id, pairing.to_stable_id, pairing.direction),
    ).fetchone()
    return _row_to_pairing(row) if row is not None else None


def _upsert_row(conn: sqlite3.Connection, pairing: Pairing) -> Pairing:
    # A reverse-stored CLI edge (``b -> a`` stored as out_of, or swapped either)
    # is this capture's edge. Update that primary key; do not insert a second one.
    # An HTTP row already covering the wire endpoints keeps the #4014 merge path.
    matched = _graph_edge_row(conn, pairing)
    if (
        matched is not None
        and _is_reverse_stored(pairing, matched)
        and _find_existing(conn, pairing) is None
    ):
        return _merge_reverse_stored_edge(conn, pairing, matched)
    # Decided before the row write: the edge is this pairing's to (re)write when
    # nobody holds it yet, or when it still carries this pairing's marker.
    owns_edge = _graph_edge_stamp(conn, pairing) is None or _http_owns_graph_edge(
        conn, pairing
    )
    snap_json = (
        None if pairing.snapshot is None else json.dumps(pairing.snapshot, separators=(",", ":"))
    )
    conn.execute(
        f"""
        INSERT INTO http_pairings ({_SELECT_HTTP})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pairing_id) DO UPDATE SET
          from_stable_id = excluded.from_stable_id,
          to_stable_id   = excluded.to_stable_id,
          direction      = excluded.direction,
          source         = excluded.source,
          notes          = excluded.notes,
          snapshot_json  = excluded.snapshot_json,
          updated_at     = excluded.updated_at
        """,
        (
            pairing.pairing_id,
            pairing.from_stable_id,
            pairing.to_stable_id,
            pairing.direction,
            pairing.source,
            pairing.notes,
            snap_json,
            pairing.created_at,
            pairing.updated_at,
        ),
    )
    if owns_edge:
        PairingsRepo(conn, ensure_schema=True).add(
            pairing.from_stable_id,
            pairing.to_stable_id,
            direction=_graph_direction(pairing.direction),
            source=pairing.source,
            notes=pairing.notes,
        )
    conn.execute(
        "UPDATE http_pairings SET graph_owner_stamp=? WHERE pairing_id=?",
        (_graph_edge_stamp(conn, pairing) if owns_edge else None, pairing.pairing_id),
    )
    row = conn.execute(
        "SELECT pairing_id FROM http_pairings WHERE pairing_id=?",
        (pairing.pairing_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("http_pairings upsert did not persist")
    stored = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings WHERE pairing_id=?",
        (pairing.pairing_id,),
    ).fetchone()
    return _row_to_pairing(stored)


def create_http_pairing(conn: sqlite3.Connection, pairing: Pairing) -> Pairing:
    """Insert or merge like :class:`InMemoryBackend.create_pairing`."""
    ensure_http_pairings_table(conn)
    # The supplied id is checked first: if it already names another edge, the
    # create is refused even when its own edge exists under a different id, so
    # the merge branch below can never answer for a pairing_id it does not own.
    same_id = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings WHERE pairing_id=?",
        (pairing.pairing_id,),
    ).fetchone()
    raise_on_pairing_id_collision(
        _row_to_pairing(same_id) if same_id is not None else None, pairing
    )
    existing = _find_existing(conn, pairing)
    if existing is not None:
        if pairing.notes and pairing.notes != existing.notes:
            merged_notes = f"{existing.notes or ''}\n{pairing.notes}".strip()
            snapshot = (
                existing.snapshot if existing.snapshot is not None else pairing.snapshot
            )
            updated = replace(
                existing,
                notes=merged_notes,
                snapshot=snapshot,
                updated_at=pairing.updated_at,
            )
            return _upsert_row(conn, updated)
        if pairing.snapshot is not None:
            if existing.snapshot is not None:
                return existing
            updated = replace(
                existing, snapshot=pairing.snapshot, updated_at=pairing.updated_at,
            )
            return _upsert_row(conn, updated)
        return existing
    return _upsert_row(conn, pairing)


def delete_http_pairing(
    conn: sqlite3.Connection, pairing_id: str, *, expected_etag: str,
) -> None:
    from .etag import compute_etag, strip_quotes

    ensure_http_pairings_table(conn)
    row = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings WHERE pairing_id=?",
        (pairing_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"pairing not found: {pairing_id}")
    existing = _row_to_pairing(row)
    current = compute_etag(existing.pairing_id, existing.updated_at)
    if strip_quotes(current) != strip_quotes(expected_etag):
        raise ConflictError(
            current={"pairing_id": existing.pairing_id, "updated_at": existing.updated_at},
            etag=current,
        )
    owns_edge = _http_owns_graph_edge(conn, existing)
    # Resolved before the HTTP row goes away: direct key, else the reverse form
    # this pairing matched. Delete removes that stored key, not a fresh ``into``.
    matched = _graph_edge_row(conn, existing) if owns_edge else None
    conn.execute("DELETE FROM http_pairings WHERE pairing_id=?", (pairing_id,))
    if matched is not None:
        PairingsRepo(conn, ensure_schema=True).remove(
            matched[0], matched[1], matched[2],
        )


__all__ = [
    "count_http_pairings",
    "create_http_pairing",
    "delete_http_pairing",
    "ensure_http_pairings_table",
    "list_http_pairings",
]
