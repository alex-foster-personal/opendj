"""``StateWriter`` play-order change events (``play_order_changed``).

Appends durable ``events`` rows through :meth:`StateWriter._append_event` so
event-cursor replay and followers see create, update, and delete. Does not
import :mod:`apps.shared.play_orders` (import-cycle guard).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Literal, Protocol

from apps.shared.state import db as _state_db
from apps.shared.state.events import EventBus, FakeEventBus
from apps.shared.state.types import Event

PlayOrderAction = Literal["created", "updated", "deleted"]


class _WriterHost(Protocol):
    bus: EventBus | FakeEventBus
    _conn: Any
    _tx_depth: int
    _sp_counter: Any

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


def _play_order_payload(playlist_id: str, name: str, action: PlayOrderAction) -> dict[str, Any]:
    return {"playlist_id": playlist_id, "name": name, "action": action}


class _PlayOrderWriterMixin:
    def append_play_order_changed(
        self: _WriterHost,
        *,
        playlist_id: str,
        name: str,
        action: PlayOrderAction,
    ) -> Event:
        """Append one ``play_order_changed`` row and fan out on the bus."""
        now = self._now_iso()
        payload = _play_order_payload(playlist_id, name, action)
        if self._tx_depth > 0:
            ev = self._append_event(
                kind="play_order_changed",
                stable_id=None,
                payload=payload,
                ts=now,
            )
            self.bus.publish(ev)
            return ev
        with _state_db.write_unit(self._conn, f"sw_po_{next(self._sp_counter)}"):
            ev = self._append_event(
                kind="play_order_changed",
                stable_id=None,
                payload=payload,
                ts=now,
            )
        self.bus.publish(ev)
        return ev


__all__ = ["PlayOrderAction", "_PlayOrderWriterMixin"]
