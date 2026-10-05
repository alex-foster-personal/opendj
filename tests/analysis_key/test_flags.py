"""Tests for apps.analysis_key.flags.evaluate_tonal_center."""
from __future__ import annotations

from apps.analysis_key.canon import Key
from apps.analysis_key.flags import (
    CONFIDENCE_THRESHOLD,
    REASON_AMBIGUOUS_MARGIN,
    REASON_LOW_CONFIDENCE,
    evaluate_tonal_center,
)
from apps.analysis_key.profiles import KeyEstimate

_KEY = Key(pitch_class=0, is_minor=False)


def test_high_confidence_high_margin_passes() -> None:
    estimate = KeyEstimate(key=_KEY, confidence=0.8, margin=0.3)
    flag = evaluate_tonal_center(estimate)
    assert flag.no_tonal_center is False
    assert flag.reason is None
    assert flag.confidence == 0.8
    assert flag.margin == 0.3


def test_low_confidence_flags_regardless_of_margin() -> None:
    estimate = KeyEstimate(key=_KEY, confidence=0.05, margin=0.5)
    flag = evaluate_tonal_center(estimate)
    assert flag.no_tonal_center is True
    assert flag.reason == REASON_LOW_CONFIDENCE


def test_thin_margin_flags_even_with_high_confidence() -> None:
    estimate = KeyEstimate(key=_KEY, confidence=0.9, margin=0.001)
    flag = evaluate_tonal_center(estimate)
    assert flag.no_tonal_center is True
    assert flag.reason == REASON_AMBIGUOUS_MARGIN


def test_low_confidence_reason_wins_over_low_margin() -> None:
    """Cause before consequence: low confidence is reported first."""
    estimate = KeyEstimate(key=_KEY, confidence=0.0, margin=0.0)
    flag = evaluate_tonal_center(estimate)
    assert flag.reason == REASON_LOW_CONFIDENCE


def test_thresholds_are_configurable() -> None:
    estimate = KeyEstimate(key=_KEY, confidence=0.5, margin=0.5)
    flag = evaluate_tonal_center(estimate, confidence_threshold=0.9)
    assert flag.no_tonal_center is True
    assert flag.reason == REASON_LOW_CONFIDENCE


def test_boundary_confidence_passes_the_confidence_check() -> None:
    estimate = KeyEstimate(key=_KEY, confidence=CONFIDENCE_THRESHOLD, margin=0.5)
    flag = evaluate_tonal_center(estimate)
    assert flag.reason != REASON_LOW_CONFIDENCE


def test_confidence_threshold_is_above_the_scorers_provable_floor() -> None:
    """Regression for a real bug: apps.analysis_key.profiles._estimate's best
    of 24 rotated cosine scores against the strictly-positive Krumhansl
    profiles cannot fall below ~0.489 for any nonzero chroma (a convex
    minimax property of those exact profile constants -- see flags.py's
    module docstring for the derivation). A CONFIDENCE_THRESHOLD at or below
    that floor can mathematically never fire for real (nonzero) input."""
    assert CONFIDENCE_THRESHOLD > 0.489


def test_the_measured_median_margin_publishes() -> None:
    """Round 1 (Fri 2 Oct 2026): the median margin over 60 real tracks was
    0.0097. The 0.02 placeholder declined it, and 88% of tracks with it."""
    estimate = KeyEstimate(key=_KEY, confidence=0.95, margin=0.0097)
    assert evaluate_tonal_center(estimate).no_tonal_center is False, (
        "if a median-margin track is declined then broken"
    )
