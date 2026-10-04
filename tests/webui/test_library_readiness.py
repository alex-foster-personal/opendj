"""Hermetic tests for GET /api/v1/library/readiness (READY-01).

Path constants and a tmp state.db via real migrations are the seam
(same pattern as tests/webui/test_ingest_routes.py). Production readers
(load_stem_bundle, local_preview_strip, fs_residency.is_materialised,
apps.analysis.store) run for real.

Regression lines:
  - if a broken-link track appears in present or any count then broken
  - if a streaming URI row is counted as present then broken
  - if counts shrink when limit shrinks then broken
  - if a stems directory load_stem_bundle rejects is reported the same as
    a track with no stems directory then broken
  - if sync_compatible is absent on a validateBeatGrid-passing stored grid
    then broken
  - [if] readiness counts include broken-link or streaming tracks [then] fail, [else stop]
"""
from __future__ import annotations

import json
import sqlite3
import wave
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.analysis_waveform import local_waveform
from apps.shared.state.db import open_rw as open_state_rw
from apps.shared.state.writer import StateWriter
from apps.stems import artifacts as stem_artifacts
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import library as library_mod

pytestmark = pytest.mark.requirement("READY-01")

DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM


@pytest.fixture
def app(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    state_db = data_dir / "state" / "state.db"
    open_state_rw(state_db).close()
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))

    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", tmp_path / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "INGEST_INBOX", tmp_path / "_ingest")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", tmp_path / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", tmp_path / "lyrics-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", stems_dir)
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(library_mod, "open_ro", lambda: sqlite3.connect(state_db))
    monkeypatch.setattr(rb_config, "STATE_DB", state_db)
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "local-waveform-cache")
    monkeypatch.setattr(rb_config, "ANLZ_CACHE_DIR", tmp_path / "anlz-cache")

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    app.include_router(library_mod.router, prefix="/api/v1")
    app.state.state_db = state_db
    app.state.stem_roots = (stems_dir,)
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


def _write_analysis(app, sid, *, downbeats: list[float], bpm: float = BPM) -> None:
    onsets = [round(0.25 + i * 1.9, 3) for i in range(30)]
    record = AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 4, 17, tzinfo=UTC),
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=bpm,
        bpm_confidence=0.9,
        key_camelot="8A",
        key_openkey="8m",
        key_confidence=0.9,
        energy=6,
        onsets_s=onsets,
        downbeats_s=downbeats,
        features_blob={"rms": [0.1] * 400, "rms_hop": 512},
    )
    upsert_record(record, db_path=app.state.state_db)


def _write_roformer_bundle(root: Path, stable_id: str) -> None:
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


def _write_corrupt_stems(root: Path, stable_id: str) -> None:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{", encoding="utf-8")


def _write_local_strip(sid: str, audio: Path) -> None:
    peaks = np.ones((200, 3), dtype=np.uint8) * 40
    local_waveform._store_peaks(sid, local_waveform._decode_key(audio), peaks)


def _downbeats() -> list[float]:
    return [i * BAR_S for i in range(int(DURATION_S / BAR_S))]


def _assert_count_invariants(body: dict) -> None:
    present = body["present"]
    counts = body["counts"]
    assert body["denominator"] == "present"
    assert body["present"] + body["unreachable"] <= body["total_tracks"]
    assert counts["ready"] + counts["not_ready"] == present
    assert (
        counts["beatgrid_ok"]
        + counts["beatgrid_missing"]
        + counts["beatgrid_invalid"]
        == present
    )
    assert counts["waveform_ok"] + counts["waveform_missing"] == present
    assert (
        counts["stems_ready"] + counts["stems_missing"] + counts["stems_corrupt"]
        == present
    )
    assert counts["sync_compatible"] + counts["sync_incompatible"] == present
    assert counts["has_analysis"] + counts["missing_analysis"] == present


def test_broken_link_is_absent_from_present_and_counts(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"y" * 4096)
    _seed_track(app, "aaa", real)
    _seed_track(app, "bbb", other)
    _seed_track(app, "ccc", tmp_path / "gone.mp3")
    body = client.get("/api/v1/library/readiness?axis=all").json()
    _assert_count_invariants(body)
    assert body["present"] == 2
    assert body["unreachable"] == 1
    assert body["total_tracks"] == 3
    ids = {item["stable_id"] for item in body["items"]}
    assert ids == {"aaa", "bbb"}
    assert "ccc" not in ids


def test_streaming_uri_is_excluded_from_present(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _seed_track(app, "tid", "tidal:abc123")
    body = client.get("/api/v1/library/readiness?axis=all").json()
    _assert_count_invariants(body)
    assert body["present"] == 1
    assert body["total_tracks"] == 2
    assert body["items"][0]["stable_id"] == "aaa"


def test_present_with_no_artifacts_is_not_ready(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    body = client.get("/api/v1/library/readiness").json()
    _assert_count_invariants(body)
    assert body["counts"]["not_ready"] == 1
    assert body["counts"]["beatgrid_missing"] == 1
    item = body["items"][0]
    assert item["stable_id"] == "aaa"
    assert item["beatgrid"] == "missing"
    assert item["waveform_preview"] == "missing"
    assert item["stems"] == "missing"
    assert item["sync_compatible"] is False
    assert item["sync_reason"] == "no_beatgrid"
    assert item["has_analysis"] is False
    assert item["gaps"] == ["beatgrid", "waveform", "stems", "sync", "analysis"]
    beatgrid = client.get("/api/v1/library/readiness?axis=beatgrid").json()
    assert [row["stable_id"] for row in beatgrid["items"]] == ["aaa"]
    assert beatgrid["counts"]["beatgrid_missing"] == 1


def test_downbeats_yield_ok_beatgrid_and_sync_compatible(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_analysis(app, "aaa", downbeats=_downbeats())
    body = client.get("/api/v1/library/readiness?axis=all").json()
    _assert_count_invariants(body)
    item = body["items"][0]
    assert item["beatgrid"] == "ok"
    assert item["sync_compatible"] is True
    assert item["sync_reason"] is None
    assert item["has_analysis"] is True
    assert item["analysis_backend"] == "librosa+madmom"
    assert item["analysis_version"] == "test-1.0"
    assert "beatgrid" not in item["gaps"]
    assert "sync" not in item["gaps"]


def test_empty_downbeats_are_beatgrid_missing(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_analysis(app, "aaa", downbeats=[])
    body = client.get("/api/v1/library/readiness?axis=all").json()
    item = body["items"][0]
    assert item["has_analysis"] is True
    assert item["beatgrid"] == "missing"
    assert item["sync_compatible"] is False
    assert item["sync_reason"] == "no_beatgrid"


def test_valid_stem_bundle_is_ready(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_roformer_bundle(Path(app.state.stem_roots[0]), "aaa")
    body = client.get("/api/v1/library/readiness?axis=all").json()
    assert body["items"][0]["stems"] == "ready"
    assert body["counts"]["stems_ready"] == 1


def test_invalid_stems_dir_is_corrupt_on_readiness_and_coverage(
    client, app, tmp_path
):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_corrupt_stems(Path(app.state.stem_roots[0]), "aaa")
    body = client.get("/api/v1/library/readiness?axis=all").json()
    item = body["items"][0]
    assert item["stems"] == "corrupt"
    assert "stems" in item["gaps"]
    assert body["counts"]["stems_corrupt"] == 1
    assert body["counts"]["stems_missing"] == 0
    coverage = client.get("/api/v1/ingest/coverage").json()
    assert coverage["corrupt"]["stems"] == 1
    assert coverage["missing"]["stems"] == 1


def test_local_waveform_strip_is_ok(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_local_strip("aaa", real)
    body = client.get("/api/v1/library/readiness?axis=all").json()
    assert body["items"][0]["waveform_preview"] == "ok"
    assert body["counts"]["waveform_ok"] == 1


def test_limit_caps_items_not_counts(client, app, tmp_path):
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.mp3"
        path.write_bytes(b"x" * 4096)
        _seed_track(app, name, path)
    body = client.get("/api/v1/library/readiness?limit=1").json()
    _assert_count_invariants(body)
    assert len(body["items"]) == 1
    assert body["counts"]["not_ready"] == 3
    assert body["present"] == 3


def test_axis_stems_corrupt_lists_only_corrupt_tracks(client, app, tmp_path):
    good = tmp_path / "good.mp3"
    good.write_bytes(b"x" * 4096)
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"y" * 4096)
    _seed_track(app, "missing-stems", good)
    _seed_track(app, "corrupt-stems", bad)
    _write_corrupt_stems(Path(app.state.stem_roots[0]), "corrupt-stems")
    body = client.get("/api/v1/library/readiness?axis=stems_corrupt").json()
    _assert_count_invariants(body)
    assert [item["stable_id"] for item in body["items"]] == ["corrupt-stems"]
    assert body["counts"]["stems_corrupt"] == 1
    assert body["counts"]["stems_missing"] == 1


def test_axis_all_lists_every_present_track(client, app, tmp_path):
    for name in ("a", "b"):
        path = tmp_path / f"{name}.mp3"
        path.write_bytes(b"x" * 4096)
        _seed_track(app, name, path)
    body = client.get("/api/v1/library/readiness?axis=all").json()
    assert len(body["items"]) == body["present"] == 2


def test_playable_ready_track_has_empty_gaps(client, app, tmp_path):
    real = tmp_path / "real.mp3"
    real.write_bytes(b"x" * 4096)
    _seed_track(app, "aaa", real)
    _write_analysis(app, "aaa", downbeats=_downbeats())
    _write_roformer_bundle(Path(app.state.stem_roots[0]), "aaa")
    _write_local_strip("aaa", real)
    body = client.get("/api/v1/library/readiness?axis=all").json()
    _assert_count_invariants(body)
    item = body["items"][0]
    assert item["beatgrid"] == "ok"
    assert item["waveform_preview"] == "ok"
    assert item["stems"] == "ready"
    assert item["sync_compatible"] is True
    assert item["has_analysis"] is True
    assert item["gaps"] == []
    assert body["counts"]["ready"] == 1
    not_ready = client.get("/api/v1/library/readiness").json()
    assert not_ready["items"] == []
    assert not_ready["counts"]["ready"] == 1


def test_unknown_axis_is_422(client):
    response = client.get("/api/v1/library/readiness?axis=stutter")
    assert response.status_code == 422


def test_present_when_only_track_location_is_materialised(client, app, tmp_path):
    real = tmp_path / "mirror.mp3"
    real.write_bytes(b"x" * 4096)
    foreign = "/Users/dev/Music/ghost.mp3"
    sid = "f" * 40

    conn = open_state_rw(app.state.state_db)
    writer = StateWriter(conn, actor="test-readiness")
    try:
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title="mirrored",
            artists=["X"],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path=str(real),
        )
        conn.execute(
            "UPDATE tracks SET file_path = ? WHERE stable_id = ?",
            (foreign, sid),
        )
        conn.commit()
    finally:
        writer.close()
        conn.close()

    _write_roformer_bundle(Path(app.state.stem_roots[0]), sid)
    body = client.get("/api/v1/library/readiness?axis=all").json()
    _assert_count_invariants(body)
    assert body["present"] == 1
    assert body["unreachable"] == 0
    item = body["items"][0]
    assert item["stable_id"] == sid
    assert item["file_path"] == str(real)
    assert item["stems"] == "ready"
