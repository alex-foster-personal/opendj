"""State store tests (META-01).

Covers the Phase 6 <-> Phase 5 wire path: upsert + publish_event now
route through :func:`apps.shared.state.db.open_rw` and fan out on the
shared :class:`EventBus`.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import (
    fetch_records_by_ids,
    open_conn,
    publish_event,
    upsert_record,
)
from apps.shared.state.events import FakeEventBus


def _rec(sid: str = "a", *, bpm: float = 120.0) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=60.0,
        sample_rate=44100,
        bpm=bpm, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
    )


@pytest.mark.requirement("META-01")
def test_upsert_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    r1 = upsert_record(_rec("sidA"), db_path=db)
    assert r1.inserted and not r1.unchanged
    r2 = upsert_record(_rec("sidA"), db_path=db)
    assert r2.unchanged and not r2.inserted


@pytest.mark.requirement("META-01")
def test_upsert_bpm_change_is_update(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    upsert_record(_rec("sidB", bpm=120.0), db_path=db)
    r = upsert_record(_rec("sidB", bpm=121.0), db_path=db)
    assert not r.unchanged and not r.inserted
    back = fetch_records_by_ids(["sidB"], db_path=db)
    assert back[0].bpm == 121.0


@pytest.mark.requirement("META-01")
def test_upsert_fires_event_on_insert(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    upsert_record(_rec("sidC"), db_path=db)
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT event_type, stable_id FROM analysis_events"
        ).fetchall()
    assert ("analyze", "sidC") in rows


@pytest.mark.requirement("META-01")
def test_upsert_no_event_when_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    upsert_record(_rec("sidD"), db_path=db)
    upsert_record(_rec("sidD"), db_path=db)
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM analysis_events WHERE stable_id=?",
            ("sidD",),
        ).fetchone()[0]
    assert n == 1


@pytest.mark.requirement("META-01")
def test_publish_event_writes_row(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    eid = publish_event(
        "beatgrid.flag",
        {"reasons": ["DOWNBEAT_TRANSIENT_GAP"]},
        stable_id="sidE",
        db_path=db,
    )
    assert eid > 0


@pytest.mark.requirement("META-01")
def test_fetch_filters_by_backend(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    rec = _rec("sidF")
    upsert_record(rec, db_path=db)
    upsert_record(dataclasses.replace(rec, backend="mik", backend_version="mik-1"), db_path=db)
    a = fetch_records_by_ids(["sidF"], backend="librosa+madmom", db_path=db)
    b = fetch_records_by_ids(["sidF"], backend="mik", db_path=db)
    assert len(a) == 1 and a[0].backend == "librosa+madmom"
    assert len(b) == 1 and b[0].backend == "mik"


@pytest.mark.requirement("META-01")
def test_store_creates_tables(tmp_path: Path) -> None:
    conn = open_conn(tmp_path / "state.db")
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "analysis" in names
        assert "analysis_events" in names
        # Phase 5's durable event log is present because open_conn now
        # routes through apps.shared.state.db.open_rw.
        assert "events" in names
    finally:
        conn.close()


@pytest.mark.requirement("META-01")
def test_upsert_publishes_on_shared_bus(tmp_path: Path) -> None:
    """Every analyze write lands on the Phase 5 :class:`EventBus`."""
    fake = FakeEventBus()
    analysis_store.set_event_bus(fake)
    try:
        db = tmp_path / "state.db"
        upsert_record(_rec("sidG"), db_path=db)
        kinds = [e.kind for e in fake.events]
        assert kinds == ["analyze"]
        assert fake.events[0].stable_id == "sidG"
        assert fake.events[0].actor == "apps.analysis"
    finally:
        analysis_store.set_event_bus(None)


@pytest.mark.requirement("META-01")
def test_publish_event_mirrors_into_phase5_events_table(tmp_path: Path) -> None:
    """publish_event writes both analysis_events and Phase 5's events."""
    db = tmp_path / "state.db"
    publish_event(
        "beatgrid.flag",
        {"reasons": ["DOWNBEAT_TRANSIENT_GAP"]},
        stable_id="sidH",
        db_path=db,
    )
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT kind, stable_id, actor FROM events WHERE kind='beatgrid.flag'"
        ).fetchall()
    assert rows == [("beatgrid.flag", "sidH", "apps.analysis")]
