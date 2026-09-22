"""``PairingsRepo`` -- thin CRUD over the ``pairings`` table.

Style matches the rest of the repo: stdlib-only, explicit SQL, no ORM.
The table lives in the shared-state SQLite DB (Phase 5).

Concurrency: the caller owns the :class:`sqlite3.Connection`. We rely on
WAL + 5s busy_timeout from :func:`apps.shared.state.db.open_rw` so brief
contention with the ingesters is absorbed silently.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

from .models import DIRECTIONS, SOURCES, PairingEdge
from .schema_sql import ensure_phase08_tables


class PairingsError(ValueError):
    """Repo-level validation error.

    Raised when an argument would otherwise produce an opaque
    ``sqlite3.IntegrityError`` or similar low-level exception.
    """


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    # datetime.fromisoformat handles the "+00:00" suffix we emit.
    return datetime.fromisoformat(value)


def _validate_enum(name: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise PairingsError(
            f"invalid {name}={value!r}; expected one of {allowed}"
        )


def _row_to_edge(row: tuple) -> PairingEdge:
    return PairingEdge(
        from_stable_id=row[0],
        to_stable_id=row[1],
        direction=row[2],
        source=row[3],
        notes=row[4],
        confidence=row[5],
        created_at=_parse_iso(row[6]),
        modified_at=_parse_iso(row[7]),
    )


_SELECT_COLS: str = (
    "from_stable_id, to_stable_id, direction, source, notes, "
    "confidence, created_at, modified_at"
)


class PairingsRepo:
    """CRUD over the ``pairings`` table.

    Instantiate with an open :class:`sqlite3.Connection`; the repo does
    not manage the connection lifecycle.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ensure_schema: bool = True,
    ) -> None:
        self.conn = conn
        if ensure_schema:
            # Idempotent CREATE-IF-NOT-EXISTS; keeps the repo usable even
            # on a brand-new state DB without requiring callers to run a
            # separate migration step.
            ensure_phase08_tables(conn)

    # -- writes -----------------------------------------------------------

    def add(
        self,
        from_id: str,
        to_id: str,
        *,
        direction: str = "either",
        source: str = "manual",
        notes: str | None = None,
        confidence: float | None = None,
    ) -> PairingEdge:
        """Insert or update (upsert) one pairing edge.

        If ``(from_id, to_id, direction)`` already exists, updates the
        mutable columns and bumps ``modified_at``. Returns the resulting
        :class:`PairingEdge`.

        Raises :class:`PairingsError` on invalid direction / source /
        confidence / identical endpoints.
        """
        _validate_enum("direction", direction, DIRECTIONS)
        _validate_enum("source", source, SOURCES)
        if not from_id or not to_id:
            raise PairingsError("from_stable_id and to_stable_id are required")
        if from_id == to_id:
            raise PairingsError(
                "from_stable_id and to_stable_id must differ; self-pair "
                "is meaningless in a transition graph"
            )
        if confidence is not None and not (0.0 <= confidence <= 1.0):
            raise PairingsError(
                f"confidence must be None or in [0,1]; got {confidence!r}"
            )

        now = _now_iso()
        # UPSERT: on conflict, keep created_at but update everything else.
        self.conn.execute(
            f"""
            INSERT INTO pairings ({_SELECT_COLS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (from_stable_id, to_stable_id, direction) DO UPDATE SET
              source      = excluded.source,
              notes       = excluded.notes,
              confidence  = excluded.confidence,
              modified_at = excluded.modified_at
            """,
            (from_id, to_id, direction, source, notes, confidence, now, now),
        )
        row = self.conn.execute(
            f"SELECT {_SELECT_COLS} FROM pairings "
            "WHERE from_stable_id=? AND to_stable_id=? AND direction=?",
            (from_id, to_id, direction),
        ).fetchone()
        return _row_to_edge(row)

    def remove(self, from_id: str, to_id: str, direction: str) -> bool:
        """Delete one edge; return True if a row was actually removed."""
        _validate_enum("direction", direction, DIRECTIONS)
        cur = self.conn.execute(
            "DELETE FROM pairings WHERE from_stable_id=? AND to_stable_id=? "
            "AND direction=?",
            (from_id, to_id, direction),
        )
        return cur.rowcount > 0

    # -- reads ------------------------------------------------------------

    def exists(self, from_id: str, to_id: str, direction: str) -> bool:
        _validate_enum("direction", direction, DIRECTIONS)
        row = self.conn.execute(
            "SELECT 1 FROM pairings WHERE from_stable_id=? AND to_stable_id=? "
            "AND direction=? LIMIT 1",
            (from_id, to_id, direction),
        ).fetchone()
        return row is not None

    def get_neighbors(
        self,
        stable_id: str,
        *,
        direction: str | None = None,
    ) -> list[PairingEdge]:
        """Return all edges involving ``stable_id`` on either endpoint.

        With ``direction`` omitted (None), returns every pairing touching
        the track. With a specific direction, returns only edges where
        ``stable_id`` is the ``from_stable_id`` and the direction matches
        (i.e. "what does this track pair *into*").
        """
        if direction is None:
            rows = self.conn.execute(
                f"SELECT {_SELECT_COLS} FROM pairings "
                "WHERE from_stable_id=? OR to_stable_id=? "
                "ORDER BY modified_at DESC",
                (stable_id, stable_id),
            ).fetchall()
        else:
            _validate_enum("direction", direction, DIRECTIONS)
            rows = self.conn.execute(
                f"SELECT {_SELECT_COLS} FROM pairings "
                "WHERE from_stable_id=? AND direction=? "
                "ORDER BY modified_at DESC",
                (stable_id, direction),
            ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def list_all(
        self,
        *,
        source: str | None = None,
        from_id: str | None = None,
    ) -> Iterator[PairingEdge]:
        """Iterate every pairing, optionally filtered by ``source`` / ``from``.

        Returns an iterator so large graphs don't materialise in memory.
        """
        where: list[str] = []
        params: list[object] = []
        if source is not None:
            _validate_enum("source", source, SOURCES)
            where.append("source = ?")
            params.append(source)
        if from_id is not None:
            where.append("from_stable_id = ?")
            params.append(from_id)
        sql = f"SELECT {_SELECT_COLS} FROM pairings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY from_stable_id, to_stable_id, direction"
        for row in self.conn.execute(sql, params):
            yield _row_to_edge(row)


__all__ = ["PairingsError", "PairingsRepo"]
