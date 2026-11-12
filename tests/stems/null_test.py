"""Null-test helper: decode four stem parts, sum them, compare to a mix.

Advances PRD-STEMS-03 hygiene: misaligned or mismatched parts must be refused
with a numeric residual, never padded or truncated to fit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

try:
    import soundfile as sf
except ImportError:  # pragma: no cover - collection uses requires_audio_stack
    sf = None  # type: ignore[assignment]

DEMUCS_PARTS: Final[tuple[str, ...]] = ("vocals", "drums", "bass", "other")
DEFAULT_RESIDUAL_THRESHOLD_DB: Final[float] = -60.0

_SNDFILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".wav", ".wave", ".flac", ".ogg", ".oga", ".aif", ".aiff", ".aifc", ".mp3"}
)


class StemNullTestError(Exception):
    """Base error for stem null-test failures."""


class StemNullTestGeometryError(StemNullTestError):
    """Unequal sample rates or frame counts among parts or mix."""


@dataclass(frozen=True, slots=True)
class StemNullTestResult:
    """Outcome of summing four decoded parts and comparing to a decoded mix."""

    passed: bool
    residual_db: float
    sample_rate: int
    frame_count: int
    threshold_db: float


def _require_soundfile() -> object:
    if sf is None:
        raise StemNullTestError("soundfile is required to decode stem fixtures")
    return sf


def decode_stem_part(path: Path) -> tuple[np.ndarray, int]:
    """Decode one stem part or mix file to ``(frames, channels)`` float32 and its rate."""
    suffix = path.suffix.lower()
    if suffix in _SNDFILE_SUFFIXES:
        lib = _require_soundfile()
        data, rate = lib.read(path, dtype="float32", always_2d=True)
        return np.asarray(data, dtype=np.float32), int(rate)
    if suffix in {".m4a", ".mp4", ".aac"}:
        from apps.shared.engine_decode import EngineDecoderUnavailable, decode_f32

        try:
            pcm, rate, channels = decode_f32(path, mono=False)
        except EngineDecoderUnavailable as exc:
            raise StemNullTestError(f"cannot decode {path.name}: {exc}") from exc
        frames = len(pcm) // (4 * channels)
        audio = np.frombuffer(pcm, dtype=np.float32).reshape(frames, channels)
        return audio, int(rate)
    raise StemNullTestError(f"unsupported stem container {path.suffix} for {path.name}")


def _channel_count(arr: np.ndarray) -> int:
    return 1 if arr.ndim == 1 else int(arr.shape[1])


def _assert_same_geometry(samples: Sequence[np.ndarray], *, label: str) -> None:
    lengths = [int(arr.shape[0]) for arr in samples]
    if len(set(lengths)) != 1:
        raise StemNullTestGeometryError(
            f"{label}: frame counts differ ({', '.join(str(n) for n in lengths)}); refuse, do not pad or truncate"
        )
    channels = [_channel_count(arr) for arr in samples]
    if len(set(channels)) != 1:
        raise StemNullTestGeometryError(
            f"{label}: channel counts differ ({', '.join(str(n) for n in channels)}); refuse, do not fold"
        )


def _assert_geometry(samples: Sequence[np.ndarray], rates: Sequence[int], *, label: str) -> None:
    if len(samples) != len(rates):
        raise StemNullTestError(f"{label}: internal geometry check length mismatch")
    if len({int(r) for r in rates}) != 1:
        raise StemNullTestGeometryError(
            f"{label}: sample rates differ ({', '.join(str(r) for r in rates)}); refuse, do not resample"
        )
    _assert_same_geometry(samples, label=label)


def _flatten_samples(audio: np.ndarray) -> np.ndarray:
    """Every channel sample counts; do not downmix before RMS (out-of-phase errors must not cancel)."""
    return audio.reshape(-1) if audio.ndim == 2 else audio


def residual_db_against_mix(residual: np.ndarray, mix: np.ndarray) -> float:
    """Return residual RMS in dB relative to the mix RMS (more negative is quieter)."""
    if _channel_count(residual) != _channel_count(mix):
        raise StemNullTestGeometryError(
            f"residual channels {_channel_count(residual)} != mix channels {_channel_count(mix)}"
        )
    ref = _flatten_samples(mix)
    res = _flatten_samples(residual)
    ref_rms = float(np.sqrt(np.mean(np.square(ref))))
    if ref_rms <= 0.0:
        raise StemNullTestError("mix RMS is zero; cannot score a null test")
    res_rms = float(np.sqrt(np.mean(np.square(res))))
    if res_rms <= 0.0:
        return -math.inf
    return 20.0 * math.log10(res_rms / ref_rms)


def null_test_decoded(
    parts: Sequence[np.ndarray],
    mix: np.ndarray,
    sample_rate: int,
    *,
    threshold_db: float = DEFAULT_RESIDUAL_THRESHOLD_DB,
) -> StemNullTestResult:
    """Sum aligned decoded parts and compare the reconstruction to ``mix``."""
    if len(parts) != 4:
        raise StemNullTestError(f"expected four stem parts, got {len(parts)}")
    _assert_same_geometry([*parts, mix], label="decoded arrays")
    summed = np.sum(parts, axis=0)
    residual = mix - summed
    db = residual_db_against_mix(residual, mix)
    passed = db <= threshold_db
    return StemNullTestResult(
        passed=passed,
        residual_db=db,
        sample_rate=int(sample_rate),
        frame_count=int(mix.shape[0]),
        threshold_db=float(threshold_db),
    )


def null_test_four_parts(
    part_paths: Mapping[str, Path],
    mix_path: Path,
    *,
    threshold_db: float = DEFAULT_RESIDUAL_THRESHOLD_DB,
    required_parts: Sequence[str] = DEMUCS_PARTS,
) -> StemNullTestResult:
    """Decode four on-disk stem parts and a mix, then run the null test."""
    missing = [name for name in required_parts if name not in part_paths]
    if missing:
        raise StemNullTestError(f"missing stem parts: {', '.join(missing)}")
    decoded_parts: list[np.ndarray] = []
    rates: list[int] = []
    for name in required_parts:
        audio, rate = decode_stem_part(part_paths[name])
        decoded_parts.append(audio)
        rates.append(rate)
    mix_audio, mix_rate = decode_stem_part(mix_path)
    _assert_geometry([*decoded_parts, mix_audio], [*rates, mix_rate], label="stem bundle")
    return null_test_decoded(decoded_parts, mix_audio, mix_rate, threshold_db=threshold_db)


__all__ = [
    "DEFAULT_RESIDUAL_THRESHOLD_DB",
    "DEMUCS_PARTS",
    "StemNullTestError",
    "StemNullTestGeometryError",
    "StemNullTestResult",
    "decode_stem_part",
    "null_test_decoded",
    "null_test_four_parts",
    "residual_db_against_mix",
]
