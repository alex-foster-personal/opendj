"""Acceptance tests for the `no_trackable_pulse` flag."""

from __future__ import annotations

import pytest

from apps.analysis_beatgrid.flags import (
    ACTIVATION_PEAK_THRESHOLD,
    MIN_BEATS,
    REASON_ACTIVATION_BELOW_THRESHOLD,
    REASON_NO_DOWNBEAT_ANCHOR,
    REASON_TOO_FEW_BEATS,
    evaluate_pulse,
)


def _beats(n: int) -> list[float]:
    return [i * 0.47 for i in range(n)]


def test_a_confident_full_grid_is_not_flagged():
    flag = evaluate_pulse(_beats(90), activation_peak=0.98)
    assert flag.no_trackable_pulse is False
    assert flag.reason is None


def test_too_few_beats_is_flagged_with_its_own_reason():
    flag = evaluate_pulse(_beats(MIN_BEATS - 1), activation_peak=0.9)
    assert flag.no_trackable_pulse is True
    assert flag.reason == REASON_TOO_FEW_BEATS


def test_exactly_the_minimum_beat_count_passes():
    """The boundary, pinned in the passing direction so an off-by-one is visible."""
    assert evaluate_pulse(_beats(MIN_BEATS), activation_peak=0.9).no_trackable_pulse is False


def test_a_low_activation_peak_is_flagged_even_with_plenty_of_beats():
    """The two symptoms are independent, so the beat count must not mask the peak."""
    flag = evaluate_pulse(_beats(90), activation_peak=ACTIVATION_PEAK_THRESHOLD - 0.01)
    assert flag.no_trackable_pulse is True
    assert flag.reason == REASON_ACTIVATION_BELOW_THRESHOLD


def test_the_peak_reason_wins_when_both_symptoms_are_present():
    """Cause before consequence: too few beats is what a low peak produces."""
    flag = evaluate_pulse(_beats(2), activation_peak=0.1)
    assert flag.reason == REASON_ACTIVATION_BELOW_THRESHOLD


def test_a_missing_activation_peak_fails_loud():
    """No hidden default. Absent evidence must not read as confidence."""
    with pytest.raises(ValueError, match="activation_peak is required"):
        evaluate_pulse(_beats(90), activation_peak=None)


def test_an_empty_beat_list_is_flagged_not_silently_accepted():
    flag = evaluate_pulse([], activation_peak=0.9)
    assert flag.no_trackable_pulse is True
    assert flag.n_beats == 0


def test_a_lowered_threshold_changes_the_verdict_and_is_reported():
    """The threshold is the round-level experiment lever; it must be a parameter."""
    beats = _beats(90)
    assert evaluate_pulse(beats, 0.42).no_trackable_pulse is True
    assert evaluate_pulse(beats, 0.42, threshold=0.35).no_trackable_pulse is False


# ----- The no-downbeat-anchor case (Codex P1 on PR #1514) -----------------


def test_beats_without_a_downbeat_anchor_are_failed_not_published():
    """A grid with no bar-1 cannot carry a 1-to-4 phase, so it is not `ok`.

    Not hypothetical: the committed round-0 artifact has stable id
    ff92cb1da9096b38d47050e448b3131e3922d7db with 22 beats and zero downbeats,
    1 of 337 fixtures. Publishing that as healthy would hand quantize and Beat
    Sync a grid they cannot use.
    """
    flag = evaluate_pulse(_beats(22), activation_peak=0.99, downbeat_times=[])
    assert flag.no_trackable_pulse is True
    assert flag.reason == REASON_NO_DOWNBEAT_ANCHOR
    assert flag.n_downbeats == 0


def test_a_single_downbeat_is_enough_of_an_anchor():
    """The control: one anchor fixes the bar phase, so it must NOT be failed.

    Without this the previous test would pass against an implementation that
    failed every track with fewer downbeats than it liked.
    """
    flag = evaluate_pulse(_beats(90), activation_peak=0.99, downbeat_times=[0.0])
    assert flag.no_trackable_pulse is False
    assert flag.n_downbeats == 1


def test_an_analyzer_with_no_downbeat_concept_is_not_penalised():
    """None means "did not look"; an empty list means "looked and found none".

    Conflating them would make a librosa-style tracker fail for a capability it
    never claimed, which is the honest-denominators rule applied to capability.
    """
    flag = evaluate_pulse(_beats(90), activation_peak=0.99, downbeat_times=None)
    assert flag.no_trackable_pulse is False
    assert flag.n_downbeats is None
