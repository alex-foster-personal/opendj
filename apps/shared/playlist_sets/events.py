"""Playlist-set event emission wired to the Phase 5 event bus."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Literal

_logger = logging.getLogger(__name__)
_logged_bus_missing = False

Action = Literal["created", "performed", "practiced"]

_BUS_LOCK = threading.Lock()
_SHARED_BUS = None  # type: ignore[var-annotated]


def _get_shared_bus():
    global _SHARED_BUS
    with _BUS_LOCK:
        if _SHARED_BUS is None:
            from apps.shared.state.events import EventBus
            _SHARED_BUS = EventBus()
        return _SHARED_BUS


def set_event_bus(bus) -> None:  # type: ignore[no-untyped-def]
    global _SHARED_BUS
    with _BUS_LOCK:
        _SHARED_BUS = bus


def emit_playlist_set_changed(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    set_id: int,
    action: Action,
    actor: str = "playlist_sets",
) -> None:
    payload = {
        "playlist_id": playlist_id,
        "set_id": set_id,
        "action": action,
    }
    ts = datetime.now(UTC).isoformat()
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ts,
                "playlist_set_changed",
                None,
                payload_json,
                actor,
            ),
        )
    except sqlite3.OperationalError as exc:
        global _logged_bus_missing
        if not _logged_bus_missing:
            _logger.info(
                "playlist_set_changed event dropped (event bus not online): %s",
                exc,
            )
            _logged_bus_missing = True
        return

    try:
        from apps.shared.state.types import Event
    except ImportError:
        return
    _get_shared_bus().publish(
        Event(
            ts=ts,
            kind="playlist_set_changed",
            stable_id=None,
            payload=payload,
            actor=actor,
        )
    )
