"""RTMP stream sink: argv, redaction, routes (STREAM-11743).

[if] the master mix is teed to RTMP [then] ffmpeg gets PCM in order, [else stop].
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture, master_mix
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE
from apps.sets.recorder_service import RecorderService
from apps.shared import ffmpeg as ffmpeg_mod
from apps.stream import rtmp as rtmp_mod
from apps.stream.config import KEY_PLACEHOLDER, RtmpConfigRefused, parse_rtmp_config
from apps.stream.encoder import EncoderUnavailable, StreamSourceNotMaster, require_master_source
from apps.stream.rtmp import (
    RtmpPush,
    build_rtmp_command,
    missing_rtmp_capabilities,
    resolve_rtmp_encoder,
)
from tests.sets.conftest import mount_sets_router

pytestmark = pytest.mark.requirement("STREAM-11743")

FAKE_FFMPEG = Path(__file__).resolve().parent / "fake_ffmpeg.py"
RATE = 48_000
SESSION = "2026-11-10T07-00-00"
URL = "rtmp://live.example.test/app"
KEY = "live_sk_SECRET123"

FULL_PROTOCOLS = (
    "Supported file protocols:\nInput:\n  file\n  pipe\n  rtmp\n  rtmps\nOutput:\n  file\n  pipe\n  rtmp\n  rtmps\n"
)
FULL_MUXERS = (
    "File formats:\n D. = Demuxing supported\n .E = Muxing supported\n--\n E flv             FLV (Flash Video)\n"
)
FULL_ENCODERS = "Encoders:\n ------\n V....D libx264           H.264\n A....D aac               AAC\n"
LGPL_PROTOCOLS = "Supported file protocols:\nInput:\n  file\n  pipe\nOutput:\n  file\n  pipe\n"
LGPL_MUXERS = (
    "File formats:\n D. = Demuxing supported\n .E = Muxing supported\n--\n E pcm_s16le       PCM signed 16-bit\n"
)
LGPL_ENCODERS = "Encoders:\n ------\n A....D pcm_s16le         PCM signed 16-bit\n"


def _redirect_to_fake(monkeypatch, placeholder: Path) -> None:
    real_popen = subprocess.Popen
    target = placeholder.resolve()

    def _redirect(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and Path(args[0]).resolve() == target:
            args = [sys.executable, str(FAKE_FFMPEG), *args[1:]]
        return real_popen(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _redirect)


def _pcm(n: int, salt: int = 0) -> bytes:
    raw = bytes((i + salt) % 256 for i in range(n))
    return raw[: len(raw) - len(raw) % 4]


def _writer(directory: Path) -> master_mix.MasterMixWriter:
    return master_mix.MasterMixWriter(
        directory, monotonic=lambda: 100.0, utc_now=lambda: datetime(2026, 11, 10, 7, tzinfo=UTC)
    )


def _no_popen(*_a, **_k):
    raise AssertionError("no subprocess may be spawned")


def _rtmp_config(*, image: Path | None) -> object:
    return parse_rtmp_config(url=URL, key=KEY, image=image)


@pytest.fixture
def no_homebrew(tmp_path: Path, monkeypatch) -> tuple[str, str]:
    paths = (str(tmp_path / "brew-arm" / "ffmpeg"), str(tmp_path / "brew-intel" / "ffmpeg"))
    monkeypatch.setattr(ffmpeg_mod, "HOMEBREW_FFMPEG_PATHS", paths)
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    monkeypatch.delenv("ODJ_FFMPEG_BIN", raising=False)
    return paths


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch, no_homebrew) -> Path:
    """MDT_FFMPEG names a placeholder; Popen launches it as `python fake_ffmpeg.py` on every platform."""
    placeholder = tmp_path / "bin" / "ffmpeg"
    placeholder.parent.mkdir()
    placeholder.write_text(
        "stand-in: launched through the test Popen redirect\n",
        encoding="utf-8",
        newline="\n",
    )
    placeholder.chmod(0o755)
    monkeypatch.setenv("MDT_FFMPEG", str(placeholder))
    _redirect_to_fake(monkeypatch, placeholder)
    return placeholder


@pytest.fixture
def empty_path(tmp_path: Path, monkeypatch, no_homebrew) -> tuple[str, str]:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return no_homebrew


def test_argv_with_still_image_matches_the_documented_line(tmp_path: Path) -> None:
    """[if] a cover image exists [then] argv equals the documented still line, [else stop]."""
    image = tmp_path / "cover.png"
    image.write_bytes(b"png")
    cfg = _rtmp_config(image=image)
    w, h = 1280, 720
    expected = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-loop",
        "1",
        "-framerate",
        "2",
        "-i",
        str(image),
        "-map",
        "1:v",
        "-map",
        "0:a",
        "-vf",
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-r",
        "2",
        "-g",
        "4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-f",
        "flv",
        f"{URL}/{KEY}",
    ]
    assert build_rtmp_command("ffmpeg", cfg, RATE) == expected


def test_argv_without_image_uses_lavfi_black_and_drops_vf() -> None:
    """[if] image is null [then] argv uses lavfi black and no -vf, [else stop]."""
    cfg = _rtmp_config(image=None)
    w, h = 1280, 720
    expected = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-f",
        "s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-i",
        "pipe:0",
        "-f",
        "lavfi",
        "-i",
        f"color=black:s={w}x{h}:r=2",
        "-map",
        "1:v",
        "-map",
        "0:a",
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-r",
        "2",
        "-g",
        "4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-shortest",
        "-f",
        "flv",
        f"{URL}/{KEY}",
    ]
    assert build_rtmp_command("ffmpeg", cfg, RATE) == expected


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"url": "http://x", "key": "k", "image": None}, "rtmp"),
        ({"url": URL, "key": "", "image": None}, "key"),
        ({"url": URL, "key": KEY, "image": None, "video_size": "1281x720"}, "even"),
        ({"url": URL, "key": KEY, "image": None, "audio_kbps": 31}, "audio_kbps"),
    ],
)
def test_bad_rtmp_config_is_refused_without_echoing_the_key(kwargs, needle) -> None:
    """[if] url, key, size or kbps is bad [then] refused and key absent from message, [else stop]."""
    with pytest.raises(RtmpConfigRefused) as raised:
        parse_rtmp_config(**kwargs)
    assert needle in str(raised.value).lower()
    assert KEY not in str(raised.value)


def test_missing_image_path_is_refused_at_config_time(tmp_path: Path) -> None:
    """[if] the image path is missing [then] RtmpConfigRefused before spawn, [else stop]."""
    missing = tmp_path / "nope.png"
    with pytest.raises(RtmpConfigRefused):
        parse_rtmp_config(url=URL, key=KEY, image=missing)


def test_missing_rtmp_capabilities_parses_real_shaped_listings() -> None:
    """[if] ffmpeg listings are full or LGPL [then] missing labels match, [else stop]."""
    assert missing_rtmp_capabilities(FULL_PROTOCOLS, FULL_MUXERS, FULL_ENCODERS, scheme="rtmp") == []
    assert missing_rtmp_capabilities(LGPL_PROTOCOLS, LGPL_MUXERS, LGPL_ENCODERS, scheme="rtmp") == [
        "rtmp output protocol",
        "flv muxer",
        "libx264 encoder",
        "aac encoder",
    ]
    rtmps_only = FULL_PROTOCOLS.replace("  rtmps\n", "")
    assert missing_rtmp_capabilities(rtmps_only, FULL_MUXERS, FULL_ENCODERS, scheme="rtmps") == [
        "rtmps output protocol"
    ]


def _touch_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("exe\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def test_bundled_lgpl_ffmpeg_is_skipped_for_a_capable_path_ffmpeg(tmp_path: Path, monkeypatch, no_homebrew) -> None:
    """[if] PATH has RTMP-capable ffmpeg and bundled does not [then] PATH wins, [else stop]."""
    bundled = tmp_path / "bundled" / "ffmpeg"
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    # On Windows, shutil.which needs ffmpeg.exe on PATH.
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    path_ffmpeg = path_bin / ffmpeg_name
    _touch_executable(bundled)
    _touch_executable(path_ffmpeg)
    monkeypatch.setenv(ffmpeg_mod.BUNDLED_ENV, str(bundled))
    monkeypatch.setenv("PATH", str(path_bin))
    bundled_abs = str(bundled.resolve())
    path_abs = str(path_ffmpeg.resolve())

    def _fake_probe(executable: str, *, scheme: str) -> list[str]:
        if os.path.abspath(executable) == bundled_abs:
            return ["rtmp output protocol", "flv muxer", "libx264 encoder", "aac encoder"]
        return []

    monkeypatch.setattr(rtmp_mod, "probe_rtmp_capabilities", _fake_probe)
    assert resolve_rtmp_encoder(scheme="rtmp") == path_abs


def test_only_bundled_lgpl_ffmpeg_is_503_naming_what_it_lacks(client, tmp_path: Path, no_homebrew, monkeypatch) -> None:
    """[if] only bundled LGPL ffmpeg exists [then] 503 names gaps and spawns nothing, [else stop]."""
    http, _ = client
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    bundled = tmp_path / "bundled" / "ffmpeg"
    _touch_executable(bundled)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv(ffmpeg_mod.BUNDLED_ENV, str(bundled))
    monkeypatch.setenv("FAKE_FFMPEG_CAPS", "lgpl")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(argv_file))
    _redirect_to_fake(monkeypatch, bundled)
    _start_master(http)
    assert http.put("/api/sets/recorder/stream/rtmp", json={"url": URL, "key": KEY, "image": None}).status_code == 200
    refused = http.post(f"/api/sets/recorder/{SESSION}/stream", json={"sink": "rtmp"})
    assert refused.status_code == 503
    detail = refused.json()["detail"]
    assert detail["code"] == "STREAM_ENCODER_UNAVAILABLE"
    assert ffmpeg_mod.BUNDLED_ENV in detail["message"]
    assert "flv muxer" in detail["message"]
    assert not argv_file.exists()
    assert KEY not in refused.text
    http.post(f"/api/sets/recorder/{SESSION}/stop")


def test_mdt_ffmpeg_incapable_never_falls_through(tmp_path: Path, monkeypatch, no_homebrew) -> None:
    """[if] MDT_FFMPEG lacks flv and PATH is capable [then] error names MDT and PATH not probed, [else stop]."""
    mdt = tmp_path / "mdt" / "ffmpeg"
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    path_ffmpeg = path_bin / ffmpeg_name
    _touch_executable(mdt)
    _touch_executable(path_ffmpeg)
    monkeypatch.setenv(ffmpeg_mod.OVERRIDE_ENV, str(mdt))
    monkeypatch.setenv("PATH", str(path_bin))
    mdt_abs = str(mdt.resolve())
    probed: list[str] = []

    def _fake_probe(executable: str, *, scheme: str) -> list[str]:
        probed.append(os.path.abspath(executable))
        if os.path.abspath(executable) == mdt_abs:
            return ["flv muxer"]
        return []

    monkeypatch.setattr(rtmp_mod, "probe_rtmp_capabilities", _fake_probe)
    with pytest.raises(EncoderUnavailable) as raised:
        resolve_rtmp_encoder(scheme="rtmp")
    assert ffmpeg_mod.OVERRIDE_ENV in str(raised.value)
    assert probed == [mdt_abs]


class _Page:
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
        list_devices=lambda: [capture.InputDevice(0, "Mic", False)],
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    setattr(app.state, MASTER_TAP_CHANNEL_STATE, _Page())
    mount_sets_router(app)
    with TestClient(app) as test_client:
        yield test_client, service


def test_put_rtmp_config_missing_image_is_422_without_key_in_body(client, monkeypatch) -> None:
    """[if] PUT names a missing image [then] 422, no spawn, key absent from text, [else stop]."""
    http, _ = client
    monkeypatch.setattr(rtmp_mod.subprocess, "Popen", _no_popen)
    body = {
        "url": URL,
        "key": KEY,
        "image": "zz-no-such-cover-10358.png",
    }
    response = http.put("/api/sets/recorder/stream/rtmp", json=body)
    assert response.status_code == 422
    assert KEY not in response.text


def test_put_rtmp_config_ok_returns_redacted_key(client, tmp_path: Path) -> None:
    """[if] PUT with a good cover [then] 200 with key placeholder, key absent from text, [else stop]."""
    http, _ = client
    image = tmp_path / "cover.png"
    image.write_bytes(b"x")
    response = http.put(
        "/api/sets/recorder/stream/rtmp",
        json={"url": URL, "key": KEY, "image": str(image)},
    )
    assert response.status_code == 200
    assert response.json()["key"] == KEY_PLACEHOLDER
    assert KEY not in response.text


def test_start_records_argv_target_and_redacts_logs_and_status(
    tmp_path: Path, fake_ffmpeg, monkeypatch, caplog
) -> None:
    """[if] fake ffmpeg starts [then] target is redacted in status and logs, [else stop]."""
    import logging

    caplog.set_level(logging.INFO, logger="apps.stream.rtmp")
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(argv_file))
    cfg = _rtmp_config(image=None)
    push = RtmpPush(cfg, sample_rate=RATE)
    push.start()
    try:
        deadline = time.monotonic() + 3.0
        while not argv_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        argv = json.loads(argv_file.read_text(encoding="utf-8"))
        assert argv[-1] == f"{URL}/{KEY}"
        status = push.status().to_dict()
        assert status["target"] == f"{URL}/{KEY_PLACEHOLDER}"
        assert f"{URL}/{KEY_PLACEHOLDER}" in caplog.text
        assert KEY not in json.dumps(status)
        assert KEY not in caplog.text
    finally:
        push.stop()


def test_pcm_reaches_stdin_in_order(tmp_path: Path, fake_ffmpeg, monkeypatch) -> None:
    """[if] three chunks are teed [then] byte count and order match, [else stop]."""
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_FFMPEG_COUNT_FILE", str(tmp_path / "count.txt"))
    monkeypatch.setenv("FAKE_FFMPEG_TEE_FILE", str(tmp_path / "tee.bin"))
    chunks = [_pcm(512, s) for s in (1, 2, 3)]
    cfg = _rtmp_config(image=None)
    push = RtmpPush(cfg, sample_rate=RATE)
    push.start()
    for pcm in chunks:
        push.tee(pcm, RATE)
    push.stop()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (tmp_path / "count.txt").exists():
            break
        time.sleep(0.05)
    count = int((tmp_path / "count.txt").read_text(encoding="utf-8"))
    assert count == sum(len(c) for c in chunks)
    assert (tmp_path / "tee.bin").read_bytes() == b"".join(chunks)


def test_stop_closes_stdin_and_is_idempotent(tmp_path: Path, fake_ffmpeg, monkeypatch) -> None:
    """[if] stop is called [then] exit 0 stopped, second stop is safe, [else stop]."""
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(tmp_path / "argv.json"))
    cfg = _rtmp_config(image=None)
    push = RtmpPush(cfg, sample_rate=RATE)
    push.start()
    first = push.stop()
    second = push.stop()
    assert first.state == "stopped"
    assert first.exit_code == 0
    assert second.state == "stopped"


def test_self_death_sets_last_error_with_redacted_key(tmp_path: Path, fake_ffmpeg, monkeypatch) -> None:
    """[if] fake dies mid-stream [then] exited, refused in error, key redacted, [else stop]."""
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_FFMPEG_DIE_AFTER_BYTES", "256")
    cfg = _rtmp_config(image=None)
    push = RtmpPush(cfg, sample_rate=RATE)
    push.start()
    push.tee(_pcm(4096, 9), RATE)
    deadline = time.monotonic() + 5.0
    status = push.status().to_dict()
    while status.get("state") != "exited" and time.monotonic() < deadline:
        time.sleep(0.05)
        status = push.status().to_dict()
    assert status["state"] == "exited"
    assert status["exit_code"] == 1
    assert status["last_error"] is not None
    assert "refused" in status["last_error"]
    assert KEY_PLACEHOLDER in status["last_error"]
    assert KEY not in (status["last_error"] or "")
    push.stop()


def test_no_encoder_raises_stream_encoder_unavailable(empty_path, monkeypatch) -> None:
    """[if] no ffmpeg anywhere [then] STREAM_ENCODER_UNAVAILABLE, no spawn, [else stop]."""
    monkeypatch.setattr(rtmp_mod.subprocess, "Popen", _no_popen)
    push = RtmpPush(_rtmp_config(image=None), sample_rate=RATE)
    with pytest.raises(EncoderUnavailable) as raised:
        push.start()
    message = str(raised.value)
    assert "PATH" in message
    for path in empty_path:
        assert path in message
    assert raised.value.code == "STREAM_ENCODER_UNAVAILABLE"


def test_stream_start_route_without_ffmpeg_is_503(client, empty_path, monkeypatch) -> None:
    """[if] POST stream with no ffmpeg [then] 503 STREAM_ENCODER_UNAVAILABLE, [else stop]."""
    http, _ = client
    _start_master(http)
    http.put("/api/sets/recorder/stream/rtmp", json={"url": URL, "key": KEY, "image": None})
    monkeypatch.setattr(rtmp_mod.subprocess, "Popen", _no_popen)
    refused = http.post(f"/api/sets/recorder/{SESSION}/stream", json={"sink": "rtmp"})
    assert refused.status_code == 503
    assert refused.json()["detail"]["code"] == "STREAM_ENCODER_UNAVAILABLE"
    http.post(f"/api/sets/recorder/{SESSION}/stop")


def _start_master(client: TestClient) -> None:
    started = client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert started.status_code == 201, started.text
    assert (
        client.post(
            f"/api/sets/recorder/{SESSION}/master-pcm",
            params={"stream": "tab", "seq": 0, "sample_rate": RATE},
            content=_pcm(1920),
            headers={"content-type": "application/octet-stream"},
        ).status_code
        == 204
    )


def test_rtmp_routes_agent_parity_over_http(client, tmp_path: Path, fake_ffmpeg, monkeypatch) -> None:
    """[if] agent drives RTMP over HTTP [then] start, status and REC stop work, [else stop]."""
    http, _ = client
    count_file = tmp_path / "count.txt"
    monkeypatch.setenv("FAKE_FFMPEG_ARGV_FILE", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_FFMPEG_COUNT_FILE", str(count_file))
    _start_master(http)
    assert http.put("/api/sets/recorder/stream/rtmp", json={"url": URL, "key": KEY, "image": None}).status_code == 200
    started = http.post(f"/api/sets/recorder/{SESSION}/stream", json={"sink": "rtmp"})
    assert started.status_code == 201, started.text
    assert started.json()["target"] == f"{URL}/{KEY_PLACEHOLDER}"
    assert KEY not in started.text
    assert (
        http.post(
            f"/api/sets/recorder/{SESSION}/master-pcm",
            params={"stream": "tab", "seq": 1, "sample_rate": RATE},
            content=_pcm(1920),
            headers={"content-type": "application/octet-stream"},
        ).status_code
        == 204
    )
    assert http.get("/api/sets/recorder/stream").json()["stream"]["state"] == "running"
    assert http.post(f"/api/sets/recorder/{SESSION}/stop").status_code == 200
    stream = http.get("/api/sets/recorder/stream").json()["stream"]
    assert stream["state"] == "stopped"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not count_file.exists():
        time.sleep(0.05)
    assert count_file.exists()
    assert KEY not in http.get("/api/sets/recorder/stream").text


def test_non_master_recording_and_no_config_are_409(client, fake_ffmpeg) -> None:
    """[if] source is not master or config missing [then] 409, [else stop]."""
    http, _ = client
    assert (
        http.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "none", "sources": []}).status_code
        == 201
    )
    refused = http.post(f"/api/sets/recorder/{SESSION}/stream", json={"sink": "rtmp"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "STREAM_SOURCE_NOT_MASTER"
    http.post(f"/api/sets/recorder/{SESSION}/stop")
    session_master = "2026-11-10T07-00-01"
    started = http.post(
        "/api/sets/recorder/start", json={"session_id": session_master, "source": "master", "sources": []}
    )
    assert started.status_code == 201, started.text
    assert (
        http.post(
            f"/api/sets/recorder/{session_master}/master-pcm",
            params={"stream": "tab", "seq": 0, "sample_rate": RATE},
            content=_pcm(1920),
            headers={"content-type": "application/octet-stream"},
        ).status_code
        == 204
    )
    missing = http.post(f"/api/sets/recorder/{session_master}/stream", json={"sink": "rtmp"})
    assert missing.status_code == 409
    assert "configure" in missing.json()["detail"]
    http.post(f"/api/sets/recorder/{session_master}/stop")


def test_require_master_source_matches_encoder() -> None:
    """[if] source is loopback [then] STREAM_SOURCE_NOT_MASTER, [else stop]."""
    with pytest.raises(StreamSourceNotMaster):
        require_master_source("loopback")
