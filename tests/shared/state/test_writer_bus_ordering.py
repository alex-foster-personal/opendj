"""INFRA-01 regression: StateWriter must not persist a mutation if the
bus publish fails.

Codex Phase-05 review flagged that mutation methods committed the DB
change before publishing, so a raising subscriber/bus left the row in
place with no event emitted (durable log + in-process fanout drift).
This test injects a bus whose ``publish`` always raises and asserts
``set_field`` leaves ``track_fields`` empty.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from apps.shared.state.types import Event
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("INFRA-01")


class _ExplodingBus:
    """Bus double whose ``publish`` always raises.

    Matches the minimal ``EventBus`` surface StateWriter uses: ``publish``
    and ``close``. Subscribing is unsupported because we never fan out.
    """

    def __init__(self) -> None:
        self.publish_attempts = 0

    def publish(self, event: Event) -> None:
        self.publish_attempts += 1
        raise RuntimeError("bus down")

    def close(self, timeout: float | None = None) -> None:
        return None


def test_set_field_rolls_back_when_bus_publish_fails(
    state_conn: sqlite3.Connection,
) -> None:
    bus = _ExplodingBus()
    t0 = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)
    writer = StateWriter(state_conn, bus=bus, clock=lambda: t0, actor="unit-test")

    # Seed a parent tracks row so the FK on track_fields is satisfied.
    # We bypass ``upsert_track`` because it too would route through the
    # exploding bus; we only want to exercise ``set_field`` ordering here.
    state_conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
        "album, isrc, duration_ms, file_path, content_hash, created_at, "
        "updated_at) VALUES (?, 'isrc', 'T', '[]', NULL, NULL, NULL, NULL, "
        "NULL, ?, ?)",
        ("a" * 40, "2026-02-02T09:00:00+00:00", "2026-02-02T09:00:00+00:00"),
    )

    with pytest.raises(RuntimeError, match="bus down"):
        writer.set_field(
            "a" * 40,
            "bpm",
            128.0,
            source="mik",
            modified_at="2026-02-02T09:00:00+00:00",
        )

    # Bus was asked to publish exactly once.
    assert bus.publish_attempts == 1

    # The mutation MUST NOT have persisted: the field row is absent and
    # the events log contains nothing for this track.
    field_rows = state_conn.execute(
        "SELECT COUNT(*) FROM track_fields WHERE stable_id = ? AND field_name = ?",
        ("a" * 40, "bpm"),
    ).fetchone()[0]
    assert field_rows == 0, "set_field persisted despite failing bus publish"

    event_rows = state_conn.execute(
        "SELECT COUNT(*) FROM events WHERE stable_id = ? AND kind = ?",
        ("a" * 40, "track.field.set"),
    ).fetchone()[0]
    assert event_rows == 0, "events row leaked despite failing bus publish"

    # History must also be empty -- no superseded row should have been
    # written for the aborted change.
    history_rows = state_conn.execute(
        "SELECT COUNT(*) FROM track_field_history WHERE stable_id = ?",
        ("a" * 40,),
    ).fetchone()[0]
    assert history_rows == 0

    writer.close()
