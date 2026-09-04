"""GET /rb-meta for a locally imported track (no rekordbox vendor mapping).

Cloud-buildable: builds a synthetic state.db (apps.shared.state.db) in
tmp_path with a track that has a real file_path but NO track_vendor_ids row --
the normal first-run state after a folder import, and every generated e2e
fixture library. No data/master.plain.db needed, so this runs in CI.

Before Fri 28 Aug 2026 the endpoint answered 404 VENDOR_MAPPING_NOT_FOUND for
those rows, so a purely local library logged one unsuppressable console error
per visible row. It now mirrors get_track_anlz's fallback: an empty-but-valid
payload, with the two non-rekordbox facts (file_exists, quality) still
measured off the state layer's own file_path.

Regression one-liners:
  - if /rb-meta 404s for a track with no rekordbox vendor mapping then broken
  - if the local payload invents a vendor_id/genre/artwork/analysis/cue fact then broken
  - if file_exists or quality for a local track disagree with disk truth then broken
  - if a local track whose file is gone reports file_exists true then broken
  - if a genuinely unknown stable_id stops 404ing TRACK_NOT_FOUND then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.routes.rb_assets import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

LOCAL_SID = "c" * 40
GONE_SID = "d" * 40
UNKNOWN_SID = "0" * 40
DURATION_MS = 300_000


def _insert_local_track(path: Path, stable_id: str, file_path: str) -> None:
    """A track row with a file_path and deliberately NO vendor mapping."""
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, duration_ms, "
            "file_path, created_at, updated_at) "
            "VALUES (?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
            (stable_id, DURATION_MS, file_path),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def audio_file(tmp_path: Path) -> Path:
    """A real on-disk mp3-shaped file so quality is measured, not guessed."""
    path = tmp_path / "imported track.mp3"
    path.write_bytes(b"\x00" * 3_000_000)
    return path


@pytest.fixture
def client(
    tmp_path: Path, audio_file: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    state_path = tmp_path / "state.db"
    _insert_local_track(state_path, LOCAL_SID, str(audio_file))
    _insert_local_track(state_path, GONE_SID, str(tmp_path / "moved away.mp3"))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    # MASTER_PLAIN_DB stays pointed at a path that does not exist: a local
    # library has no rekordbox db, and the endpoint must never need one.
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")

    app = FastAPI()
    app.state.backend = make_backend()
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _meta(client: TestClient, stable_id: str) -> dict:
    response = client.get(f"/api/v1/tracks/{stable_id}/rb-meta")
    assert response.status_code == 200, response.text
    return response.json()


def test_local_track_serves_200_not_404(client: TestClient) -> None:
    meta = _meta(client, LOCAL_SID)
    assert meta["stable_id"] == LOCAL_SID
    assert meta["vendor"] == "local"


def test_local_track_invents_no_rekordbox_facts(client: TestClient) -> None:
    meta = _meta(client, LOCAL_SID)
    assert meta["vendor_id"] is None
    assert meta["genre"] is None
    assert meta["comment"] is None
    assert meta["analysis_available"] is False
    assert meta["beatgrid_issue"] is None
    assert meta["cue_count"] == 0


def test_local_track_flags_match_disk_truth(
    client: TestClient, audio_file: Path
) -> None:
    meta = _meta(client, LOCAL_SID)
    assert meta["folder_path"] == str(audio_file)
    assert meta["file_exists"] is True
    assert meta["is_streaming"] is False
    assert meta["duration_s"] == DURATION_MS // 1000
    quality = meta["quality"]
    assert quality["container"] == ".mp3"
    # Measured from the real size + duration, never a guessed rung.
    assert quality["kbps"] == round(audio_file.stat().st_size * 8 / DURATION_MS)


def test_local_track_with_missing_file_reports_it(client: TestClient) -> None:
    meta = _meta(client, GONE_SID)
    assert meta["vendor"] == "local"
    assert meta["file_exists"] is False
    assert meta["quality"]["venue"] is None


def test_unknown_stable_id_still_404s_loudly(client: TestClient) -> None:
    response = client.get(f"/api/v1/tracks/{UNKNOWN_SID}/rb-meta")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "TRACK_NOT_FOUND"
