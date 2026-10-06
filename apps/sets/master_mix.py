"""The set recorder's "Master mix (internal)" capture (SET-12).

Every other DJ app records its own master bus: what the audience hears, after
the master fader, before the hardware output, never the headphone cue. Open DJ
mixes in the page (Web Audio), so the page taps its master bus with an
AudioWorklet and streams 16-bit PCM here in sequenced chunks; this module
writes them as the same rolling WAV segments ``odj-audio record`` writes
(``audio_<UTC start>.wav``, 5 minutes each, every file created new, header
sizes rewritten after each chunk so a killed daemon leaves a playable file).
No loopback driver and no input device are involved.

Wire contract, one chunk per ``POST /api/sets/recorder/{id}/master-pcm``:

  * body: interleaved little-endian int16 stereo frames;
  * ``stream``: an id the page picks per tap. A new stream starts a new
    segment, because a new tap follows a gap (the page reloaded, or the audio
    graph was rebuilt on a device change) and segment names carry start times;
  * ``seq``: 0, 1, 2... within a stream. Anything else is a lost chunk and is
    refused, never written around;
  * ``sample_rate``: the page's AudioContext rate. A change starts a new
    segment, since a WAV file holds one rate.

The capture is ``starting`` until the first chunk, ``recording`` while chunks
arrive, and ``failed`` (with the reason) when none arrived within
:data:`ATTACH_TIMEOUT_S` of the start or for :data:`STALL_TIMEOUT_S` since the
last one: the page that was recording closed, or its engine stopped.
"""
from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from .capture_types import CaptureStateName

#: The manifest / sets-DB ``capture_device`` of a master-mix recording.
MASTER_MIX_DEVICE_LABEL = "Open DJ master mix (internal)"
CHANNELS = 2
SAMPLE_WIDTH_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH_BYTES
#: Same roll as odj-audio's capture and ffmpeg's segmenter.
SEGMENT_SECONDS = 300
#: The page has this long after the start to deliver its first chunk: it
#: loads the worklet module and waits for its first half-second of audio.
ATTACH_TIMEOUT_S = 10.0
#: Chunks arrive every 0.5 s; this many seconds without one is a dead tap.
STALL_TIMEOUT_S = 5.0
#: A chunk is at most a few seconds of audio; anything bigger is a bug.
MAX_CHUNK_BYTES = 4 * 1024 * 1024
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 192_000
_HEADER_BYTES = 44


class MasterMixChunkRefused(ValueError):
    """A chunk that cannot be written where it claims to belong."""


class MasterMixRecordingStopped(MasterMixChunkRefused):
    """A chunk that arrived after its recording was cleanly stopped.

    Expected once at most per stop (the page's last chunk was in flight), so
    the route answers 410 and the page drops it quietly. A chunk out of order
    DURING a recording stays :class:`MasterMixChunkRefused` (409, loud).
    """


def _wav_header(sample_rate: int, data_bytes: int) -> bytes:
    byte_rate = sample_rate * FRAME_BYTES
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_bytes)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, CHANNELS, sample_rate, byte_rate, FRAME_BYTES, 16)
        + b"data"
        + struct.pack("<I", data_bytes)
    )


class _Segment:
    """One open WAV file; created new, never replacing anything."""

    def __init__(self, path: Path, sample_rate: int) -> None:
        self.path = path
        self.sample_rate = sample_rate
        self.data_bytes = 0
        self._fh: BinaryIO = path.open("xb")
        self._fh.write(_wav_header(sample_rate, 0))

    @property
    def frames(self) -> int:
        return self.data_bytes // FRAME_BYTES

    def append(self, pcm: bytes) -> None:
        self._fh.seek(0, 2)
        self._fh.write(pcm)
        self.data_bytes += len(pcm)
        self._fh.seek(0)
        self._fh.write(_wav_header(self.sample_rate, self.data_bytes))
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class MasterMixWriter:
    """Writes the page's master-mix chunks for one recording session."""

    def __init__(
        self,
        session_dir: Path,
        *,
        segment_seconds: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.session_dir = Path(session_dir)
        # Read at construction, not def time, so a test can shorten the roll.
        self.segment_seconds = SEGMENT_SECONDS if segment_seconds is None else segment_seconds
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._lock = threading.Lock()
        self._started_at = monotonic()
        self._last_chunk_at: float | None = None
        # When the first accepted frame was captured: arrival minus its own
        # duration (the page posts a chunk once it is full), server clock.
        self._first_frame_at: float | None = None
        self._stream: str | None = None
        self._next_seq = 0
        self._segment: _Segment | None = None
        self._closed = False
        # What the page SENT and this writer accepted, counted from the request
        # bodies, independently of what reached disk: the two must match exactly.
        self.chunks_accepted = 0
        self.frames_accepted = 0
        self.streams = 0

    # -- capture-handle surface (what Recorder.capture_state reads) --------

    def current_state(self) -> CaptureStateName:
        return self._state_and_error()[0]

    @property
    def error(self) -> str | None:
        return self._state_and_error()[1]

    def _state_and_error(self) -> tuple[CaptureStateName, str | None]:
        with self._lock:
            if self._closed:
                return "stopped", None
            now = self._monotonic()
            if self._last_chunk_at is None:
                if now - self._started_at > ATTACH_TIMEOUT_S:
                    return "failed", (
                        f"no master-mix audio arrived within {ATTACH_TIMEOUT_S:.0f} s of the "
                        "start: the master mix is recorded by an open /performance page "
                        "playing through the Web Audio engine"
                    )
                return "starting", None
            if now - self._last_chunk_at > STALL_TIMEOUT_S:
                return "failed", (
                    f"no master-mix audio for {now - self._last_chunk_at:.0f} s: the page "
                    "recording the master mix closed or its audio engine stopped"
                )
            return "recording", None

    # -- writing -----------------------------------------------------------

    def append(self, *, stream: str, seq: int, sample_rate: int, pcm: bytes) -> None:
        """Write one chunk, or refuse it with :class:`MasterMixChunkRefused`."""
        if not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise MasterMixChunkRefused(f"sample_rate {sample_rate} is outside 8000..192000")
        if not pcm or len(pcm) % FRAME_BYTES:
            raise MasterMixChunkRefused(
                f"a chunk is whole stereo int16 frames ({FRAME_BYTES} bytes each); "
                f"got {len(pcm)} bytes"
            )
        if len(pcm) > MAX_CHUNK_BYTES:
            raise MasterMixChunkRefused(f"a {len(pcm)}-byte chunk exceeds {MAX_CHUNK_BYTES}")
        with self._lock:
            if self._closed:
                raise MasterMixRecordingStopped("the recording has stopped")
            if stream != self._stream:
                if seq != 0:
                    raise MasterMixChunkRefused(
                        f"stream {stream!r} must start at seq 0, not {seq}"
                    )
                self._stream = stream
                self.streams += 1
                self._next_seq = 0
                self._close_segment()
            if seq != self._next_seq:
                raise MasterMixChunkRefused(
                    f"chunk {seq} of stream {stream!r} arrived where {self._next_seq} was "
                    "expected: a chunk was lost, so the recording would have a hole"
                )
            segment = self._segment
            if (
                segment is None
                or segment.sample_rate != sample_rate
                or segment.frames >= self.segment_seconds * segment.sample_rate
            ):
                self._close_segment()
                segment = self._segment = self._open_segment(sample_rate)
            segment.append(pcm)
            self._next_seq += 1
            self._last_chunk_at = self._monotonic()
            if self._first_frame_at is None:
                self._first_frame_at = self._last_chunk_at - len(pcm) / FRAME_BYTES / sample_rate
            self.chunks_accepted += 1
            self.frames_accepted += len(pcm) // FRAME_BYTES

    def _open_segment(self, sample_rate: int) -> _Segment:
        """A new ``audio_<UTC start>.wav``; a taken second moves to the next one."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        start = self._utc_now().replace(microsecond=0)
        for _ in range(60):
            path = self.session_dir / f"audio_{start.strftime('%Y-%m-%dT%H-%M-%S')}.wav"
            try:
                return _Segment(path, sample_rate)
            except FileExistsError:
                start += timedelta(seconds=1)
        raise MasterMixChunkRefused(f"no free segment name in {self.session_dir} near {start}")

    def _close_segment(self) -> None:
        if self._segment is not None:
            self._segment.close()
            self._segment = None

    def start_to_first_frame_ms(self) -> int | None:
        """Server-clock ms from the recording's start to its first captured
        frame (SET-12: under about 100 ms once a page is attached on start)."""
        if self._first_frame_at is None:
            return None
        return round((self._first_frame_at - self._started_at) * 1000)

    def summary(self) -> dict[str, int | None]:
        """What was accepted, for the ``master_mix_closed`` timeline event: the
        frame total must equal the WAV frames on disk across every segment."""
        with self._lock:
            return {
                "frames_accepted": self.frames_accepted,
                "chunks_accepted": self.chunks_accepted,
                "streams": self.streams,
                "start_to_first_frame_ms": self.start_to_first_frame_ms(),
            }

    def close(self) -> None:
        """Close the open segment; later chunks are refused."""
        with self._lock:
            self._close_segment()
            self._closed = True


__all__ = [
    "ATTACH_TIMEOUT_S",
    "MASTER_MIX_DEVICE_LABEL",
    "MAX_CHUNK_BYTES",
    "STALL_TIMEOUT_S",
    "MasterMixChunkRefused",
    "MasterMixRecordingStopped",
    "MasterMixWriter",
]
