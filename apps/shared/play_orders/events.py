"""Best-effort play-order event emission.

Phase 5's event bus may not be online yet. When it is, we write an
``events`` row directly into the shared state DB (the bus' durable
floor). When the ``events`` table is missing, we log at INFO and drop.

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
from datetime import datetime, timezone
from typing import Literal

_logger = logging.getLogger(__name__)
_logged_bus_missing = False

Action = Literal["created", "updated", "deleted"]


def emit_play_order_changed(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    name: str,
    action: Action,
    actor: str = "play_orders",
) -> None:
    """Append a ``play_order_changed`` event to the Phase 5 event log.

    Silently becomes a no-op when the event log does not exist yet; the
    first such call logs a one-time warning so the dev is aware.
    """
    global _logged_bus_missing
    payload = {"playlist_id": playlist_id, "name": name, "action": action}
    try:
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                "play_order_changed",
                None,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                actor,
            ),
        )
    except sqlite3.OperationalError as exc:
        # Typically: "no such table: events" when Phase 5 hasn't landed.
        if not _logged_bus_missing:
            _logger.info(
                "play_order_changed event dropped (event bus not online): %s",
                exc,
            )
            _logged_bus_missing = True
