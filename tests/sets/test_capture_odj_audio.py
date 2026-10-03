"""Set recording through odj-audio, the capture the installed app has (no ffmpeg).

Covers backend choice (odj-audio when it can capture, ffmpeg only as a
checkout fallback, never a silent fallback in the installed app), the
odj-audio listing and record/stop process contract, and that every segment
consumer takes the ``.wav`` segments odj-audio writes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from apps.sets import audio as sets_audio
from apps.sets import capture, retention
from apps.sets import paths as sets_paths
from apps.sets.record import _segment_start_from_name

pytestmark = pytest.mark.requirement("SET-11")


def _exe(tmp_path: Path, name: str = "odj-audio") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _result(argv: list[str], stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _version_runner(capture_flag: object, calls: list[list[str]] | None = None):
    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(argv)
        assert argv[1:] == ["version"], argv
        return _result(argv, json.dumps({"engine": "odj-audio 0.1.0", "protocol": 1, "capture": capture_flag}))

    return run


def _no_ffmpeg() -> str:
    raise capture.CaptureUnavailable("ffmpeg is needed to record set audio: not found")


# ---------------------------------------------------------------------------
# backend choice
# ---------------------------------------------------------------------------


def test_a_capture_capable_odj_audio_is_chosen_over_ffmpeg(tmp_path: Path):
    """[if] the bundled engine can capture [then] REC uses it, even with ffmpeg present."""
    exe = _exe(tmp_path)
    backend = capture.capture_backend(
        environ={"ODJ_AUDIO_BIN": str(exe)},
        repo_root=tmp_path,
        run=_version_runner(True),
        resolve_ffmpeg=lambda: "/opt/homebrew/bin/ffmpeg",
    )
    assert backend == capture.CaptureBackend("odj-audio", str(exe))


def test_the_installed_app_never_falls_back_to_ffmpeg(tmp_path: Path):
    """[if] ODJ_AUDIO_BIN cannot capture [then] refuse, naming it, without trying ffmpeg."""
    exe = _exe(tmp_path)

    def ffmpeg_must_not_be_asked() -> str:
        raise AssertionError("the installed app must not reach for ffmpeg")

    with pytest.raises(capture.CaptureUnavailable, match="without audio input"):
        capture.capture_backend(
            environ={"ODJ_AUDIO_BIN": str(exe)},
            repo_root=tmp_path,
            run=_version_runner(False),
            resolve_ffmpeg=ffmpeg_must_not_be_asked,
        )
    with pytest.raises(capture.CaptureUnavailable, match="is not a file"):
        capture.capture_backend(
            environ={"ODJ_AUDIO_BIN": str(tmp_path / "missing")},
            repo_root=tmp_path,
            run=_version_runner(True),
            resolve_ffmpeg=ffmpeg_must_not_be_asked,
        )


def test_a_checkout_build_without_capture_falls_back_to_ffmpeg(tmp_path: Path):
    """[if] the repo build lacks the device feature [then] ffmpeg records, as before."""
    build = tmp_path / "apps" / "audio-engine" / "target" / "debug"
    build.mkdir(parents=True)
    _exe(build)
    backend = capture.capture_backend(
        environ={}, repo_root=tmp_path, run=_version_runner(False), resolve_ffmpeg=lambda: "/usr/bin/ffmpeg"
    )
    assert backend == capture.CaptureBackend("ffmpeg", "/usr/bin/ffmpeg")
    # Control: the same build reporting capture is chosen.
    backend = capture.capture_backend(
        environ={}, repo_root=tmp_path, run=_version_runner(True), resolve_ffmpeg=lambda: "/usr/bin/ffmpeg"
    )
    assert backend.kind == "odj-audio"


def test_with_neither_the_error_names_both_reasons(tmp_path: Path):
    """[if] no odj-audio and no ffmpeg [then] the error says what is missing for each."""
    with pytest.raises(capture.CaptureUnavailable) as exc:
        capture.capture_backend(environ={}, repo_root=tmp_path, run=_version_runner(True), resolve_ffmpeg=_no_ffmpeg)
    message = str(exc.value)
    assert "ffmpeg is needed" in message and "no local build" in message, message


@pytest.mark.parametrize(
    "version",
    [_result(["x"], "not json"), _result(["x"], "", code=1, stderr="boom"), _result(["x"], "[1]")],
)
def test_an_unreadable_version_probe_is_not_a_capture(tmp_path: Path, version: subprocess.CompletedProcess[str]):
    """[if] `version` fails or prints garbage [then] that build is not trusted to record."""
    exe = _exe(tmp_path)
    with pytest.raises(capture.CaptureUnavailable, match="cannot record set audio"):
        capture.capture_backend(
            environ={"ODJ_AUDIO_BIN": str(exe)},
            repo_root=tmp_path,
            run=lambda argv, **_: version,
            resolve_ffmpeg=lambda: "/usr/bin/ffmpeg",
        )


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

ODJ = capture.CaptureBackend("odj-audio", "/app/bin/odj-audio")


def test_odj_audio_lists_inputs_on_any_host_and_flags_loopbacks():
    """[if] odj-audio lists inputs [then] the picker gets its indices and names, macOS or not."""
    listing = {
        "devices": [
            {"index": 0, "name": "MacBook Pro Microphone", "channels": 1, "rate": 48000},
            {"index": 1, "name": "BlackHole 2ch", "channels": 2, "rate": 48000},
        ]
    }
    calls: list[list[str]] = []

    def run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _result(argv, json.dumps(listing))

    devices = capture.list_input_devices(run=run, platform="linux", backend=ODJ)
    assert calls == [["/app/bin/odj-audio", "input-devices"]]
    assert devices == [
        capture.InputDevice(0, "MacBook Pro Microphone", False),
        capture.InputDevice(1, "BlackHole 2ch", True),
    ]
    assert capture.default_input_device(devices) == devices[1]


@pytest.mark.parametrize(
    "result",
    [
        _result(["x"], "", code=2, stderr="odj-audio: cannot list audio inputs: no host"),
        _result(["x"], "{}"),
        _result(["x"], '{"devices": [{"index": 0}]}'),
        _result(["x"], "garbage"),
    ],
)
def test_an_unreadable_odj_audio_listing_is_unavailable_not_empty(result: subprocess.CompletedProcess[str]):
    """[if] the listing fails or is malformed [then] CaptureUnavailable, never []."""
    with pytest.raises(capture.CaptureUnavailable, match="input-devices"):
        capture.list_input_devices(run=lambda argv, **_: result, backend=ODJ)


def test_a_real_empty_odj_audio_listing_is_an_empty_list():
    """[if] odj-audio measured no inputs [then] that is a real empty list."""
    assert capture.list_input_devices(run=lambda argv, **_: _result(argv, '{"devices": []}'), backend=ODJ) == []


# ---------------------------------------------------------------------------
# record / stop
# ---------------------------------------------------------------------------


# A stand-in for `odj-audio record` run as a real process, so the pipe
# contract (state lines on stdout, stop on stdin EOF) is what is tested. It
# is a Python file named `record`, run as `python record ...` from its own
# directory, which works on every OS. `mode` picks its behavior.
_STUB = """
import json, pathlib, sys, time
here = pathlib.Path(__file__).resolve().parent
(here / "argv.json").write_text(json.dumps(sys.argv[1:]))
mode = (here / "mode").read_text()
if mode == "denied":
    print("odj-audio: microphone access for Open DJ is off", file=sys.stderr)
    sys.exit(2)
if mode == "prompt":
    print(json.dumps({"waiting": "microphone_permission"}), flush=True)
    while not (here / "granted").exists():
        time.sleep(0.02)
print(json.dumps({"recording": {"device": "BlackHole 2ch"}}), flush=True)
if mode == "deaf":
    time.sleep(60)
for line in sys.stdin:
    if line.strip() == "stop":
        break
if mode == "dies":
    sys.exit(1)
print(json.dumps({"stopped": {"segments": [], "frames": 0, "dropped_samples": 0}}), flush=True)
"""


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "stub"
    home.mkdir()
    (home / "record").write_text(_STUB)
    monkeypatch.chdir(home)

    def make(mode: str) -> capture.CaptureBackend:
        (home / "mode").write_text(mode)
        return capture.CaptureBackend("odj-audio", sys.executable)

    make.home = home  # type: ignore[attr-defined]
    return make


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_odj_audio_records_by_name_and_stops_when_stdin_closes(tmp_path: Path, stub: Any):
    """[if] REC starts on odj-audio by name [then] it opens that exact name and stops on stdin EOF."""
    session = tmp_path / "session"
    handle = capture.start_capture(
        session, 1, backend=stub("ok"), segment_time_s=300, device_name="BlackHole 2ch", startup_check_s=0.3
    )
    assert handle.backend == "odj-audio"
    assert handle.stderr_log == session / "odj-audio.stderr.log"
    assert _wait_for(lambda: handle.current_state() == "recording")
    assert capture.stop_capture(handle) == 0
    assert handle.current_state() == "stopped"
    assert handle.log_fh.closed and handle.proc.stdin is not None and handle.proc.stdin.closed
    argv = json.loads((stub.home / "argv.json").read_text())
    assert argv == ["--dir", str(session), "--device", "BlackHole 2ch", "--segment-seconds", "300"]
    log = handle.stderr_log.read_text()
    assert '"recording"' in log and '"stopped"' in log


def test_odj_audio_without_a_name_records_the_index():
    """Control: a scripted start by index passes the index."""
    argv = capture.build_record_argv("/x/odj-audio", 4, Path("/s"), segment_time_s=60)
    assert argv[argv.index("--device-index") + 1] == "4" and "--device" not in argv


def test_rec_waits_while_the_macos_microphone_prompt_is_up(tmp_path: Path, stub: Any):
    """[if] macOS is still asking for the microphone [then] the state is waiting, not recording,
    until the grant (Silver lost 42 s to a REC that looked live during the prompt)."""
    handle = capture.start_capture(tmp_path / "s", 1, backend=stub("prompt"), startup_check_s=0.3)
    assert _wait_for(lambda: handle.current_state() == "waiting_permission")
    time.sleep(0.2)
    assert handle.current_state() == "waiting_permission"
    (stub.home / "granted").write_text("")
    assert _wait_for(lambda: handle.current_state() == "recording")
    assert capture.stop_capture(handle) == 0


def test_a_capture_that_dies_reads_failed(tmp_path: Path, stub: Any):
    """[if] odj-audio exits without its stopped line [then] REC shows the capture failed."""
    handle = capture.start_capture(tmp_path / "s", 1, backend=stub("dies"), startup_check_s=0.3)
    assert _wait_for(lambda: handle.current_state() == "recording")
    assert capture.stop_capture(handle) == 1
    assert handle.current_state() == "failed"


def test_an_odj_audio_that_ignores_stdin_is_killed(tmp_path: Path, stub: Any):
    """[if] closing stdin does not end it [then] stop still returns, by kill."""
    handle = capture.start_capture(tmp_path / "s", 1, backend=stub("deaf"), startup_check_s=0.3)
    assert _wait_for(lambda: handle.current_state() == "recording")
    assert capture.stop_capture(handle, timeout=0.2) != 0
    assert handle.proc.poll() is not None
    assert handle.log_fh.closed


def test_microphone_access_turned_off_refuses_rec_with_the_reason(tmp_path: Path, stub: Any):
    """[if] odj-audio refuses at start (access off) [then] REC fails with its message, pipes closed."""
    with pytest.raises(capture.CaptureUnavailable) as exc:
        capture.start_capture(tmp_path / "s", 1, backend=stub("denied"), startup_check_s=1.0)
    assert "odj-audio stopped" in str(exc.value) and "microphone access" in str(exc.value)


class _FfmpegPopen:
    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.argv = argv
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def test_a_pinned_ffmpeg_still_records_mp3_through_avfoundation(tmp_path: Path):
    """Control: the ffmpeg backend is unchanged when it is the one picked."""
    handle = capture.start_capture(
        tmp_path, 2, popen=cast(Any, _FfmpegPopen), backend=capture.CaptureBackend("ffmpeg", "/usr/bin/ffmpeg")
    )
    assert handle.backend == "ffmpeg"
    assert handle.current_state() == "recording"
    assert handle.argv[0] == "/usr/bin/ffmpeg" and handle.argv[handle.argv.index("-i") + 1] == ":2"
    assert handle.argv[-1].endswith(".mp3")
    handle.log_fh.close()


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
