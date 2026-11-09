"""``PairingsRepo`` -- thin CRUD over the ``pairings`` table.

Style matches the rest of the repo: stdlib-only, explicit SQL, no ORM.
The table lives in the shared-state SQLite DB (Phase 5).

Concurrency: the caller owns the :class:`sqlite3.Connection`. We rely on
WAL + 5s busy_timeout from :func:`apps.shared.state.db.open_rw` so brief
contention with the ingesters is absorbed silently.

Writes go through :class:`StateWriter`: each add/remove is one write unit
that changes the row and appends one ``events`` row (``pairing.add`` /
``pairing.remove``). A caller already inside a writer unit passes
``writer=`` so the write joins it as a SAVEPOINT.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from apps.shared.state.writer import StateWriter
from apps.shared.state.writer_pairings import PAIRING_COLS

from .models import DIRECTIONS, SOURCES, PairingEdge
from .schema_sql import ensure_phase08_tables, prepare_repo_connection


class PairingsError(ValueError):
    """Repo-level validation error.

    Raised when an argument would otherwise produce an opaque
    ``sqlite3.IntegrityError`` or similar low-level exception.
    """


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
        writer: StateWriter | None = None,
    ) -> None:
        if writer is not None and writer.raw_conn is not conn:
            raise PairingsError("writer must be bound to the repo's connection")
        self.conn = prepare_repo_connection(
            conn,
            ensure_phase08_tables,
            ensure_schema=ensure_schema,
        )
        self._writer = writer

    # -- write unit -------------------------------------------------------

    @contextmanager
    def _writer_for_write(self) -> Iterator[StateWriter]:
        """The injected writer, else a short-lived one bound to this connection."""
        if self._writer is not None:
            yield self._writer
            return
        with StateWriter(self.conn) as writer:
            yield writer

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

        with self._writer_for_write() as writer:
            row = writer.upsert_pairing(
                from_id,
                to_id,
                direction=direction,
                source=source,
                notes=notes,
                confidence=confidence,
            )
        return _row_to_edge(row)

    def remove(self, from_id: str, to_id: str, direction: str) -> bool:
        """Delete one edge; return True if a row was actually removed."""
        _validate_enum("direction", direction, DIRECTIONS)
        with self._writer_for_write() as writer:
            return writer.delete_pairing(from_id, to_id, direction)

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
                f"SELECT {PAIRING_COLS} FROM pairings "
                "WHERE from_stable_id=? OR to_stable_id=? "
                "ORDER BY modified_at DESC",
                (stable_id, stable_id),
            ).fetchall()
        else:
            _validate_enum("direction", direction, DIRECTIONS)
            rows = self.conn.execute(
                f"SELECT {PAIRING_COLS} FROM pairings "
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
        sql = f"SELECT {PAIRING_COLS} FROM pairings"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY from_stable_id, to_stable_id, direction"
        for row in self.conn.execute(sql, params):
            yield _row_to_edge(row)


__all__ = ["PairingsError", "PairingsRepo"]
