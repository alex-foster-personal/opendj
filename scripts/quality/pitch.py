"""Amendment 8: the master-tempo promise and key-shift accuracy, in cents.

The tracker is a numpy YIN, PROPOSED rather than assumed; the calibration
argument is in ops/quality/stretch/README.md and cross-checked against
librosa pyin by scripts/quality/pyin_crosscheck.py.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from scripts.quality.contract import (
    PITCH_CONFIDENCE_FLOOR,
    PITCH_FRAME_SAMPLES,
    PITCH_HOP_SAMPLES,
    PITCH_MAX_HZ,
    PITCH_MIN_HZ,
    PITCH_MIN_VOICED_FRACTION,
    PITCH_YIN_THRESHOLD,
    SAMPLE_RATE_HZ,
    UnpitchedMaterialError,
)
from scripts.quality.pcm import to_mono


def track_f0(
    signal: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = PITCH_HOP_SAMPLES,
    frame_samples: int = PITCH_FRAME_SAMPLES,
) -> tuple[np.ndarray, np.ndarray]:
    """YIN f0 contour. Returns (f0_hz, confidence) per frame; unvoiced f0 is NaN.

    Cumulative mean normalised difference with parabolic interpolation, all in
    numpy. The difference function is expanded as ``d(tau) = P0 + P(tau) -
    2*C(tau)`` so the cross term comes from one FFT per frame instead of a
    quadratic loop.
    """
    mono = to_mono(signal)
    tau_min = max(math.floor(sample_rate_hz / PITCH_MAX_HZ), 1)
    tau_max = math.ceil(sample_rate_hz / PITCH_MIN_HZ)
    buffer_samples = frame_samples + tau_max
    if len(mono) < buffer_samples:
        raise UnpitchedMaterialError(
            f"signal of {len(mono)} samples is shorter than the {buffer_samples}-sample "
            "pitch analysis buffer"
        )
    starts = np.arange(0, len(mono) - buffer_samples + 1, hop_samples)
    buffers = np.stack([mono[start : start + buffer_samples] for start in starts])

    squares = np.square(buffers)
    cumulative = np.concatenate([np.zeros((len(buffers), 1)), np.cumsum(squares, axis=1)], axis=1)
    power = cumulative[:, frame_samples:] - cumulative[:, : buffer_samples - frame_samples + 1]
    fft_size = 1 << (2 * buffer_samples - 1).bit_length()
    head = np.fft.rfft(buffers[:, :frame_samples], n=fft_size, axis=1)
    whole = np.fft.rfft(buffers, n=fft_size, axis=1)
    cross = np.fft.irfft(np.conj(head) * whole, n=fft_size, axis=1)[:, : tau_max + 1]

    difference = power[:, 0:1] + power[:, : tau_max + 1] - 2.0 * cross
    difference = np.maximum(difference, 0.0)
    running = np.cumsum(difference[:, 1:], axis=1)
    taus = np.arange(1, tau_max + 1)
    normalised = np.ones_like(difference)
    with np.errstate(divide="ignore", invalid="ignore"):
        normalised[:, 1:] = np.where(running > 0.0, difference[:, 1:] * taus / running, 1.0)

    # YIN's absolute-threshold step takes the first LOCAL MINIMUM below the
    # threshold, not the first sample below it. Taking the first sample instead
    # stops on the way down the same valley -- measured on a 220 Hz tone it
    # stops at tau 191 where the true minimum sits at 200, a +75 cent bias. And
    # a plain global argmin picks the deeper octave-down valley at tau 401.
    search = normalised[:, tau_min : tau_max + 1]
    interior = search[:, 1:-1]
    is_local_min = (interior < search[:, :-2]) & (interior <= search[:, 2:])
    candidate = is_local_min & (interior < PITCH_YIN_THRESHOLD)
    tau = (
        np.where(candidate.any(axis=1), candidate.argmax(axis=1) + 1, search.argmin(axis=1))
        + tau_min
    )

    rows = np.arange(len(buffers))
    left = normalised[rows, np.maximum(tau - 1, 0)]
    centre = normalised[rows, tau]
    right = normalised[rows, np.minimum(tau + 1, tau_max)]
    denominator = left - 2.0 * centre + right
    with np.errstate(divide="ignore", invalid="ignore"):
        shift = np.where(np.abs(denominator) > 1e-12, 0.5 * (left - right) / denominator, 0.0)
    refined = tau + np.clip(shift, -1.0, 1.0)

    confidence = np.clip(1.0 - centre, 0.0, 1.0)
    f0 = np.where(refined > 0.0, sample_rate_hz / np.maximum(refined, 1e-9), np.nan)
    voiced = (confidence >= PITCH_CONFIDENCE_FLOOR) & (f0 >= PITCH_MIN_HZ) & (f0 <= PITCH_MAX_HZ)
    return np.where(voiced, f0, np.nan), confidence


@dataclass(frozen=True)
class PitchError:
    """Master-tempo / key-shift accuracy in cents (amendment 8).

    ``p50_cents`` is SIGNED: its sign is the drift direction, which is the
    whole point of the master-tempo check. ``p95_cents`` is the 95th percentile
    of the absolute error.
    """

    expected_cents: float
    p50_cents: float
    p95_cents: float
    voiced_frames: int
    voiced_fraction: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def pitch_error_cents(
    reference: np.ndarray,
    rendered: np.ndarray,
    rate: float,
    semitones: float,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    hop_samples: int = PITCH_HOP_SAMPLES,
) -> PitchError:
    """Cents offset between a FORWARD render and its condition's expected pitch.

    ``rendered`` must be the SINGLE-PASS render, never the round trip: a round
    trip restores the original pitch by construction and would score ~0 for any
    build, measuring nothing.

    The expected shift is ``semitones * 100`` cents and is INDEPENDENT OF RATE.
    That independence is the master-tempo promise itself: a stretcher drifting
    30 cents at rate 1.15 otherwise surfaces only as unattributed spectral
    divergence.

    A forward render is time-scaled by ``1/rate``, so render frame ``k`` is
    compared against the reference at ``k * rate``; no waveform alignment is
    involved and none is needed.
    """
    if rate <= 0.0:
        raise ValueError(f"rate must be positive, got {rate}")
    reference_f0, _ = track_f0(reference, sample_rate_hz, hop_samples)
    rendered_f0, _ = track_f0(rendered, sample_rate_hz, hop_samples)

    render_indices = np.arange(len(rendered_f0))
    source_indices = np.rint(render_indices * rate).astype(int)
    usable = source_indices < len(reference_f0)
    render_indices, source_indices = render_indices[usable], source_indices[usable]

    pair_render = rendered_f0[render_indices]
    pair_reference = reference_f0[source_indices]
    voiced = np.isfinite(pair_render) & np.isfinite(pair_reference)
    total = len(render_indices)
    voiced_fraction = float(np.sum(voiced)) / total if total else 0.0
    if voiced_fraction < PITCH_MIN_VOICED_FRACTION:
        raise UnpitchedMaterialError(
            f"only {voiced_fraction:.1%} of frames are jointly voiced, below the "
            f"{PITCH_MIN_VOICED_FRACTION:.0%} needed; this fixture is not pitched enough "
            "to carry a cents metric"
        )

    expected = semitones * 100.0
    cents = 1200.0 * np.log2(pair_render[voiced] / pair_reference[voiced]) - expected
    return PitchError(
        expected_cents=expected,
        p50_cents=float(np.median(cents)),
        p95_cents=float(np.percentile(np.abs(cents), 95)),
        voiced_frames=int(np.sum(voiced)),
        voiced_fraction=voiced_fraction,
    )
