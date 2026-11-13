"""Classic sampler captures: a deck loop rendered to WAV, stored beside ``state.db`` (issue #9998).

The browser renders the engaged loop with an ``OfflineAudioContext`` and posts the WAV; this
module checks it really is a WAV and stores it as ``<state dir>/sampler-captures/<sha256>.wav``.
The returned absolute path is what a classic sampler bank slot stores (``{"path": ...}`` in
``sampler-banks.json``, ``apps.shared.state.sampler_banks``), so the banks document reloads it
after a restart. Named by content hash, so posting the same audio twice keeps one file.

Size cap: the longest capture is a 32 beat loop at 60 BPM (32 s), rendered as the
``OfflineAudioContext`` renders it, 48 kHz stereo 32-bit float, so 32 x 48 000 x 2 x 4 =
12 288 000 bytes of samples plus 4 KiB for the RIFF header and any extra chunks.

CloudSync: not synced, like ``sampler-banks.json`` (machine-local files).

Mini-PRD:
  ✔︎ ✅ 🎯 a valid WAV is stored once under sampler-captures/ and its duration answered
    [if] a WAV is stored [then] the file sits at the returned path with the same bytes [else ⛔️]
    [if] the same WAV is stored twice [then] one file, the same path both times [else ⛔️]
  ✔︎ ✅ 🎯 anything that is not a sane PCM or float WAV, or is over the cap, is refused and nothing stored
    [if] the bytes are not RIFF/WAVE [then] InvalidCapture and no file [else ⛔️]
    [if] the bytes are over MAX_CAPTURE_BYTES [then] CaptureTooLarge and no file [else ⛔️]
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apps.shared.atomic_text import write_atomic_bytes
from apps.shared.audio_chunk_io import UnplayableAudioError
from apps.shared.audio_wav_probe import probe_wav_bytes

DIRNAME: Final = "sampler-captures"
LONGEST_LOOP_BEATS: Final = 32
SLOWEST_LOOP_BPM: Final = 60
RENDER_RATE_HZ: Final = 48_000
RENDER_CHANNELS: Final = 2
RENDER_SAMPLE_BYTES: Final = 4  # 32-bit float
WAV_HEADER_SLACK_BYTES: Final = 4096
LONGEST_LOOP_S: Final = LONGEST_LOOP_BEATS * 60 // SLOWEST_LOOP_BPM
MAX_CAPTURE_BYTES: Final = (
    LONGEST_LOOP_S * RENDER_RATE_HZ * RENDER_CHANNELS * RENDER_SAMPLE_BYTES + WAV_HEADER_SLACK_BYTES
)


class InvalidCapture(ValueError):
    """Bytes that are not a PCM or float WAV with a sane channel count and sample rate."""


class CaptureTooLarge(ValueError):
    """A capture over ``MAX_CAPTURE_BYTES``."""


@dataclass(frozen=True)
class StoredCapture:
    """Where a capture lives and how long it plays."""

    path: Path
    duration_ms: int


def captures_dir(state_db_path: Path) -> Path:
    """The ``sampler-captures/`` directory beside the given ``state.db``."""
    return Path(state_db_path).parent / DIRNAME


def store_capture(state_db_path: Path, data: bytes) -> StoredCapture:
    """Validate WAV bytes, then store them atomically under their sha256 unless already there."""
    if len(data) > MAX_CAPTURE_BYTES:
        raise CaptureTooLarge(f"capture is {len(data)} bytes, over the {MAX_CAPTURE_BYTES} byte limit")
    try:
        rate, _channels, frames = probe_wav_bytes(data)
    except UnplayableAudioError as error:
        raise InvalidCapture(f"not a PCM or float WAV: {error}") from error
    path = captures_dir(state_db_path) / f"{hashlib.sha256(data).hexdigest()}.wav"
    if not path.is_file():
        write_atomic_bytes(path, data)
    return StoredCapture(path=path, duration_ms=round(frames * 1000 / rate))
