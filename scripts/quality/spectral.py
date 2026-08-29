"""Magnitude spectra: the STFT, the LSD that ranks builds, and the residual.

LSD ranks because it compares magnitude spectra and is phase-insensitive by
construction. Residual is carried alongside as a diagnostic and never ranks --
see the caveat the report prints above every table that contains it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from scripts.quality.contract import (
    LOW_BAND_MAX_BIN_HZ,
    LOW_BAND_MIN_BINS_BELOW_250HZ,
    LSD_FLOOR_DB_BELOW_REF_PEAK,
    LSD_FRAME_MS,
    SAMPLE_RATE_HZ,
    LowBandResolutionError,
    SilentExcerptError,
)


def stft_magnitude(signal: np.ndarray, fft_samples: int, hop_samples: int) -> np.ndarray:
    """Hann-windowed magnitude STFT, frames x bins."""
    mono = np.asarray(signal, dtype=np.float64)
    if len(mono) < fft_samples:
        raise ValueError(f"signal of {len(mono)} samples is shorter than the {fft_samples} FFT")
    window = np.hanning(fft_samples)
    starts = np.arange(0, len(mono) - fft_samples + 1, hop_samples)
    frames = np.stack([mono[start : start + fft_samples] * window for start in starts])
    return np.abs(np.fft.rfft(frames, axis=1))


def assert_low_band_resolution(fft_samples: int, sample_rate_hz: int = SAMPLE_RATE_HZ) -> None:
    """Structural analogue of the librosa mel dead-low-band defect.

    librosa's ``onset_strength`` at ``n_fft=512`` with ``n_mels=128`` silently
    produces dead low-frequency mel bands and swallows the warning, weakening
    bass-led onsets on exactly the bass-heavy fixture. This module uses linear
    bins so that failure mode cannot occur, and asserts the equivalent property
    directly rather than assuming it.
    """
    bin_hz = sample_rate_hz / fft_samples
    if bin_hz > LOW_BAND_MAX_BIN_HZ:
        raise LowBandResolutionError(
            f"FFT of {fft_samples} gives {bin_hz:.1f} Hz bins, coarser than the "
            f"{LOW_BAND_MAX_BIN_HZ} Hz low-band requirement; bass-led onsets would be blunted"
        )
    bins_below_250 = int(250.0 / bin_hz)
    if bins_below_250 < LOW_BAND_MIN_BINS_BELOW_250HZ:
        raise LowBandResolutionError(
            f"only {bins_below_250} bins fall below 250 Hz, need {LOW_BAND_MIN_BINS_BELOW_250HZ}"
        )


def log_spectral_distance(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    frame_ms: float = LSD_FRAME_MS,
) -> float:
    """LSD in dB, floored 80 dB below the REFERENCE PEAK (amendment 4).

    An absolute -100 dBFS floor lets empty-bin numerical noise dominate: the
    same null test reads 39.7 dB absolute-floored and 1.14 dB relative-floored.
    """
    fft_samples = round(frame_ms * sample_rate_hz / 1000.0)
    hop_samples = fft_samples // 2
    ref_spectrum = stft_magnitude(reference, fft_samples, hop_samples)
    test_spectrum = stft_magnitude(rendered, fft_samples, hop_samples)
    frames = min(ref_spectrum.shape[0], test_spectrum.shape[0])
    ref_spectrum, test_spectrum = ref_spectrum[:frames], test_spectrum[:frames]
    reference_peak = float(np.max(ref_spectrum))
    if reference_peak <= 0.0:
        raise SilentExcerptError("LSD reference has no spectral energy")
    floor = reference_peak * (10.0 ** (-LSD_FLOOR_DB_BELOW_REF_PEAK / 20.0))
    ref_db = 20.0 * np.log10(np.maximum(ref_spectrum, floor))
    test_db = 20.0 * np.log10(np.maximum(test_spectrum, floor))
    per_frame = np.sqrt(np.mean(np.square(ref_db - test_db), axis=1))
    return float(np.mean(per_frame))


@dataclass(frozen=True)
class Residual:
    """Round-trip residual, a DIAGNOSTIC only -- it never ranks a build."""

    residual_dbr: float
    correlation_rho: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def residual_dbr(reference: np.ndarray, rendered: np.ndarray) -> Residual:
    """Least-squares-gain-fitted residual, and the rho it is equivalent to.

    Amendment 1 records the identity ``residual_dBr = 10*log10(1 - rho^2)``;
    both are computed here so the pair cross-checks the implementation.
    """
    ref = np.asarray(reference, dtype=np.float64)
    test = np.asarray(rendered, dtype=np.float64)
    ref_energy = float(np.dot(ref, ref))
    test_energy = float(np.dot(test, test))
    if ref_energy <= 0.0 or test_energy <= 0.0:
        raise SilentExcerptError("residual needs two non-silent signals")
    gain = float(np.dot(ref, test)) / test_energy
    residual = ref - gain * test
    rho = float(np.dot(ref, test)) / math.sqrt(ref_energy * test_energy)
    value = float(np.dot(residual, residual)) / ref_energy
    return Residual(
        residual_dbr=10.0 * math.log10(max(value, 1e-30)),
        correlation_rho=rho,
    )
