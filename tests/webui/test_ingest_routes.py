"""Tests for /api/v1/ingest (config, coverage, refresh, upload+dedup).

Hermetic but REAL: module path constants are monkeypatched to tmp dirs, the
state.db is created through the real state-layer migrations, and
``MDT_DATA_DIR`` points refresh subprocesses at the same tmp data dir - so
refresh tests execute the production ``apps.analysis.run`` CLI end to end
(parser, backend, store write) instead of a recorder stub. Upload/dedup and
refresh tests use the committed phase7 mp3 fixtures and the real audio
stack (librosa/madmom, fpcalc) - marked ``requires_audio_stack``.

Regression lines:
  - if PUT /ingest/config accepts an unknown step id then broken
  - if coverage counts a broken-link track as missing-analysis then broken
  - if POST /ingest/refresh can run twice concurrently then broken
  - if a library refresh stores analysis under a non-canonical id then broken
  - if a failing runner does not surface phase=error then broken
  - if an uploaded exact duplicate is staged without force then broken
  - if force=True does not stage a duplicate then broken
  - if a re-uploaded filename overwrites the staged file then broken
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.analysis import store as analysis_store
from apps.lyrics import cache as lyrics_cache
from apps.shared.state.db import open_rw as open_state_rw
from apps.stems import artifacts as stem_artifacts
from apps.vocals import cache as vocals_cache
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_upload as ingest_upload_mod

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase7-dedup"


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Real state-layer schema in a canonical data-dir layout; MDT_DATA_DIR
    # makes every refresh SUBPROCESS (apps.analysis.run) resolve its store
    # to this same file, so tests observe the production write path.
    data_dir = tmp_path / "data"
    state_db = data_dir / "state" / "state.db"
    open_state_rw(state_db).close()
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))

    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", tmp_path / "stems")
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(ingest_mod._JOBS, "current", None)
    monkeypatch.setattr(ingest_mod._JOBS, "last_unmapped", None)

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    app.include_router(ingest_upload_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def _seed_track(app, sid, path, duration_ms=200_000, analysed=False):
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
    if analysed:
        # Through the store's own DDL so the row lives in the REAL analysis
        # schema (the same table apps.analysis.run writes).
        conn = analysis_store.open_conn(app.state.state_db)
        conn.execute(
            "INSERT INTO analysis (stable_id, backend, backend_version, "
            "analyzed_at, duration_s, sample_rate, bpm, bpm_confidence, "
            "key_camelot, key_openkey, key_confidence, energy, energy_source, "
            "record_json) VALUES (?, 'librosa+madmom', 'seed', "
            "'2026-08-28T00:00:00Z', 200.0, 44100, 120.0, 0.9, '8A', '1d', "
            "0.9, 5, 'seed', '{}')",
            (sid,),
        )
        conn.commit()
        conn.close()


def _write_roformer_bundle(root: Path, stable_id: str) -> None:
    """Write a real, aligned v3 bundle so coverage uses its production reader."""
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in stem_artifacts.ROFORMER_PARTS:
        with wave.open(str(bundle / f"{part}.wav"), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44_100)
            output.writeframes(b"\x00\x00" * 24)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stable_id": stable_id,
                "layout": "roformer2",
                "model": {"name": "roformer", "version": "1"},
                "source": {"path": "/music/source.wav", "sha256": "a" * 64},
                "audio": {"sample_rate": 44_100, "frame_count": 12, "channels": 2},
                "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            }
        ),
        encoding="utf-8",
    )


def _wait_refresh(client, timeout_s=600):
    deadline = time.time() + timeout_s
    status = client.get("/api/v1/ingest/refresh/status").json()
    while time.time() < deadline:
        status = client.get("/api/v1/ingest/refresh/status").json()
        if not status["running"]:
            return status
        time.sleep(0.5)
    raise AssertionError(
        f"refresh still running after {timeout_s}s: {status['log_tail']}"
    )


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
    assert out["missing"]["vocals"] == 2
    assert out["missing"]["lyrics"] == 2
    assert out["corrupt"] == {"analysis": 0, "stems": 0, "vocals": 0, "lyrics": 0}


def test_coverage_counts_a_valid_secondary_roformer_bundle(client, app, tmp_path):
    """A RoFormer-only track is covered and never enters the stems refresh queue.

    Configures the roots the same way ``create_app`` does for remote library
    mode (``app.state.stem_roots``), not by monkeypatching the ``stem_roots``
    helper - the route and the worker must read the SAME configured roots a
    real deployment would set, or this test can pass while the endpoint still
    ignores them.
    """
    audio = tmp_path / "real.mp3"
    audio.write_bytes(b"x" * 4096)
    _seed_track(app, "roformer-only", audio)
    roformer_root = tmp_path / "stems-roformer-spike"
    _write_roformer_bundle(roformer_root, "roformer-only")
    app.state.stem_roots = (ingest_mod.DEFAULT_STEMS_DIR, roformer_root)

    coverage = client.get("/api/v1/ingest/coverage").json()
    targets = ingest_mod._library_targets(
        ingest_mod._RefreshJob(0.0, []), ingest_mod._stem_roots(app)
    )

    assert coverage["missing"]["stems"] == 0
    assert targets["stems"] == []


def test_coverage_distinguishes_corrupt_stems(client, app, tmp_path):
    """A stems directory load_stem_bundle rejects is corrupt, not merely missing.

    Coverage keeps corrupt as a subset of missing (refresh still targets it);
    the extra corrupt.stems count is what tells absence apart from garbage.
    """
    audio = tmp_path / "real.mp3"
    audio.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", audio)
    stems_dir = tmp_path / "stems"
    bundle = stems_dir / "aaa"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{", encoding="utf-8")
    app.state.stem_roots = (stems_dir,)

    out = client.get("/api/v1/ingest/coverage").json()
    assert out["on_disk"] == 1
    assert out["missing"]["stems"] == 1
    assert out["corrupt"]["stems"] == 1
    assert out["corrupt"]["analysis"] == 0
    assert out["corrupt"]["vocals"] == 0
    assert out["corrupt"]["lyrics"] == 0


def test_refresh_resolves_configured_stem_roots_through_the_background_worker(
    client, app, tmp_path
):
    """POST /ingest/refresh must resolve ``app.state.stem_roots`` too, not just
    GET /ingest/coverage - the route hands the roots to a background thread
    (``_refresh_worker``), which has no ``Request`` to read them from itself.

    A mutation that made ``start_refresh`` ignore the configured roots and
    fall back to ``stem_roots(DEFAULT_STEMS_DIR)`` passed the whole suite
    silently before this test existed: the coverage test above never drives
    the refresh route, so nothing exercised this half of the fix.
    """
    audio = tmp_path / "real.mp3"
    audio.write_bytes(b"x" * 4096)
    _seed_track(app, "roformer-only", audio)
    roformer_root = tmp_path / "stems-roformer-spike"
    _write_roformer_bundle(roformer_root, "roformer-only")
    app.state.stem_roots = (ingest_mod.DEFAULT_STEMS_DIR, roformer_root)

    client.put(
        "/api/v1/ingest/config",
        json={"enabled": {"analysis": False, "stems": True, "vocals": False}},
    )
    resp = client.post("/api/v1/ingest/refresh", json={"scope": "library"})
    assert resp.status_code == 202
    status = _wait_refresh(client)

    assert status["phase"] == "done"
    assert any("stems: 0 missing" in line for line in status["log_tail"])


def _vocal_worker_result(**overrides):
    base = {
        "schema": vocals_cache.VOCAL_CACHE_SCHEMA,
        "source": vocals_cache.VOCAL_CACHE_SOURCE,
        "fps": 2.0,
        "duration_s": 120.0,
        "coverage_pct": 41.7,
        "regions": [{"start_s": 10.0, "end_s": 60.0, "confidence": 0.84}],
        "params": {"hop_s": 0.5, "on_ratio": 0.1},
        "device": "cpu",
        "timings": {"load_s": 1.0, "separate_s": 100.0},
        "source_sample_rate": 44100,
        "analysis_sample_rate": 44100,
    }
    base.update(overrides)
    return base


def test_coverage_reports_lyrics_completion(client, app, tmp_path):
    """The lyrics dot on the browser panel reads this key - a track with a
    REAL, valid lyrics-cache entry (written through the production
    apps.lyrics.cache writer, not a fabricated file) must count as covered,
    one without must count as missing, and a broken-link track must never
    appear in either."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _seed_track(app, "bbb", real)
    _seed_track(app, "ccc", tmp_path / "gone.mp3")   # broken link
    lyrics_dir = ingest_mod.LYRICS_CACHE_DIR
    lyrics_dir.mkdir(parents=True)
    lyrics_cache.write(
        lyrics_dir / "aaa.json",
        lyrics_cache.Lyrics(
            stable_id="aaa", source="lrclib",
            lines=(lyrics_cache.LyricLine(start_ms=0, text="la la"),),
        ),
    )
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["on_disk"] == 2
    assert out["missing"]["lyrics"] == 1             # bbb only, never ccc
    assert out["corrupt"]["lyrics"] == 0             # a real entry is not corrupt


def test_coverage_rejects_malformed_lyrics_cache_entry(client, app, tmp_path):
    """An empty/truncated/schema-invalid lyrics-cache write (an interrupted
    worker's leftover, exactly what a naive filename-only check would count
    as done - the P1 bug this coverage path used to have) must count as
    MISSING, never as complete."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    lyrics_dir = ingest_mod.LYRICS_CACHE_DIR
    lyrics_dir.mkdir(parents=True)
    (lyrics_dir / "aaa.json").write_text("{}")       # malformed: no schema
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["missing"]["lyrics"] == 1
    assert out["corrupt"]["lyrics"] == 1             # distinct from ordinary missing


def test_coverage_rejects_identity_mismatched_lyrics_cache_entry(client, app, tmp_path):
    """A lyrics-cache file named for one stable_id but whose own contents
    describe a DIFFERENT stable_id (the same identity-mismatch
    LyricsService.fetch guards against) must not count that filename as
    complete."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    lyrics_dir = ingest_mod.LYRICS_CACHE_DIR
    lyrics_dir.mkdir(parents=True)
    lyrics_cache.write(
        lyrics_dir / "aaa.json",
        lyrics_cache.Lyrics(
            stable_id="not-aaa", source="lrclib",
            lines=(lyrics_cache.LyricLine(start_ms=0, text="la la"),),
        ),
    )
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["missing"]["lyrics"] == 1
    assert out["corrupt"]["lyrics"] == 1             # identity mismatch is corruption


def test_coverage_reports_vocals_completion(client, app, tmp_path):
    """The vocals dot: a REAL vocal-cache entry (written through the
    production apps.vocals.cache writer, valid for the on-disk audio file)
    counts as covered."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _seed_track(app, "bbb", real)
    vocal_dir = ingest_mod.VOCAL_CACHE_DIR
    vocals_cache.write_entry(vocal_dir / "aaa.json", _vocal_worker_result(), real)
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["missing"]["vocals"] == 1             # bbb only
    assert out["corrupt"]["vocals"] == 0             # a real entry is not corrupt


def test_coverage_rejects_malformed_vocal_cache_entry(client, app, tmp_path):
    """Same P1 hazard as lyrics: a filename alone is not proof of work.
    A corrupt/invalid vocal-cache JSON must count as missing."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    vocal_dir = ingest_mod.VOCAL_CACHE_DIR
    vocal_dir.mkdir(parents=True)
    (vocal_dir / "aaa.json").write_text("not even json")
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["missing"]["vocals"] == 1
    assert out["corrupt"]["vocals"] == 1             # distinct from ordinary missing


def test_coverage_rejects_stale_vocal_cache_entry(client, app, tmp_path):
    """A vocal-cache entry computed for a PRIOR generation of the audio file
    (the file was since replaced, so its audio_signature no longer matches)
    must count as missing, exactly like the live /anlz merge path treats it -
    never silently served as still-complete."""
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    vocal_dir = ingest_mod.VOCAL_CACHE_DIR
    vocals_cache.write_entry(vocal_dir / "aaa.json", _vocal_worker_result(), real)
    real.write_bytes(b"y" * 8192)                    # replaced -> new signature
    out = client.get("/api/v1/ingest/coverage").json()
    assert out["missing"]["vocals"] == 1
    # THE distinction that matters: stale is not corrupt. A track whose audio
    # was simply replaced needs a routine re-run, not an alarm - conflating
    # the two would make every stale entry scream as if it were corruption.
    assert out["corrupt"]["vocals"] == 0


# ----- refresh job ----------------------------------------------------------

@pytest.mark.requires_audio_stack
def test_refresh_library_scope_writes_canonical_ids(client, app):
    """Real end-to-end regression for the pathid_* identity bug: a library
    refresh must store the analysis row under the CANONICAL tracks.stable_id
    so coverage stops reporting the track as missing."""
    src = FIXTURES / "src-128.mp3"
    _seed_track(app, "aaa", src, analysed=False)
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": False, "vocals": False}})
    r = client.post("/api/v1/ingest/refresh")
    assert r.status_code == 202
    # a second refresh while the real runner is still up front -> 409
    assert client.post("/api/v1/ingest/refresh").status_code == 409
    status = _wait_refresh(client)
    assert status["phase"] == "done", status["log_tail"]
    assert status["steps_completed"] == ["analysis"]
    ids = {
        r[0] for r in sqlite3.connect(app.state.state_db).execute(
            "SELECT stable_id FROM analysis"
        )
    }
    assert ids == {"aaa"}, f"analysis stored under non-canonical ids: {ids}"
    cov = client.get("/api/v1/ingest/coverage").json()
    assert cov["missing"]["analysis"] == 0


@pytest.mark.requires_audio_stack
def test_refresh_error_surfaces_from_real_runner(client, app):
    """A non-audio .mp3 makes the production runner exit 1; the job must
    surface phase=error with the exit, never report done."""
    batch = ingest_mod.INGEST_INBOX / "junk"
    batch.mkdir(parents=True)
    (batch / "junk.mp3").write_bytes(b"x" * 4096)
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": False, "vocals": False}})
    r = client.post("/api/v1/ingest/refresh", json={"batch_dir": str(batch)})
    assert r.status_code == 202
    status = _wait_refresh(client)
    assert status["phase"] == "error", status["log_tail"]
    assert "apps.analysis.run exited 1" in status["error"]


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
@pytest.mark.requires_fpcalc
def test_upload_skips_exact_duplicate_unless_forced(client, app):
    src = FIXTURES / "src-128.mp3"
    from tinytag import TinyTag
    dur_ms = int(TinyTag.get(src).duration * 1000)
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


@pytest.mark.requires_audio_stack
def test_refresh_batch_scope_runs_real_analysis(client, app):
    """Batch scope really analyzes the staged file (pathid_* pre-ingest
    identity) and skips the id-keyed steps with an explicit log line."""
    batch = ingest_mod.INGEST_INBOX / "scoped"
    batch.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "src-128.mp3", batch / "src-128.mp3")
    client.put("/api/v1/ingest/config",
               json={"enabled": {"analysis": True, "stems": True, "vocals": True}})
    r = client.post("/api/v1/ingest/refresh", json={"batch_dir": str(batch)})
    assert r.status_code == 202
    status = _wait_refresh(client)
    assert status["phase"] == "done", status["log_tail"]
    assert status["steps_completed"] == ["analysis"]
    assert any("skipped for batch scope" in ln for ln in status["log_tail"])
    ids = [
        r[0] for r in sqlite3.connect(app.state.state_db).execute(
            "SELECT stable_id FROM analysis"
        )
    ]
    assert ids and ids[0].startswith("pathid_"), ids


@pytest.mark.requires_audio_stack
def test_upload_rejects_staged_filename_collision(client, app):
    src = FIXTURES / "src-128.mp3"
    r1 = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, src.read_bytes(), "audio/mpeg"))],
        data={"batch": "collide"},
    )
    assert r1.status_code == 200, r1.text
    staged = Path(r1.json()["results"][0]["staged_path"])
    before = staged.stat().st_mtime_ns
    r2 = client.post(
        "/api/v1/ingest/upload",
        files=[("files", (src.name, b"different bytes", "audio/mpeg"))],
        data={"batch": "collide", "force": "true"},
    )
    assert r2.status_code == 409, r2.text
    assert staged.stat().st_mtime_ns == before, "staged file was overwritten"


def test_refresh_batch_scope_rejects_outside_inbox(client, tmp_path):
    r = client.post("/api/v1/ingest/refresh", json={"batch_dir": str(tmp_path)})
    assert r.status_code == 422


def test_coverage_on_never_analysed_library(client, app):
    # Fresh state.db: the analysis table does not exist until the analysis
    # store first writes. Coverage must read that as zero analysed, not 500.
    has = sqlite3.connect(app.state.state_db).execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis'"
    ).fetchone()
    assert has is None, "fixture unexpectedly pre-created the analysis table"
    out = client.get("/api/v1/ingest/coverage")
    assert out.status_code == 200
    assert out.json()["missing"]["analysis"] == 0

pytestmark = pytest.mark.rb_parity
