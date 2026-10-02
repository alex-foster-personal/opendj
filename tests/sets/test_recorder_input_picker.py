"""REC input picker: inputs listed by name, started by name, or no audio (SET-10).

[if] REC lists or starts an input [then] it is by name or explicitly none, [else stop].
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture
from apps.sets.api import router
from apps.sets.recorder_service import NO_AUDIO_DEVICE_LABEL, RecorderService
from apps.shared import ffmpeg as shared_ffmpeg

pytestmark = pytest.mark.requirement("SET-10")

LISTING = """\
[AVFoundation indev @ 0x6000] AVFoundation video devices:
[AVFoundation indev @ 0x6000] [0] FaceTime HD Camera
[AVFoundation indev @ 0x6000] AVFoundation audio devices:
[AVFoundation indev @ 0x6000] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x6000] [1] Loopback Audio
[AVFoundation indev @ 0x6000] [2] BlackHole 2ch
"""


def _runner(stderr: str):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        return MagicMock(stderr=stderr, returncode=1)

    return run, calls


def test_list_input_devices_names_every_audio_input_and_flags_loopbacks():
    """[if] ffmpeg lists audio inputs [then] each comes back named, loopbacks flagged."""
    run, calls = _runner(LISTING)
    devices = capture.list_input_devices(run=run, platform="darwin", ffmpeg="/x/ffmpeg")
    assert devices == [
        capture.InputDevice(0, "MacBook Pro Microphone", False),
        capture.InputDevice(1, "Loopback Audio", True),
        capture.InputDevice(2, "BlackHole 2ch", True),
    ]
    assert calls[0][0] == "/x/ffmpeg"
    assert calls[0][calls[0].index("-f") + 1] == "avfoundation"


def test_list_input_devices_refuses_a_listing_it_cannot_read():
    """[if] ffmpeg prints no audio-device header [then] UNKNOWN raises, never an empty list."""
    run, _ = _runner("Unknown input format: 'avfoundation'\n")
    with pytest.raises(capture.CaptureUnavailable, match="Unknown input format"):
        capture.list_input_devices(run=run, platform="darwin", ffmpeg="ffmpeg")


def test_list_input_devices_with_a_header_and_no_rows_is_a_real_empty_list():
    """[if] the header is there but no inputs follow [then] the list is measured empty."""
    run, _ = _runner("[AVFoundation indev @ 0x1] AVFoundation audio devices:\n")
    assert capture.list_input_devices(run=run, platform="darwin", ffmpeg="ffmpeg") == []


def test_list_input_devices_names_the_platform_off_macos():
    """[if] the host is not macOS [then] the error names the platform."""
    with pytest.raises(capture.CaptureUnavailable, match="'linux'"):
        capture.list_input_devices(run=_runner(LISTING)[0], platform="linux", ffmpeg="ffmpeg")


def test_list_input_devices_reports_a_hung_ffmpeg():
    """[if] the listing times out [then] CaptureUnavailable says so."""
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    with pytest.raises(capture.CaptureUnavailable, match="failed"):
        capture.list_input_devices(run=run, platform="darwin", ffmpeg="ffmpeg")


def test_default_prefers_blackhole_then_any_loopback_and_never_the_mic():
    """[if] inputs are listed [then] BlackHole wins, a loopback next, a mic never."""
    mic = capture.InputDevice(0, "MacBook Pro Microphone", False)
    loop = capture.InputDevice(1, "Loopback Audio", True)
    hole = capture.InputDevice(2, "BlackHole 2ch", True)
    assert capture.default_input_device([mic, loop, hole]) == hole
    assert capture.default_input_device([mic, loop]) == loop
    assert capture.default_input_device([mic]) is None


def test_capture_ffmpeg_falls_back_to_homebrew_only_without_an_override(monkeypatch, tmp_path):
    """[if] PATH has no ffmpeg [then] Homebrew's is used, unless MDT_FFMPEG is set."""
    brew = tmp_path / "ffmpeg"
    brew.write_text("#!/bin/sh\n")
    brew.chmod(0o755)
    monkeypatch.setattr(shared_ffmpeg, "HOMEBREW_FFMPEG_PATHS", (str(tmp_path / "absent"), str(brew)))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    assert capture.resolve_capture_ffmpeg() == str(brew)

    monkeypatch.setenv("MDT_FFMPEG", str(tmp_path / "broken"))
    with pytest.raises(capture.CaptureUnavailable, match="MDT_FFMPEG"):
        capture.resolve_capture_ffmpeg()

    monkeypatch.delenv("MDT_FFMPEG")
    monkeypatch.setattr(shared_ffmpeg, "HOMEBREW_FFMPEG_PATHS", (str(tmp_path / "absent"),))
    with pytest.raises(capture.CaptureUnavailable, match="brew install ffmpeg"):
        capture.resolve_capture_ffmpeg()


class _ExitedPopen:
    def __init__(self, argv, **kwargs):
        kwargs["stderr"].write(b"[avfoundation] Selected audio device not found\n")
        self.returncode = 1

    def poll(self):
        return self.returncode


class _RunningPopen:
    def __init__(self, argv, **kwargs):
        self.returncode = None

    def poll(self):
        return self.returncode


def test_startup_check_refuses_a_capture_that_already_exited(tmp_path: Path):
    """[if] ffmpeg exits inside the startup window [then] REC fails with its stderr."""
    with pytest.raises(capture.CaptureUnavailable, match="device not found"):
        capture.start_capture(
            tmp_path, 4, popen=cast(Any, _ExitedPopen), ffmpeg="ffmpeg", startup_check_s=0.1
        )


def test_startup_check_passes_a_capture_that_is_still_running(tmp_path: Path):
    """[if] ffmpeg is alive after the window [then] the handle is returned."""
    handle = capture.start_capture(
        tmp_path, 4, popen=cast(Any, _RunningPopen), ffmpeg="ffmpeg", startup_check_s=0.1
    )
    assert handle.argv[handle.argv.index("-i") + 1] == ":4"
    handle.log_fh.close()


DEVICES = [
    capture.InputDevice(0, "MacBook Pro Microphone", False),
    capture.InputDevice(3, "BlackHole 2ch", True),
]


@pytest.fixture
def picker_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
        list_devices=lambda: list(DEVICES),
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def _manifest(service: RecorderService, session_id: str) -> dict:
    return json.loads((service.sets_root / session_id / "manifest.json").read_text())


def test_devices_route_lists_inputs_and_the_preselected_one(picker_client):
    """[if] the picker asks for inputs [then] it gets names and BlackHole preselected."""
    client, _ = picker_client
    response = client.get("/api/sets/recorder/devices")
    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {"index": 0, "name": "MacBook Pro Microphone", "loopback": False},
            {"index": 3, "name": "BlackHole 2ch", "loopback": True},
        ],
        "default_name": "BlackHole 2ch",
    }


def test_devices_route_is_503_with_the_reason_when_it_cannot_list(tmp_path: Path):
    """[if] inputs cannot be listed [then] 503 carries why, never an empty list."""
    def broken() -> list[capture.InputDevice]:
        raise capture.CaptureUnavailable("ffmpeg is needed to record set audio")

    app = FastAPI()
    app.state.sets_recorder_service = RecorderService(
        sets_root=tmp_path, db_path=tmp_path / "sets.db", list_devices=broken
    )
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/sets/recorder/devices")
    assert response.status_code == 503
    assert "ffmpeg is needed" in response.json()["detail"]


def test_start_by_name_records_that_input_and_names_it_in_the_manifest(picker_client):
    """[if] REC starts with a device name [then] the session records under that name."""
    client, service = picker_client
    session_id = "2026-10-02T21-00-00"
    started = client.post(
        "/api/sets/recorder/start",
        json={"session_id": session_id, "device_name": "BlackHole 2ch", "sources": []},
    )
    assert started.status_code == 201
    assert service._recorder.config.ffmpeg_device_idx == 3
    client.post(f"/api/sets/recorder/{session_id}/stop")
    assert _manifest(service, session_id)["capture_device"] == "BlackHole 2ch"


def test_start_by_a_disconnected_name_is_503_and_records_nothing(picker_client):
    """[if] the remembered input is unplugged [then] 503 names it and no session starts."""
    client, service = picker_client
    response = client.post(
        "/api/sets/recorder/start",
        json={"device_name": "Scarlett 2i2", "sources": []},
    )
    assert response.status_code == 503
    assert "'Scarlett 2i2' is not connected" in response.json()["detail"]
    assert client.get("/api/sets/recorder").json()["active"] is False
    assert not service.sets_root.exists() or not any(service.sets_root.iterdir())


def test_tracklist_only_start_records_no_audio(picker_client):
    """[if] REC starts with capture_audio false [then] no input is used or listed."""
    client, service = picker_client
    service.list_devices = lambda: pytest.fail("a no-audio start must not list inputs")
    session_id = "2026-10-02T21-10-00"
    started = client.post(
        "/api/sets/recorder/start",
        json={"session_id": session_id, "capture_audio": False, "sources": []},
    )
    assert started.status_code == 201
    assert service._recorder.config.capture_disabled is True
    assert service._recorder.config.ffmpeg_device_idx is None
    client.post(f"/api/sets/recorder/{session_id}/stop")
    assert _manifest(service, session_id)["capture_device"] == NO_AUDIO_DEVICE_LABEL


def test_the_started_input_is_remembered_by_the_daemon_across_restarts(picker_client):
    """[if] REC starts by name or with no audio [then] a new daemon reads that choice back."""
    client, service = picker_client
    assert client.get("/api/sets/recorder/remembered-input").json() == {"remembered": None}
    first = "2026-10-02T22-00-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": first, "device_name": "BlackHole 2ch", "sources": []},
    )
    client.post(f"/api/sets/recorder/{first}/stop")
    assert client.get("/api/sets/recorder/remembered-input").json() == {
        "remembered": {"kind": "device", "name": "BlackHole 2ch"}
    }
    restarted = RecorderService(
        sets_root=service.sets_root, db_path=service.db_path, capture_enabled=False
    )
    assert restarted.remembered_input() == {"kind": "device", "name": "BlackHole 2ch"}

    second = "2026-10-02T22-10-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": second, "capture_audio": False, "sources": []},
    )
    client.post(f"/api/sets/recorder/{second}/stop")
    assert client.get("/api/sets/recorder/remembered-input").json() == {
        "remembered": {"kind": "none", "name": None}
    }


def test_a_refused_start_leaves_the_remembered_input_alone(picker_client):
    """[if] a start fails [then] the last good choice stays remembered."""
    client, service = picker_client
    session_id = "2026-10-02T22-20-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": session_id, "device_name": "BlackHole 2ch", "sources": []},
    )
    client.post(f"/api/sets/recorder/{session_id}/stop")
    refused = client.post(
        "/api/sets/recorder/start", json={"device_name": "Scarlett 2i2", "sources": []}
    )
    assert refused.status_code == 503
    assert service.remembered_input() == {"kind": "device", "name": "BlackHole 2ch"}


def test_a_start_by_index_does_not_overwrite_the_remembered_name(picker_client):
    """[if] a script starts by ffmpeg index [then] the picker's remembered name is kept."""
    client, service = picker_client
    by_name = "2026-10-02T22-30-00"
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": by_name, "device_name": "BlackHole 2ch", "sources": []},
    )
    client.post(f"/api/sets/recorder/{by_name}/stop")
    by_index = "2026-10-02T22-40-00"
    started = client.post(
        "/api/sets/recorder/start",
        json={"session_id": by_index, "ffmpeg_device_idx": 0, "sources": []},
    )
    assert started.status_code == 201
    client.post(f"/api/sets/recorder/{by_index}/stop")
    assert service.remembered_input() == {"kind": "device", "name": "BlackHole 2ch"}


@pytest.mark.parametrize(
    "content",
    ['{"kind": "device", "name": ""}', '{"kind": "device"}', "not json", '["none"]'],
)
def test_a_malformed_remembered_input_is_an_error_not_nothing(picker_client, content):
    """[if] the remembered-input file is junk [then] 500 names it, never "nothing yet"."""
    client, service = picker_client
    service.remembered_input_path.parent.mkdir(parents=True, exist_ok=True)
    service.remembered_input_path.write_text(content, encoding="utf-8")
    response = client.get("/api/sets/recorder/remembered-input")
    assert response.status_code == 500
    assert str(service.remembered_input_path) in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"sources": []},
        {"ffmpeg_device_idx": 0, "device_name": "BlackHole 2ch", "sources": []},
        {"capture_audio": False, "device_name": "BlackHole 2ch", "sources": []},
    ],
)
def test_start_requires_exactly_one_input_or_an_explicit_no_audio(picker_client, body):
    """[if] a start names zero or two inputs [then] 422, never a silent default."""
    client, _ = picker_client
    assert client.post("/api/sets/recorder/start", json=body).status_code == 422


def test_start_by_a_name_two_inputs_share_is_refused(tmp_path: Path):
    """[if] two inputs share the picked name [then] 503, never the first one silently."""
    twins = [capture.InputDevice(0, "USB Audio", False), capture.InputDevice(1, "USB Audio", False)]
    app = FastAPI()
    app.state.sets_recorder_service = RecorderService(
        sets_root=tmp_path, db_path=tmp_path / "sets.db", capture_enabled=False,
        list_devices=lambda: twins,
    )
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/sets/recorder/start", json={"device_name": "USB Audio", "sources": []}
        )
    assert response.status_code == 503
    assert "2 audio inputs are named 'USB Audio'" in response.json()["detail"]


class _EnvPopen(_RunningPopen):
    env: dict[str, str] | None = None

    def __init__(self, argv, **kwargs):
        super().__init__(argv, **kwargs)
        _EnvPopen.env = kwargs.get("env")


def test_capture_runs_ffmpeg_in_utc_so_segment_names_match_the_set_id(tmp_path: Path):
    """[if] ffmpeg names segments [then] in UTC like the set id, never local time."""
    handle = capture.start_capture(tmp_path, 1, popen=cast(Any, _EnvPopen), ffmpeg="ffmpeg")
    assert _EnvPopen.env is not None and _EnvPopen.env["TZ"] == "UTC"
    handle.log_fh.close()
