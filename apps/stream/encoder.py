"""Turn the master-mix PCM chunks into an encoded stream for a sink (STREAM-11486).

Mini-PRD
--------
R1 ok   Build the ffmpeg argv per format: mp3, aac (ADTS) and opus (Ogg), 32 to
        320 kbps (default 192, mimicked from the Mixxx manual's example).
R2 ok   Find ffmpeg the way SET-10 does (overrides, PATH, then Homebrew); none
        found raises :class:`EncoderUnavailable` naming every place looked, and
        nothing is spawned.
R3 ok   Fed by a tee on :class:`~apps.sets.master_mix.MasterMixWriter`: the tee
        only touches a 64-chunk queue that drops the oldest and counts it, so a
        slow consumer never blocks the master-pcm route.
R4 ok   Encoded bytes come out of :meth:`MasterMixEncoder.encoded_chunks`; an
        encoder that exits on its own ends it with :class:`EncoderExited`.
R5 ok   Stop closes stdin, waits at most :data:`STOP_GRACE_S`, then terminates.
R6 ok   Only a ``master`` recording can feed it: anything else is
        ``STREAM_SOURCE_NOT_MASTER``.

Acceptance
----------
[if] three chunks are teed to a cat-like encoder [then] they come out byte for byte, in order
[if] no ffmpeg on PATH or in Homebrew's prefixes [then] EncoderUnavailable names all three, no spawn
[if] mp3 at 192 kbps and 48 kHz                   [then] the argv is the documented line exactly
[if] the format is unknown or bitrate is 31       [then] refused before any process starts
[if] the encoder exits after one chunk            [then] EncoderExited carries its code and stderr
[if] nobody reads the encoder                     [then] the tee never blocks; oldest chunks drop
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import IO, Any, Literal

from apps.sets import master_mix
from apps.shared import ffmpeg as ffmpeg_mod

# -----
# constants

DEFAULT_BITRATE_KBPS = 192
MIN_BITRATE_KBPS = 32
MAX_BITRATE_KBPS = 320
QUEUE_MAX_CHUNKS = 64
STOP_GRACE_S = 2.0
READ_BYTES = 64 * 1024
STDERR_TAIL_BYTES = 8 * 1024
AF_SERVICE_ID = "com.opendj.stream-encoder"
STREAM_SOURCE_NOT_MASTER = "STREAM_SOURCE_NOT_MASTER"
STREAM_ENCODER_UNAVAILABLE = "STREAM_ENCODER_UNAVAILABLE"

EncoderFormat = Literal["mp3", "aac", "opus"]
EncoderState = Literal["idle", "running", "stopped", "exited", "failed"]
#: format -> (muxer, codec args placed before ``-b:a``). Ogg defaults to Vorbis.
_FORMATS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mp3": ("mp3", ()),
    "aac": ("adts", ()),
    "opus": ("ogg", ("-c:a", "libopus")),
}
ENCODER_FORMATS: tuple[str, ...] = tuple(_FORMATS)

# -----
# errors


class EncoderRequestRefused(ValueError):
    """An unknown format, or a bitrate or sample rate out of range."""


class EncoderUnavailable(RuntimeError):
    """No encoder executable was found; the message names every place looked."""

    code = STREAM_ENCODER_UNAVAILABLE


class EncoderExited(RuntimeError):
    """The encoder process ended on its own."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        super().__init__(f"encoder exited with code {exit_code}: {stderr[-500:]}")
        self.exit_code = exit_code
        self.stderr = stderr


class StreamSourceNotMaster(RuntimeError):
    """A recording that does not capture the app's own master mix."""

    code = STREAM_SOURCE_NOT_MASTER

    def __init__(self, source: str | None) -> None:
        kind = "a tracklist-only recording" if source in (None, "none") else "an input recording"
        super().__init__(
            f"{STREAM_SOURCE_NOT_MASTER}: source {source!r} is {kind}; only the app's own "
            "master mix (source: master) can feed a stream"
        )
        self.source = source


# -----
# pure policy


def validate_encoder_request(fmt: str, bitrate_kbps: int, sample_rate: int) -> None:
    if fmt not in _FORMATS:
        raise EncoderRequestRefused(f"format {fmt!r} is not one of {', '.join(ENCODER_FORMATS)}")
    if isinstance(bitrate_kbps, bool) or not isinstance(bitrate_kbps, int):
        raise EncoderRequestRefused(f"bitrate {bitrate_kbps!r} is not a whole number of kbps")
    if not MIN_BITRATE_KBPS <= bitrate_kbps <= MAX_BITRATE_KBPS:
        raise EncoderRequestRefused(f"bitrate {bitrate_kbps} kbps is outside {MIN_BITRATE_KBPS} to {MAX_BITRATE_KBPS}")
    if not master_mix.MIN_SAMPLE_RATE <= sample_rate <= master_mix.MAX_SAMPLE_RATE:
        raise EncoderRequestRefused(f"sample rate {sample_rate} is outside 8000..192000")


def build_encoder_command(executable: str, fmt: str, bitrate_kbps: int, sample_rate: int) -> list[str]:
    validate_encoder_request(fmt, bitrate_kbps, sample_rate)
    muxer, codec_args = _FORMATS[fmt]
    return [
        executable,
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(master_mix.CHANNELS),
        "-i",
        "pipe:0",
        "-f",
        muxer,
        *codec_args,
        "-b:a",
        f"{bitrate_kbps}k",
        "pipe:1",
    ]


def resolve_encoder() -> str:
    try:
        return ffmpeg_mod.resolve_ffmpeg_including_homebrew()
    except ffmpeg_mod.FfmpegUnavailable as exc:
        homebrew = " and in ".join(ffmpeg_mod.HOMEBREW_FFMPEG_PATHS)
        raise EncoderUnavailable(
            f"no stream encoder (ffmpeg) found: looked on PATH, in {homebrew} "
            f"(install it with `brew install ffmpeg`, or set {ffmpeg_mod.OVERRIDE_ENV}); {exc}"
        ) from exc


def require_master_source(source: str | None) -> None:
    if source != "master":
        raise StreamSourceNotMaster(source)


# -----
# the encoder


@dataclass(frozen=True)
class EncoderStatus:
    format: str
    bitrate_kbps: int
    sample_rate: int
    state: EncoderState
    pid: int | None
    chunks_in: int
    bytes_in: int
    bytes_out: int
    queued_chunks: int
    dropped_chunks: int
    exit_code: int | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MasterMixEncoder:
    """One ffmpeg subprocess fed by a master-mix tee; threads, so the tee needs no loop."""

    def __init__(self, *, fmt: str, bitrate_kbps: int = DEFAULT_BITRATE_KBPS, sample_rate: int) -> None:
        validate_encoder_request(fmt, bitrate_kbps, sample_rate)
        self.format = fmt
        self.bitrate_kbps = bitrate_kbps
        self.sample_rate = sample_rate
        self._cond = threading.Condition()
        self._queue: collections.deque[bytes] = collections.deque()
        self._proc: subprocess.Popen[bytes] | None = None
        self._state: EncoderState = "idle"
        self._stopping = False
        self._iterating = False
        self._stderr_tail = bytearray()
        self._feeder: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._chunks_in = self._bytes_in = self._bytes_out = self._dropped = 0
        self._exit_code: int | None = None
        self._error: str | None = None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("already started")
        argv = build_encoder_command(resolve_encoder(), self.format, self.bitrate_kbps, self.sample_rate)
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env={**os.environ, "AF_SERVICE_ID": AF_SERVICE_ID},
        )
        self._state = "running"
        self._feeder = threading.Thread(target=self._feed, name="stream-encoder-feed", daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, name="stream-encoder-stderr", daemon=True)
        self._feeder.start()
        self._stderr_reader.start()

    def tee(self, pcm: bytes, sample_rate: int) -> None:
        """The :data:`~apps.sets.master_mix.MasterMixTee`: never blocks, never raises."""
        with self._cond:
            if self._state != "running":
                return
            if sample_rate != self.sample_rate:
                self._state = "failed"
                self._error = (
                    f"master mix sample rate changed from {self.sample_rate} to {sample_rate}; restart the stream"
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
                return  # the encoder died; encoded_chunks reports its exit
        with contextlib.suppress(BrokenPipeError):
            stdin.close()

    def _read_stderr(self) -> None:
        stderr = self._pipe("stderr")
        while data := stderr.read(4096):
            with self._cond:
                self._stderr_tail += data
                del self._stderr_tail[:-STDERR_TAIL_BYTES]

    def _pipe(self, name: str) -> IO[bytes]:
        pipe = getattr(self._proc, name, None)
        if pipe is None:
            raise RuntimeError("the encoder has not been started")
        return pipe

    async def encoded_chunks(self) -> AsyncIterator[bytes]:
        stdout = self._pipe("stdout")
        with self._cond:
            if self._iterating:
                raise RuntimeError("encoded_chunks is already being read")
            self._iterating = True
        while data := await asyncio.to_thread(stdout.read, READ_BYTES):
            with self._cond:
                self._bytes_out += len(data)
            yield data
        assert self._proc is not None
        code = await asyncio.to_thread(self._proc.wait)
        if self._stderr_reader is not None:
            await asyncio.to_thread(self._stderr_reader.join, 1.0)
        with self._cond:
            if self._stopping:
                return
            self._state = "exited"
            self._exit_code = code
            stderr = self._stderr_tail.decode(errors="replace")
        raise EncoderExited(code, stderr)

    def stop(self) -> EncoderStatus:
        """Close stdin, wait at most STOP_GRACE_S, then terminate. Idempotent."""
        proc = self._proc
        if proc is None:
            return self.status()
        with self._cond:
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
        with self._cond:
            if self._state == "running":
                self._state = "stopped"
            if self._exit_code is None:
                self._exit_code = code
        return self.status()

    def status(self) -> EncoderStatus:
        with self._cond:
            return EncoderStatus(
                format=self.format,
                bitrate_kbps=self.bitrate_kbps,
                sample_rate=self.sample_rate,
                state=self._state,
                pid=None if self._proc is None else self._proc.pid,
                chunks_in=self._chunks_in,
                bytes_in=self._bytes_in,
                bytes_out=self._bytes_out,
                queued_chunks=len(self._queue),
                dropped_chunks=self._dropped,
                exit_code=self._exit_code,
                error=self._error,
            )


__all__ = [
    "DEFAULT_BITRATE_KBPS",
    "ENCODER_FORMATS",
    "QUEUE_MAX_CHUNKS",
    "STOP_GRACE_S",
    "STREAM_ENCODER_UNAVAILABLE",
    "STREAM_SOURCE_NOT_MASTER",
    "EncoderExited",
    "EncoderFormat",
    "EncoderRequestRefused",
    "EncoderStatus",
    "EncoderUnavailable",
    "MasterMixEncoder",
    "StreamSourceNotMaster",
    "build_encoder_command",
    "require_master_source",
    "resolve_encoder",
    "validate_encoder_request",
]
