"""Measured integer-delay alignment on the envelope-rise carrier.

Amendment 7 and its Wed 19 Aug 2026 refinement live here: raw waveforms are
never correlated at any stage, and four gates stand between a correlation and a
reported delay. Each gate is a refusal to produce a number, never a quality
verdict.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from scripts.quality.contract import (
    ALIGN_AMBIGUITY_GUARD_MS,
    ALIGN_AMBIGUITY_MAX_RATIO,
    ALIGN_COARSE_MAX_LAG_MS,
    ALIGN_CORRELATION_FLOOR,
    ALIGN_FINE_HOP_SAMPLES,
    ALIGN_SELF_SIMILARITY_CEILING,
    CARRIER_ACTIVITY_FLOOR,
    ENVELOPE_WINDOW_SAMPLES,
    ONSET_HOP_SAMPLES,
    SAMPLE_RATE_HZ,
    AlignmentAmbiguousError,
    AlignmentBoundError,
    AlignmentCorrelationFloorError,
    AlignmentError,
    DegenerateCarrierError,
    ReferenceSelfSimilarityError,
)
from scripts.quality.pcm import to_mono


def envelope_rms(signal: np.ndarray, hop_samples: int, window_samples: int) -> np.ndarray:
    """Sliding-RMS amplitude envelope at ``hop_samples``."""
    if hop_samples < 1 or window_samples < 1:
        raise ValueError("hop and window must be >= 1 sample")
    mono = np.asarray(signal, dtype=np.float64)
    cumulative = np.concatenate(([0.0], np.cumsum(np.square(mono))))
    starts = np.arange(0, max(len(mono) - window_samples + 1, 1), hop_samples)
    ends = np.minimum(starts + window_samples, len(mono))
    return np.sqrt((cumulative[ends] - cumulative[starts]) / np.maximum(ends - starts, 1))


def envelope_rise_carrier(
    signal: np.ndarray,
    hop_samples: int,
    window_samples: int = ENVELOPE_WINDOW_SAMPLES,
    label: str = "signal",
) -> np.ndarray:
    """Half-wave-rectified envelope RISE (amendments 6 and 7).

    Amendment 6: a plain amplitude envelope flatlines on sustained material and
    slides to the search bound. The RISE is what carries alignment information
    there. Amendment 7: this is the ONLY carrier alignment may correlate on --
    raw waveform cycle-skips on quasi-periodic material and reports 0.9995
    confidence while doing it.
    """
    envelope = envelope_rms(signal, hop_samples, window_samples)
    if envelope.size < 2:
        raise DegenerateCarrierError(f"{label}: signal too short for an envelope carrier")
    rise = np.maximum(np.diff(envelope), 0.0)
    level = float(np.mean(envelope))
    activity = float(np.std(rise)) / level if level > 0.0 else 0.0
    if level <= 0.0 or activity < CARRIER_ACTIVITY_FLOOR:
        raise DegenerateCarrierError(
            f"{label}: envelope-rise carrier is flat (std/level {activity:.3e} < "
            f"{CARRIER_ACTIVITY_FLOOR:.0e}); an unmodulated tone carries no "
            "alignment information"
        )
    return rise


def normalised_cross_correlation(a: np.ndarray, b: np.ndarray, lags: np.ndarray) -> np.ndarray:
    """NCC of ``a`` against ``b`` at each integer lag, normalised per overlap.

    A positive lag means ``b`` is LATE relative to ``a``: ``b[n + lag]``
    lines up with ``a[n]``.
    """
    scores = np.zeros(len(lags), dtype=np.float64)
    for index, lag in enumerate(lags):
        if lag >= 0:
            left, right = a[: len(a) - lag], b[lag:]
        else:
            left, right = a[-lag:], b[: len(b) + lag]
        size = min(len(left), len(right))
        if size < 8:
            continue
        left, right = left[:size], right[:size]
        left = left - left.mean()
        right = right - right.mean()
        denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
        if denominator > 0.0:
            scores[index] = float(np.dot(left, right)) / denominator
    return scores


def carrier_self_similarity(
    carrier: np.ndarray, lags: np.ndarray, guard_lags: int
) -> tuple[float, int]:
    """Gate (d): how well the reference's own carrier matches itself elsewhere.

    Returns the best non-trivial self-correlation inside the search window and
    the lag it sits at. Lags within ``guard_lags`` of zero are excluded, because
    every signal matches itself near zero and that says nothing about whether a
    delay is identifiable.
    """
    scores = normalised_cross_correlation(carrier, carrier, lags)
    masked = np.where(np.abs(lags) <= guard_lags, -np.inf, scores)
    if not np.any(np.isfinite(masked)):
        return 0.0, 0
    index = int(np.argmax(masked))
    return float(masked[index]), int(lags[index])


@dataclass(frozen=True)
class Alignment:
    """The measured integer delay and the evidence it is trustworthy.

    ``runner_up_delay_samples`` exists because a bound check alone cannot prove
    a trim is right: on 16th-note material at 103-158 BPM a 120 ms-early render
    aliases onto the next musical period and reports a near-zero lag. Carrying
    the runner-up peak makes that alias visible in the row instead of absorbed
    by it.
    """

    delay_samples: int
    correlation: float
    peak_ratio: float
    runner_up_delay_samples: int
    self_similarity: float
    self_similarity_delay_samples: int
    carrier: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _guarded_peak(
    scores: np.ndarray, lags: np.ndarray, guard_lags: int, label: str
) -> tuple[int, float, float, int]:
    best_index = int(np.argmax(scores))
    best_score = float(scores[best_index])
    if best_score < ALIGN_CORRELATION_FLOOR:
        raise AlignmentCorrelationFloorError(
            f"{label}: best align correlation {best_score:.4f} is below the floor "
            f"{ALIGN_CORRELATION_FLOOR}; independent signals 'align' at ~0.005, so this "
            "is a refusal, not a measurement"
        )
    masked = scores.copy()
    low = max(best_index - guard_lags, 0)
    high = min(best_index + guard_lags + 1, len(scores))
    masked[low:high] = -np.inf
    if np.any(np.isfinite(masked)):
        runner_index = int(np.argmax(masked))
        runner_up, runner_lag = float(masked[runner_index]), int(lags[runner_index])
    else:
        runner_up, runner_lag = 0.0, 0
    peak_ratio = max(runner_up, 0.0) / best_score if best_score > 0.0 else 1.0
    if peak_ratio > ALIGN_AMBIGUITY_MAX_RATIO:
        raise AlignmentAmbiguousError(
            f"{label}: runner-up correlation {runner_up:.4f} at lag {runner_lag} is "
            f"indistinguishable from the best {best_score:.4f} at lag {int(lags[best_index])} "
            f"(ratio {peak_ratio:.3f} > {ALIGN_AMBIGUITY_MAX_RATIO}); the correlation does "
            "not identify a delay"
        )
    return int(lags[best_index]), best_score, peak_ratio, runner_lag


def estimate_delay_samples(
    reference: np.ndarray,
    rendered: np.ndarray,
    sample_rate_hz: int = SAMPLE_RATE_HZ,
    label: str = "row",
) -> Alignment:
    """Two-stage integer-delay alignment on the envelope-rise carrier.

    Positive ``delay_samples`` means ``rendered`` is late by that many samples.
    Both stages correlate the half-wave-rectified envelope rise; raw waveform
    is never correlated anywhere (amendment 7).
    """
    ref_mono, test_mono = to_mono(reference), to_mono(rendered)

    coarse_hop = ONSET_HOP_SAMPLES
    ref_coarse = envelope_rise_carrier(ref_mono, coarse_hop, label=f"{label} reference")
    test_coarse = envelope_rise_carrier(test_mono, coarse_hop, label=f"{label} render")
    max_lag_frames = round(ALIGN_COARSE_MAX_LAG_MS * sample_rate_hz / 1000.0 / coarse_hop)
    coarse_lags = np.arange(-max_lag_frames, max_lag_frames + 1)
    guard_frames = max(round(ALIGN_AMBIGUITY_GUARD_MS * sample_rate_hz / 1000.0 / coarse_hop), 1)

    # Gate (d) runs BEFORE any search: if the reference cannot distinguish its
    # own lags, no estimate off it means anything, whatever confidence comes back.
    self_similarity, self_lag = carrier_self_similarity(ref_coarse, coarse_lags, guard_frames)
    if self_similarity >= ALIGN_SELF_SIMILARITY_CEILING:
        raise ReferenceSelfSimilarityError(
            f"{label}: the reference carrier matches itself at lag "
            f"{self_lag * coarse_hop} samples with correlation {self_similarity:.4f} "
            f">= {ALIGN_SELF_SIMILARITY_CEILING}; this reference is too periodic for a "
            "correlation to identify a delay"
        )

    coarse_scores = normalised_cross_correlation(ref_coarse, test_coarse, coarse_lags)
    # Gate (b), coarse half: an argmax on the coarse bound means the true delay
    # is outside the searched window, so the number would be a clipped guess.
    coarse_index = int(np.argmax(coarse_scores))
    if coarse_index in (0, len(coarse_lags) - 1):
        raise AlignmentBoundError(
            f"{label}: coarse-stage argmax landed on its search bound "
            f"(lag {int(coarse_lags[coarse_index]) * coarse_hop} samples, window "
            f"+-{ALIGN_COARSE_MAX_LAG_MS} ms); the true delay is outside the search"
        )
    coarse_lag, _, coarse_ratio, coarse_runner = _guarded_peak(
        coarse_scores, coarse_lags, guard_frames, f"{label} coarse"
    )

    fine_hop = ALIGN_FINE_HOP_SAMPLES
    ref_fine = envelope_rise_carrier(ref_mono, fine_hop, label=f"{label} reference fine")
    test_fine = envelope_rise_carrier(test_mono, fine_hop, label=f"{label} render fine")
    centre = round(coarse_lag * coarse_hop / fine_hop)
    span = math.ceil(coarse_hop / fine_hop) * 2
    fine_lags = np.arange(centre - span, centre + span + 1)
    fine_scores = normalised_cross_correlation(ref_fine, test_fine, fine_lags)
    fine_index = int(np.argmax(fine_scores))
    if fine_index in (0, len(fine_lags) - 1):
        raise AlignmentBoundError(
            f"{label}: fine-stage argmax landed on its search bound "
            f"(lag {int(fine_lags[fine_index]) * fine_hop} samples, window "
            f"+-{span * fine_hop} samples around the coarse estimate); the coarse "
            "stage and the fine stage disagree"
        )
    fine_guard = max(round(ALIGN_AMBIGUITY_GUARD_MS * sample_rate_hz / 1000.0 / fine_hop), 1)
    fine_lag, correlation, fine_ratio, _ = _guarded_peak(
        fine_scores, fine_lags, fine_guard, f"{label} fine"
    )
    return Alignment(
        delay_samples=int(fine_lag * fine_hop),
        correlation=correlation,
        peak_ratio=max(coarse_ratio, fine_ratio),
        runner_up_delay_samples=int(coarse_runner * coarse_hop),
        self_similarity=self_similarity,
        self_similarity_delay_samples=int(self_lag * coarse_hop),
        carrier="half-wave-rectified envelope rise",
    )


def apply_delay(
    reference: np.ndarray, rendered: np.ndarray, delay_samples: int
) -> tuple[np.ndarray, np.ndarray]:
    """Trim both signals to their aligned, common span."""
    ref_mono, test_mono = to_mono(reference), to_mono(rendered)
    if delay_samples >= 0:
        test_mono = test_mono[delay_samples:]
    else:
        ref_mono = ref_mono[-delay_samples:]
    size = min(len(ref_mono), len(test_mono))
    if size <= 0:
        raise AlignmentError(f"alignment by {delay_samples} samples leaves no overlap")
    return ref_mono[:size], test_mono[:size]
