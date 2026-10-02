"""LIBUX-34: All Tracks rows carry the same BPM provenance as playlist rows.

[if] a track has BPM provenance [then] GET /api/v1/tracks serves its method and confidence, [else stop].

Mac check on PR #4014 (Fri 2 Oct 2026): the BPM hover read "Beat grid: own
beatgrid.backfill. Confidence: 93%." in a playlist but only "Exact BPM" in All
Tracks and its search. Both views build their rows with
``rb_vendor.build_track_rows``, which emits bpm_source/bpm_method/
bpm_confidence; ``GET /api/v1/tracks`` then re-declared every row field on
``TrackListItemOut`` by hand and dropped those three.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import rb_vendor
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-34")

STABLE_ID = "c" * 40
STAMP = "2026-10-02T00:00:00Z"


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Provenance",
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
            "confidence, modified_at) VALUES (?, 'bpm', '127.0', 'manual', 0.93, ?)",
            (STABLE_ID, STAMP),
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


PROVENANCE = ("bpm_source", "bpm_method", "bpm_confidence")


def test_all_tracks_rows_carry_bpm_provenance(state_path: Path) -> None:
    """[if] a track has bpm provenance [then] /tracks serves method and confidence [else stop]."""
    with _client(state_path) as client:
        response = client.get("/api/v1/tracks")
    assert response.status_code == 200, response.text
    (item,) = [row for row in response.json()["items"] if row["stable_id"] == STABLE_ID]
    assert item["bpm_source"] == "manual"
    assert item["bpm_method"] == "manual"
    assert item["bpm_confidence"] == pytest.approx(0.93)


def test_all_tracks_and_playlist_rows_agree_on_bpm_provenance(state_path: Path) -> None:
    """Control: the list route serves exactly what the shared row builder emits."""
    backend = SqliteBackend(state_path)
    (row,) = rb_vendor.build_track_rows([backend.get_track(STABLE_ID)])
    assert row["bpm_method"] == "manual"  # the builder itself is not the gap
    with _client(state_path) as client:
        items = client.get("/api/v1/tracks").json()["items"]
    (item,) = [r for r in items if r["stable_id"] == STABLE_ID]
    assert {key: item[key] for key in PROVENANCE} == {key: row[key] for key in PROVENANCE}


def _set_confidence(path: Path, value: object) -> None:
    # The state schema's CHECK constraint already refuses an out-of-range or
    # text confidence on write; ignore it here to stand in for a row written
    # before that constraint or by a tool that bypassed it.
    raw = sqlite3.connect(path)
    try:
        raw.execute("PRAGMA ignore_check_constraints = ON")
        raw.execute(
            "UPDATE track_fields SET confidence = ? WHERE stable_id = ? AND field_name = 'bpm'",
            (value, STABLE_ID),
        )
        raw.commit()
    finally:
        raw.close()


@pytest.mark.parametrize("bad", [1.5, -0.1])
def test_invalid_bpm_confidence_is_reported_not_dropped(state_path: Path, bad: object) -> None:
    """[if] stored bpm confidence is invalid [then] the row names the error and the listing still serves [else stop]."""
    _set_confidence(state_path, bad)
    with _client(state_path) as client:
        response = client.get("/api/v1/tracks")
    assert response.status_code == 200, response.text
    (item,) = [row for row in response.json()["items"] if row["stable_id"] == STABLE_ID]
    assert item["bpm_confidence"] is None
    assert item["bpm_confidence_error"] is not None
    assert repr(bad) in item["bpm_confidence_error"]
    assert item["bpm_method"] == "manual"  # the rest of the provenance still serves


def test_text_bpm_confidence_is_reported_by_the_row_builder(state_path: Path) -> None:
    """[if] stored bpm confidence is text [then] the shared row builder names the error [else stop].

    Builder level only: the list route also maps provenance into
    ``ProvenanceOut``, whose float field already refuses a text confidence
    for the whole request (pre-existing, outside this PR).
    """
    _set_confidence(state_path, "not-a-number")
    backend = SqliteBackend(state_path)
    (row,) = rb_vendor.build_track_rows([backend.get_track(STABLE_ID)])
    assert "bpm_confidence" not in row
    assert "'not-a-number'" in row["bpm_confidence_error"]


def test_valid_bpm_confidence_carries_no_error(state_path: Path) -> None:
    """Control: a valid confidence serves as a number with no error field set."""
    with _client(state_path) as client:
        items = client.get("/api/v1/tracks").json()["items"]
    (item,) = [r for r in items if r["stable_id"] == STABLE_ID]
    assert item["bpm_confidence"] == pytest.approx(0.93)
    assert item["bpm_confidence_error"] is None
