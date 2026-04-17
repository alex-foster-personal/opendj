"""Tests for apps.shared.play_orders.events (PLAY-01 wire to Phase 5)."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.play_orders import events as po_events
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus


pytestmark = pytest.mark.requirement("PLAY-01")


def _open_phase5_conn(tmp_path: Path):
    """Fresh state.db with Phase 5 migrations applied."""
    return state_db.open_rw(tmp_path / "state.db")


def test_emit_writes_to_phase5_events_table(tmp_path: Path) -> None:
    conn = _open_phase5_conn(tmp_path)
    fake = FakeEventBus()
    po_events.set_event_bus(fake)
    try:
        po_events.emit_play_order_changed(
            conn,
            playlist_id="PL-001",
            name="PLAY IT",
            action="created",
        )
    finally:
        po_events.set_event_bus(None)

    rows = conn.execute(
        "SELECT kind, actor, payload_json FROM events "
        "WHERE kind = 'play_order_changed'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "play_order_changed"
    assert rows[0][1] == "play_orders"
    # payload_json is a deterministic JSON string we can parse + compare.
    import json as _json
    assert _json.loads(rows[0][2]) == {
        "action": "created",
        "name": "PLAY IT",
        "playlist_id": "PL-001",
    }


def test_emit_fans_out_on_shared_event_bus(tmp_path: Path) -> None:
    conn = _open_phase5_conn(tmp_path)
    fake = FakeEventBus()
    po_events.set_event_bus(fake)
    try:
        po_events.emit_play_order_changed(
            conn,
            playlist_id="PL-002",
            name="PLAY IT",
            action="updated",
            actor="dj_copilot",
        )
    finally:
        po_events.set_event_bus(None)
        conn.close()

    assert len(fake.events) == 1
    ev = fake.events[0]
    assert ev.kind == "play_order_changed"
    assert ev.actor == "dj_copilot"
    assert ev.payload == {
        "action": "updated",
        "name": "PLAY IT",
        "playlist_id": "PL-002",
    }


def test_emit_is_best_effort_when_events_table_missing(tmp_path: Path) -> None:
    """Caller that opens a bare sqlite3 file without Phase 5 migrations
    should not see the call raise."""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        # No 'events' table -- expected drop + one-time log.
        po_events.emit_play_order_changed(
            conn,
            playlist_id="PL-003",
            name="PLAY IT",
            action="deleted",
        )
    finally:
        conn.close()
