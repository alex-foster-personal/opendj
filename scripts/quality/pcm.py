"""PCM sidecars, mono folding, and the excerpt asserts.

Every render this harness measures is read back from disk and re-verified
against its pinned sha256, so a table row can never be produced from a file
that changed after it was written.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from scripts.quality.contract import (
    NON_SILENCE_MIN_PEAK_DBFS,
    NON_SILENCE_MIN_RMS_DBFS,
    SAMPLE_RATE_HZ,
    ChannelCollapseError,
    HarnessIntegrityError,
    SilentExcerptError,
)


@dataclass(frozen=True)
class PcmMeta:
    """Sidecar metadata for one raw float32 PCM file."""

    sample_rate_hz: int
    channels: int
    frames: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_pcm(path: Path, samples: np.ndarray, sample_rate_hz: int = SAMPLE_RATE_HZ) -> PcmMeta:
    """Write ``samples`` (channels x frames) as interleaved little-endian f32."""
    if samples.ndim != 2:
        raise ValueError(f"expected a channels x frames array, got shape {samples.shape}")
    interleaved = np.ascontiguousarray(samples.T, dtype="<f4")
    payload = interleaved.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    meta = PcmMeta(
        sample_rate_hz=sample_rate_hz,
        channels=int(samples.shape[0]),
        frames=int(samples.shape[1]),
        sha256=sha256_bytes(payload),
    )
    path.with_suffix(path.suffix + ".json").write_text(
        json.dumps(meta.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def read_pcm(path: Path) -> tuple[np.ndarray, PcmMeta]:
    """Read a PCM sidecar and RE-VERIFY its pinned sha256 before returning it."""
    payload = path.read_bytes()
    meta_path = path.with_suffix(path.suffix + ".json")
    if not meta_path.exists():
        raise HarnessIntegrityError(f"PCM sidecar has no metadata: {meta_path}")
    raw = json.loads(meta_path.read_text(encoding="utf-8"))
    meta = PcmMeta(
        sample_rate_hz=int(raw["sample_rate_hz"]),
        channels=int(raw["channels"]),
        frames=int(raw["frames"]),
        sha256=str(raw["sha256"]),
    )
    actual = sha256_bytes(payload)
    if actual != meta.sha256:
        raise HarnessIntegrityError(
            f"PCM sha256 mismatch for {path}: pinned {meta.sha256}, read {actual}"
        )
    samples = np.frombuffer(payload, dtype="<f4").reshape(meta.frames, meta.channels).T
    return np.ascontiguousarray(samples, dtype=np.float64), meta


def to_mono(samples: np.ndarray) -> np.ndarray:
    """Channel mean. Analysis carriers are mono; distinctness is checked apart."""
    if samples.ndim == 1:
        return np.asarray(samples, dtype=np.float64)
    return np.asarray(samples, dtype=np.float64).mean(axis=0)


# ----- excerpt asserts ------------------------------------------------------


def _dbfs(value: float) -> float:
    return -math.inf if value <= 0.0 else 20.0 * math.log10(value)


def assert_loud_non_silence(samples: np.ndarray, label: str) -> tuple[float, float]:
    """Raise unless the excerpt is audibly loud. Returns (rms_dbfs, peak_dbfs).

    An iCloud-evicted stub is served as an empty body rather than an error, so
    it decodes to digital silence. Without this assert the whole grid would be
    measured against silence and every metric would look excellent.
    """
    mono = to_mono(samples)
    if mono.size == 0:
        raise SilentExcerptError(f"{label}: excerpt is empty")
    rms_dbfs = _dbfs(float(np.sqrt(np.mean(np.square(mono)))))
    peak_dbfs = _dbfs(float(np.max(np.abs(mono))))
    if rms_dbfs < NON_SILENCE_MIN_RMS_DBFS or peak_dbfs < NON_SILENCE_MIN_PEAK_DBFS:
        raise SilentExcerptError(
            f"{label}: excerpt is not loud non-silence "
            f"(rms {rms_dbfs:.1f} dBFS, peak {peak_dbfs:.1f} dBFS; "
            f"need rms >= {NON_SILENCE_MIN_RMS_DBFS} and peak >= {NON_SILENCE_MIN_PEAK_DBFS})"
        )
    return rms_dbfs, peak_dbfs


def channels_are_bit_identical(samples: np.ndarray) -> bool:
    if samples.ndim != 2 or samples.shape[0] < 2:
        return True
    first = samples[0]
    return all(np.array_equal(first, samples[channel]) for channel in range(1, samples.shape[0]))


def assert_channel_distinctness_preserved(
    reference: np.ndarray, rendered: np.ndarray, label: str
) -> bool:
    """Raise if a distinct-channel source came back with duplicated channels.

    Checking channel COUNT is what lets a mono-collapsed render through: a
    renderer that duplicates its last channel still reports two. The observable
    that discriminates is whether the channels are still distinct.

    Returns True when the reference itself is mono-equivalent, i.e. when the
    check is inapplicable rather than passed.
    """
    if channels_are_bit_identical(reference):
        return True
    if channels_are_bit_identical(rendered):
        raise ChannelCollapseError(
            f"{label}: source channels are distinct but the render's are bit-identical "
            "(mono collapse, most likely silent last-channel duplication)"
        )
    return False
