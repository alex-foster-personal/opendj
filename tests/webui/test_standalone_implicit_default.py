"""STANDALONE-03 / STANDALONE-06: the implicit own default never hides a value.

[if] an unmapped track has a tag BPM and no own record [then] it is served, [else stop].

`cad91e688` (#3926) made own the default for every unmapped track. With no own
record yet, that hid the track's own `track_fields.bpm` (a tag or a manual
value) behind `missing`, emptied BPM filters, and 500'd `/anlz` on a state DB
that had never run the analysis pipeline. These tests pin the repair in both
directions: the value is served under the implicit default, and an own record,
an explicit own toggle, or a promoted lane still win exactly as before.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import TrackFilter
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("STANDALONE-03")

STABLE_ID = "b" * 40
TAG_BPM = 124.0
OWN_BPM = 128.0
STAMP = "2026-09-26T00:00:00Z"
FINGERPRINT = "sha256:" + "cd" * 32


def _own_beatgrid_record(stable_id: str) -> AnalysisRecord:
    beats = [
        {"t": round(index * 60.0 / OWN_BPM, 5), "n": (index % 4) + 1, "bpm": OWN_BPM}
        for index in range(16)
    ]
    payload = {
        "beats": beats,
        "bpm": OWN_BPM,
        "bpm_confidence": 0.9,
        "octave_reason": "in_band",
        "first_downbeat_s": 0.0,
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }
    return AnalysisRecord(
        stable_id=stable_id,
        backend="own_beatgrid.backfill",
        backend_version="1.0.0",
        analyzed_at=datetime.now(UTC),
        duration_s=64.0,
        sample_rate=44100,
        bpm=OWN_BPM,
        bpm_confidence=0.9,
        key_camelot="",
        key_openkey="",
        key_confidence=0.0,
        energy=0,
        producer="backfill",
        producer_version="1.0.0",
        uses_model=False,
        model_sha256=None,
        decode_fingerprint=FINGERPRINT,
        lanes={"beatgrid": LaneResult(status="ok", payload=payload)},
    )


@pytest.fixture(autouse=True)
def _reset_toggles() -> Iterator[None]:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An unmapped track carrying a manual BPM, on a DB with no analysis schema."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Tagged import",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=str(tmp_path / "absent.wav"),
        )
    finally:
        writer.close()
        conn.close()
    raw = sqlite3.connect(path)
    try:
        raw.execute(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
            "confidence, modified_at) VALUES (?, 'bpm', ?, 'manual', 1.0, ?)",
            (STABLE_ID, str(TAG_BPM), STAMP),
        )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(rb_config, "STATE_DB", path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "wf-cache")
    return path


def _client(path: Path) -> TestClient:
    app = create_app()
    app.state.backend = SqliteBackend(path)
    app.state.state_db_path = str(path)
    app.state.analysis_db_path = str(path)
    return TestClient(app)


def _served_bpm(path: Path) -> tuple[object, str]:
    track = SqliteBackend(path).get_track(STABLE_ID)
    return track.bpm, track.provenance["bpm"].source


def _table_present(path: Path, name: str) -> bool:
    raw = sqlite3.connect(path)
    try:
        return raw.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    finally:
        raw.close()


def test_unmapped_anlz_with_no_analysis_schema_is_not_a_500(state_path: Path) -> None:
    """[if] unmapped track, no analysis_canonical table [then] /anlz 200, [else stop]."""
    assert not _table_present(state_path, "analysis_canonical")
    with _client(state_path) as client:
        response = client.get(f"/api/v1/tracks/{STABLE_ID}/anlz")
    assert response.status_code == 200, response.text
    assert response.json()["key_segments"]["status"] == "missing"


def test_tag_bpm_is_served_while_no_own_record_exists(state_path: Path) -> None:
    """[if] unmapped, tag bpm, no own record [then] bpm served and filterable, [else stop]."""
    assert _served_bpm(state_path) == (TAG_BPM, "manual")
    page = SqliteBackend(state_path).list_tracks(TrackFilter(bpm_min=120.0, bpm_max=125.0))
    assert [t.stable_id for t in page.items] == [STABLE_ID]


def test_an_own_record_still_wins_over_the_tag_bpm(state_path: Path) -> None:
    """[if] an own beatgrid record exists [then] own bpm wins over the tag, [else stop]."""
    upsert_record(_own_beatgrid_record(STABLE_ID), db_path=state_path)
    bpm, source = _served_bpm(state_path)
    assert bpm == OWN_BPM
    assert source.startswith("own_beatgrid.")


def test_an_explicit_own_toggle_still_answers_missing(state_path: Path) -> None:
    """[if] beatgrid toggle set to own, no own record [then] bpm missing, [else stop]."""
    selection.set_toggle("beatgrid", "own")
    assert _served_bpm(state_path) == (None, selection.OWN_ANALYSIS_SOURCE)


def test_a_promoted_lane_still_answers_missing(state_path: Path) -> None:
    """[if] beatgrid promoted to own, no own record [then] bpm missing, [else stop]."""
    conn = open_conn(state_path)
    try:
        selection.set_default(conn, "beatgrid", "own")
        conn.commit()
    finally:
        conn.close()
    assert _served_bpm(state_path) == (None, selection.OWN_ANALYSIS_SOURCE)
