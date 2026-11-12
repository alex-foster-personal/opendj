"""Play-order event emission wired to the Phase 5 event bus.

On every ``emit_play_order_changed`` call:

1. :class:`apps.shared.state.writer.StateWriter` appends the durable ``events``
   row via :meth:`~apps.shared.state.writer.StateWriter.append_play_order_changed`
   (the SAVEPOINT / ``BEGIN IMMEDIATE`` contract in
   :func:`apps.shared.state.db.write_unit`).
2. The same :class:`apps.shared.state.types.Event` is fanned out on the shared
   :class:`apps.shared.state.events.EventBus` so in-process subscribers see the
   change without polling the DB.

If Phase 5's schema has not been applied to the supplied connection
(e.g. a legacy caller), we log a one-time INFO and drop the event
rather than raising, preserving the best-effort contract.

Payload schema (see (private path) for the full contract):

    {
      "playlist_id": str,
      "name": str,
      "action": "created" | "updated" | "deleted",
    }
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Literal

from apps.shared.lazy_event_bus import LazyModuleEventBus

_logger = logging.getLogger(__name__)
_logged_bus_missing = False

Action = Literal["created", "updated", "deleted"]

_MODULE_BUS = LazyModuleEventBus()


def _get_shared_bus():
    """Lazy module-scoped :class:`EventBus` for play-order fanout.

    Imported lazily so this module stays importable in environments
    that don't have the Phase 5 state package (e.g. minimal tests).
    """
    return _MODULE_BUS.get()


def set_event_bus(bus) -> None:  # type: ignore[no-untyped-def]
    """Inject a custom bus (primarily for tests using FakeEventBus)."""
    _MODULE_BUS.set(bus)


def emit_play_order_changed(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    name: str,
    action: Action,
    actor: str = "play_orders",
) -> None:
    """Append a ``play_order_changed`` event + fan out on the EventBus.

    Silently becomes a no-op when the event log does not exist on the
    supplied connection; the first such call logs a one-time warning
    so the dev is aware. In that case the bus fanout is still skipped
    to avoid emitting without a durable row backing it.
    """
    global _logged_bus_missing  # noqa: PLW0603
    try:
        from apps.shared.state.writer import StateWriter
    except ImportError:
        return

    writer = StateWriter(conn, bus=_get_shared_bus(), actor=actor)
    try:
        writer.append_play_order_changed(playlist_id=playlist_id, name=name, action=action)
    except sqlite3.OperationalError as exc:
        if not _logged_bus_missing:
            _logger.info(
                "play_order_changed event dropped (event bus not online): %s",
                exc,
            )
            _logged_bus_missing = True
