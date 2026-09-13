"""LIBMX-13: ingest flow papercuts (issue #2453, FLOW-01/02/03/04).

[if] a detected USB drive is clicked [then] an import action is reachable from that row, not just a yours/not-yours classifier, [else stop].
[if] a folder containing audio files is dropped onto the ingest surface [then] the files inside it are found and staged, not reported as no audio files, [else stop].
[if] a batch is staged and awaiting a manual Rekordbox import [then] a persistent UI indicator says so until the user confirms it is done, [else stop].
[if] a possible-dup match is shown during staging [then] the user can accept or reject it inline, not just read a status line, [else stop].
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state.db import open_rw as open_state_rw
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_pending as ingest_pending_mod
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
    app.include_router(ingest_pending_mod.router, prefix="/api/v1")
    app.include_router(ingest_upload_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_pending_lists_staged_batch(client, app):
    batch = ingest_mod.INGEST_INBOX / "drop-20260101-1200"
    batch.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "src-128.mp3", batch / "track.mp3")

    r = client.get("/api/v1/ingest/pending")
    assert r.status_code == 200
    batches = r.json()["batches"]
    assert len(batches) == 1
    assert batches[0]["name"] == "drop-20260101-1200"
    assert batches[0]["file_count"] == 1
    assert batches[0]["awaiting_rb"] is True


def test_pending_omits_empty_and_part_only(client, app):
    empty = ingest_mod.INGEST_INBOX / "drop-empty"
    empty.mkdir(parents=True)

    part_only = ingest_mod.INGEST_INBOX / "drop-hold"
    part_only.mkdir(parents=True)
    (part_only / "track.mp3.part").write_bytes(b"x")

    r = client.get("/api/v1/ingest/pending")
    assert r.json()["batches"] == []


def test_pending_hides_after_confirm(client, app):
    batch = ingest_mod.INGEST_INBOX / "drop-done"
    batch.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "src-128.mp3", batch / "a.mp3")

    r = client.post("/api/v1/ingest/pending/drop-done/confirm")
    assert r.status_code == 204
    assert (batch / ".rb-imported").exists()

    r2 = client.get("/api/v1/ingest/pending")
    assert r2.json()["batches"] == []


def test_confirm_unknown_batch_404(client):
    r = client.post("/api/v1/ingest/pending/missing-batch/confirm")
    assert r.status_code == 404


def test_confirm_bad_batch_name_422(client):
    r = client.post("/api/v1/ingest/pending/invalid!batch/confirm")
    assert r.status_code == 422
