"""Push the master mix to RTMP through ffmpeg (STREAM-11743).

Mini-PRD
--------
R1 ok   Build the ffmpeg argv for FLV over RTMP: master PCM on stdin, still image
        or black lavfi video, libx264 stillimage tune, AAC at configured kbps.
R2 ok   Resolve an RTMP-capable ffmpeg: ``MDT_FFMPEG`` alone when set, else PATH,
        Homebrew, then the bundled ``ODJ_FFMPEG_BIN``, each probed with
        ``-protocols/-muxers/-encoders`` for the rtmp protocol, flv, libx264 and
        aac. The bundled LGPL build has no network or flv, so it is skipped with a
        reason. None capable is ``STREAM_ENCODER_UNAVAILABLE`` naming every place
        looked and what each lacks, before any push process is spawned.
R3 ok   The stream key never appears in status, logs or errors; argv is logged
        with ``<key>``.
R4 ok   A tee on the master-mix writer feeds PCM; stop closes stdin and ffmpeg
        exits via ``-shortest``.
R5 ok   Self-exit sets ``last_error`` with stderr and the key redacted.

Acceptance
----------
[if] image path exists [then] argv matches the documented still-image line exactly
[if] image is null [then] argv uses lavfi black and omits -vf
[if] ffmpeg dies alone [then] state exited and last_error has no raw key
[if] stop is called [then] stdin closes and state becomes stopped
[if] only the bundled LGPL ffmpeg exists [then] STREAM_ENCODER_UNAVAILABLE names ODJ_FFMPEG_BIN and what it lacks, nothing spawned

Documented ffmpeg line (with image)
-----------------------------------
Inputs: ``exe``, ``rate``, ``W``x``H``, ``kbps``, ``target = url + "/" + key``.

With an image::

    [exe, "-hide_banner", "-nostats",
     "-f", "s16le", "-ar", str(rate), "-ac", "2", "-i", "pipe:0",
     "-loop", "1", "-framerate", "2", "-i", image_path,
     "-map", "1:v", "-map", "0:a",
     "-vf", f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
     "-c:v", "libx264", "-tune", "stillimage", "-r", "2", "-g", "4", "-pix_fmt", "yuv420p",
     "-c:a", "aac", "-b:a", f"{kbps}k",
     "-shortest",
     "-f", "flv", target]

With no image the second input is ``"-f", "lavfi", "-i", f"color=black:s={W}x{H}:r=2"``
and the ``-vf`` pair is dropped.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Literal

from apps.sets import master_mix
from apps.shared import ffmpeg as ffmpeg_mod
from apps.stream.config import RtmpConfig, RtmpConfigRefused, redact
from apps.stream.encoder import (
    QUEUE_MAX_CHUNKS,
    STDERR_TAIL_BYTES,
    STOP_GRACE_S,
    EncoderUnavailable,
)

_log = logging.getLogger(__name__)
RTMP_AF_SERVICE_ID = "com.opendj.stream-rtmp"
CAPABILITY_PROBE_TIMEOUT_S = 10.0
RTMP_MUXER = "flv"
RTMP_ENCODERS = ("libx264", "aac")

RtmpState = Literal["idle", "running", "stopped", "exited", "failed"]


def _is_executable(path: str) -> bool:
    return Path(path).is_file() and os.access(path, os.X_OK)


def _listed_names(listing: str) -> set[str]:
    """Names from ffmpeg -muxers / -encoders: second column after the -- rule line."""
    names: set[str] = set()
    past_rule = False
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not past_rule and stripped and all(c == "-" for c in stripped):
            past_rule = True
            continue
        if not past_rule:
            continue
        tokens = stripped.split()
        if len(tokens) >= 2:
            for part in tokens[1].split(","):
                names.add(part)
    return names


def _output_protocols(listing: str) -> set[str]:
    """Names under Output: in ffmpeg -protocols."""
    names: set[str] = set()
    in_output = False
    for line in listing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Output:":
            in_output = True
            continue
        if in_output and stripped.endswith(":"):
            break
        if in_output:
            for token in stripped.split():
                names.add(token)
    return names


def missing_rtmp_capabilities(protocols: str, muxers: str, encoders: str, *, scheme: str) -> list[str]:
    missing: list[str] = []
    if scheme not in _output_protocols(protocols):
        missing.append(f"{scheme} output protocol")
    if RTMP_MUXER not in _listed_names(muxers):
        missing.append("flv muxer")
    enc = _listed_names(encoders)
    if "libx264" not in enc:
        missing.append("libx264 encoder")
    if "aac" not in enc:
        missing.append("aac encoder")
    return missing


def probe_rtmp_capabilities(executable: str, *, scheme: str) -> list[str]:
    listings: list[str] = []
    for flag in ("-protocols", "-muxers", "-encoders"):
        try:
            proc = subprocess.run(
                [executable, "-hide_banner", flag],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CAPABILITY_PROBE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [f"could not be probed ({type(exc).__name__})"]
        if proc.returncode != 0:
            return [f"could not be probed (exit {proc.returncode})"]
        listings.append(proc.stdout)
    return missing_rtmp_capabilities(listings[0], listings[1], listings[2], scheme=scheme)


def resolve_rtmp_encoder(*, scheme: str) -> str:
    """Return the first RTMP-capable ffmpeg path, or raise :class:`EncoderUnavailable`."""
    configured = os.environ.get(ffmpeg_mod.OVERRIDE_ENV)
    if configured is not None:
        resolved = os.path.abspath(configured)
        if not _is_executable(resolved):
            raise EncoderUnavailable(
                f"no RTMP-capable stream encoder (ffmpeg with the {scheme} output protocol, "
                f"flv muxer, libx264 and aac encoders) found: "
                f"{ffmpeg_mod.OVERRIDE_ENV}={configured!r} is not an executable file"
            )
        missing = probe_rtmp_capabilities(resolved, scheme=scheme)
        if missing:
            raise EncoderUnavailable(
                f"no RTMP-capable stream encoder (ffmpeg with the {scheme} output protocol, "
                f"flv muxer, libx264 and aac encoders) found: "
                f"{ffmpeg_mod.OVERRIDE_ENV}={configured!r} (lacks {', '.join(missing)})"
            )
        return resolved

    notes: list[str] = []
    path_hit = shutil.which(ffmpeg_mod.FFMPEG_BINARY)
    if path_hit is not None and _is_executable(path_hit):
        missing = probe_rtmp_capabilities(path_hit, scheme=scheme)
        if not missing:
            return os.path.abspath(path_hit)
        notes.append(f"on PATH at {path_hit} (found but lacks {', '.join(missing)})")
    else:
        notes.append("looked on PATH (not found)")

    for brew in ffmpeg_mod.HOMEBREW_FFMPEG_PATHS:
        if _is_executable(brew):
            missing = probe_rtmp_capabilities(brew, scheme=scheme)
            if not missing:
                return os.path.abspath(brew)
            notes.append(f"in {brew} (found but lacks {', '.join(missing)})")
        else:
            notes.append(f"in {brew} (not found)")

    bundled = os.environ.get(ffmpeg_mod.BUNDLED_ENV)
    if bundled is not None:
        bundled_abs = os.path.abspath(bundled)
        label = f"{ffmpeg_mod.BUNDLED_ENV}={bundled}"
        if _is_executable(bundled_abs):
            missing = probe_rtmp_capabilities(bundled_abs, scheme=scheme)
            if not missing:
                return bundled_abs
            lack = f"lacks {', '.join(missing)}: the bundled LGPL ffmpeg cannot push RTMP"
            notes.append(f"at {label} (found but {lack})")
        else:
            notes.append(f"at {label} (not an executable file)")

    raise EncoderUnavailable(
        f"no RTMP-capable stream encoder (ffmpeg with the {scheme} output protocol, "
        f"flv muxer, libx264 and aac encoders) found: {', '.join(notes)}; "
        f"install a full ffmpeg (brew install ffmpeg) or set {ffmpeg_mod.OVERRIDE_ENV}"
    )


def build_rtmp_command(executable: str, config: RtmpConfig, sample_rate: int) -> list[str]:
    if not master_mix.MIN_SAMPLE_RATE <= sample_rate <= master_mix.MAX_SAMPLE_RATE:
        raise RtmpConfigRefused(f"sample rate {sample_rate} is outside 8000..192000")
    w, h = config.width_height()
    kbps = config.audio_kbps
    target = config.target
    argv: list[str] = [
        executable,
        "-hide_banner",
        "-nostats",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(master_mix.CHANNELS),
        "-i",
        "pipe:0",
    ]
    if config.image is not None:
        argv.extend(
            [
                "-loop",
                "1",
                "-framerate",
                "2",
                "-i",
                str(config.image),
                "-map",
                "1:v",
                "-map",
                "0:a",
                "-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            ]
        )
    else:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=black:s={w}x{h}:r=2",
                "-map",
                "1:v",
                "-map",
                "0:a",
            ]
        )
    argv.extend(
        [
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
            f"{kbps}k",
            "-shortest",
            "-f",
            "flv",
            target,
        ]
    )
    return argv


def redacted_argv(argv: list[str], key: str) -> list[str]:
    return [redact(part, key) for part in argv]


@dataclass(frozen=True)
class RtmpStatus:
    sink: Literal["rtmp"]
    state: RtmpState
    target: str
    image: str | None
    video_size: str
    audio_kbps: int
    sample_rate: int
    pid: int | None
    chunks_in: int
    bytes_in: int
    queued_chunks: int
    dropped_chunks: int
    exit_code: int | None
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RtmpPush:
    """One ffmpeg subprocess pushing FLV to RTMP; fed by a master-mix tee."""

    def __init__(self, config: RtmpConfig, *, sample_rate: int) -> None:
        self._config = config
        self._key = config.key
        self.sample_rate = sample_rate
        self._cond = threading.Condition()
        self._queue: collections.deque[bytes] = collections.deque()
        self._proc: subprocess.Popen[bytes] | None = None
        self._state: RtmpState = "idle"
        self._stopping = False
        self._stderr_tail = bytearray()
        self._feeder: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._chunks_in = self._bytes_in = self._dropped = 0
        self._exit_code: int | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("already started")
        exe = resolve_rtmp_encoder(scheme=self._config.url.split("://", 1)[0])
        argv = build_rtmp_command(exe, self._config, self.sample_rate)
        _log.info("rtmp: starting %s", shlex.join(redacted_argv(argv, self._key)))
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                env={**os.environ, "AF_SERVICE_ID": RTMP_AF_SERVICE_ID},
            )
        except OSError as exc:
            raise RuntimeError(redact(str(exc), self._key)) from None
        self._state = "running"
        self._feeder = threading.Thread(target=self._feed, name="stream-rtmp-feed", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="stream-rtmp-stderr", daemon=True)
        self._watcher = threading.Thread(target=self._watch, name="stream-rtmp-watch", daemon=True)
        self._feeder.start()
        self._stderr_reader.start()
        self._watcher.start()

    def tee(self, pcm: bytes, sample_rate: int) -> None:
        with self._cond:
            if self._state != "running":
                return
            if sample_rate != self.sample_rate:
                self._state = "failed"
                self._last_error = redact(
                    f"master mix sample rate changed from {self.sample_rate} to {sample_rate}; restart the stream",
                    self._key,
                )
                self._stopping = True
                self._cond.notify_all()
                return
            if len(self._queue) >= QUEUE_MAX_CHUNKS:
                self._queue.popleft()
                self._dropped += 1
            self._queue.append(pcm)
            self._chunks_in += 1
            self._bytes_in += len(pcm)
            self._cond.notify_all()

    def _feed(self) -> None:
        stdin = self._pipe("stdin")
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if not self._queue:
                    break
                pcm = self._queue.popleft()
            try:
                stdin.write(pcm)
            except (BrokenPipeError, ValueError):
                return
        with contextlib.suppress(BrokenPipeError):
            stdin.close()

    def _read_stderr(self) -> None:
        stderr = self._pipe("stderr")
        while data := stderr.read(4096):
            with self._cond:
                self._stderr_tail += data
                del self._stderr_tail[:-STDERR_TAIL_BYTES]

    def _watch(self) -> None:
        proc = self._proc
        assert proc is not None
        code = proc.wait()
        if self._stderr_reader is not None:
            self._stderr_reader.join(1.0)
        with self._cond:
            if not self._stopping:
                self._state = "exited"
                self._exit_code = code
                tail = self._stderr_tail.decode(errors="replace")
                self._last_error = redact(f"ffmpeg exited with code {code}: {tail[-2000:]}", self._key)
                self._stopping = True
                self._cond.notify_all()
            elif self._exit_code is None:
                self._exit_code = code

    def _pipe(self, name: str) -> IO[bytes]:
        pipe = getattr(self._proc, name, None)
        if pipe is None:
            raise RuntimeError("the rtmp push has not been started")
        return pipe

    def stop(self) -> RtmpStatus:
        proc = self._proc
        if proc is None:
            return self.status()
        with self._cond:
            if self._stopping and self._state in ("exited", "stopped", "failed"):
                return self.status()
            self._stopping = True
            self._cond.notify_all()
        grace = STOP_GRACE_S
        deadline = time.monotonic() + grace
        if self._feeder is not None:
            self._feeder.join(grace)
        if proc.stdin is not None and not proc.stdin.closed:
            with contextlib.suppress(BrokenPipeError):
                proc.stdin.close()
        try:
            code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
        if self._watcher is not None:
            self._watcher.join(1.0)
        with self._cond:
            if self._state == "running":
                self._state = "stopped"
            if self._exit_code is None:
                self._exit_code = code
        return self.status()

    def status(self) -> RtmpStatus:
        with self._cond:
            image = None if self._config.image is None else redact(str(self._config.image), self._key)
            last_error = None if self._last_error is None else redact(self._last_error, self._key)
            return RtmpStatus(
                sink="rtmp",
                state=self._state,
                target=self._config.redacted_target,
                image=image,
                video_size=self._config.video_size,
                audio_kbps=self._config.audio_kbps,
                sample_rate=self.sample_rate,
                pid=None if self._proc is None else self._proc.pid,
                chunks_in=self._chunks_in,
                bytes_in=self._bytes_in,
                queued_chunks=len(self._queue),
                dropped_chunks=self._dropped,
                exit_code=self._exit_code,
                last_error=last_error,
            )


__all__ = [
    "RtmpPush",
    "RtmpState",
    "RtmpStatus",
    "build_rtmp_command",
    "missing_rtmp_capabilities",
    "probe_rtmp_capabilities",
    "redacted_argv",
    "resolve_rtmp_encoder",
]
