"""Onset strength, picking, and the displacement/recovery comparison.

Amendments 5, 6 and 9: hop 64 (gate/4 or finer, asserted), a half-wave-rectified
flux carrier, and a LINEAR-frequency basis so the librosa mel dead-low-band
defect has no way in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from scripts.quality.contract import (
    ONSET_FLUX_FFT_SAMPLES,
    ONSET_HOP_SAMPLES,
    ONSET_MATCH_WINDOW_MS,
    ONSET_MIN_SEPARATION_MS,
    ONSET_PEAK_DELTA,
    ONSET_PEAK_FACTOR,
    ONSET_PEAK_MEDIAN_WINDOW_MS,
    ONSET_RECOVERY_GATE_MS,
    SAMPLE_RATE_HZ,
    HarnessIntegrityError,
    HopResolutionError,
)
from scripts.quality.spectral import assert_low_band_resolution, stft_magnitude


def assert_onset_hop_resolution(
    hop_samples: int,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    gate_ms: float = ONSET_RECOVERY_GATE_MS,
) -> float:
    """Amendment 5: raise when hop resolution exceeds gate/4.

    librosa's default 512 hop is 11.61 ms at 44.1 kHz, ONE quantisation step
    below a 12 ms gate, so a displacement measured with it cannot distinguish
    "inside the gate" from "one step outside it".
    """
    resolution_ms = hop_samples / sample_rate_hz * 1000.0
    limit_ms = gate_ms / 4.0
    if resolution_ms > limit_ms:
        raise HopResolutionError(
            f"onset hop {hop_samples} samples = {resolution_ms:.3f} ms exceeds gate/4 "
            f"= {limit_ms:.3f} ms for a {gate_ms} ms gate"
        )
    return resolution_ms


def onset_strength(
    signal: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
    fft_samples: int = ONSET_FLUX_FFT_SAMPLES,
) -> np.ndarray:
    """Half-wave-rectified spectral flux (amendment 6), normalised to unit max."""
    assert_onset_hop_resolution(hop_samples, sample_rate_hz)
    assert_low_band_resolution(fft_samples, sample_rate_hz)
    spectrum = stft_magnitude(signal, fft_samples, hop_samples)
    flux = np.sum(np.maximum(np.diff(spectrum, axis=0), 0.0), axis=1)
    peak = float(np.max(flux)) if flux.size else 0.0
    return flux / peak if peak > 0.0 else flux


def pick_onsets(
    strength: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
) -> np.ndarray:
    """Onset times in seconds, picked off the rectified flux."""
    if strength.size == 0:
        return np.zeros(0)
    frame_ms = hop_samples / sample_rate_hz * 1000.0
    median_frames = max(round(ONSET_PEAK_MEDIAN_WINDOW_MS / frame_ms), 1)
    padded = np.pad(strength, median_frames, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * median_frames + 1)
    threshold = np.median(windows, axis=1)[: len(strength)] * ONSET_PEAK_FACTOR + ONSET_PEAK_DELTA
    candidates = (
        np.flatnonzero(
            (strength[1:-1] > strength[:-2])
            & (strength[1:-1] >= strength[2:])
            & (strength[1:-1] > threshold[1:-1])
        )
        + 1
    )
    separation_frames = max(round(ONSET_MIN_SEPARATION_MS / frame_ms), 1)
    kept: list[int] = []
    for frame in candidates[np.argsort(-strength[candidates])]:
        if all(abs(frame - chosen) >= separation_frames for chosen in kept):
            kept.append(int(frame))
    return np.sort(np.asarray(kept, dtype=np.float64)) * hop_samples / sample_rate_hz


@dataclass(frozen=True)
class OnsetComparison:
    """Onset displacement and recovery -- two of the three RANKING metrics."""

    reference_onsets: int
    matched_onsets: int
    displacement_p95_ms: float
    displacement_median_ms: float
    recovery_fraction: float
    recovery_gate_ms: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def compare_onsets(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = ONSET_HOP_SAMPLES,
    gate_ms: float = ONSET_RECOVERY_GATE_MS,
) -> OnsetComparison:
    """Match each reference onset to its nearest render onset."""
    assert_onset_hop_resolution(hop_samples, sample_rate_hz, gate_ms)
    ref_onsets = pick_onsets(
        onset_strength(reference, sample_rate_hz, hop_samples), sample_rate_hz, hop_samples
    )
    test_onsets = pick_onsets(
        onset_strength(rendered, sample_rate_hz, hop_samples), sample_rate_hz, hop_samples
    )
    if ref_onsets.size == 0:
        raise HarnessIntegrityError(
            "no onsets found in the reference; displacement and recovery are undefined"
        )
    displacements: list[float] = []
    for onset in ref_onsets:
        if test_onsets.size == 0:
            continue
        nearest = float(test_onsets[int(np.argmin(np.abs(test_onsets - onset)))])
        delta_ms = (nearest - onset) * 1000.0
        if abs(delta_ms) <= ONSET_MATCH_WINDOW_MS:
            displacements.append(delta_ms)
    absolute = np.abs(np.asarray(displacements)) if displacements else np.zeros(0)
    recovered = int(np.sum(absolute <= gate_ms)) if absolute.size else 0
    return OnsetComparison(
        reference_onsets=int(ref_onsets.size),
        matched_onsets=int(absolute.size),
        displacement_p95_ms=float(np.percentile(absolute, 95)) if absolute.size else float("inf"),
        displacement_median_ms=float(np.median(absolute)) if absolute.size else float("inf"),
        recovery_fraction=recovered / float(ref_onsets.size),
        recovery_gate_ms=gate_ms,
    )
