"""Durable HTTP ``Pairing`` entities in state.db (PAIR-04).

Phase 8 ``pairings`` edges use ``into|out_of|either``; the webui HTTP model uses
``->`` and ``<->`` with a UUID ``pairing_id`` and optional open-time snapshot
JSON. This module owns a dedicated ``http_pairings`` table so neither shape is
lossy, and mirrors edges into the graph table for CAT-03 tooling.
"""
from __future__ import annotations

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
        updated_at     TEXT NOT NULL
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


def _graph_direction(http_dir: str) -> str:
    return "either" if http_dir == "<->" else "into"


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
    repo = PairingsRepo(conn, ensure_schema=True)
    repo.add(
        pairing.from_stable_id,
        pairing.to_stable_id,
        direction=_graph_direction(pairing.direction),
        source=pairing.source,
        notes=pairing.notes,
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
    same_id = conn.execute(
        f"SELECT {_SELECT_HTTP} FROM http_pairings WHERE pairing_id=?",
        (pairing.pairing_id,),
    ).fetchone()
    raise_on_pairing_id_collision(
        _row_to_pairing(same_id) if same_id is not None else None, pairing
    )
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
    conn.execute("DELETE FROM http_pairings WHERE pairing_id=?", (pairing_id,))
    repo = PairingsRepo(conn, ensure_schema=True)
    repo.remove(
        existing.from_stable_id,
        existing.to_stable_id,
        _graph_direction(existing.direction),
    )


__all__ = [
    "count_http_pairings",
    "create_http_pairing",
    "delete_http_pairing",
    "ensure_http_pairings_table",
    "list_http_pairings",
]
