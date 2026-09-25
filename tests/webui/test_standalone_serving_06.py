"""STANDALONE-06: default source follows the library, not a constant."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.lanes import LANES, LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import open_conn, upsert_record
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay
from apps.webui.server.routes.rb_assets import router as assets_router
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = pytest.mark.requirement("STANDALONE-06")

STABLE_ID = "d" * 40
BPM = 122.0
FINGERPRINT = "sha256:" + "ef" * 32


def _beatgrid_record(stable_id: str) -> AnalysisRecord:
    beats = [
        {"t": round(index * 0.5, 5), "n": (index % 4) + 1, "bpm": BPM}
        for index in range(8)
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
        duration_s=32.0,
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


def test_unmapped_track_defaults_every_lane_to_own(tmp_path: Path) -> None:
    """[if] no rekordbox mapping [then] effective source own for all lanes, [else stop]."""
    state_path = tmp_path / "state.db"
    open_conn(state_path).close()
    conn = open_conn(state_path)
    try:
        for lane in LANES:
            assert (
                selection.effective_source_for_track(conn, lane, has_rb_mapping=False)
                == "own"
            )
    finally:
        conn.close()


def test_mapped_track_keeps_rekordbox_beatgrid_when_default_rbx(tmp_path: Path) -> None:
    """[if] mapped track with rbx default [then] own overlay no-op, [else stop]."""
    state_path = tmp_path / "state.db"
    open_conn(state_path).close()
    upsert_record(_beatgrid_record(STABLE_ID), db_path=state_path)
    payload = {
        "beatgrid": {
            "source": own_beatgrid_overlay.SOURCE_REKORDBOX,
            "beat_count": 1,
            "beats": [{"n": 1, "bpm": 100.0, "t": 0.0}],
        }
    }
    served = own_beatgrid_overlay.apply_own_beatgrid(
        payload, STABLE_ID, state_path, has_rb_mapping=True
    )
    assert served["beatgrid"]["source"] == own_beatgrid_overlay.SOURCE_REKORDBOX


@pytest.fixture
def anlz_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    audio = tmp_path / "t.wav"
    audio.write_bytes(b"\x00" * 128)
    try:
        writer.upsert_track(
            stable_id=STABLE_ID,
            stable_id_tier="inferred",
            title="Anlz own",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=60_000,
            file_path=str(audio),
        )
    finally:
        writer.close()
        conn.close()
    upsert_record(_beatgrid_record(STABLE_ID), db_path=state_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.analysis_db_path = state_path
    app.include_router(assets_router, prefix="/api/v1")
    app.include_router(analysis_router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def test_unmapped_anlz_beatgrid_source_is_own(anlz_client: TestClient) -> None:
    """[if] unmapped with own record [then] /anlz beatgrid_source own, [else stop]."""
    response = anlz_client.get(f"/api/v1/tracks/{STABLE_ID}/anlz")
    assert response.status_code == 200
    body = response.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid"]["source"] == "own"
