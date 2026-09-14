"""v1 stem bundles must load MP3 and FLAC parts by their own container."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes.stems import router
from apps.webui.server.stem_artifacts import (
    StemArtifactError,
    WavMetadata,
    load_stem_bundle,
)

FFMPEG = shutil.which("ffmpeg")


def _require_ffmpeg() -> str:
    if FFMPEG is None:
        pytest.skip("ffmpeg is required to synthesize real MP3/FLAC stem fixtures")
    return FFMPEG


def _write_wav(path: Path, *, frames: int = 12, sample_rate: int = 44_100) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frames * 2)


def _write_sine_wav(path: Path, *, duration_s: float = 0.25, sample_rate: int = 44_100) -> None:
    ffmpeg = _require_ffmpeg()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}:sample_rate={sample_rate}",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
    )


def _transcode_part(src: Path, dest: Path, codec: str) -> None:
    ffmpeg = _require_ffmpeg()
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
    ]
    if codec == "mp3":
        args.extend(["-codec:a", "libmp3lame", "-b:a", "128k", str(dest)])
    elif codec == "flac":
        args.extend(["-codec:a", "flac", str(dest)])
    else:
        raise ValueError(codec)
    subprocess.run(args, check=True)


def _manifest(stable_id: str, *, ext: str) -> dict:
    return {
        "schema_version": 1,
        "stable_id": stable_id,
        "model": {"name": "hdemucs_mmi", "version": "4.0.1"},
        "source": {
            "path": "/music/example.flac",
            "sha256": "a" * 64,
        },
        "files": {
            "vocals": f"vocals.{ext}",
            "drums": f"drums.{ext}",
            "bass": f"bass.{ext}",
            "other": f"other.{ext}",
        },
        "layout": "demucs4",
    }


def _bundle_with_codec(root: Path, stable_id: str, codec: str) -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    sine = bundle / "source.wav"
    _write_sine_wav(sine)
    ext = codec
    for part in ("vocals", "drums", "bass", "other"):
        _transcode_part(sine, bundle / f"{part}.{ext}", codec)
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext=ext)),
        encoding="utf-8",
    )
    return bundle


def _wav_bundle(root: Path, stable_id: str = "track-wav") -> Path:
    bundle = root / stable_id
    bundle.mkdir(parents=True)
    for part in ("vocals", "drums", "bass", "other"):
        _write_wav(bundle / f"{part}.wav")
    (bundle / "manifest.json").write_text(
        json.dumps(_manifest(stable_id, ext="wav")),
        encoding="utf-8",
    )
    return bundle


def _client(stems_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_mp3_v1_bundle_loads_with_audio_mpeg(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-mp3", "mp3")
    bundle = load_stem_bundle("track-mp3", stems_dir=tmp_path)
    assert bundle.media_type == "audio/mpeg"
    assert bundle.alignment.sample_rate > 0
    assert bundle.alignment.frame_count > 0
    assert bundle.alignment.channels == 2


def test_flac_v1_bundle_loads(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-flac", "flac")
    bundle = load_stem_bundle("track-flac", stems_dir=tmp_path)
    assert bundle.media_type == "audio/flac"
    assert bundle.alignment.sample_rate == 44_100
    assert bundle.alignment.frame_count > 0


def test_wav_v1_bundle_still_loads_exactly(tmp_path: Path) -> None:
    _wav_bundle(tmp_path)
    bundle = load_stem_bundle("track-wav", stems_dir=tmp_path)
    assert bundle.media_type == "audio/wav"
    assert bundle.alignment == WavMetadata(
        sample_rate=44_100,
        frame_count=12,
        channels=2,
    )


def test_mixed_containers_raise(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-mixed", "mp3")
    _write_wav(bundle / "other.wav")
    manifest = _manifest("track-mixed", ext="mp3")
    manifest["files"]["other"] = "other.wav"
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StemArtifactError, match="mixes containers"):
        load_stem_bundle("track-mixed", stems_dir=tmp_path)


def test_mismatched_sample_rate_raises(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-rate", "mp3")
    bad = bundle / "other-bad.wav"
    _write_wav(bad, sample_rate=22_050)
    _transcode_part(bad, bundle / "other.mp3", "mp3")
    with pytest.raises(StemArtifactError, match="sample_rate"):
        load_stem_bundle("track-rate", stems_dir=tmp_path)


def test_mp3_suffix_with_wav_bytes_raises(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-garbage", "mp3")
    _write_wav(bundle / "vocals.mp3")
    with pytest.raises(StemArtifactError, match="not MPEG"):
        load_stem_bundle("track-garbage", stems_dir=tmp_path)


def test_truncated_mp3_raises(tmp_path: Path) -> None:
    bundle = _bundle_with_codec(tmp_path, "track-trunc", "mp3")
    vocals = bundle / "vocals.mp3"
    vocals.write_bytes(vocals.read_bytes()[:32])
    with pytest.raises(StemArtifactError):
        load_stem_bundle("track-trunc", stems_dir=tmp_path)


def test_http_manifest_and_part_stream_for_mp3_bundle(tmp_path: Path) -> None:
    _bundle_with_codec(tmp_path, "track-http", "mp3")
    with _client(tmp_path) as client:
        manifest = client.get("/api/v1/tracks/track-http/stems")
        stem = client.get("/api/v1/tracks/track-http/stems/vocals")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["schema"] == 1
    assert body["parts"]["vocals"]["media_type"] == "audio/mpeg"
    assert stem.status_code == 200
    assert stem.headers["content-type"].startswith("audio/mpeg")
    assert stem.content[:3] != b"RIFF"


pytestmark = pytest.mark.rb_parity
