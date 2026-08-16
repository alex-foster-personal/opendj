"""Tests for /api/v1/ingest (config, coverage, refresh, upload+dedup).

Hermetic: module path constants are monkeypatched to tmp dirs and a seeded
temp state.db; the refresh worker's subprocess runner is replaced with a
recorder (the real CLIs have their own suites). Upload/dedup tests use the
committed phase7 mp3 fixtures and real chromaprint (fpcalc) - marked
``requires_audio_stack`` so they skip cleanly where the audio stack is absent.

Regression lines:
  - if PUT /ingest/config accepts an unknown step id then broken
  - if coverage counts a broken-link track as missing-analysis then broken
  - if POST /ingest/refresh can run twice concurrently then broken
  - if an uploaded exact duplicate is staged without force then broken
  - if force=True does not stage a duplicate then broken
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes import ingest as ingest_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.fixture
def app(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT, "
        "artists_json TEXT, duration_ms INTEGER, file_path TEXT)"
    )
    conn.execute("CREATE TABLE analysis (stable_id TEXT, backend TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", tmp_path / "stems")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(ingest_mod, "_job", None)

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed_track(app, sid, path, duration_ms=200_000, analysed=False):
    conn = sqlite3.connect(app.state.state_db)
    conn.execute(
        "INSERT INTO tracks VALUES (?,?,?,?,?)",
        (sid, f"t-{sid}", "[]", duration_ms, str(path)),
    )
    if analysed:
        conn.execute("INSERT INTO analysis VALUES (?, 'librosa+madmom')", (sid,))
    conn.commit()
    conn.close()


# ----- config ---------------------------------------------------------------

def test_config_defaults_and_persist(client):
    out = client.get("/api/v1/ingest/config").json()
    by_id = {s["id"]: s for s in out["steps"]}
    assert by_id["analysis"]["enabled"] is True
    assert by_id["stems"]["enabled"] is False
    assert Path(out["path"]).exists()


def test_config_put_roundtrip(client):
    client.put("/api/v1/ingest/config", json={"enabled": {"stems": True}})
    out = client.get("/api/v1/ingest/config").json()
    assert {s["id"]: s["enabled"] for s in out["steps"]}["stems"] is True


def test_config_rejects_unknown_step(client):
    r = client.put("/api/v1/ingest/config", json={"enabled": {"frobnicate": True}})
    assert r.status_code == 422


# ----- coverage -------------------------------------------------------------

def test_coverage_excludes_broken_links(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real, analysed=True)
    _seed_track(app, "bbb", real, analysed=False)
    _seed_track(app, "ccc", tmp_path / "gone.mp3")   # broken link
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["on_disk"] == 2
    assert out["unreachable"] == 1
    assert out["missing"]["analysis"] == 1           # bbb only, never ccc
    assert out["missing"]["stems"] == 2


# ----- refresh job ----------------------------------------------------------

def test_refresh_runs_enabled_steps_and_blocks_concurrent(client, monkeypatch):
    ran: list[list[str]] = []

    def fake_run_cli(job, argv):
        ran.append(argv)
        time.sleep(0.15)

    monkeypatch.setattr(ingest_mod, "_run_cli", fake_run_cli)
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": False, "vocals": True}})
    r = client.post("/api/v1/ingest/refresh")
    assert r.status_code == 202
    assert client.post("/api/v1/ingest/refresh").status_code == 409

    for _ in range(80):
        status = client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["phase"] == "done", status["log_tail"]
    assert status["steps_completed"] == ["analysis", "vocals"]
    # vocals step ran its CLI; analysis had no targets so no CLI call for it
    assert any("apps.vocals" in " ".join(argv) for argv in ran)


def test_refresh_error_surfaces(client, monkeypatch):
    def boom(job, argv):
        raise RuntimeError("worker exploded")

    monkeypatch.setattr(ingest_mod, "_run_cli", boom)
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": False, "vocals": True}})
    client.post("/api/v1/ingest/refresh")
    for _ in range(80):
        status = client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["phase"] == "error"
    assert "worker exploded" in status["error"]


def test_refresh_rejects_no_steps(client):
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": False, "stems": False, "vocals": False}})
    assert client.post("/api/v1/ingest/refresh").status_code == 422


# ----- upload + duplicates --------------------------------------------------

def test_upload_rejects_non_audio(client):
    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", ("notes.txt", b"hello", "text/plain"))],
        data={"batch": "b1"},
    )
    assert r.status_code == 422


def test_upload_rejects_bad_batch_name(client):
    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", ("a.mp3", b"x", "audio/mpeg"))],
        data={"batch": "../evil"},
    )
    assert r.status_code == 422


@pytest.mark.requires_audio_stack
def test_upload_stages_new_file(client, app):
    src = FIXTURES / "src-128.mp3"
    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "new-batch"},
    )
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["skipped_duplicate"] is False
    assert Path(res["staged_path"]).exists()
    assert res["duration_s"] > 0


@pytest.mark.requires_audio_stack
def test_upload_skips_exact_duplicate_unless_forced(client, app):
    src = FIXTURES / "src-128.mp3"
    import mutagen
    dur_ms = int(mutagen.File(src).info.length * 1000)
    _seed_track(app, "dup01", src, duration_ms=dur_ms)

    r = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "dups"},
    )
    res = r.json()["results"][0]
    assert res["skipped_duplicate"] is True
    assert res["staged_path"] is None
    assert res["duplicate_of"]["stable_id"] == "dup01"
    assert res["duplicate_of"]["score"] >= 0.92

    r2 = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "dups", "force": "true"},
    )
    res2 = r2.json()["results"][0]
    assert res2["skipped_duplicate"] is False
    assert Path(res2["staged_path"]).exists()


def test_refresh_batch_scope_runs_analysis_only(client, monkeypatch, tmp_path):
    ran: list[list[str]] = []
    monkeypatch.setattr(ingest_mod, "_run_cli", lambda job, argv: ran.append(argv))
    batch = ingest_mod.INGEST_INBOX / "scoped"
    batch.mkdir(parents=True)
    (batch / "a.mp3").write_bytes(b"x" * 2048)
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": True, "vocals": True}})
    r = client.post("/api/v1/ingest/refresh", json={"batch_dir": str(batch)})
    assert r.status_code == 202
    for _ in range(80):
        status = client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    assert status["phase"] == "done", status["log_tail"]
    assert status["steps_completed"] == ["analysis"]
    assert any("apps.analysis.run" in " ".join(a) for a in ran)
    assert not any("apps.stems" in " ".join(a) for a in ran)
    assert any("skipped for batch scope" in ln for ln in status["log_tail"])


def test_refresh_batch_scope_rejects_outside_inbox(client, tmp_path):
    r = client.post("/api/v1/ingest/refresh", json={"batch_dir": str(tmp_path)})
    assert r.status_code == 422


def test_coverage_on_never_analysed_library(client, app):
    conn = sqlite3.connect(app.state.state_db)
    conn.execute("DROP TABLE analysis")
    conn.commit()
    conn.close()
    out = client.get("/api/v1/ingest/coverage")
    assert out.status_code == 200
    assert out.json()["missing"]["analysis"] == 0
