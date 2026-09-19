"""Play-order event emission wired to the Phase 5 event bus.

Two things happen on every ``emit_play_order_changed`` call:

1. A row lands in Phase 5's durable ``events`` table via the caller's
   sqlite3 connection (which the caller got from
   :func:`apps.shared.state.db.open_rw`, so the table and pragmas are
   already in place).
2. An :class:`apps.shared.state.types.Event` is fanned out on a shared
   :class:`apps.shared.state.events.EventBus` so in-process subscribers
   (Phase 12 sets, Phase 14 voice contexts, etc.) see the change
   without polling the DB.

If Phase 5's schema has not been applied to the supplied connection
(e.g. a legacy caller), we log a one-time INFO and drop the event
rather than raising, preserving the best-effort contract.

Payload schema (see docs/dj_copilot_events.md for the full contract):

    {
      "playlist_id": str,
      "name": str,
      "action": "created" | "updated" | "deleted",
    }
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Literal

_logger = logging.getLogger(__name__)
_logged_bus_missing = False

Action = Literal["created", "updated", "deleted"]


_BUS_LOCK = threading.Lock()
_SHARED_BUS = None  # type: ignore[var-annotated]


def _get_shared_bus():
    """Lazy module-scoped :class:`EventBus` for play-order fanout.

    Imported lazily so this module stays importable in environments
    that don't have the Phase 5 state package (e.g. minimal tests).
    """
    global _SHARED_BUS
    with _BUS_LOCK:
        if _SHARED_BUS is None:
            from apps.shared.state.events import EventBus
            _SHARED_BUS = EventBus()
        return _SHARED_BUS


def set_event_bus(bus) -> None:  # type: ignore[no-untyped-def]
    """Inject a custom bus (primarily for tests using FakeEventBus)."""
    global _SHARED_BUS
    with _BUS_LOCK:
        _SHARED_BUS = bus


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
    global _logged_bus_missing
    payload = {"playlist_id": playlist_id, "name": name, "action": action}
    ts = datetime.now(UTC).isoformat()
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                "play_order_changed",
                None,
                payload_json,
                actor,
            ),
        )
    except sqlite3.OperationalError as exc:
        # Typically: "no such table: events" when the caller opened a
        # bare sqlite3 file without running Phase 5 migrations.
        if not _logged_bus_missing:
            _logger.info(
                "play_order_changed event dropped (event bus not online): %s",
                exc,
            )
            _logged_bus_missing = True
        return

    # Durable row is in. Fan out to in-process subscribers.
    try:
        from apps.shared.state.types import Event
    except ImportError:
        return
    _get_shared_bus().publish(
        Event(
            ts=ts,
            kind="play_order_changed",
            stable_id=None,
            payload=payload,
            actor=actor,
        )
    )
