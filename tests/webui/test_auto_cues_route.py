"""Issue #13170: production create_app must serve GET auto-cues ((private path)).

Regression one-liners:
- [if] analyzed library track requests auto-cues [then] HTTP 200 with proposals, [else stop].
- [if] library track has no analysis [then] HTTP 200 empty backend none not 404, [else stop].
- [if] auto-cues route is missing from create_app [then] unknown sid gets generic 404, [else stop].
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes.analysis import AUTO_CUES_SOURCE
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.testclient_host import TEST_HOST_BASE_URL

pytestmark = pytest.mark.requirement("SEARCH-NAV-13170")

DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM

SID_ANALYZED = "sid-analyzed"
SID_UNANALYZED = "sid-unanalyzed"
SID_UNKNOWN = "sid-unknown"


def _record(sid: str) -> AnalysisRecord:
    downbeats = [i * BAR_S for i in range(int(DURATION_S / BAR_S))]
    onsets = [round(0.25 + i * 1.9, 3) for i in range(30)]
    rms = [0.1] * 200 + [0.9] * 40 + [0.1] * 200
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=BPM,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="8m",
        key_confidence=0.9,
        energy=6,
        onsets_s=onsets,
        downbeats_s=downbeats,
        features_blob={"rms": rms, "rms_hop": 512},
    )


@pytest.fixture
def auto_cues_client(tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        for sid in (SID_ANALYZED, SID_UNANALYZED):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=sid,
                artists=["Test Artist"],
                album=None,
                isrc=None,
                duration_ms=int(DURATION_S * 1000),
                file_path=f"/music/{sid}.mp3",
            )
    finally:
        writer.close()
        conn.close()

    upsert_record(_record(SID_ANALYZED), db_path=state_path)

    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app, base_url=TEST_HOST_BASE_URL) as client:
        yield client


def test_auto_cues_analyzed_library_track_returns_proposals(
    auto_cues_client: TestClient,
) -> None:
    """[if] production app serves analyzed auto-cues [then] proposals are non-empty, [else stop]."""
    response = auto_cues_client.get(f"/api/v1/tracks/{SID_ANALYZED}/auto-cues")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"] is True
    assert body["stable_id"] == SID_ANALYZED
    assert body["source"] == AUTO_CUES_SOURCE
    assert body["backend"] == "librosa+madmom"
    assert body["proposals"]
    assert all(p["source"] == AUTO_CUES_SOURCE for p in body["proposals"])


def test_auto_cues_unanalyzed_library_track_returns_200_empty(
    auto_cues_client: TestClient,
) -> None:
    """[if] unanalyzed library track requests auto-cues [then] empty 200 not route 404, [else stop]."""
    response = auto_cues_client.get(f"/api/v1/tracks/{SID_UNANALYZED}/auto-cues")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["proposal"] is True
    assert body["stable_id"] == SID_UNANALYZED
    assert body["source"] == AUTO_CUES_SOURCE
    assert body["backend"] == "none"
    assert body["backend_version"] == "none"
    assert body["proposals"] == []


def test_auto_cues_route_mounted_in_production_app(
    auto_cues_client: TestClient,
) -> None:
    """[if] auto-cues route is mounted on create_app [then] unknown sid is ANALYSIS_NOT_FOUND, [else stop]."""
    response = auto_cues_client.get(f"/api/v1/tracks/{SID_UNKNOWN}/auto-cues")
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "ANALYSIS_NOT_FOUND"
