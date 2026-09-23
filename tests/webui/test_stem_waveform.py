"""Stem mini-waveform envelope route (issue #1036)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.stems import router

pytestmark = pytest.mark.requirement(
    "DECKUX-19",
    "if stem waveform endpoint missing [then stop] else envelope contract holds.",
)


def _manifest(stable_id: str) -> dict:
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {"path": "/music/example.flac", "sha256": "a" * 64},
        "files": {
            "vocals": "vocals.wav",
            "drums": "drums.wav",
            "bass": "bass.wav",
            "other": "other.wav",
        },
        "layout": "demucs4",
    }


def _write_wav(path: Path, *, frames: int = 4410) -> None:
    import wave

    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44_100)
        amplitude = 5000
        samples = []
        for i in range(frames):
            value = amplitude if (i // 100) % 2 == 0 else 0
            samples.extend([value, value])
        output.writeframes(b"".join(s.to_bytes(2, "little", signed=True) for s in samples))


def _bundle(root: Path, stable_id: str = "track-stem-wave") -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle / f"{part}.wav")
    (bundle / "manifest.json").write_text(json.dumps(_manifest(stable_id)), encoding="utf-8")
    return bundle


def _client(stems_dir: Path, data_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.state.data_dir = data_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_stem_waveform_endpoint_returns_envelope(tmp_path: Path) -> None:
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    _bundle(stems_dir)
    client = _client(stems_dir, data_dir)
    response = client.get("/api/v1/tracks/track-stem-wave/stems/vocals/waveform")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == 1
    assert body["part"] == "vocals"
    assert body["points"] == 512
    assert len(body["envelope"]) == 512
    assert max(body["envelope"]) > 0


def test_stem_waveform_uses_disk_cache(tmp_path: Path) -> None:
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    _bundle(stems_dir)
    client = _client(stems_dir, data_dir)
    first = client.get("/api/v1/tracks/track-stem-wave/stems/drums/waveform")
    assert first.status_code == 200
    cache_dir = data_dir / "state" / "stem-waveform-cache" / "track-stem-wave"
    assert any(cache_dir.glob("drums-*.json"))
    second = client.get("/api/v1/tracks/track-stem-wave/stems/drums/waveform")
    assert second.status_code == 200
    assert second.json()["envelope"] == first.json()["envelope"]


def test_stem_waveform_missing_bundle_404(tmp_path: Path) -> None:
    stems_dir = tmp_path / "stems"
    data_dir = tmp_path / "data"
    stems_dir.mkdir(parents=True)
    client = _client(stems_dir, data_dir)
    response = client.get("/api/v1/tracks/missing/stems/vocals/waveform")
    assert response.status_code == 404
