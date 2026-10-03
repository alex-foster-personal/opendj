"""Set audio capture (subprocess-only): odj-audio, else ffmpeg.

Plan 12-01 Step 2 + CONTEXT D1. The recorder never imports an audio
library; it runs a capture process and reads its segments off disk.

Two backends, chosen by :func:`capture_backend`:

  * ``odj-audio`` (the Rust engine, built with feature ``device``): records
    16-bit PCM WAV segments through CoreAudio/WASAPI/ALSA. The installed app
    bundles it and no ffmpeg (decision #4766), so this is how REC records
    there (``docs/decisions/*-set-recording-without-ffmpeg.md``).
  * ``ffmpeg`` (AVFoundation, macOS only): MP3 segments. Used only when no
    capture-capable odj-audio is found, i.e. a checkout without a
    ``--features device`` build.

Public API:

  * :func:`detect_input_device(name)` -- parse ``ffmpeg -f avfoundation
    -list_devices true -i ""`` and return the numeric index of a named
    audio device (usually ``"BlackHole 2ch"``) or ``None`` if absent.
  * :func:`list_input_devices()` -- the same listing as named
    :class:`InputDevice` rows for the REC input picker (SET-10); raises
    :class:`CaptureUnavailable` rather than returning an empty list when
    it could not measure.
  * :func:`build_segment_argv(device_idx, output_dir, ...)` -- compose
    the rolling-segment command without running it (pure unit-testable).
  * :func:`start_capture(...)` -- spawn the subprocess.
  * :func:`stop_capture(proc, timeout)` -- graceful stop (odj-audio: close
    its stdin; ffmpeg: SIGTERM), then SIGKILL; both close the active segment.
  * :func:`check_silence(mp3_path)` -- mean dB via ``ffmpeg -af
    volumedetect``; returns ``-inf`` for empty files, used to warn when
    BlackHole isn't being routed through.

No ffprobe; duration is recovered later from filenames + manifest.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from apps.shared.ffmpeg import FfmpegUnavailable, resolve_ffmpeg_including_homebrew
from apps.shared.odj_audio_binary import OdjAudioUnavailable, find_binary

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# The default name of the MIT-licensed BlackHole virtual device.
DEFAULT_DEVICE_NAME = "BlackHole 2ch"

# 5-minute rolling segments at 320 kbps. CONTEXT D1.
DEFAULT_SEGMENT_TIME_S = 300
DEFAULT_BITRATE_KBPS = 320

# Name fragments of virtual loopback inputs. A loopback carries the master
# output back in as an input, so it is the right default for a set recording;
# a microphone would record the room. Matched case-insensitively.
LOOPBACK_NAME_HINTS: tuple[str, ...] = ("blackhole", "loopback", "soundflower")

# How long a freshly spawned capture must stay alive before REC reports it
# started. ffmpeg exits within this window for a vanished device or a refused
# microphone permission; without the wait those read as a recording.
CAPTURE_STARTUP_CHECK_S = 1.0
LIST_DEVICES_TIMEOUT_S = 10.0
VERSION_PROBE_TIMEOUT_S = 10.0

# ``-strftime 1`` so ffmpeg can interpolate timestamps into segment names.
_SEGMENT_NAME_PATTERN = "audio_%Y-%m-%dT%H-%M-%S.mp3"


@dataclass(frozen=True)
class CaptureHandle:
    """Live handle returned by :func:`start_capture`.

    Stores the ``Popen`` plus the capture argv (so tests can assert on
    it), the resolved stderr log path, the open log file handle so it
    can be closed in :func:`stop_capture`, and which backend runs it.
    """

    proc: subprocess.Popen
    argv: list[str]
    stderr_log: Path
    log_fh: IO[bytes]
    backend: Literal["odj-audio", "ffmpeg"] = "ffmpeg"


class CaptureUnavailable(RuntimeError):
    """Audio capture cannot be measured or started here. Never an empty result."""


@dataclass(frozen=True)
class CaptureBackend:
    """The program that records: ``kind`` and the executable to run."""

    kind: Literal["odj-audio", "ffmpeg"]
    exe: str


@dataclass(frozen=True)
class InputDevice:
    """One audio input, as the REC picker shows it, in its backend's order."""

    index: int
    name: str
    loopback: bool


# ---------------------------------------------------------------------------
# device detection
# ---------------------------------------------------------------------------


def resolve_capture_ffmpeg() -> str:
    """ffmpeg for capture: ``MDT_FFMPEG``, then PATH, then Homebrew's prefixes.

    The Homebrew step is what lets REC work in the packaged app, which is
    launched without a shell PATH (apps.shared.ffmpeg owns the lookup).
    """
    try:
        return resolve_ffmpeg_including_homebrew()
    except FfmpegUnavailable as exc:
        raise CaptureUnavailable(f"ffmpeg is needed to record set audio: {exc}") from exc


def _odj_audio_capture(
    environ: dict[str, str] | None,
    repo_root: Path | None,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[CaptureBackend | None, str]:
    """A capture-capable odj-audio, or None and why not.

    An ``ODJ_AUDIO_BIN`` (the installed app) that cannot capture raises
    :class:`CaptureUnavailable` instead: the app must never quietly record
    through some other ffmpeg than the one it does not ship.
    """
    try:
        binary = find_binary(os.environ if environ is None else environ, repo_root or REPO_ROOT)
    except OdjAudioUnavailable as exc:
        if (os.environ if environ is None else environ).get("ODJ_AUDIO_BIN", "").strip():
            raise CaptureUnavailable(f"cannot record set audio: {exc}") from exc
        return None, str(exc)
    reason = ""
    try:
        result = run(
            [str(binary.path), "version"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=VERSION_PROBE_TIMEOUT_S,
        )
        version = json.loads(result.stdout) if result.returncode == 0 else None
        if isinstance(version, dict) and version.get("capture") is True:
            return CaptureBackend("odj-audio", str(binary.path)), ""
        reason = (
            f"{binary.path} was built without audio input (feature device)"
            if isinstance(version, dict)
            else f"{binary.path} version exited {result.returncode}: {(result.stderr or '').strip()[-200:]}"
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        reason = f"{binary.path} version failed: {exc}"
    if binary.source == "env":
        raise CaptureUnavailable(f"cannot record set audio: {reason}")
    return None, reason


def capture_backend(
    *,
    environ: dict[str, str] | None = None,
    repo_root: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    resolve_ffmpeg: Callable[[], str] | None = None,
) -> CaptureBackend:
    """odj-audio when it can capture, else ffmpeg, else :class:`CaptureUnavailable`.

    The error names why each was refused, so "REC cannot record" always
    says what to install or rebuild.
    """
    odj, why_not = _odj_audio_capture(environ, repo_root, run if run is not None else subprocess.run)
    if odj is not None:
        return odj
    try:
        return CaptureBackend("ffmpeg", (resolve_ffmpeg or resolve_capture_ffmpeg)())
    except CaptureUnavailable as exc:
        raise CaptureUnavailable(f"{exc}; and odj-audio cannot record either: {why_not}") from exc


def is_loopback_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in LOOPBACK_NAME_HINTS)


def list_input_devices(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    platform: str | None = None,
    ffmpeg: str | None = None,
    backend: CaptureBackend | None = None,
) -> list[InputDevice]:
    """Every audio input, in the capture backend's index order.

    ``ffmpeg`` pins the ffmpeg backend; otherwise :func:`capture_backend`
    picks. Raises :class:`CaptureUnavailable` when nothing can capture, or
    when the listing cannot be read: an unparsable listing is a failed
    measurement, not a machine with no inputs.
    """
    runner = run if run is not None else subprocess.run
    if ffmpeg is None:
        chosen = backend if backend is not None else capture_backend(run=runner)
        if chosen.kind == "odj-audio":
            return _odj_audio_input_devices(chosen.exe, runner)
        ffmpeg = chosen.exe
    return _ffmpeg_input_devices(ffmpeg, runner, platform)


def _odj_audio_input_devices(
    exe: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[InputDevice]:
    try:
        result = runner(
            [exe, "input-devices"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LIST_DEVICES_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureUnavailable(f"listing audio inputs with {exe} failed: {exc}") from exc
    try:
        listed = json.loads(result.stdout) if result.returncode == 0 else None
        rows = listed["devices"] if isinstance(listed, dict) else None
        if not isinstance(rows, list):
            raise ValueError("no devices list")
        return [
            InputDevice(index=int(row["index"]), name=str(row["name"]), loopback=is_loopback_name(str(row["name"])))
            for row in rows
        ]
    except (ValueError, KeyError, TypeError) as exc:
        tail = (result.stderr or "").strip()[-300:] or "no stderr"
        raise CaptureUnavailable(
            f"{exe} input-devices exited {result.returncode} without a device list ({exc}): {tail}"
        ) from exc


def _ffmpeg_input_devices(
    exe: str,
    runner: Callable[..., subprocess.CompletedProcess[str]],
    platform: str | None,
) -> list[InputDevice]:
    host = platform if platform is not None else sys.platform
    if host != "darwin":
        raise CaptureUnavailable(
            f"set audio capture through ffmpeg uses macOS AVFoundation; this host is {host!r}"
        )
    argv = _list_devices_argv(exe)
    try:
        result = runner(
            argv,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=LIST_DEVICES_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureUnavailable(f"listing audio inputs with {exe} failed: {exc}") from exc
    stderr = result.stderr or ""
    if "AVFoundation audio devices" not in stderr:
        tail = " | ".join(stderr.strip().splitlines()[-3:]) or "no output"
        raise CaptureUnavailable(f"{exe} did not list any AVFoundation audio devices: {tail}")
    return [
        InputDevice(index=idx, name=name, loopback=is_loopback_name(name))
        for idx, name in parse_audio_devices(stderr)
    ]


def default_input_device(devices: list[InputDevice]) -> InputDevice | None:
    """The input REC preselects: BlackHole 2ch, else any loopback, else none.

    No loopback means no default on purpose: picking the microphone for the
    DJ would record the room and call it the set.
    """
    for device in devices:
        if device.name == DEFAULT_DEVICE_NAME:
            return device
    return next((device for device in devices if device.loopback), None)


def _list_devices_argv(ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-f",
        "avfoundation",
        "-list_devices",
        "true",
        "-i",
        "",
    ]


_DEVICE_LINE = re.compile(r"\[AVFoundation[^\]]*\] \[(?P<idx>\d+)\] (?P<name>.+)$")


def parse_audio_devices(stderr_text: str) -> list[tuple[int, str]]:
    """Given ffmpeg stderr from ``-list_devices true``, return audio devices.

    The output format on macOS::

        [AVFoundation indev @ ...] AVFoundation video devices:
        [AVFoundation indev @ ...] [0] FaceTime HD Camera
        [AVFoundation indev @ ...] AVFoundation audio devices:
        [AVFoundation indev @ ...] [0] MacBook Pro Microphone
        [AVFoundation indev @ ...] [1] BlackHole 2ch
    """
    in_audio = False
    out: list[tuple[int, str]] = []
    for line in stderr_text.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        m = _DEVICE_LINE.search(line)
        if not m:
            continue
        out.append((int(m.group("idx")), m.group("name").strip()))
    return out


def detect_input_device(
    name: str = DEFAULT_DEVICE_NAME,
    *,
    _runner: "subprocess._Popen | None" = None,
) -> int | None:
    """Return the numeric index of the named audio device or ``None``.

    ``runner`` lets tests inject a fake subprocess module. When
    ``None`` we invoke ``ffmpeg``; absence of ``ffmpeg`` on the PATH
    returns ``None`` (surfaces as a friendlier error in the CLI).
    """
    try:
        result = subprocess.run(
            _list_devices_argv(),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    devices = parse_audio_devices(result.stderr or "")
    for idx, dev_name in devices:
        if dev_name == name:
            return idx
    return None


# ---------------------------------------------------------------------------
# argv composition
# ---------------------------------------------------------------------------


def build_segment_argv(
    device_idx: int,
    output_dir: Path,
    *,
    segment_time_s: int = DEFAULT_SEGMENT_TIME_S,
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Compose the ffmpeg argv for rolling-MP3 capture.

    Output files land as ``<output_dir>/audio_YYYY-MM-DDTHH-MM-SS.mp3``.
    ``segment_time_s`` is ffmpeg's ``-segment_time``.
    """
    out_pattern = str(output_dir / _SEGMENT_NAME_PATTERN)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "avfoundation",
        "-i",
        f":{device_idx}",
        "-c:a",
        "libmp3lame",
        "-b:a",
        f"{bitrate_kbps}k",
        "-f",
        "segment",
        "-segment_time",
        str(segment_time_s),
        "-strftime",
        "1",
        "-reset_timestamps",
        "1",
        out_pattern,
    ]


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


def start_capture(
    session_dir: Path,
    device_idx: int,
    *,
    segment_time_s: int = DEFAULT_SEGMENT_TIME_S,
    bitrate_kbps: int = DEFAULT_BITRATE_KBPS,
    popen: "type[subprocess.Popen] | None" = None,
    ffmpeg: str | None = None,
    startup_check_s: float = 0.0,
    backend: CaptureBackend | None = None,
) -> CaptureHandle:
    """Spawn the capture process; return a :class:`CaptureHandle`.

    ``ffmpeg`` pins the ffmpeg backend; otherwise :func:`capture_backend`
    picks. ``popen`` lets tests inject a fake Popen class. The subprocess
    is non-blocking; its output goes to ``<session_dir>/<backend>.stderr.log``
    for later post-mortem.

    With ``startup_check_s`` > 0 the process must still be running after
    that long, else :class:`CaptureUnavailable` carries its stderr tail.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    chosen = (
        CaptureBackend("ffmpeg", ffmpeg)
        if ffmpeg is not None
        else backend if backend is not None else capture_backend()
    )
    popen_cls = popen if popen is not None else subprocess.Popen
    if chosen.kind == "odj-audio":
        return _start_odj_audio(chosen.exe, session_dir, device_idx, segment_time_s, popen_cls, startup_check_s)
    argv = build_segment_argv(
        device_idx,
        session_dir,
        segment_time_s=segment_time_s,
        bitrate_kbps=bitrate_kbps,
        ffmpeg=chosen.exe,
    )
    stderr_log = session_dir / "ffmpeg.stderr.log"
    # Open the log fresh each start; stderr_log cleanup is retention's job.
    log_fh = stderr_log.open("ab", buffering=0)
    try:
        proc = popen_cls(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            # -strftime stamps segment names in the process's local time, but
            # the set id and _segment_start_from_name are UTC; a local stamp
            # shifts every segment by the UTC offset.
            env={**os.environ, "TZ": "UTC"},
        )
    except Exception:
        log_fh.close()
        raise
    handle = CaptureHandle(proc=proc, argv=argv, stderr_log=stderr_log, log_fh=log_fh)
    if startup_check_s > 0:
        _require_running(handle, startup_check_s)
    return handle


def build_record_argv(exe: str, device_idx: int, output_dir: Path, *, segment_time_s: int) -> list[str]:
    """The odj-audio argv for rolling WAV capture of input ``device_idx``.

    The index is a place in ``odj-audio input-devices``, the listing
    :func:`list_input_devices` returns on this backend.
    """
    return [
        exe,
        "record",
        "--dir",
        str(output_dir),
        "--device-index",
        str(device_idx),
        "--segment-seconds",
        str(segment_time_s),
    ]


def _start_odj_audio(
    exe: str,
    session_dir: Path,
    device_idx: int,
    segment_time_s: int,
    popen_cls: "type[subprocess.Popen]",
    startup_check_s: float,
) -> CaptureHandle:
    argv = build_record_argv(exe, device_idx, session_dir, segment_time_s=segment_time_s)
    stderr_log = session_dir / "odj-audio.stderr.log"
    log_fh = stderr_log.open("ab", buffering=0)
    try:
        # stdin is the stop signal: closing it (or this process dying) ends
        # the recording and closes the last segment with its final sizes.
        proc = popen_cls(argv, stdin=subprocess.PIPE, stdout=log_fh, stderr=log_fh)
    except Exception:
        log_fh.close()
        raise
    handle = CaptureHandle(proc=proc, argv=argv, stderr_log=stderr_log, log_fh=log_fh, backend="odj-audio")
    if startup_check_s > 0:
        _require_running(handle, startup_check_s)
    return handle


def _require_running(handle: CaptureHandle, window_s: float) -> None:
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if handle.proc.poll() is not None:
            break
        time.sleep(0.05)
    code = handle.proc.poll()
    if code is None:
        return
    handle.log_fh.close()
    try:
        tail = " | ".join(handle.stderr_log.read_text(errors="replace").strip().splitlines()[-3:])
    except OSError:
        tail = ""
    raise CaptureUnavailable(
        f"{handle.backend} stopped {window_s:g}s after starting (exit {code}): {tail or 'no stderr'}"
    )


def stop_capture(handle: CaptureHandle, *, timeout: float = 10.0) -> int:
    """Graceful stop, then SIGKILL on timeout.

    Returns the subprocess return code. odj-audio stops when its stdin
    closes and closes the open WAV segment; ffmpeg gets SIGTERM, after
    which its segmenter flushes the active segment's muxer so the
    trailing MP3 file is not corrupt.
    """
    proc = handle.proc
    try:
        if proc.poll() is not None:
            return int(proc.returncode)
        if handle.backend == "odj-audio":
            return _stop_odj_audio(proc, timeout)
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return int(proc.returncode or 0)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        return int(proc.returncode or 0)
    finally:
        if handle.log_fh and not handle.log_fh.closed:
            handle.log_fh.close()


def _stop_odj_audio(proc: subprocess.Popen, timeout: float) -> int:
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass  # already gone: wait() below reads its exit
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Its header is rewritten every second, so even this leaves a WAV
        # that plays up to the last second written.
        proc.kill()
        proc.wait(timeout=2.0)
    return int(proc.returncode or 0)


# ---------------------------------------------------------------------------
# silence check
# ---------------------------------------------------------------------------


_MEAN_VOL = re.compile(r"mean_volume:\s*(-?\d+\.?\d*) dB")


def check_silence(mp3_path: Path) -> float:
    """Return the mean volume in dB for ``mp3_path``.

    Uses ``ffmpeg -af volumedetect``; emits ``-inf`` for a zero-size
    or unreadable file (the caller interprets that as "BlackHole is
    installed but nothing is routed through it yet"). If ``ffmpeg``
    is missing on the host, returns ``-inf`` (same signal).
    """
    path = Path(mp3_path)
    if not path.exists() or path.stat().st_size == 0:
        return float("-inf")
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        "volumedetect",
        "-f",
        "null",
        os.devnull,
    ]
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return float("-inf")
    m = _MEAN_VOL.search(result.stderr or "")
    if not m:
        return float("-inf")
    return float(m.group(1))


__all__ = [
    "CAPTURE_STARTUP_CHECK_S",
    "CaptureBackend",
    "CaptureUnavailable",
    "build_record_argv",
    "capture_backend",
    "DEFAULT_DEVICE_NAME",
    "InputDevice",
    "default_input_device",
    "list_input_devices",
    "resolve_capture_ffmpeg",
    "DEFAULT_SEGMENT_TIME_S",
    "DEFAULT_BITRATE_KBPS",
    "CaptureHandle",
    "parse_audio_devices",
    "detect_input_device",
    "build_segment_argv",
    "start_capture",
    "stop_capture",
    "check_silence",
]
