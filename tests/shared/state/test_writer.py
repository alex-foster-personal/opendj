"""INFRA-01 StateWriter tests.

Writer tests use :class:`FakeEventBus` to avoid thread-timing noise and
a fixed ``clock`` fixture so event timestamps are deterministic.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter, compute_playlist_id

pytestmark = pytest.mark.requirement("INFRA-01")


FIXED_TIMES = iter([])  # populated per test via fixture.


@pytest.fixture
def writer(state_conn: sqlite3.Connection):
    """A StateWriter with a deterministic clock + FakeEventBus."""
    bus = FakeEventBus()

    t0 = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)
    counter = {"n": 0}

    def clock() -> datetime:
        counter["n"] += 1
        return t0.replace(microsecond=counter["n"])

    w = StateWriter(state_conn, bus=bus, clock=clock, actor="unit-test")
    try:
        yield w
    finally:
        w.close()


def test_upsert_track_insert_returns_true(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    changed = writer.upsert_track(
        stable_id="a" * 40, stable_id_tier="isrc", title="Lanterns",
        artists=["Marlow Quay"], album="FLAB", isrc="GBCEN0900132",
        duration_ms=634000, file_path="/x.flac",
    )
    assert changed is True
    row = state_conn.execute(
        "SELECT stable_id, title FROM tracks WHERE stable_id = ?", ("a" * 40,)
    ).fetchone()
    assert row == ("a" * 40, "Lanterns")
    # One event row appended.
    assert state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    # FakeEventBus captured the publish.
    assert writer.bus.events[0].kind == "track.insert"  # type: ignore[attr-defined]


def test_upsert_track_no_change_returns_false(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    kwargs = dict(
        stable_id="b" * 40, stable_id_tier="isrc", title="T",
        artists=["A"], album="B", isrc=None, duration_ms=100, file_path=None,
    )
    assert writer.upsert_track(**kwargs) is True
    events_after_first = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert writer.upsert_track(**kwargs) is False
    events_after_second = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert events_after_second == events_after_first


def test_set_field_publishes_event(writer: StateWriter) -> None:
    writer.upsert_track(
        stable_id="c" * 40, stable_id_tier="isrc", title=None, artists=[],
        album=None, isrc=None, duration_ms=None, file_path=None,
    )
    writer.bus.events.clear()  # type: ignore[attr-defined]
    assert writer.set_field(
        "c" * 40, "bpm", 128.0,
        source="rekordbox", modified_at="2026-02-02T09:00:00Z",
    ) is True
    kinds = [e.kind for e in writer.bus.events]  # type: ignore[attr-defined]
    assert "track.field.set" in kinds
    assert writer.bus.events[-1].payload["field_name"] == "bpm"  # type: ignore[attr-defined]


def test_playlist_memberships_full_replace(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    # Insert three tracks + one playlist, assign 3 members, then reassign 2.
    for n in range(3):
        writer.upsert_track(
            stable_id=str(n).rjust(40, "x"), stable_id_tier="inferred",
            title=f"t{n}", artists=[], album=None, isrc=None,
            duration_ms=None, file_path=f"/t{n}",
        )
    pid = compute_playlist_id("rekordbox", "123")
    writer.insert_playlist(playlist_id=pid, name="set-1", vendor="rekordbox", vendor_pl_id="123")
    writer.set_playlist_memberships(pid, ["0".rjust(40, "x"), "1".rjust(40, "x"), "2".rjust(40, "x")])
    assert state_conn.execute(
        "SELECT COUNT(*) FROM playlist_memberships WHERE playlist_id = ?", (pid,)
    ).fetchone()[0] == 3
    writer.set_playlist_memberships(pid, ["2".rjust(40, "x"), "0".rjust(40, "x")])
    rows = state_conn.execute(
        "SELECT stable_id, position FROM playlist_memberships "
        "WHERE playlist_id = ? ORDER BY position", (pid,)
    ).fetchall()
    assert rows == [("2".rjust(40, "x"), 0), ("0".rjust(40, "x"), 1)]


def test_events_row_appended_per_operation(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    writer.upsert_track(
        stable_id="d" * 40, stable_id_tier="isrc", title="T", artists=[],
        album=None, isrc=None, duration_ms=None, file_path=None,
    )
    writer.set_vendor_id("d" * 40, "rekordbox", "777")
    writer.set_field("d" * 40, "bpm", 128, source="rekordbox",
                     modified_at="2026-02-02T09:00:00Z")
    # Each op appends exactly one event row.
    kinds = [r[0] for r in state_conn.execute(
        "SELECT kind FROM events ORDER BY id"
    ).fetchall()]
    assert kinds == ["track.insert", "track.vendor_id.set", "track.field.set"]


def test_writer_transaction_rolled_back_on_error(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    # Pick the CHECK violation path: set_field with unknown source bypasses
    # validation intentionally to force a rollback? No -- validation happens
    # before the DB write, so pick an unknown field_name instead. Either way
    # the transaction must not leave dangling state.
    writer.upsert_track(
        stable_id="e" * 40, stable_id_tier="isrc", title="T", artists=[],
        album=None, isrc=None, duration_ms=None, file_path=None,
    )
    events_before = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    with pytest.raises(ValueError):
        writer.set_field("e" * 40, "not_a_field", 1,
                         source="rekordbox", modified_at="2026-02-02T09:00:00Z")
    events_after = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert events_after == events_before


def test_register_adapter_upserts(writer: StateWriter, state_conn: sqlite3.Connection) -> None:
    writer.register_adapter("rekordbox", last_run_at="2026-02-02T09:00:00Z",
                             last_ok=True, notes="ok")
    writer.register_adapter("rekordbox", last_run_at="2026-02-03T09:00:00Z",
                             last_ok=False, notes="oops")
    row = state_conn.execute(
        "SELECT last_run_at, last_ok, notes FROM adapters WHERE adapter_id = ?",
        ("rekordbox",),
    ).fetchone()
    assert row == ("2026-02-03T09:00:00Z", 0, "oops")


def test_compute_playlist_id_is_deterministic() -> None:
    a = compute_playlist_id("rekordbox", "42")
    b = compute_playlist_id("rekordbox", "42")
    c = compute_playlist_id("djay", "42")
    assert a == b
    assert a != c
    assert len(a) == 40


def test_publish_inside_tx_rolls_back_db_on_bus_failure(
    state_conn,
):
    """If bus.publish raises inside _tx, the DB write must roll back.

    if bus.publish error after SAVEPOINT RELEASE leaves orphaned DB row then broken
    """
    from datetime import datetime

    from apps.shared.state.events import FakeEventBus
    from apps.shared.state.writer import StateWriter

    class _BombBus(FakeEventBus):
        def publish(self, event):
            super().publish(event)
            raise RuntimeError("bus exploded")

    bomb_bus = _BombBus()
    t0 = datetime(2026, 2, 2, 9, 0, 0, tzinfo=UTC)
    counter = {"n": 0}

    def clock():
        counter["n"] += 1
        return t0.replace(microsecond=counter["n"])

    w = StateWriter(state_conn, bus=bomb_bus, clock=clock, actor="unit-test")

    import pytest
    events_before = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    tracks_before = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    with pytest.raises(RuntimeError, match="bus exploded"):
        w.upsert_track(
            stable_id="z" * 40, stable_id_tier="isrc", title="Boom",
            artists=["Test"], album=None, isrc=None,
            duration_ms=100, file_path=None,
        )

    events_after = state_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    tracks_after = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    assert events_after == events_before, "event row leaked after bus failure"
    assert tracks_after == tracks_before, "track row leaked after bus failure"
    w.close()
