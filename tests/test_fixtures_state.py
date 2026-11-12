"""Acceptance for shared ``state_db`` and ``write_synthetic_gridded_track`` (#10406).

[if] shared fixture helpers ship [then] these tests prove schema and synthetic grid parity, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.backends.own_beatgrid import BACKEND_NAME
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.routes.rb_assets import router as assets_router
from apps.webui.server.sqlite_backend import make_backend
from tests import fixtures_state
from tests.fixtures_state import (
    FIXTURE_MODEL_SHA256,
    FIXTURE_PROVENANCE_NAME,
    assert_state_db_schema,
    write_synthetic_gridded_track,
)

# The fixture under test, bound in this module: the root conftest must not import app code,
# or every change under apps/analysis would select the whole suite (CI test selection rule).
state_db = fixtures_state.state_db

pytestmark = pytest.mark.requirement("FIXT-10616")

STABLE_ID = "f" * 40


@pytest.fixture(autouse=True)
def _reset_toggles() -> Iterator[None]:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def test_state_db_fixture_yields_schema_complete_empty_db(state_db: Path) -> None:
    """[if] state_db runs [then] Phase 5 tables exist and tracks is empty, [else stop]."""
    assert_state_db_schema(state_db)
    conn = open_conn(state_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


def test_write_synthetic_gridded_track_grid_matches_known_formula(state_db: Path) -> None:
    """[if] writer runs [then] beats_ms follow bar1 + i*60000/bpm, [else stop]."""
    bpm = 120.0
    written = write_synthetic_gridded_track(
        state_db,
        stable_id=STABLE_ID,
        bpm=bpm,
        seconds=32.0,
        bar1_ms=50.0,
        phrase_bars=4,
    )
    period_ms = 60_000.0 / bpm
    for index, beat_ms in enumerate(written.beats_ms):
        assert beat_ms == pytest.approx(50.0 + index * period_ms)
    assert written.downbeats_ms == tuple(written.beats_ms[0::4])
    assert len(written.phrases) >= 1
    lane = written.record.lanes["beatgrid"]
    assert lane.status == "ok"
    assert len(lane.payload["beats"]) == len(written.beats_ms)


def test_write_synthetic_gridded_track_provenance_names_fixture(state_db: Path) -> None:
    """[if] writer runs [then] model_sha256 names fixture-synthetic provenance, [else stop]."""
    written = write_synthetic_gridded_track(state_db, stable_id=STABLE_ID, bpm=128.0, seconds=16.0)
    assert written.record.backend == BACKEND_NAME
    assert written.record.uses_model is True
    expected_digest = f"sha256:{FIXTURE_MODEL_SHA256}"
    assert written.record.model_sha256 == expected_digest
    conn = open_conn(state_db)
    try:
        row = conn.execute(
            "SELECT record_json FROM analysis WHERE stable_id = ? AND backend = ?",
            (STABLE_ID, BACKEND_NAME),
        ).fetchone()
        assert row is not None
        persisted = AnalysisRecord.from_json(row[0])
        assert persisted.model_sha256 == expected_digest
        # upsert_record publishes analyze events with actor apps.analysis (store.publish).
        analyze_actor = conn.execute(
            "SELECT actor FROM events WHERE stable_id = ? AND kind = 'analyze' LIMIT 1",
            (STABLE_ID,),
        ).fetchone()[0]
        assert analyze_actor == "apps.analysis"
        track_actor = conn.execute(
            "SELECT actor FROM events WHERE stable_id = ? AND kind != 'analyze' LIMIT 1",
            (STABLE_ID,),
        ).fetchone()[0]
        assert track_actor == FIXTURE_PROVENANCE_NAME
    finally:
        conn.close()


def test_anlz_route_serves_synthetic_grid_beat_count_and_first_downbeat(
    state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] app binds the fixture DB [then] /anlz beat count and first downbeat match, [else stop]."""
    written = write_synthetic_gridded_track(state_db, stable_id=STABLE_ID, bpm=100.0, seconds=40.0, bar1_ms=0.0)
    monkeypatch.setattr(rb_config, "STATE_DB", state_db)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", state_db.parent / "absent.db")
    selection.set_toggle("beatgrid", "own")

    app = FastAPI()
    app.state.backend = make_backend()
    app.state.analysis_db_path = state_db
    app.include_router(assets_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")

    with TestClient(app) as client:
        response = client.get(f"/api/v1/tracks/{STABLE_ID}/anlz")
    assert response.status_code == 200, response.text
    grid = response.json()["beatgrid"]
    assert grid["status"] == "ok"
    assert grid["beat_count"] == len(written.beats_ms)
    downbeat_s = [beat["t"] for beat in grid["beats"] if beat["n"] == 1]
    assert downbeat_s, "route served no bar-1 beats"
    assert downbeat_s[0] == pytest.approx(written.downbeats_ms[0] / 1000.0, abs=0.05)
