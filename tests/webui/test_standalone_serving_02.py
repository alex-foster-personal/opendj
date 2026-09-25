"""STANDALONE-02: computed BPM reaches the user without promotion.

[if] unmapped own backfill ok and no promotion [then] own_beatgrid bpm, [else stop].
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("STANDALONE-02")

STABLE_ID = "a" * 40
BPM = 128.0
FINGERPRINT = "sha256:" + "ab" * 32


def _beatgrid_record(stable_id: str) -> AnalysisRecord:
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": BPM}
        for index in range(16)
    ]
    payload = {
        "beats": beats,
        "bpm": BPM,
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
        bpm=BPM,
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
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Standalone BPM",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path=str(tmp_path / "track.wav"),
        )
    finally:
        writer.close()
        conn.close()
    upsert_record(_beatgrid_record(STABLE_ID), db_path=state_path)
    conn_check = open_conn(state_path)
    try:
        assert not selection.lane_is_promoted(conn_check, "beatgrid")
    finally:
        conn_check.close()
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    app = create_app()
    app.state.backend = SqliteBackend(state_path)
    app.state.state_db_path = str(state_path)
    app.state.analysis_db_path = str(state_path)
    with TestClient(app) as test_client:
        yield test_client


def test_unmapped_own_backfill_serves_bpm_without_promotion(client: TestClient) -> None:
    """[if] unmapped own backfill ok and no promotion [then] own_beatgrid bpm, [else stop]."""
    response = client.get(f"/api/v1/tracks/{STABLE_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["bpm"] == BPM
    bpm_prov = body["provenance"]["bpm"]
    assert bpm_prov["source"].startswith("own_beatgrid.")
    assert bpm_prov["source"] != "rekordbox"


def test_rekordbox_installed_does_not_change_unmapped_serving(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] pyrekordbox importable but track unmapped [then] same bpm serving, [else stop]."""
    fake_rb: Any = object()
    monkeypatch.setitem(__import__("sys").modules, "pyrekordbox", fake_rb)
    baseline = client.get(f"/api/v1/tracks/{STABLE_ID}").json()
    with_rb = client.get(f"/api/v1/tracks/{STABLE_ID}").json()
    assert with_rb["bpm"] == baseline["bpm"]
    assert with_rb["provenance"]["bpm"]["source"] == baseline["provenance"]["bpm"]["source"]
