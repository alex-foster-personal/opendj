"""#190 precomputed Demucs stem artifact acceptance tests.

Mini-PRD
========

* [if] a v1 ``htdemucs`` artifact has four real, aligned WAV stems [then
  \u26d4] its manifest and each declared stem file must be available through the
  mountable API router.
* [if] a manifest omits a standard part, has a mismatched stable id, or has
  unproven source/model provenance [then \u26d4] the API must reject the
  artifact as malformed rather than publishing a partial result.
* [if] a declared stem escapes the bundle or differs in WAV sample rate,
  frames, or channels [then \u26d4] the API must reject it and never stream
  the file.
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.app import create_app
from apps.webui.server.routes.stems import router


def _write_wav(path: Path, *, frames: int = 12, sample_rate: int = 44_100,
               channels: int = 2) -> None:
    """Write a tiny real PCM WAV fixture with the requested metadata."""
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames * channels)


def _manifest(stable_id: str, *, files: dict[str, str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "htdemucs", "version": "4.0.1"},
        "source": {
            "path": "C:/Music/Example.wav",
            "sha256": "a" * 64,
        },
        "files": files or {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
    }


def _bundle(root: Path, stable_id: str = "track-001") -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle / f"{part}.wav")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id)), encoding="utf-8"
    )
    return bundle


def _client(stems_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_get_manifest_and_real_stem_file(tmp_path: Path) -> None:
    """A complete v1 bundle is exposed as manifest JSON and a WAV stream."""
    _bundle(tmp_path)

    with _client(tmp_path) as client:
        manifest = client.get("/api/v1/tracks/track-001/stems")
        stem = client.get("/api/v1/tracks/track-001/stems/vocals")

    assert manifest.status_code == 200
    assert manifest.json() == {
        "schema": 1,
        "stable_id": "track-001",
        "source": "demucs",
        "model": "htdemucs",
        # A 4-part Demucs bundle must still declare demucs4 explicitly, so the
        # client never has to infer a layout from the part names.
        "layout": "demucs4",
        "sample_rate_hz": 44_100,
        "frame_count": 12,
        "channel_count": 2,
        "parts": {
            "vocals": {"media_type": "audio/wav"},
            "drums": {"media_type": "audio/wav"},
            "bass": {"media_type": "audio/wav"},
            "other": {"media_type": "audio/wav"},
        },
    }
    assert stem.status_code == 200
    assert stem.headers["content-type"] == "audio/wav"
    assert stem.content.startswith(b"RIFF")


def test_production_app_registers_stem_artifact_contract() -> None:
    """The app factory must expose the real stem routes, not only test mounts."""
    paths = create_app(mount_frontend=False).openapi()["paths"]

    assert "/api/v1/tracks/{stable_id}/stems" in paths
    assert "/api/v1/tracks/{stable_id}/stems/{part}" in paths


def test_rejects_incomplete_or_unproven_manifest(tmp_path: Path) -> None:
    """Missing standard files and a stable-id mismatch fail explicitly."""
    bundle = _bundle(tmp_path)
    manifest = _manifest("another-track", files={
        "vocals": "vocals.wav",
        "drums": "drums.wav",
        "bass": "bass.wav",
    })
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with _client(tmp_path) as client:
        response = client.get("/api/v1/tracks/track-001/stems")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STEM_ARTIFACT_INVALID"


def test_rejects_invalid_source_provenance(tmp_path: Path) -> None:
    """A source fingerprint is mandatory evidence, not optional display data."""
    bundle = _bundle(tmp_path)
    manifest = _manifest("track-001")
    manifest["source"]["sha256"] = "not-a-sha256"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with _client(tmp_path) as client:
        response = client.get("/api/v1/tracks/track-001/stems")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STEM_ARTIFACT_INVALID"


def test_rejects_escaped_or_misaligned_stem_files(tmp_path: Path) -> None:
    """Traversal and WAV metadata differences cannot be served as stems."""
    bundle = _bundle(tmp_path)
    outside = tmp_path.parent / "outside.wav"
    _write_wav(outside)
    manifest = _manifest("track-001", files={
        "vocals": "../outside.wav",
        "drums": "drums.wav",
        "bass": "bass.wav",
        "other": "other.wav",
    })
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with _client(tmp_path) as client:
        escaped = client.get("/api/v1/tracks/track-001/stems/vocals")

    assert escaped.status_code == 422
    assert escaped.json()["detail"]["code"] == "STEM_ARTIFACT_INVALID"

    manifest["files"]["vocals"] = "vocals.wav"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_wav(bundle / "other.wav", frames=13)

    with _client(tmp_path) as client:
        misaligned = client.get("/api/v1/tracks/track-001/stems/other")

    assert misaligned.status_code == 422
    assert misaligned.json()["detail"]["code"] == "STEM_ARTIFACT_INVALID"
