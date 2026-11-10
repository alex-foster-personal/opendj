"""The master-mix stream encoder: tee in, encoded bytes out (STREAM-11486).

[if] the master mix is teed to an encoder [then] its bytes reach a sink, [else stop].
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture, master_mix
from apps.sets.api import router
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE
from apps.sets.recorder_service import RecorderService
from apps.shared import ffmpeg as ffmpeg_mod
from apps.stream import encoder as encoder_mod
from apps.stream.encoder import (
    EncoderExited,
    EncoderRequestRefused,
    EncoderUnavailable,
    MasterMixEncoder,
    build_encoder_command,
    require_master_source,
)

pytestmark = pytest.mark.requirement("STREAM-11486")

FAKE_ENCODER = Path(__file__).resolve().parent / "fake_encoder.py"
RATE = 48_000
SESSION = "2026-11-10T03-00-00"


def _pcm(n: int, salt: int = 0) -> bytes:
    raw = bytes((i + salt) % 256 for i in range(n))
    return raw[: len(raw) - len(raw) % 4]


def _writer(directory: Path) -> master_mix.MasterMixWriter:
    return master_mix.MasterMixWriter(
        directory, monotonic=lambda: 100.0, utc_now=lambda: datetime(2026, 11, 10, 3, tzinfo=UTC)
    )


def _no_popen(*_a, **_k):
    raise AssertionError("no subprocess may be spawned")


@pytest.fixture
def no_homebrew(tmp_path: Path, monkeypatch) -> tuple[str, str]:
    paths = (str(tmp_path / "brew-arm" / "ffmpeg"), str(tmp_path / "brew-intel" / "ffmpeg"))
    monkeypatch.setattr(ffmpeg_mod, "HOMEBREW_FFMPEG_PATHS", paths)
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.delenv("ODJ_FFMPEG_BIN", raising=False)
    return paths


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch, no_homebrew) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "ffmpeg"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_ENCODER}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return wrapper


@pytest.fixture
def empty_path(tmp_path: Path, monkeypatch, no_homebrew) -> tuple[str, str]:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return no_homebrew


async def _collect(enc: MasterMixEncoder) -> bytes:
    out = bytearray()
    async for data in enc.encoded_chunks():
        out += data
    return bytes(out)


@pytest.mark.asyncio
async def test_three_chunks_pass_through_byte_for_byte_and_the_wav_is_unchanged(tmp_path, fake_ffmpeg) -> None:
    """[if] three chunks are teed [then] they come out in order and the WAV is identical, [else stop]."""
    chunks = [_pcm(4096, salt) for salt in (1, 2, 3)]
    plain = _writer(tmp_path / "a")
    for seq, pcm in enumerate(chunks):
        plain.append(stream="s", seq=seq, sample_rate=RATE, pcm=pcm)
    plain.close()

    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    enc.start()
    teed = _writer(tmp_path / "b")
    teed.add_tee(enc.tee)
    reader = asyncio.create_task(_collect(enc))
    for seq, pcm in enumerate(chunks):
        teed.append(stream="s", seq=seq, sample_rate=RATE, pcm=pcm)
    teed.close()
    await asyncio.to_thread(enc.stop)
    assert await reader == b"".join(chunks)
    [wav_a] = (tmp_path / "a").glob("*.wav")
    [wav_b] = (tmp_path / "b").glob("*.wav")
    assert wav_a.read_bytes() == wav_b.read_bytes()
    assert enc.status().state == "stopped"


def test_no_encoder_anywhere_raises_naming_every_place_and_spawns_nothing(empty_path, monkeypatch) -> None:
    """[if] no ffmpeg on PATH or Homebrew [then] EncoderUnavailable names all three, no spawn, [else stop]."""
    monkeypatch.setattr(encoder_mod.subprocess, "Popen", _no_popen)
    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    with pytest.raises(EncoderUnavailable) as raised:
        enc.start()
    message = str(raised.value)
    assert "PATH" in message
    for path in empty_path:
        assert path in message
    assert raised.value.code == "STREAM_ENCODER_UNAVAILABLE"


def test_the_real_homebrew_paths_are_the_two_documented_prefixes() -> None:
    """[if] the lookup list drifts [then] the message no longer names SET-10's places, [else stop]."""
    assert ffmpeg_mod.HOMEBREW_FFMPEG_PATHS == ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")


def test_command_strings_are_exactly_the_documented_lines() -> None:
    """[if] the argv drifts from the documented ffmpeg line [then] the gate fails, [else stop]."""
    assert " ".join(build_encoder_command("ffmpeg", "mp3", 192, 48_000)) == (
        "ffmpeg -f s16le -ar 48000 -ac 2 -i pipe:0 -f mp3 -b:a 192k pipe:1"
    )
    assert " ".join(build_encoder_command("ffmpeg", "aac", 128, 44_100)) == (
        "ffmpeg -f s16le -ar 44100 -ac 2 -i pipe:0 -f adts -b:a 128k pipe:1"
    )
    assert " ".join(build_encoder_command("ffmpeg", "opus", 96, 48_000)) == (
        "ffmpeg -f s16le -ar 48000 -ac 2 -i pipe:0 -f ogg -c:a libopus -b:a 96k pipe:1"
    )
    assert encoder_mod.DEFAULT_BITRATE_KBPS == 192


@pytest.mark.parametrize(("fmt", "kbps"), [("flac", 192), ("mp3", 31), ("mp3", 321), ("mp3", True)])
def test_unknown_format_or_out_of_range_bitrate_is_refused_before_any_process(monkeypatch, fmt, kbps) -> None:
    """[if] format or bitrate is bad [then] refused before a spawn, [else stop]."""
    monkeypatch.setattr(encoder_mod.subprocess, "Popen", _no_popen)
    with pytest.raises(EncoderRequestRefused):
        MasterMixEncoder(fmt=fmt, bitrate_kbps=kbps, sample_rate=RATE)
    assert build_encoder_command("ffmpeg", "mp3", 32, RATE)[-2] == "32k"
    assert build_encoder_command("ffmpeg", "mp3", 320, RATE)[-2] == "320k"


@pytest.mark.asyncio
async def test_an_encoder_that_exits_early_ends_the_iterator_with_its_code_and_stderr(fake_ffmpeg, monkeypatch) -> None:
    """[if] the encoder exits after one chunk [then] EncoderExited carries code and stderr, [else stop]."""
    first = _pcm(4096, 7)
    monkeypatch.setenv("FAKE_ENCODER_EXIT_AFTER_BYTES", str(len(first)))
    monkeypatch.setenv("FAKE_ENCODER_EXIT_CODE", "3")
    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    enc.start()
    got = bytearray()
    try:
        enc.tee(first, RATE)
        with pytest.raises(EncoderExited) as raised:
            async for data in enc.encoded_chunks():
                got += data
                enc.tee(_pcm(4096, 8), RATE)
        assert raised.value.exit_code == 3
        assert "fake-encoder: exiting early" in raised.value.stderr
        assert bytes(got) == first
        assert enc.status().state == "exited"
    finally:
        enc.stop()


def test_stop_closes_stdin_then_terminates_after_the_grace(fake_ffmpeg, monkeypatch) -> None:
    """[if] the encoder ignores stdin EOF [then] stop terminates it after the grace, [else stop]."""
    assert encoder_mod.STOP_GRACE_S == 2.0
    monkeypatch.setattr(encoder_mod, "STOP_GRACE_S", 0.3)
    monkeypatch.setenv("FAKE_ENCODER_IGNORE_EOF", "1")
    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    enc.start()
    t0 = time.monotonic()
    status = enc.stop()
    elapsed = time.monotonic() - t0
    assert 0.3 <= elapsed < 1.8
    assert status.exit_code == -signal.SIGTERM
    assert status.state == "stopped"


def test_a_clean_encoder_stops_well_inside_the_grace(fake_ffmpeg) -> None:
    """[if] the encoder exits on stdin EOF [then] stop returns fast with 0, [else stop]."""
    enc = MasterMixEncoder(fmt="aac", sample_rate=RATE)
    enc.start()
    t0 = time.monotonic()
    status = enc.stop()
    assert time.monotonic() - t0 < 1.0
    assert status.exit_code == 0


def test_a_consumer_that_never_reads_never_blocks_the_master_pcm_path(tmp_path, fake_ffmpeg, monkeypatch) -> None:
    """[if] nobody drains the encoder [then] appends stay fast and oldest chunks drop, [else stop]."""
    monkeypatch.setenv("FAKE_ENCODER_STALL", "1")
    monkeypatch.setattr(encoder_mod, "STOP_GRACE_S", 0.2)
    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    enc.start()
    writer = _writer(tmp_path)
    writer.add_tee(enc.tee)
    chunk = _pcm(256 * 1024)
    try:
        for seq in range(100):
            t0 = time.monotonic()
            writer.append(stream="s", seq=seq, sample_rate=RATE, pcm=chunk)
            assert time.monotonic() - t0 < 0.25
        status = enc.status()
        assert status.queued_chunks == 64
        assert status.dropped_chunks >= 100 - 64 - 2
        assert writer.frames_accepted == 100 * len(chunk) // 4
    finally:
        writer.close()
        enc.stop()


def test_a_rate_change_fails_the_stream_not_the_recording(fake_ffmpeg) -> None:
    """[if] the master rate changes mid-stream [then] the encoder fails, the tee never raises, [else stop]."""
    enc = MasterMixEncoder(fmt="mp3", sample_rate=RATE)
    enc.start()
    try:
        enc.tee(_pcm(1024), 44_100)
        status = enc.status()
        assert status.state == "failed"
        assert "48000" in (status.error or "") and "44100" in (status.error or "")
    finally:
        enc.stop()


@pytest.mark.parametrize("source", [None, "none", "loopback", "external"])
def test_a_non_master_source_is_refused_as_stream_source_not_master(source) -> None:
    """[if] the recording is not the master mix [then] STREAM_SOURCE_NOT_MASTER, [else stop]."""
    with pytest.raises(encoder_mod.StreamSourceNotMaster) as raised:
        require_master_source(source)
    assert raised.value.code == "STREAM_SOURCE_NOT_MASTER"
    require_master_source("master")


# -----
# routes (agent parity)


class _Page:
    def __init__(self) -> None:
        self.reason = None

    def unavailable_reason(self, request) -> str | None:
        return None

    async def attach(self, request, session_id: str) -> None:
        return None


@pytest.fixture
def client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
        list_devices=lambda: [
            capture.InputDevice(0, "MacBook Pro Microphone", False),
            capture.InputDevice(1, "BlackHole 2ch", True),
        ],
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    setattr(app.state, MASTER_TAP_CHANNEL_STATE, _Page())
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client, service


def _post_pcm(client: TestClient, seq: int, pcm: bytes):
    return client.post(
        f"/api/sets/recorder/{SESSION}/master-pcm",
        params={"stream": "tab", "seq": seq, "sample_rate": RATE},
        content=pcm,
        headers={"content-type": "application/octet-stream"},
    )


def _start_master(client: TestClient) -> None:
    started = client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert started.status_code == 201, started.text
    assert _post_pcm(client, 0, _pcm(1920)).status_code == 204


def test_stream_encoder_routes_start_report_and_stop_over_http(client, fake_ffmpeg) -> None:
    """[if] an agent drives the encoder over HTTP [then] start, status and stop work, [else stop]."""
    http, _ = client
    _start_master(http)
    started = http.post(f"/api/sets/recorder/{SESSION}/stream-encoder", json={"format": "mp3"})
    assert started.status_code == 201, started.text
    assert {k: started.json()[k] for k in ("state", "bitrate_kbps", "sample_rate")} == {
        "state": "running",
        "bitrate_kbps": 192,
        "sample_rate": RATE,
    }
    assert _post_pcm(http, 1, _pcm(1920)).status_code == 204
    assert _post_pcm(http, 2, _pcm(1920)).status_code == 204
    assert http.get("/api/sets/recorder/stream-encoder").json()["encoder"]["chunks_in"] == 2
    stopped = http.post(f"/api/sets/recorder/{SESSION}/stream-encoder/stop")
    assert (stopped.status_code, stopped.json()["state"]) == (200, "stopped")
    assert http.post(f"/api/sets/recorder/{SESSION}/stop").status_code == 200


def test_a_recording_stop_stops_its_encoder_with_no_orphan(client, fake_ffmpeg) -> None:
    """[if] REC stops with an encoder running [then] the encoder process is gone, [else stop]."""
    http, service = client
    _start_master(http)
    assert http.post(f"/api/sets/recorder/{SESSION}/stream-encoder", json={"format": "opus"}).status_code == 201
    proc = service._encoder._proc  # type: ignore[union-attr]
    assert http.post(f"/api/sets/recorder/{SESSION}/stop").status_code == 200
    assert http.get("/api/sets/recorder/stream-encoder").json()["encoder"]["state"] == "stopped"
    assert proc is not None and proc.poll() is not None


@pytest.mark.parametrize(
    "start",
    [
        {"source": "none"},
        {"source": "loopback", "device_name": "BlackHole 2ch"},
    ],
)
def test_stream_encoder_start_on_a_non_master_recording_is_409_stream_source_not_master(
    client, fake_ffmpeg, start
) -> None:
    """[if] the recording is tracklist-only or an input [then] 409 STREAM_SOURCE_NOT_MASTER, [else stop]."""
    http, _ = client
    assert (
        http.post("/api/sets/recorder/start", json={"session_id": SESSION, "sources": [], **start}).status_code == 201
    )
    refused = http.post(f"/api/sets/recorder/{SESSION}/stream-encoder", json={"format": "mp3"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "STREAM_SOURCE_NOT_MASTER"
    http.post(f"/api/sets/recorder/{SESSION}/stop")


def test_stream_encoder_start_with_no_ffmpeg_is_503_naming_the_places(client, empty_path, monkeypatch) -> None:
    """[if] no ffmpeg anywhere [then] 503 STREAM_ENCODER_UNAVAILABLE, no spawn, [else stop]."""
    http, _ = client
    _start_master(http)
    monkeypatch.setattr(encoder_mod.subprocess, "Popen", _no_popen)
    refused = http.post(f"/api/sets/recorder/{SESSION}/stream-encoder", json={"format": "mp3"})
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "STREAM_ENCODER_UNAVAILABLE"
    assert empty_path[0] in refused.json()["detail"]["message"]
    http.post(f"/api/sets/recorder/{SESSION}/stop")


def test_stream_encoder_bad_bitrate_is_422(client) -> None:
    """[if] the bitrate is out of range [then] 422 before anything starts, [else stop]."""
    http, _ = client
    assert (
        http.post(
            f"/api/sets/recorder/{SESSION}/stream-encoder", json={"format": "mp3", "bitrate_kbps": 400}
        ).status_code
        == 422
    )
