"""STANDALONE-04: native beatgrid backfill must not degrade the deck grid.

Uses the real analysis router and store paths; no mocks or fabricated records.
ANLZ availability goes through the production probe: the fixture tracks carry
no rekordbox vendor mapping, so ``resolve_content`` raises
``VENDOR_MAPPING_NOT_FOUND`` and the probe reports no ANLZ by definition.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection as sel
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from tests.analysis_contract.conftest import beatgrid_payload, own_record

DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM
OWN_BPM = 128.0

SID_UNMAPPED = "sid-standalone04-unmapped"
SID_FAILED = "sid-standalone04-failed"

_TS = "2026-09-01T00:00:00Z"


def _legacy_record(
    sid: str,
    *,
    analyzed_at: datetime = datetime(2026, 9, 1, tzinfo=UTC),
) -> AnalysisRecord:
    downbeats = [i * BAR_S for i in range(int(DURATION_S / BAR_S))]
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=analyzed_at,
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=BPM,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="8m",
        key_confidence=0.9,
        energy=6,
        onsets_s=[],
        downbeats_s=downbeats,
        features_blob={"rms": [], "rms_hop": 512},
    )


def _add_unmapped_track(db: Path, sid: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            sid,
            "inferred",
            f"title-{sid}",
            "[]",
            str(db.parent / f"{sid}.flac"),
            _TS,
            _TS,
            None,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def _clean_toggles() -> Iterator[None]:
    sel.reset_toggles()
    yield
    sel.reset_toggles()


@pytest.fixture()
def standalone_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    db = tmp_path / "state.db"
    upsert_record(_legacy_record(SID_UNMAPPED), db_path=db)
    upsert_record(_legacy_record(SID_FAILED), db_path=db)
    upsert_record(
        own_record(
            stable_id=SID_FAILED,
            result=LaneResult(status="failed", reason="no_trackable_pulse"),
        ),
        db_path=db,
    )
    _add_unmapped_track(db, SID_UNMAPPED)
    _add_unmapped_track(db, SID_FAILED)

    monkeypatch.setattr(rb_config, "STATE_DB", db)
    app = create_app(
        backend=InMemoryBackend(),
        state_db_path=str(db),
        mount_frontend=False,
        port=18722,
        frontend_port=19424,
    )
    with TestClient(app) as test_client:
        yield test_client


def _set_source(client: TestClient, source: str) -> None:
    r = client.put("/api/v1/analysis/source", json={"lane": "beatgrid", "toggle": source})
    assert r.status_code == 200, r.text


@pytest.mark.requirement("STANDALONE-04")
def test_successful_native_backfill_does_not_shrink_the_legacy_fallback_grid(
    standalone_client: TestClient,
) -> None:
    """[if] grid exists before backfill [then] ok own keeps >= beat count, [else stop]."""
    before = standalone_client.get(
        f"/api/v1/tracks/{SID_UNMAPPED}/beatgrid-fallback"
    )
    assert before.status_code == 200, before.text
    before_count = before.json()["beatgrid"]["beat_count"]
    assert before_count > 0

    db = Path(standalone_client.app.state.analysis_db_path)
    upsert_record(
        own_record(
            stable_id=SID_UNMAPPED,
            result=LaneResult(status="ok", payload=beatgrid_payload(bpm=OWN_BPM)),
        ),
        db_path=db,
    )

    after = standalone_client.get(f"/api/v1/tracks/{SID_UNMAPPED}/beatgrid-fallback")
    assert after.status_code == 200, after.text
    assert after.json()["beatgrid"]["beat_count"] >= before_count

    _set_source(standalone_client, "own")
    own_anlz = standalone_client.get(f"/api/v1/tracks/{SID_UNMAPPED}/anlz")
    assert own_anlz.status_code == 200, own_anlz.text
    own_body = own_anlz.json()
    assert own_body["beatgrid_source"] == "own"
    assert own_body["beatgrid"]["status"] == "ok"
    assert own_body["beatgrid"]["beat_count"] > 0


@pytest.mark.requirement("STANDALONE-04")
def test_failed_own_beatgrid_is_inert_and_blocks_default_legacy_resurrection(
    standalone_client: TestClient,
) -> None:
    """[if] own record is failed [then] grid is inert with reason, [else stop]."""
    _set_source(standalone_client, "own")
    anlz = standalone_client.get(f"/api/v1/tracks/{SID_FAILED}/anlz")
    assert anlz.status_code == 200, anlz.text
    body = anlz.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid"] == {
        "source": "own",
        "status": "failed",
        "reason": "no_trackable_pulse",
        "beat_count": 0,
        "beats": [],
    }

    fallback = standalone_client.get(f"/api/v1/tracks/{SID_FAILED}/beatgrid-fallback")
    assert fallback.status_code == 404, fallback.text
    assert fallback.json()["detail"]["code"] == "BEATGRID_FALLBACK_NOT_FOUND"

    explicit = standalone_client.get(
        f"/api/v1/tracks/{SID_FAILED}/beatgrid-fallback",
        params={"backend": "librosa+madmom"},
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["backend"] == "librosa+madmom"
    assert explicit.json()["beatgrid"]["beat_count"] > 0
