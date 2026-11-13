"""Classic sampler captures: POST /api/v1/sampler/captures stores a rendered loop WAV a bank slot reloads (#9998).

[if] a capture is not stored, refused, deduplicated or reloadable through the banks as issue #9998 says [then] these fail, [else stop].

Regression one-liners:
  - if a posted WAV's returned path does not reload through PUT then GET /banks on a fresh client then broken
  - if a non-WAV body is not a 422, or leaves a file in sampler-captures/, then broken
  - if a body over MAX_CAPTURE_BYTES is not a 413, or leaves a file, then broken
  - if the same WAV posted twice leaves two files or answers two paths then broken
"""

from __future__ import annotations

import io
import math
import struct
import wave
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import sampler_banks, sampler_captures
from apps.webui.server.routes import sampler
from tests.webui.sampler_support import FakeSources, make_app, make_env

URL = "/api/v1/sampler/captures"
BANKS = "/api/v1/sampler/banks"
WAV = {"Content-Type": "audio/wav"}


def _app(state: Path, sources: FakeSources) -> FastAPI:
    app = make_app(state, sources)
    app.include_router(sampler.router, prefix="/api/v1")
    return app


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    for base in make_env(tmp_path, monkeypatch):
        with TestClient(_app(base.state, base.sources)) as client:
            yield SimpleNamespace(**vars(base), client=client)


def _loop_wav(beats: int = 4, bpm: float = 120.0, rate: int = 48_000, channels: int = 2) -> bytes:
    """A real 16-bit PCM WAV of ``beats`` beats of a 440 Hz sine at ``bpm``, built with the stdlib."""
    frames = round(beats * 60 / bpm * rate)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        sample = (int(9000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(frames))
        w.writeframes(b"".join(struct.pack("<h", v) * channels for v in sample))
    return out.getvalue()


def _with_channels(data: bytes, channels: int) -> bytes:
    """The WAV with its fmt chunk's channel count (offset 22) overwritten."""
    patched = bytearray(data)
    struct.pack_into("<H", patched, 22, channels)
    return bytes(patched)


def _stored(env: SimpleNamespace) -> list[Path]:
    root = sampler_captures.captures_dir(env.state)
    return sorted(root.iterdir()) if root.exists() else []


def test_valid_wav_is_stored_and_reloads_through_the_banks(env: SimpleNamespace) -> None:
    """[if] a 4-beat 120 BPM WAV is posted [then] its path, put in a bank slot, reloads from a fresh client, [else stop]."""
    data = _loop_wav()
    r = env.client.post(URL, content=data, headers=WAV)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["duration_ms"] == 2000
    path = Path(body["path"])
    assert path.parent == sampler_captures.captures_dir(env.state)
    assert path.read_bytes() == data
    doc = sampler_banks.default_document()
    doc["banks"][1][3] = {"path": body["path"], "mode": "loop", "gain_db": 0.0, "title": "Track loop 4"}
    assert env.client.put(BANKS, json=doc).status_code == 200
    with TestClient(_app(env.state, FakeSources(env.sources.audio))) as fresh:
        slot = fresh.get(BANKS).json()["banks"][1][3]
    assert slot == {"path": body["path"], "mode": "loop", "gain_db": 0.0, "title": "Track loop 4", "missing": False}
    assert Path(slot["path"]).read_bytes() == data


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"ID3\x04\x00" + b"\x00" * 64,
        b"RIFF\x24\x00\x00\x00AVI " + b"\x00" * 32,
        _with_channels(_loop_wav(beats=1), 16),
        _loop_wav(beats=1, rate=4000),
    ],
    ids=["empty", "mp3", "riff-not-wave", "sixteen-channels", "4khz"],
)
def test_non_wav_is_422_and_stores_nothing(env: SimpleNamespace, data: bytes) -> None:
    """[if] the body is empty, not RIFF/WAVE, 16 channels or 4 kHz [then] 422 CAPTURE_NOT_WAV and no file stored, [else stop]."""
    r = env.client.post(URL, content=data, headers=WAV)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "CAPTURE_NOT_WAV"
    assert _stored(env) == []


def test_oversize_is_413_and_stores_nothing(env: SimpleNamespace) -> None:
    """[if] the body is one byte over MAX_CAPTURE_BYTES [then] 413 CAPTURE_TOO_LARGE and no file is stored, [else stop]."""
    data = _loop_wav()
    data += b"\x00" * (sampler_captures.MAX_CAPTURE_BYTES + 1 - len(data))
    r = env.client.post(URL, content=data, headers=WAV)
    assert r.status_code == 413, r.text
    assert r.json()["detail"]["code"] == "CAPTURE_TOO_LARGE"
    assert _stored(env) == []


def test_cap_fits_the_longest_loop_the_issue_implies() -> None:
    """[if] a 32-beat 60 BPM 48 kHz stereo float capture is sized [then] it fits under MAX_CAPTURE_BYTES, [else stop]."""
    samples_bytes = 32 * 48_000 * 2 * 4
    assert samples_bytes < sampler_captures.MAX_CAPTURE_BYTES <= samples_bytes + 4096


def test_same_wav_posted_twice_yields_one_file(env: SimpleNamespace) -> None:
    """[if] the same WAV is posted twice [then] both answers name one path and one file sits in sampler-captures/, [else stop]."""
    data = _loop_wav(beats=8)
    first = env.client.post(URL, content=data, headers=WAV).json()
    second = env.client.post(URL, content=data, headers=WAV).json()
    assert first == second
    assert _stored(env) == [Path(first["path"])]
    other = env.client.post(URL, content=_loop_wav(beats=2), headers=WAV).json()
    assert other["path"] != first["path"] and len(_stored(env)) == 2
