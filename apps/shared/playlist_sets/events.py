"""Playlist-set event emission wired to the Phase 5 event bus."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from typing import Literal

from apps.shared.lazy_event_bus import LazyModuleEventBus

_logger = logging.getLogger(__name__)
_logged_bus_missing = False

Action = Literal["created", "performed", "practiced"]
ComponentAction = Literal["component_created", "component_updated", "component_deleted"]

_MODULE_BUS = LazyModuleEventBus()


def _get_shared_bus():
    return _MODULE_BUS.get()


def set_event_bus(bus) -> None:  # type: ignore[no-untyped-def]
    _MODULE_BUS.set(bus)


def emit_playlist_set_changed(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    set_id: int,
    action: Action,
    actor: str = "playlist_sets",
) -> None:
    """A set changed: ``playlist_set_changed`` always carries ``set_id``."""
    _emit(
        conn,
        "playlist_set_changed",
        {"playlist_id": playlist_id, "set_id": set_id, "action": action},
        actor,
    )


def emit_playlist_component_changed(
    conn: sqlite3.Connection,
    *,
    playlist_id: str,
    component_id: int,
    action: ComponentAction,
    actor: str = "playlist_sets",
) -> None:
    """A component changed: its own kind, so set subscribers never see it."""
    _emit(
        conn,
        "playlist_component_changed",
        {"playlist_id": playlist_id, "component_id": component_id, "action": action},
        actor,
    )


def _emit(
    conn: sqlite3.Connection, kind: str, payload: dict[str, object], actor: str
) -> None:
    ts = datetime.now(UTC).isoformat()
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, kind, None, payload_json, actor),
        )
    except sqlite3.OperationalError as exc:
        global _logged_bus_missing  # noqa: PLW0603
        if not _logged_bus_missing:
            _logger.info("%s event dropped (event bus not online): %s", kind, exc)
            _logged_bus_missing = True
        return

    try:
        from apps.shared.state.types import Event
    except ImportError:
        return
    _get_shared_bus().publish(
        Event(ts=ts, kind=kind, stable_id=None, payload=payload, actor=actor)
    )
