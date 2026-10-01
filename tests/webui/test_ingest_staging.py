"""LIBMX-13: ingest flow papercuts (issue #2453, FLOW-01/02/03/04).

[if] a detected USB drive is clicked [then] an import action is reachable from that row, not just a yours/not-yours classifier, [else stop].
[if] a folder containing audio files is dropped onto the ingest surface [then] the files inside it are found and staged, not reported as no audio files, [else stop].
[if] a batch is staged and awaiting a manual Rekordbox import [then] a persistent UI indicator says so until the user confirms it is done, [else stop].
[if] a possible-dup match is shown during staging [then] the user can accept or reject it inline, not just read a status line, [else stop].
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared import tag_reader
from apps.shared.fingerprints import ChromaprintMissing
from apps.shared.paths import AUDIO_EXTENSIONS
from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_upload as ingest_upload_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"

pytestmark = pytest.mark.requirement("LIBMX-13")


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
    app.state.state_db = state_db
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
        (sid, "inferred", f"t-{sid}", "[]", duration_ms, str(path),
         "2026-08-28T00:00:00Z", "2026-08-28T00:00:00Z"),
    )
    conn.commit()
    conn.close()


@pytest.mark.requires_audio_stack
def test_possible_dup_held_as_part(client, app, monkeypatch):
    src = FIXTURES / "src-128.mp3"
    dur_ms = int(tag_reader.read_tags(Path(src)).duration_s * 1000)
    other = FIXTURES / "src-320.mp3"
    _seed_track(app, "near01", other, duration_ms=dur_ms)

    monkeypatch.setattr(
        ingest_upload_mod, "compute",
        lambda _p: (_ for _ in ()).throw(ChromaprintMissing()),
    )

    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "hold"},
    )
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["verdict"] == "possible_duplicate"
    assert res["staged_path"] is None
    assert res["duplicate_of"] is not None

    dest = Path(r.json()["dest_dir"])
    assert not (dest / src.name).exists()
    assert (dest / f"{src.name}.part").exists()


@pytest.mark.requires_audio_stack
def test_decide_accept_renames_part(client, app, monkeypatch):
    src = FIXTURES / "src-128.mp3"
    dur_ms = int(tag_reader.read_tags(Path(src)).duration_s * 1000)
    other = FIXTURES / "src-320.mp3"
    _seed_track(app, "near02", other, duration_ms=dur_ms)

    monkeypatch.setattr(
        ingest_upload_mod, "compute",
        lambda _p: (_ for _ in ()).throw(ChromaprintMissing()),
    )

    up = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "accept"},
    )
    batch = up.json()["batch"]
    dest = Path(up.json()["dest_dir"])

    r = client.post(
        "/api/v1/ingest/upload/decide",
        json={"batch": batch, "filename": src.name, "action": "accept"},
    )
    assert r.status_code == 200
    res = r.json()
    assert res["verdict"] == "new"
    assert Path(res["staged_path"]).exists()
    assert not (dest / f"{src.name}.part").exists()


@pytest.mark.requires_audio_stack
def test_decide_reject_unlinks_part(client, app, monkeypatch):
    src = FIXTURES / "src-128.mp3"
    dur_ms = int(tag_reader.read_tags(Path(src)).duration_s * 1000)
    other = FIXTURES / "src-320.mp3"
    _seed_track(app, "near03", other, duration_ms=dur_ms)

    monkeypatch.setattr(
        ingest_upload_mod, "compute",
        lambda _p: (_ for _ in ()).throw(ChromaprintMissing()),
    )

    up = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "reject"},
    )
    batch = up.json()["batch"]
    dest = Path(up.json()["dest_dir"])

    r = client.post(
        "/api/v1/ingest/upload/decide",
        json={"batch": batch, "filename": src.name, "action": "reject"},
    )
    assert r.status_code == 200
    assert r.json()["verdict"] == "skipped_duplicate"
    assert not (dest / f"{src.name}.part").exists()
    assert not (dest / src.name).exists()

    staged = [
        p for p in dest.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    assert staged == []


@pytest.mark.requires_audio_stack
def test_confirmed_dup_still_skips_unless_forced(client, app):
    src = FIXTURES / "src-128.mp3"
    dur_ms = int(tag_reader.read_tags(Path(src)).duration_s * 1000)
    _seed_track(app, "dup01", src, duration_ms=dur_ms)

    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "dups"},
    )
    res = r.json()["results"][0]
    assert res["verdict"] == "skipped_duplicate"
    assert res["skipped_duplicate"] is True

    r2 = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "dups", "force": "true"},
    )
    res2 = r2.json()["results"][0]
    assert res2["verdict"] == "new"
    assert Path(res2["staged_path"]).exists()


def test_relative_path_stages_under_subdir(client, app, monkeypatch):
    src = FIXTURES / "src-128.mp3"
    monkeypatch.setattr(ingest_upload_mod, "_duration_s", lambda _p: None)
    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", ("subdir/a.mp3", src.read_bytes(), "audio/mpeg"))],
        data={"batch": "nested"},
    )
    assert r.status_code == 200
    dest = Path(r.json()["dest_dir"])
    assert (dest / "subdir" / "a.mp3").exists()


def test_relative_path_rejects_traversal(client, app):
    src = FIXTURES / "src-128.mp3"
    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", ("../evil.mp3", src.read_bytes(), "audio/mpeg"))],
        data={"batch": "evil"},
    )
    assert r.status_code == 422
