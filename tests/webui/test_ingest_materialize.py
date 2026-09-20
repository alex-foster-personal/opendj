"""LIBUX-16: materialize staged ingest batch into state.db (issue #3182).

[if] a staged batch contains new audio [then] materialize returns stable_ids [else stop].
[if] upload skips an exact duplicate and stages a new file [then] both resolve after materialize [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.fingerprints import ChromaprintMissing
from apps.shared.paths import AUDIO_EXTENSIONS
from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_materialize as ingest_materialize_mod
from apps.webui.server.routes import ingest_upload as ingest_upload_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"

pytestmark = pytest.mark.requirement("LIBUX-16")


@pytest.fixture
def app(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_db = data_dir / "state" / "state.db"
    open_state_rw(state_db).close()
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))

    app = FastAPI()
    app.include_router(ingest_upload_mod.router, prefix="/api/v1")
    app.include_router(ingest_materialize_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    app.state.state_db_path = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed_track(app, sid, path, duration_ms=200_000):
    conn = sqlite3.connect(app.state.state_db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "duration_ms, file_path, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            sid,
            "inferred",
            f"t-{sid}",
            "[]",
            duration_ms,
            str(path),
            "2026-08-28T00:00:00Z",
            "2026-08-28T00:00:00Z",
        ),
    )
    conn.commit()
    conn.close()


@pytest.mark.requires_audio_stack
def test_materialize_returns_stable_ids(client, app):
    src = FIXTURES / "src-128.mp3"
    up = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "agnes"},
    )
    assert up.status_code == 200
    batch = up.json()["batch"]

    mat = client.post(f"/api/v1/ingest/batch/{batch}/materialize")
    assert mat.status_code == 200
    body = mat.json()
    assert body["batch"] == batch
    assert len(body["tracks"]) == 1
    assert body["tracks"][0]["relative_path"] == src.name
    assert body["tracks"][0]["inserted"] is True
    stable_id = body["tracks"][0]["stable_id"]
    assert stable_id

    conn = sqlite3.connect(app.state.state_db)
    row = conn.execute(
        "SELECT stable_id FROM tracks WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    conn.close()
    assert row is not None


@pytest.mark.requires_audio_stack
def test_materialize_mixed_new_and_exact_duplicate(client, app, monkeypatch):
    dup_src = FIXTURES / "src-128.mp3"
    new_src = FIXTURES / "src-320.mp3"
    import mutagen

    dur_ms = int(mutagen.File(dup_src).info.length * 1000)
    _seed_track(app, "dup01", dup_src, duration_ms=dur_ms)

    real_compute = ingest_upload_mod.compute

    def _compute(path):
        if Path(path).name == dup_src.name:
            return real_compute(dup_src)
        raise ChromaprintMissing()

    monkeypatch.setattr(ingest_upload_mod, "compute", _compute)

    up = client.post(
        "/api/v1/ingest/upload",
        files=[
            ("files", (dup_src.name, dup_src.read_bytes(), "audio/mpeg")),
            ("files", (new_src.name, new_src.read_bytes(), "audio/mpeg")),
        ],
        data={"batch": "mixed-folder"},
    )
    assert up.status_code == 200
    results = up.json()["results"]
    dup_row = next(r for r in results if r["filename"] == dup_src.name)
    new_row = next(r for r in results if r["filename"] == new_src.name)
    assert dup_row["verdict"] == "skipped_duplicate"
    assert dup_row["duplicate_of"]["stable_id"] == "dup01"
    assert new_row["verdict"] == "new"

    batch = up.json()["batch"]
    mat = client.post(f"/api/v1/ingest/batch/{batch}/materialize")
    assert mat.status_code == 200
    tracks = {t["relative_path"]: t for t in mat.json()["tracks"]}
    assert new_src.name in tracks
    assert tracks[new_src.name]["inserted"] is True

    dest = Path(up.json()["dest_dir"])
    staged = [
        p
        for p in dest.rglob("*")
        if p.is_file()
        and not p.name.endswith(".part")
        and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    assert len(staged) == 1
    assert staged[0].name == new_src.name
