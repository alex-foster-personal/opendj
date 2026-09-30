"""Acceptance tests for the least-squares tempo fit and the octave policy.

Synthetic beat times only. No real track has a known-exact tempo, so a test
against one would be pinning an opinion; these pin arithmetic.
"""

from __future__ import annotations

import itertools
import math

import pytest

from apps.analysis_beatgrid.bpm import (
    OCTAVE_RANGE_MAX_BPM,
    OCTAVE_RANGE_MIN_BPM,
    REASON_AMBIGUOUS_MODEL_LEVEL,
    REASON_AMBIGUOUS_NEAREST_CENTER,
    REASON_GENRE_FAMILY,
    REASON_NO_OCTAVE_IN_RANGE,
    REASON_PRIOR_SELECTED,
    REASON_SINGLE_OCTAVE_IN_RANGE,
    choose_octave,
    estimate_bpm,
    least_squares_bpm,
)
from apps.analysis_beatgrid.tempo_family import tempo_family_for_genre

FRAME_S = 0.02  # Beat This! emits at 50 fps, so its beat times land on 20 ms.


def _beats(bpm: float, count: int, start: float = 0.0) -> list[float]:
    period = 60.0 / bpm
    return [start + i * period for i in range(count)]


def _quantized(bpm: float, count: int, start: float = 0.0) -> list[float]:
    """The same grid as a frame-based model would actually emit it."""
    return [round(t / FRAME_S) * FRAME_S for t in _beats(bpm, count, start)]


# ----- The fit ------------------------------------------------------------


def test_exact_grid_fits_its_own_tempo():
    bpm, residual = least_squares_bpm(_beats(128.0, 90))
    assert bpm == pytest.approx(128.0, abs=1e-9)
    assert residual == pytest.approx(0.0, abs=1e-9)


def test_least_squares_beats_the_median_interval_on_quantized_beats():
    """The round-0 finding, pinned: quantization is what the median cannot survive.

    This is the regression that motivated scorer v1.1.0. If someone swaps the
    fit back to a median of inter-beat intervals, this test is what catches it,
    so it asserts the median estimator is WORSE on the same input rather than
    merely asserting the fit is good.
    """
    times = _quantized(128.0, 90)
    fitted, _ = least_squares_bpm(times)

    gaps = sorted(b - a for a, b in itertools.pairwise(times))
    median_estimate = 60.0 / gaps[len(gaps) // 2]

    assert abs(fitted - 128.0) < 0.05
    assert abs(median_estimate - 128.0) > abs(fitted - 128.0)


def test_too_few_beats_returns_none_not_a_guess():
    assert least_squares_bpm([0.0, 0.5, 1.0]) is None
    assert least_squares_bpm([]) is None


def test_non_increasing_beats_yield_no_fit():
    assert least_squares_bpm([1.0, 1.0, 1.0, 1.0]) is None


# ----- The octave policy --------------------------------------------------


def test_single_octave_in_range_wins_outright():
    bpm, multiple, reason, ambiguous = choose_octave(128.0)
    assert (bpm, multiple, reason, ambiguous) == (
        128.0,
        1.0,
        REASON_SINGLE_OCTAVE_IN_RANGE,
        False,
    )


def test_out_of_range_tempo_is_doubled_into_the_band():
    bpm, multiple, reason, ambiguous = choose_octave(60.0)
    assert multiple == 2.0
    assert bpm == pytest.approx(120.0)
    assert reason == REASON_SINGLE_OCTAVE_IN_RANGE
    assert ambiguous is False
    assert OCTAVE_RANGE_MIN_BPM <= bpm <= OCTAVE_RANGE_MAX_BPM


def test_two_octaves_inside_the_band_are_flagged_ambiguous():
    """80 and 160 are both inside [70, 180]; the band is wider than one octave."""
    bpm, multiple, reason, ambiguous = choose_octave(80.0)
    assert ambiguous is True
    assert reason == REASON_AMBIGUOUS_MODEL_LEVEL
    # The model's own level wins a two-octave tie: 80 stays 80.
    assert (bpm, multiple) == (pytest.approx(80.0), 1.0)


def test_a_fast_pulse_the_model_tracked_whole_is_not_halved():
    """174 BPM drum and bass: 87 and 174 are both in the band.

    The old tie-break (nearest the band's 112 BPM center) published 87, which
    halved every 163-175 BPM track in round 4 that the model had tracked at
    174. The model's level now wins, so 174 stays 174. Control in the other
    direction: a model that tracked 87 is not doubled without a genre hint.
    """
    fast = choose_octave(174.0)
    assert (fast[0], fast[1], fast[2]) == (pytest.approx(174.0), 1.0, REASON_AMBIGUOUS_MODEL_LEVEL)
    slow = choose_octave(87.0)
    assert (slow[0], slow[1]) == (pytest.approx(87.0), 1.0)


def test_the_center_rule_still_picks_when_the_model_level_is_outside_the_band():
    """Model at 40 BPM: 80 and 160 are in the band, 40 is not; center picks 80."""
    bpm, multiple, reason, ambiguous = choose_octave(40.0)
    assert reason == REASON_AMBIGUOUS_NEAREST_CENTER
    assert ambiguous is True
    assert (bpm, multiple) == (pytest.approx(80.0), 2.0)


def test_a_genre_family_picks_the_octave_inside_its_range_unflagged():
    dnb = tempo_family_for_genre("Drum & Bass")
    assert estimate_bpm(_beats(87.0, 60), family=dnb).bpm == pytest.approx(174.0, abs=0.05)
    _bpm, multiple, reason, ambiguous = choose_octave(87.0, family=dnb)
    assert (multiple, reason, ambiguous) == (2.0, REASON_GENRE_FAMILY, False)
    # Psytrance at 145 tracked as 72.5: the family doubles it.
    psy = tempo_family_for_genre("Psytrance")
    assert choose_octave(72.5, family=psy)[:2] == (pytest.approx(145.0), 2.0)


def test_a_genre_family_with_no_octave_in_range_is_ignored():
    """A DnB tag on a 128 BPM house track (64/128/256 never reach 160-185)."""
    dnb = tempo_family_for_genre("dnb")
    assert choose_octave(128.0, family=dnb) == choose_octave(128.0)


def test_the_scoring_prior_still_outranks_a_genre_family():
    dnb = tempo_family_for_genre("dnb")
    bpm, _m, reason, _a = choose_octave(174.0, prior_bpm=87.0, scoring=True, family=dnb)
    assert (bpm, reason) == (pytest.approx(87.0), REASON_PRIOR_SELECTED)


def test_nothing_in_the_band_is_flagged_rather_than_hidden():
    _bpm, _multiple, reason, ambiguous = choose_octave(1000.0)
    assert reason == REASON_NO_OCTAVE_IN_RANGE
    assert ambiguous is True


def test_prior_breaks_the_tie_when_scoring():
    bpm, _multiple, reason, ambiguous = choose_octave(80.0, prior_bpm=80.0, scoring=True)
    assert bpm == pytest.approx(80.0)
    assert reason == REASON_PRIOR_SELECTED
    assert ambiguous is False


def test_prior_is_refused_at_runtime():
    """The own producer must never read rekordbox's BPM outside scoring.

    A silently ignored prior would be indistinguishable from a respected one at
    the call site, so this must be an error rather than a no-op.
    """
    with pytest.raises(ValueError, match="scoring-only"):
        choose_octave(80.0, prior_bpm=80.0)
    with pytest.raises(ValueError, match="scoring-only"):
        estimate_bpm(_beats(128.0, 40), prior_bpm=128.0)


def test_half_time_candidate_is_recovered_by_the_prior_not_by_luck():
    """A tracker that found every other beat of a 160 BPM track reads 80.

    Both 80 and 160 sit inside the band, so this is the case where the prior
    genuinely decides. The control is the same input WITHOUT the prior: the
    policy has no way to know the answer is 160 and must flag rather than
    pretend, and it must pick the other octave, so a pass here cannot be luck.
    """
    half_time = _beats(80.0, 40)

    scored = estimate_bpm(half_time, prior_bpm=160.0, scoring=True)
    assert scored is not None
    assert scored.bpm == pytest.approx(160.0)
    assert scored.octave_multiple == 2.0
    assert scored.octave_reason == REASON_PRIOR_SELECTED
    assert scored.octave_ambiguous is False

    runtime = estimate_bpm(half_time)
    assert runtime is not None
    assert runtime.octave_ambiguous is True
    assert runtime.bpm == pytest.approx(80.0)


def test_a_half_time_read_below_the_band_needs_no_prior():
    """64 is outside [70, 180] and 128 is the only octave inside, so it is unambiguous."""
    est = estimate_bpm(_beats(64.0, 40))
    assert est is not None
    assert est.bpm == pytest.approx(128.0)
    assert est.octave_ambiguous is False
    assert est.octave_reason == REASON_SINGLE_OCTAVE_IN_RANGE


# ----- The estimate as a whole --------------------------------------------


def test_estimate_carries_the_audit_trail():
    est = estimate_bpm(_quantized(128.0, 90))
    assert est is not None
    assert est.bpm == pytest.approx(128.0, abs=0.05)
    assert est.n_beats == 90
    assert est.octave_multiple == 1.0
    assert est.residual_rms_s < FRAME_S
    assert 0.0 < est.confidence <= 1.0


def test_scattered_beats_lose_confidence_even_at_the_right_tempo():
    """A grid whose beats do not lie on a line is not a confident grid."""
    tight = estimate_bpm(_beats(128.0, 60))
    period = 60.0 / 128.0
    scattered = estimate_bpm(
        [t + (0.2 * period if i % 2 else -0.2 * period) for i, t in enumerate(_beats(128.0, 60))]
    )
    assert tight is not None and scattered is not None
    assert scattered.bpm == pytest.approx(tight.bpm, abs=0.1)
    assert scattered.confidence < tight.confidence


def test_ambiguous_octave_halves_confidence():
    unambiguous = estimate_bpm(_beats(128.0, 60))
    ambiguous = estimate_bpm(_beats(80.0, 60))
    assert unambiguous is not None and ambiguous is not None
    assert ambiguous.octave_ambiguous is True
    assert ambiguous.confidence == pytest.approx(unambiguous.confidence * 0.5, abs=1e-6)


def test_range_constants_span_more_than_one_octave():
    """Documents WHY ambiguity is possible at all, so nobody 'simplifies' it away."""
    assert math.log2(OCTAVE_RANGE_MAX_BPM / OCTAVE_RANGE_MIN_BPM) > 1.0
