"""ffmpeg AVFoundation capture wrapper (subprocess-only).

Plan 12-01 Step 2 + CONTEXT D1. The recorder never imports an audio
library; it shells out to ffmpeg and reads its MP3 segments off disk.

Public API:

  * :func:`detect_input_device(name)` -- parse ``ffmpeg -f avfoundation
    -list_devices true -i ""`` and return the numeric index of a named
    audio device (usually ``"BlackHole 2ch"``) or ``None`` if absent.
  * :func:`build_segment_argv(device_idx, output_dir, ...)` -- compose
    the rolling-segment command without running it (pure unit-testable).
  * :func:`start_capture(...)` -- spawn the subprocess.
  * :func:`stop_capture(proc, timeout)` -- graceful SIGTERM + SIGKILL
    fallback; ffmpeg's segmenter flushes the active segment on SIGTERM.
  * :func:`check_silence(mp3_path)` -- mean dB via ``ffmpeg -af
    volumedetect``; returns ``-inf`` for empty files, used to warn when
    BlackHole isn't being routed through.

No ffprobe; duration is recovered later from filenames + manifest.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The default name of the MIT-licensed BlackHole virtual device.
DEFAULT_DEVICE_NAME = "BlackHole 2ch"

# 5-minute rolling segments at 320 kbps. CONTEXT D1.
DEFAULT_SEGMENT_TIME_S = 300
DEFAULT_BITRATE_KBPS = 320

# ``-strftime 1`` so ffmpeg can interpolate timestamps into segment names.
_SEGMENT_NAME_PATTERN = "audio_%Y-%m-%dT%H-%M-%S.mp3"


@dataclass(frozen=True)
class CaptureHandle:
    """Live handle returned by :func:`start_capture`.

    Stores the ``Popen`` plus the ffmpeg argv (so tests can assert on
    it), the resolved stderr log path, and the open log file handle so
    it can be closed in :func:`stop_capture`.
    """

    proc: subprocess.Popen
    argv: list[str]
    stderr_log: Path
    log_fh: "IO[bytes]"


# ---------------------------------------------------------------------------
# device detection
# ---------------------------------------------------------------------------


def _list_devices_argv() -> list[str]:
    return [
        "ffmpeg",
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
) -> list[str]:
    """Compose the ffmpeg argv for rolling-MP3 capture.

    Output files land as ``<output_dir>/audio_YYYY-MM-DDTHH-MM-SS.mp3``.
    ``segment_time_s`` is ffmpeg's ``-segment_time``.
    """
    out_pattern = str(output_dir / _SEGMENT_NAME_PATTERN)
    return [
        "ffmpeg",
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
) -> CaptureHandle:
    """Spawn ffmpeg; return a :class:`CaptureHandle`.

    ``popen`` lets tests inject a fake Popen class. The subprocess is
    non-blocking; stderr is redirected to
    ``<session_dir>/ffmpeg.stderr.log`` for later post-mortem.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    argv = build_segment_argv(
        device_idx,
        session_dir,
        segment_time_s=segment_time_s,
        bitrate_kbps=bitrate_kbps,
    )
    stderr_log = session_dir / "ffmpeg.stderr.log"
    popen_cls = popen if popen is not None else subprocess.Popen
    # Open the log fresh each start; stderr_log cleanup is retention's job.
    log_fh = stderr_log.open("ab", buffering=0)
    try:
        proc = popen_cls(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    except Exception:
        log_fh.close()
        raise
    return CaptureHandle(proc=proc, argv=argv, stderr_log=stderr_log, log_fh=log_fh)


def stop_capture(handle: CaptureHandle, *, timeout: float = 10.0) -> int:
    """Graceful stop: SIGTERM, wait, SIGKILL on timeout.

    Returns the subprocess return code. After SIGTERM, ffmpeg's
    segmenter flushes the active segment's muxer so the trailing MP3
    file is not corrupt.
    """
    proc = handle.proc
    try:
        if proc.poll() is not None:
            return int(proc.returncode)
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
    "DEFAULT_DEVICE_NAME",
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
