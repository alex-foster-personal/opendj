"""Tests for :mod:`apps.sets.capture`."""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apps.sets import capture

SAMPLE_STDERR = """\
[AVFoundation indev @ 0x600000000000] AVFoundation video devices:
[AVFoundation indev @ 0x600000000000] [0] FaceTime HD Camera
[AVFoundation indev @ 0x600000000000] [1] Capture screen 0
[AVFoundation indev @ 0x600000000000] AVFoundation audio devices:
[AVFoundation indev @ 0x600000000000] [0] MacBook Pro Microphone
[AVFoundation indev @ 0x600000000000] [1] BlackHole 2ch
"""


@pytest.mark.requirement("SET-01")
def test_parse_audio_devices_returns_only_audio_block():
    devices = capture.parse_audio_devices(SAMPLE_STDERR)
    assert devices == [
        (0, "MacBook Pro Microphone"),
        (1, "BlackHole 2ch"),
    ]


@pytest.mark.requirement("SET-01")
def test_parse_audio_devices_handles_empty_stderr():
    assert capture.parse_audio_devices("") == []


@pytest.mark.requirement("SET-01")
def test_detect_input_device_finds_named_device(monkeypatch):
    fake_result = MagicMock(stderr=SAMPLE_STDERR)
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: fake_result)
    idx = capture.detect_input_device("BlackHole 2ch")
    assert idx == 1


@pytest.mark.requirement("SET-01")
def test_detect_input_device_returns_none_when_missing(monkeypatch):
    fake_result = MagicMock(stderr="")
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: fake_result)
    assert capture.detect_input_device("BlackHole 2ch") is None


@pytest.mark.requirement("SET-01")
def test_detect_input_device_handles_missing_ffmpeg(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(capture.subprocess, "run", _raise)
    assert capture.detect_input_device() is None


@pytest.mark.requirement("SET-01")
def test_build_segment_argv_composes_correct_flags(tmp_path: Path):
    argv = capture.build_segment_argv(
        device_idx=1,
        output_dir=tmp_path,
        segment_time_s=300,
        bitrate_kbps=320,
    )
    assert argv[0] == "ffmpeg"
    assert "-f" in argv and "avfoundation" in argv
    assert "-i" in argv
    assert ":1" in argv  # device spec
    assert "-c:a" in argv and "libmp3lame" in argv
    assert "-b:a" in argv and "320k" in argv
    assert "-segment_time" in argv and "300" in argv
    assert "-strftime" in argv and "1" in argv
    # The output pattern must land inside session dir.
    out_arg = argv[-1]
    assert out_arg.endswith("audio_%Y-%m-%dT%H-%M-%S.mp3")
    assert str(tmp_path) in out_arg


class _FakePopen:
    """Minimal stand-in: records argv + stores a pollable return code."""

    instances: list["_FakePopen"] = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode: int | None = None
        self.signals: list[int] = []
        _FakePopen.instances.append(self)

    def poll(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0  # graceful exit simulated

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.requirement("SET-01")
def test_start_capture_spawns_subprocess_with_argv(tmp_path: Path):
    _FakePopen.instances.clear()
    handle = capture.start_capture(tmp_path, 1, popen=_FakePopen, ffmpeg="ffmpeg")
    assert isinstance(handle, capture.CaptureHandle)
    assert handle.argv[0] == "ffmpeg"
    assert handle.stderr_log == tmp_path / "ffmpeg.stderr.log"
    assert handle.stderr_log.exists()
    assert _FakePopen.instances and _FakePopen.instances[0].argv == handle.argv


@pytest.mark.requirement("SET-01")
def test_stop_capture_sends_sigterm(tmp_path: Path):
    _FakePopen.instances.clear()
    handle = capture.start_capture(tmp_path, 1, popen=_FakePopen, ffmpeg="ffmpeg")
    rc = capture.stop_capture(handle)
    assert rc == 0
    assert signal.SIGTERM in _FakePopen.instances[0].signals


@pytest.mark.requirement("SET-01")
def test_stop_capture_kills_on_timeout(tmp_path: Path, monkeypatch):
    """If SIGTERM times out, stop_capture falls back to kill."""
    _FakePopen.instances.clear()

    class _StubbornPopen(_FakePopen):
        def send_signal(self, sig):  # don't change returncode
            self.signals.append(sig)

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            return self.returncode

    handle = capture.start_capture(tmp_path, 1, popen=_StubbornPopen, ffmpeg="ffmpeg")
    rc = capture.stop_capture(handle, timeout=0.01)
    assert rc == -9  # kill path
    assert signal.SIGTERM in handle.proc.signals


@pytest.mark.requirement("SET-01")
def test_start_stop_closes_log_file_handle(tmp_path: Path):
    """If stop_capture does not close log_fh, repeated cycles leak fds."""
    _FakePopen.instances.clear()
    handle = capture.start_capture(tmp_path, 1, popen=_FakePopen, ffmpeg="ffmpeg")
    assert hasattr(handle, "log_fh"), "CaptureHandle must store log_fh"
    assert not handle.log_fh.closed, "log_fh should be open while capturing"
    capture.stop_capture(handle)
    assert handle.log_fh.closed, "stop_capture must close log_fh"


@pytest.mark.requirement("SET-01")
def test_stop_capture_closes_log_fh_even_on_kill_path(tmp_path: Path):
    """log_fh must close even when the process requires SIGKILL."""
    _FakePopen.instances.clear()

    class _StubbornPopen2(_FakePopen):
        def send_signal(self, sig):
            self.signals.append(sig)

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
            return self.returncode

    handle = capture.start_capture(tmp_path, 1, popen=_StubbornPopen2, ffmpeg="ffmpeg")
    capture.stop_capture(handle, timeout=0.01)
    assert handle.log_fh.closed, "log_fh must close even after SIGKILL path"


@pytest.mark.requirement("SET-01")
def test_start_capture_closes_log_fh_on_popen_failure(tmp_path: Path):
    """If Popen raises, the stderr log fd must be closed -- not leaked.

    if start_capture does not close log_fh on Popen failure then broken
    """
    captured_fh = {}

    original_open = Path.open

    def _spy_open(self, *args, **kwargs):
        fh = original_open(self, *args, **kwargs)
        if self.name == "ffmpeg.stderr.log":
            captured_fh["fh"] = fh
        return fh

    class _ExplodingPopen:
        def __init__(self, *args, **kwargs):
            raise OSError("simulated Popen failure")

    import unittest.mock as _um

    with _um.patch.object(Path, "open", _spy_open):
        with pytest.raises(OSError, match="simulated Popen failure"):
            capture.start_capture(tmp_path, 1, popen=_ExplodingPopen, ffmpeg="ffmpeg")

    assert "fh" in captured_fh, "log file handle was never opened"
    assert captured_fh["fh"].closed, "log_fh leaked -- not closed after Popen failure"


@pytest.mark.requirement("SET-01")
def test_check_silence_returns_neg_inf_for_empty_file(tmp_path: Path):
    mp3 = tmp_path / "empty.mp3"
    mp3.touch()
    assert capture.check_silence(mp3) == float("-inf")


@pytest.mark.requirement("SET-01")
def test_check_silence_parses_mean_volume(tmp_path: Path, monkeypatch):
    mp3 = tmp_path / "seg.mp3"
    mp3.write_bytes(b"\x00" * 1024)
    stderr = (
        "[Parsed_volumedetect_0 @ 0x0] n_samples: 44100\n"
        "[Parsed_volumedetect_0 @ 0x0] mean_volume: -23.4 dB\n"
        "[Parsed_volumedetect_0 @ 0x0] max_volume: -0.1 dB\n"
    )
    fake = MagicMock(stderr=stderr)
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: fake)
    assert capture.check_silence(mp3) == -23.4
