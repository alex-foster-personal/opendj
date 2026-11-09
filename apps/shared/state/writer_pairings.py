"""``StateWriter`` pairing edge upsert/delete with durable events.

Each mutation of the ``pairings`` table runs in one writer unit that also
appends exactly one ``events`` row (``pairing.add`` / ``pairing.remove``),
so event-cursor replay and events-table followers see it. Arguments arrive
already validated by :class:`apps.shared.pairings.repo.PairingsRepo`; this
module never imports the pairings package (import-cycle guard).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.types import Event

PAIRING_COLS: str = "from_stable_id, to_stable_id, direction, source, notes, confidence, created_at, modified_at"

_EDGE_WHERE: str = "WHERE from_stable_id=? AND to_stable_id=? AND direction=?"


class _WriterHost(Protocol):
    bus: EventBus | FakeEventBus
    _conn: Any

    def _tx(self) -> AbstractContextManager[Any]: ...

    def _now_iso(self) -> str: ...

    def _append_event(
        self,
        *,
        kind: str,
        stable_id: str | None,
        payload: dict[str, Any],
        ts: str | None = None,
    ) -> Event: ...


def _edge_payload(from_id: str, to_id: str, direction: str, source: str) -> dict[str, Any]:
    return {"from_id": from_id, "to_id": to_id, "direction": direction, "source": source}


class _PairingWriterMixin:
    def upsert_pairing(
        self: _WriterHost,
        from_id: str,
        to_id: str,
        *,
        direction: str,
        source: str,
        notes: str | None,
        confidence: float | None,
    ) -> tuple[Any, ...]:
        """Upsert one pairing edge and append one ``pairing.add`` event in the same unit.

        Returns the stored row in ``PAIRING_COLS`` order, read inside the unit.
        """
        now = self._now_iso()
        with self._tx() as conn:
            conn.execute(
                f"""
                INSERT INTO pairings ({PAIRING_COLS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (from_stable_id, to_stable_id, direction) DO UPDATE SET
                  source      = excluded.source,
                  notes       = excluded.notes,
                  confidence  = excluded.confidence,
                  modified_at = excluded.modified_at
                """,
                (from_id, to_id, direction, source, notes, confidence, now, now),
            )
            row = conn.execute(
                f"SELECT {PAIRING_COLS} FROM pairings {_EDGE_WHERE}",
                (from_id, to_id, direction),
            ).fetchone()
            ev = self._append_event(
                kind="pairing.add",
                stable_id=from_id,
                payload=_edge_payload(from_id, to_id, direction, source),
                ts=now,
            )
            self.bus.publish(ev)
        return tuple(row)

    def delete_pairing(self: _WriterHost, from_id: str, to_id: str, direction: str) -> bool:
        """Delete one edge; append one ``pairing.remove`` event only when a row went away."""
        with self._tx() as conn:
            found = conn.execute(
                f"SELECT source FROM pairings {_EDGE_WHERE}",
                (from_id, to_id, direction),
            ).fetchone()
            if found is None:
                return False
            conn.execute(f"DELETE FROM pairings {_EDGE_WHERE}", (from_id, to_id, direction))
            ev = self._append_event(
                kind="pairing.remove",
                stable_id=from_id,
                payload=_edge_payload(from_id, to_id, direction, found[0]),
            )
            self.bus.publish(ev)
        return True


__all__ = ["PAIRING_COLS", "_PairingWriterMixin"]
