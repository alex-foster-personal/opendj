"""Set recording through odj-audio, the capture the installed app has (no ffmpeg).

Covers backend choice (odj-audio when it can capture, ffmpeg only as a
checkout fallback, never a silent fallback in the installed app), the
odj-audio listing and record/stop process contract, and that every segment
consumer takes the ``.wav`` segments odj-audio writes.

[if] REC starts on a Mac without ffmpeg [then] the bundled odj-audio records the input as WAV, [else stop].

Everything here drives the real ``odj-audio``, built from this checkout: the
default build (no audio input) and a ``--features device`` build. Recording
needs an input that cannot hear a room, so it uses ALSA's ``null`` capture
device (zero samples) and reports UNAVAILABLE on a host without one, as it
does where the crate cannot build (tests/rust_build_env.py).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import audio as sets_audio
from apps.sets import capture, retention
from apps.sets import paths as sets_paths
from apps.sets.api import router
from apps.sets.capture_odj_audio import (
    _parse_odj_audio_devices,
    follow_record_output,
    odj_audio_backend,
)
from apps.sets.capture_types import CaptureState
from apps.sets.record import _segment_start_from_name
from apps.sets.recorder_service import RecorderService
from apps.shared import platform_paths
from apps.shared.odj_audio_binary import EXE_NAME, REPO_TARGET
from tests.rust_build_env import build_audio_engine

pytestmark = pytest.mark.requirement("SET-11")

CRATE = platform_paths.PROJECT_ROOT / "apps" / "audio-engine"
#: ALSA's null PCM as cpal names it: zero samples, no microphone, no prompt.
NULL_INPUT_HINT = "generate zero samples"


@pytest.fixture(scope="module")
def plain_engine() -> Path:
    """The default build: decode and render, no audio input."""
    return build_audio_engine(CRATE)


@pytest.fixture(scope="module")
def capture_engine() -> Path:
    """The ``--features device`` build, which the app bundles."""
    return build_audio_engine(CRATE, features=("device",))


@pytest.fixture(scope="module")
def null_input(capture_engine: Path) -> str:
    listing = subprocess.run(
        [str(capture_engine), "input-devices"], capture_output=True, text=True, check=False, timeout=30
    )
    devices = (_parse_odj_audio_devices(listing.stdout) if listing.returncode == 0 else None) or []
    if listing.returncode != 0:
        pytest.skip(f"UNAVAILABLE: odj-audio input-devices exited {listing.returncode}: {listing.stderr[-300:]}")
    names = [d.name for d in devices if NULL_INPUT_HINT in d.name]
    if not names:
        pytest.skip(
            "UNAVAILABLE: no silent ALSA null input here to record without a microphone "
            f"(inputs: {[d.name for d in devices]}, listing: {listing.stdout[-300:]!r})"
        )
    return names[0]


def _install(src: Path, repo: Path, profile: str, mtime: float) -> Path:
    """A copy of a real build where a checkout's cargo would put it."""
    dest = repo / REPO_TARGET / profile / EXE_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    os.utime(dest, (mtime, mtime))
    return dest


# ---------------------------------------------------------------------------
# backend choice
# ---------------------------------------------------------------------------


def test_the_installed_app_records_through_its_engine(tmp_path: Path, capture_engine: Path):
    """[if] the bundled engine can capture [then] REC uses it, whatever ffmpeg there is."""
    backend = capture.capture_backend(environ={"ODJ_AUDIO_BIN": str(capture_engine)}, repo_root=tmp_path)
    assert backend == capture.CaptureBackend("odj-audio", str(capture_engine))


def test_the_installed_app_never_falls_back_to_ffmpeg(tmp_path: Path, plain_engine: Path):
    """[if] ODJ_AUDIO_BIN cannot capture [then] refuse, naming why, without trying ffmpeg."""
    with pytest.raises(capture.CaptureUnavailable, match="without audio input") as exc:
        capture.capture_backend(environ={"ODJ_AUDIO_BIN": str(plain_engine)}, repo_root=tmp_path)
    assert "ffmpeg" not in str(exc.value)
    with pytest.raises(capture.CaptureUnavailable, match="is not a file"):
        capture.capture_backend(environ={"ODJ_AUDIO_BIN": str(tmp_path / "missing")}, repo_root=tmp_path)
    # A program that is not odj-audio at all prints no version object.
    with pytest.raises(capture.CaptureUnavailable, match="without a version object"):
        capture.capture_backend(environ={"ODJ_AUDIO_BIN": sys.executable}, repo_root=tmp_path)


def test_a_checkout_finds_its_capture_build_behind_a_newer_plain_one(
    tmp_path: Path, plain_engine: Path, capture_engine: Path
):
    """[if] the newest local build lacks the device feature [then] an older one that has it records."""
    now = time.time()
    _install(plain_engine, tmp_path, "debug", now)
    release = _install(capture_engine, tmp_path, "release", now - 3600)
    assert odj_audio_backend({}, tmp_path, subprocess.run) == (capture.CaptureBackend("odj-audio", str(release)), "")
    # Control: with only the plain build, there is no odj-audio capture, and why.
    release.unlink()
    backend, why = odj_audio_backend({}, tmp_path, subprocess.run)
    assert backend is None and "without audio input" in why, why


def test_with_no_build_the_reason_says_to_build_one(tmp_path: Path):
    backend, why = odj_audio_backend({}, tmp_path, subprocess.run)
    assert backend is None and "no local build" in why, why


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

#: `odj-audio input-devices` as the device build printed it on a Linux host.
LISTING = (
    '{"devices":[{"channels":2,"index":0,"name":"Discard all samples (playback) or generate zero samples '
    '(capture)","rate":48000},{"channels":2,"index":1,"name":"BlackHole 2ch","rate":48000}]}'
)


def test_the_listing_gives_the_picker_indices_names_and_loopbacks():
    devices = _parse_odj_audio_devices(LISTING)
    assert devices is not None
    assert [(d.index, d.loopback) for d in devices] == [(0, False), (1, True)]
    assert capture.default_input_device(devices) == devices[1]


@pytest.mark.parametrize("stdout", ["{}", '{"devices": [{"index": 0}]}', "garbage", '{"devices": 3}'])
def test_a_malformed_listing_is_not_a_list(stdout: str):
    assert _parse_odj_audio_devices(stdout) is None
    assert _parse_odj_audio_devices('{"devices": []}') == []


def test_the_device_build_lists_this_hosts_inputs(capture_engine: Path):
    """[if] odj-audio can capture [then] its listing reaches the picker, on any host."""
    backend = capture.CaptureBackend("odj-audio", str(capture_engine))
    devices = capture.list_input_devices(backend=backend)
    assert [d.index for d in devices] == list(range(len(devices)))


def test_an_unavailable_real_input_does_not_hide_other_inputs(capture_engine: Path, tmp_path: Path, caplog: Any):
    """[if] input cannot configure [then] list usable inputs and refuse that name, [else stop]."""
    listing = subprocess.run(
        [str(capture_engine), "input-devices"], capture_output=True, text=True, check=False, timeout=30
    )
    assert listing.returncode == 0, listing.stderr
    prefix = "odj-audio: UNAVAILABLE audio input "
    rejected = [line[len(prefix) :] for line in listing.stderr.splitlines() if line.startswith(prefix)]
    if not rejected:
        pytest.skip("UNAVAILABLE: this host has no actual rejected input configuration to verify")
    name, consumed = json.JSONDecoder().raw_decode(rejected[0])
    reason = rejected[0][consumed:].removeprefix(": ")
    with caplog.at_level("WARNING", logger="apps.sets.capture_odj_audio"):
        devices = capture.list_input_devices(backend=capture.CaptureBackend("odj-audio", str(capture_engine)))
    assert [device.index for device in devices] == list(range(len(devices)))
    assert name not in [device.name for device in devices]
    assert name in caplog.text and reason in caplog.text
    recording = subprocess.run(
        [str(capture_engine), "record", "--device", name, "--dir", str(tmp_path)],
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert recording.returncode != 0
    failed = [json.loads(line)["failed"] for line in recording.stdout.splitlines() if "failed" in json.loads(line)]
    assert failed and name in failed[0] and reason in failed[0]
    assert not list(tmp_path.glob("*.wav")), "unavailable named input wrote audio"


def test_a_build_that_cannot_list_is_unavailable_not_empty(plain_engine: Path):
    """[if] the listing fails [then] CaptureUnavailable with odj-audio's reason, never []."""
    with pytest.raises(capture.CaptureUnavailable, match="rebuild with --features device"):
        capture.list_input_devices(backend=capture.CaptureBackend("odj-audio", str(plain_engine)))


# ---------------------------------------------------------------------------
# record / stop
# ---------------------------------------------------------------------------


def _wait_for(predicate: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def started() -> Any:
    """Handles a test started; any still running at teardown are killed, so a
    failed assertion never leaves an engine (or a frozen one) behind."""
    handles: list[capture.CaptureHandle] = []
    yield handles
    for handle in handles:
        if handle.proc.poll() is None:
            if sys.platform != "win32":
                os.kill(handle.proc.pid, signal.SIGCONT)
            handle.proc.kill()
            handle.proc.wait(timeout=5)
        if not handle.log_fh.closed:
            handle.log_fh.close()


def _start(
    capture_engine: Path, session: Path, name: str, started: list[capture.CaptureHandle]
) -> capture.CaptureHandle:
    handle = capture.start_capture(
        session,
        0,
        backend=capture.CaptureBackend("odj-audio", str(capture_engine)),
        segment_time_s=300,
        device_name=name,
        startup_check_s=0.5,
    )
    started.append(handle)
    return handle


def test_odj_audio_records_wav_by_name_and_stops_when_stdin_closes(
    tmp_path: Path, capture_engine: Path, null_input: str, started: Any
):
    """[if] REC starts on odj-audio by name [then] it writes WAV from that input and stops on stdin EOF."""
    session = tmp_path / "session"
    handle = _start(capture_engine, session, null_input, started)
    assert handle.backend == "odj-audio"
    assert handle.argv[handle.argv.index("--device") + 1] == null_input
    assert handle.stderr_log == session / "odj-audio.stderr.log"
    assert _wait_for(lambda: handle.current_state() == "recording")
    # "recording" is reported only once frames are in a segment file.
    assert sets_paths.segment_files(session), "REC lit before any audio was written"
    # A header is rewritten every second, so a segment with frames proves audio is landing.
    assert _wait_for(lambda: any(p.stat().st_size > 44 for p in sets_paths.segment_files(session)))
    assert capture.stop_capture(handle) == 0
    assert handle.current_state() == "stopped"
    assert handle.log_fh.closed and handle.proc.stdin is not None and handle.proc.stdin.closed
    segments = sets_paths.segment_files(session)
    assert segments and all(p.suffix == ".wav" for p in segments), segments
    with wave.open(str(segments[0])) as w:
        assert (w.getnchannels(), w.getsampwidth()) == (2, 2)
        assert w.getframerate() > 0 and w.getnframes() > 0
    log = handle.stderr_log.read_text()
    assert '"recording"' in log and '"stopped"' in log


def test_odj_audio_without_a_name_records_the_index():
    """Control: a scripted start by index passes the index."""
    argv = capture.build_record_argv("/x/odj-audio", 4, Path("/s"), segment_time_s=60)
    assert argv[argv.index("--device-index") + 1] == "4" and "--device" not in argv


def test_an_input_that_is_gone_refuses_rec_with_the_reason(tmp_path: Path, capture_engine: Path, null_input: str):
    """[if] odj-audio refuses at start [then] REC fails with its message, and nothing records."""
    with pytest.raises(capture.CaptureUnavailable) as exc:
        capture.start_capture(
            tmp_path / "s",
            0,
            backend=capture.CaptureBackend("odj-audio", str(capture_engine)),
            device_name="No Such Input",
            startup_check_s=5.0,
        )
    assert "odj-audio stopped" in str(exc.value) and "is not connected" in str(exc.value)
    assert sets_paths.segment_files(tmp_path / "s") == []


def test_a_capture_that_dies_reads_failed(tmp_path: Path, capture_engine: Path, null_input: str, started: Any):
    """[if] odj-audio exits without its stopped line [then] REC shows the capture failed."""
    handle = _start(capture_engine, tmp_path / "s", null_input, started)
    assert _wait_for(lambda: handle.current_state() == "recording")
    handle.proc.kill()
    handle.proc.wait(timeout=5)
    assert _wait_for(lambda: handle.current_state() == "failed")
    assert capture.stop_capture(handle) != 0


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP is POSIX; Windows has no way to freeze a process")
def test_an_odj_audio_that_ignores_stdin_is_killed(tmp_path: Path, capture_engine: Path, null_input: str, started: Any):
    """[if] closing stdin does not end it (here: frozen) [then] stop still returns, by kill."""
    handle = _start(capture_engine, tmp_path / "s", null_input, started)
    assert _wait_for(lambda: handle.current_state() == "recording")
    os.kill(handle.proc.pid, signal.SIGSTOP)
    assert capture.stop_capture(handle, timeout=0.3) != 0
    assert handle.proc.poll() is not None
    assert handle.log_fh.closed


def test_rec_by_ffmpeg_index_is_refused_when_odj_audio_records(tmp_path: Path, capture_engine: Path):
    """[if] a start names an ffmpeg input index but odj-audio is the recorder [then] 503, nothing starts:
    the two number inputs differently, so the index could record the room microphone."""
    # The installed app's own configuration: its engine, which can capture.
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        environ={"ODJ_AUDIO_BIN": str(capture_engine)},
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/sets/recorder/start", json={"session_id": None, "ffmpeg_device_idx": 1})
        assert response.status_code == 503, response.text
        assert "start it by device_name" in response.json()["detail"]
        assert client.get("/api/sets/recorder").json()["active"] is False


def test_a_recording_that_ends_on_an_error_says_why_on_stdout(tmp_path: Path, capture_engine: Path):
    """[if] `odj-audio record` ends on an error [then] its stdout carries {"failed": why},
    so REC can show why after the start has returned (a microphone denied at a late prompt)."""
    run = subprocess.run(
        [str(capture_engine), "record", "--dir", str(tmp_path), "--device", "No Such Input"],
        input="",
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert run.returncode != 0
    lines = [json.loads(line) for line in run.stdout.splitlines() if line.strip()]
    assert len(lines) == 1 and "is not connected" in lines[0]["failed"], run.stdout


#: `odj-audio record` stdout during a first-run macOS microphone prompt, as
#: record_cmd prints it (apps/audio-engine/src/bin/odj-audio.rs). The prompt
#: only exists on macOS, so this is that protocol, read through a real pipe.
PROMPT_LINES = [
    b'{"waiting":"microphone_permission"}\n',
    b'{"recording":{"channels":2,"device":"BlackHole 2ch","rate":48000,"source_channels":2}}\n',
    b'{"stopped":{"dropped_samples":0,"frames":96000,"segments":["audio_2026-10-03T03-29-00.wav"]}}\n',
]


def _follow(lines: list[bytes], expect: list[str]) -> tuple[CaptureState, io.BytesIO]:
    """Feed ``lines`` through a pipe to the reader, one at a time, waiting after
    each for the state ``expect`` names (the reader logs a line before it sets
    the state, so the log is not the signal)."""
    read_fd, write_fd = os.pipe()
    state = CaptureState("starting")
    log = io.BytesIO()
    with os.fdopen(read_fd, "rb") as stdout, os.fdopen(write_fd, "wb", buffering=0) as feed:
        thread = follow_record_output(stdout, log, state)
        for line, want in zip(lines, expect, strict=True):
            feed.write(line)
            assert _wait_for(lambda w=want: state.value == w, timeout=5), (line, want, state.value)
        feed.close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    return state, log


def test_rec_waits_while_the_macos_microphone_prompt_is_up():
    """[if] macOS is still asking for the microphone [then] the state is waiting, not recording,
    until audio is written (Silver lost 42 s to a REC that looked live during the prompt)."""
    state, log = _follow(PROMPT_LINES, ["waiting_permission", "recording", "stopped"])
    assert state.value == "stopped"
    assert log.getvalue() == b"".join(PROMPT_LINES)


def test_a_failed_line_keeps_the_engines_reason():
    """[if] the engine reports {"failed": why} after the prompt [then] the state is failed with that why."""
    why = "microphone access for Open DJ is off; turn it on in System Settings"
    state, _ = _follow(
        [PROMPT_LINES[0], (json.dumps({"failed": why}) + "\n").encode()], ["waiting_permission", "failed"]
    )
    assert (state.value, state.error) == ("failed", why)


def test_output_that_ends_without_stopped_is_a_failed_capture():
    """Control: the same prompt, then the engine gone, ends failed, not waiting."""
    state, _ = _follow(PROMPT_LINES[:1], ["waiting_permission"])
    assert (state.value, state.error) == ("failed", None)


# ---------------------------------------------------------------------------
# .wav segments everywhere a segment is read
# ---------------------------------------------------------------------------

SESSION = "2026-10-03T02-55-00"


def _session(tmp_path: Path) -> Path:
    session_dir = sets_paths.session_dir(SESSION, root=tmp_path)
    session_dir.mkdir(parents=True)
    for name in ("audio_2026-10-03T03-00-00.wav", "audio_2026-10-03T02-55-00.mp3", "audio_2026-10-03T03-05-00.ogg"):
        (session_dir / name).write_bytes(b"RIFF")
    return session_dir


def test_segments_of_both_backends_are_listed_in_start_order(tmp_path: Path):
    """[if] a session holds .mp3 and .wav segments [then] both are listed, by start; others are not."""
    _session(tmp_path)
    names = [v.name for v in sets_audio.list_segments(SESSION, sets_root=tmp_path)]
    assert names == ["audio_2026-10-03T02-55-00.mp3", "audio_2026-10-03T03-00-00.wav"]
    assert sets_audio.resolve_segment_path(SESSION, "audio_2026-10-03T03-00-00.wav", sets_root=tmp_path).name.endswith(
        ".wav"
    )
    with pytest.raises(sets_audio.PathTraversalError):
        sets_audio.resolve_segment_path(SESSION, "audio_2026-10-03T03-05-00.ogg", sets_root=tmp_path)


def test_a_wav_segment_start_is_read_from_its_utc_name():
    started = datetime(2026, 10, 3, 2, 55, tzinfo=UTC)
    assert _segment_start_from_name("audio_2026-10-03T03-00-00.wav", started) == 300.0


def test_retention_prunes_wav_segments_too(tmp_path: Path):
    session_dir = _session(tmp_path)
    old = 1_000_000.0
    for p in session_dir.iterdir():
        os.utime(p, (old, old))
    found = sorted(c.path.name for c in retention.find_candidates(1, root=tmp_path, now=old + 3 * 86400))
    assert found == ["audio_2026-10-03T02-55-00.mp3", "audio_2026-10-03T03-00-00.wav"]
