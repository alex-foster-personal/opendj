"""Regression for Codex P05-F02 / INFRA-03.

Dry-run ingest must NOT deliver events to subscribers, because the outer
SAVEPOINT is rolled back and the mutations are never persisted. Prior to
the fix, ``writer.bus.publish`` was called inline from every
``upsert_track`` / ``set_field`` / ``set_vendor_id`` / ``insert_playlist``
inside the savepoint, so subscribers saw phantom events for rows that
never landed.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.ingest import rekordbox as rb_ingest
from apps.shared.state.writer import StateWriter


pytestmark = pytest.mark.requirement("INFRA-03")


@pytest.fixture
def rb_fixture(rb_plain_db_path: Path, tmp_path: Path) -> Path:
    dst = tmp_path / "rb.db"
    shutil.copy2(rb_plain_db_path, dst)
    return dst


def test_dry_run_delivers_no_events_to_subscribers(
    rb_fixture: Path, state_db_path: Path
) -> None:
    conn = state_db.open_rw(state_db_path)
    bus = FakeEventBus()
    received: list = []
    bus.subscribe("*", lambda ev: received.append(ev))
    w = StateWriter(conn, bus=bus, actor="ingest-rb")
    try:
        report = rb_ingest.ingest_rb(w, rb_fixture, dry_run=True)
    finally:
        w.close()
        conn.close()

    # The bus instance is the same one the caller injected; the dry-run
    # silencing must be scoped to the ingest call and restored on exit.
    assert w.bus is bus
    assert report.dry_run is True
    # Zero phantom events: no subscribers were notified, and the bus
    # captured no published events, because mutations are rolled back.
    assert received == []
    assert bus.events == []


def test_live_run_still_publishes_events(
    rb_fixture: Path, state_db_path: Path
) -> None:
    conn = state_db.open_rw(state_db_path)
    bus = FakeEventBus()
    w = StateWriter(conn, bus=bus, actor="ingest-rb")
    try:
        report = rb_ingest.ingest_rb(w, rb_fixture, dry_run=False, limit=3)
    finally:
        w.close()
        conn.close()
    assert report.dry_run is False
    # Live run must still publish at least the track + adapter events.
    assert len(bus.events) > 0


def test_dry_run_restores_bus_on_exception(
    rb_fixture: Path, state_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = state_db.open_rw(state_db_path)
    bus = FakeEventBus()
    w = StateWriter(conn, bus=bus, actor="ingest-rb")

    # Force the ingest to blow up inside the SAVEPOINT.
    def boom(*_a, **_k):
        raise RuntimeError("synthetic ingest failure")

    monkeypatch.setattr(rb_ingest, "_rb_rows", boom)
    try:
        with pytest.raises(RuntimeError, match="synthetic ingest failure"):
            rb_ingest.ingest_rb(w, rb_fixture, dry_run=True)
        # Bus must be restored even when the ingest raised.
        assert w.bus is bus
    finally:
        w.close()
        conn.close()
