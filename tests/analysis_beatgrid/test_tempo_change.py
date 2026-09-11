"""Acceptance tests for changepoint detection over inter-beat intervals.

The two numbers this lane lives or dies by are the DETECTION RATE on real
tempo changes and the FALSE-POSITIVE RATE on fixed-tempo tracks, so the tests
come in pairs: every detection assertion has a control asserting the detector
stays silent on the corresponding unchanged input. A detector that fires on
everything would pass a detection-only suite.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest

from apps.analysis_beatgrid.tempo_change import (
    BIC_PENALTY_MULTIPLIER,
    MIN_SEGMENT_BARS,
    MIN_SEGMENT_BEATS,
    _segment_cost,
    detect_tempo_changes,
)


def _steady(bpm: float, count: int, start: float = 0.0) -> list[float]:
    period = 60.0 / bpm
    return [start + i * period for i in range(count)]


def _two_tempos(bpm_a: float, n_a: int, bpm_b: float, n_b: int) -> list[float]:
    times = _steady(bpm_a, n_a)
    period_b = 60.0 / bpm_b
    last = times[-1]
    times.extend(last + (i + 1) * period_b for i in range(n_b))
    return times


def _jittered(times: list[float], sigma_s: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    out = [t + rng.gauss(0.0, sigma_s) for t in times]
    return sorted(out)


# ----- The control: silence on a fixed tempo ------------------------------


def test_fixed_tempo_yields_no_markers():
    result = detect_tempo_changes(_steady(128.0, 400))
    assert result.markers == ()
    assert result.static_grid_untrusted is False


def test_fixed_tempo_with_realistic_jitter_yields_no_markers():
    """20 ms of frame quantization plus tracker wobble must not read as a change."""
    for seed in range(8):
        times = _jittered(_steady(128.0, 400), sigma_s=0.010, seed=seed)
        result = detect_tempo_changes(times)
        assert result.markers == (), f"false positive on seed {seed}: {result.markers}"


def test_a_single_dropped_beat_is_not_a_tempo_change():
    times = _steady(128.0, 400)
    del times[200]
    assert detect_tempo_changes(times).markers == ()


# ----- Detection ----------------------------------------------------------


def test_three_percent_step_is_detected_with_before_and_after_tempi():
    times = _two_tempos(128.0, 200, 128.0 * 1.03, 200)
    result = detect_tempo_changes(times)

    assert result.static_grid_untrusted is True
    assert len(result.markers) == 1
    marker = result.markers[0]
    assert marker.bpm_before == pytest.approx(128.0, abs=0.05)
    assert marker.bpm_after == pytest.approx(131.84, abs=0.05)
    assert 0.0 < marker.confidence <= 1.0


def test_the_marker_lands_within_eight_bars_of_the_change():
    """The acceptance line from the lane brief, on synthetic beats.

    Eight bars is the minimum segment length, so it is also the finest
    resolution the detector can honestly claim.
    """
    times = _two_tempos(128.0, 200, 128.0 * 1.03, 200)
    change_at_s = times[200]
    tolerance_s = MIN_SEGMENT_BARS * 4 * (60.0 / 128.0)

    result = detect_tempo_changes(times)
    assert any(abs(m.at_s - change_at_s) <= tolerance_s for m in result.markers)


def test_two_changes_produce_two_markers_in_time_order():
    times = _steady(120.0, 150)
    for bpm, count in ((132.0, 150), (110.0, 150)):
        period = 60.0 / bpm
        last = times[-1]
        times.extend(last + (i + 1) * period for i in range(count))

    result = detect_tempo_changes(times)
    assert len(result.markers) == 2
    assert [m.at_s for m in result.markers] == sorted(m.at_s for m in result.markers)
    assert result.markers[0].bpm_before == pytest.approx(120.0, abs=0.1)
    assert result.markers[1].bpm_after == pytest.approx(110.0, abs=0.1)


def test_a_change_smaller_than_the_materiality_floor_is_ignored():
    """0.2 percent is below MIN_RELATIVE_BPM_DELTA, so it must not be reported.

    The mirror of the detection test above: same shape of input, same detector,
    and the only difference is the size of the step.
    """
    times = _two_tempos(128.0, 200, 128.0 * 1.002, 200)
    assert detect_tempo_changes(times).markers == ()


# ----- Boundaries ---------------------------------------------------------


def test_no_marker_is_ever_placed_inside_eight_bars_of_either_end():
    """An invariant, not a value: the minimum segment length bounds placement.

    A change that starts nearer the end than the minimum segment is still
    reported (losing it entirely would be worse), but the marker is pulled to
    the last legal position, so the guarantee is about WHERE markers can land
    rather than about which changes are found.
    """
    times = _two_tempos(128.0, 300, 140.0, MIN_SEGMENT_BEATS - 2)
    result = detect_tempo_changes(times)
    assert result.markers, "a 9 percent step must not vanish just because it is late"
    # at_s is rounded to 4 decimals on the way out, so the bound needs a
    # sub-millisecond allowance; anything larger would be a real violation.
    rounding_s = 1e-3
    for marker in result.markers:
        assert times[MIN_SEGMENT_BEATS] - rounding_s <= marker.at_s
        assert marker.at_s <= times[-1 - MIN_SEGMENT_BEATS] + rounding_s


def test_a_track_too_short_to_hold_two_segments_reports_one_segment():
    times = _steady(128.0, MIN_SEGMENT_BEATS)
    result = detect_tempo_changes(times)
    assert result.markers == ()
    assert len(result.segments) == 1


def test_non_monotonic_beats_fail_loud():
    with pytest.raises(ValueError, match="strictly increasing"):
        detect_tempo_changes([0.0, 1.0, 0.5, 2.0])


def test_segments_tile_the_whole_interval_series():
    """Every interval belongs to exactly one segment; no beat goes unaccounted."""
    times = _two_tempos(120.0, 200, 140.0, 200)
    result = detect_tempo_changes(times)
    edges = [(lo, hi) for lo, hi, _ in result.segments]
    assert edges[0][0] == 0
    assert edges[-1][1] == len(times) - 1
    assert all(a[1] == b[0] for a, b in itertools.pairwise(edges))


# ----- A jitter split must not hide a real tempo change --------------------


def _noisy_segment(bpm: float, count: int, sigma_s: float, seed: int, start: float) -> tuple:
    """`(beat times, next start)` for one segment at a chosen tempo AND jitter."""
    rng = random.Random(seed)
    period = 60.0 / bpm
    times, t = [], start
    for _ in range(count):
        times.append(t + rng.gauss(0.0, sigma_s))
        t += period
    return sorted(times), t


def _jitter_step_then_tempo_step() -> list[float]:
    """Three segments: clean 128, NOISY 128, noisy 130.

    The clean-to-noisy boundary is a pure VARIANCE change at an unchanged mean
    tempo, and it is the highest-BIC-gain split in the whole track. The
    128 -> 130 change is the real one. Verified by direct measurement rather
    than by assumption: over the full track the argmax split lands on the
    variance boundary with a mean-tempo delta of about 0.78 percent, below the
    1 percent materiality floor.
    """
    clean, t1 = _noisy_segment(128.0, 60, 0.001, 3, 0.0)
    noisy, t2 = _noisy_segment(128.0, 60, 0.04, 4, t1)
    faster, _ = _noisy_segment(130.0, 60, 0.04, 5, t2)
    return sorted(clean + noisy + faster)


def test_a_real_change_survives_a_stronger_jitter_split() -> None:
    """The materiality gate must reject a CANDIDATE, never the whole search.

    The detector used to take the single highest-BIC-gain split and hand only
    that one to the materiality gate. When the argmax is a change in tracker
    JITTER at an unchanged tempo, the gate rejected it and `_accepted_split`
    returned None, which also stops the recursion, so a real tempo change lower
    down the gain ranking was never examined and the track was published
    `static_grid_untrusted=False` (Codex P1 BLOCKING on PR #1514).
    """
    result = detect_tempo_changes(_jitter_step_then_tempo_step())

    assert result.static_grid_untrusted is True, (
        "a real tempo change was hidden by a stronger jitter split"
    )
    material = [
        m for m in result.markers
        if abs(m.bpm_after - m.bpm_before) / m.bpm_before >= 0.01
    ]
    assert material, f"no material marker among {[round(m.at_s, 1) for m in result.markers]}"
    assert any(m.bpm_after > m.bpm_before for m in material)


def test_a_jitter_only_change_still_reports_nothing() -> None:
    """The control that keeps the fix honest: variance alone is not a tempo change.

    Without it, the test above could be satisfied by deleting the materiality
    gate, which would make every noisy track report a marker.
    """
    clean, t1 = _noisy_segment(128.0, 60, 0.001, 3, 0.0)
    noisy, t2 = _noisy_segment(128.0, 60, 0.04, 4, t1)
    more, _ = _noisy_segment(128.0, 60, 0.04, 5, t2)

    result = detect_tempo_changes(sorted(clean + noisy + more))

    material = [
        m for m in result.markers
        if abs(m.bpm_after - m.bpm_before) / m.bpm_before >= 0.01
    ]
    assert not material, f"variance-only change produced a tempo marker: {material}"


# ----- Every published marker must clear the gate it claims ----------------

# A six-section track that REPRODUCES the defect, found by searching arrangements
# rather than by guessing one. Two earlier hand-picked fixtures left the pre-fix
# implementation green, which meant they never exercised it: a wide random search
# over section counts, jitter and tempo deltas located this one, where the
# unrevalidated detector publishes a marker of 151.41 -> 151.37 BPM, 0.026
# percent, against a 1 percent floor.
_REPRO_TEMPOS = (157.2346, 154.0294, 151.3666, 154.9914, 151.2539, 144.7885)
_REPRO_LENGTHS = (80, 80, 60, 45, 80, 45)
_REPRO_SIGMA_S = 0.01


def _reproducing_track() -> list[float]:
    times: list[float] = []
    start = 0.0
    for i, (bpm, count) in enumerate(zip(_REPRO_TEMPOS, _REPRO_LENGTHS, strict=True)):
        segment, start = _noisy_segment(bpm, count, _REPRO_SIGMA_S, 170 + i, start)
        times += segment
    return sorted(times)


def _immaterial(markers) -> list:
    return [
        m for m in markers
        if abs(m.bpm_after - m.bpm_before) / m.bpm_before < 0.01
    ]


def test_no_published_marker_violates_the_materiality_floor() -> None:
    """The invariant, asserted over the markers the detector actually emits.

    Materiality is tested while SPLITTING, against the segment being split.
    Recursion then subdivides those segments, so a surviving boundary ends up
    between two NARROWER sections whose mean tempos can differ by less than the
    floor, and the detector published a marker violating its own threshold
    (Codex P1 BLOCKING on PR #1514).

    Written as an INVARIANT over whatever markers come back rather than as a
    pinned marker list, because where the recursion cuts is not the point and a
    pinned list would stop biting the moment it moved.
    """
    result = detect_tempo_changes(_reproducing_track())

    assert not _immaterial(result.markers), (
        "published markers below the 1 percent floor: "
        + str([
            (m.at_s, m.bpm_before, m.bpm_after, m.confidence)
            for m in _immaterial(result.markers)
        ])
    )


def test_the_reproducing_track_still_reports_its_real_changes() -> None:
    """The control: revalidation must drop the invalid markers, not all of them.

    Deleting every marker would satisfy the invariant above completely, so this
    pins that the six-section track is still recognised as non-static.
    """
    result = detect_tempo_changes(_reproducing_track())

    assert result.static_grid_untrusted is True
    assert len(result.markers) >= 2, (
        f"a six-section track collapsed to {len(result.markers)} marker(s)"
    )


def test_the_materiality_invariant_holds_across_several_arrangements() -> None:
    """The same invariant over a spread of tracks, so one case cannot pass by luck."""
    arrangements = [
        (142.97, 145.73, 144.99),
        (128.0, 131.0, 130.4),
        (120.0, 124.0, 123.5),
        (100.0, 103.0, 102.7),
    ]
    for i, tempos in enumerate(arrangements):
        times: list[float] = []
        start = 0.0
        for j, bpm in enumerate(tempos):
            segment, start = _noisy_segment(bpm, 60, 0.004, 40 + i * 10 + j, start)
            times += segment
        result = detect_tempo_changes(sorted(times))
        assert not _immaterial(result.markers), f"arrangement {tempos}"


# ----- The statistical gate must hold after refinement too -----------------

# A seven-section track found by search, not by guessing: under the round-11
# revalidation, which rechecked only the tempo delta, it left a boundary whose
# FINAL adjacent-segment BIC gain was 6.42 against a penalty of 9.61, published
# at confidence 1.0 carried over from a broader ancestor segment.
_BIC_TEMPOS = (98.5661, 94.8887, 92.5967, 90.6602, 88.3786, 91.1863, 87.8941)
_BIC_LENGTHS = (80, 45, 80, 80, 45, 45, 80)
_BIC_SIGMA_S = 0.02


def _bic_repro_track() -> list[float]:
    times: list[float] = []
    start = 0.0
    for i, (bpm, count) in enumerate(zip(_BIC_TEMPOS, _BIC_LENGTHS, strict=True)):
        segment, start = _noisy_segment(bpm, count, _BIC_SIGMA_S, 860 + i, start)
        times += segment
    return sorted(times)


def _final_gain_and_penalty(beats: list[float], segments, index: int) -> tuple[float, float]:
    """Re-derive one boundary's gain and penalty over its FINAL adjacent pair."""
    intervals = [b - a for a, b in itertools.pairwise(beats)]
    lo, split = segments[index][0], segments[index][1]
    hi = segments[index + 1][1]
    penalty = BIC_PENALTY_MULTIPLIER * math.log(hi - lo)
    gain = (
        _segment_cost(intervals, lo, hi)
        - _segment_cost(intervals, lo, split)
        - _segment_cost(intervals, split, hi)
    )
    return gain, penalty


def test_every_published_boundary_still_clears_its_bic_penalty() -> None:
    """A gain earned against a broad ancestor must not survive into a narrow pair.

    The materiality fix rechecked only the relative BPM delta, so a boundary
    whose statistical support evaporated once refinement narrowed its
    neighbours was still published, carrying the ancestor's confidence
    (Codex P1 BLOCKING on PR #1514).
    """
    beats = _bic_repro_track()
    result = detect_tempo_changes(beats)

    for i in range(len(result.markers)):
        gain, penalty = _final_gain_and_penalty(beats, result.segments, i)
        assert gain > penalty, (
            f"marker at {result.markers[i].at_s}s has final BIC gain {gain:.2f} "
            f"against penalty {penalty:.2f}, published at confidence "
            f"{result.markers[i].confidence}"
        )


def test_confidence_is_restated_from_the_final_gain() -> None:
    """Confidence must describe the split that was kept, not an ancestor of it."""
    beats = _bic_repro_track()
    result = detect_tempo_changes(beats)

    for i, marker in enumerate(result.markers):
        gain, penalty = _final_gain_and_penalty(beats, result.segments, i)
        expected = round(1.0 - math.exp(-max(0.0, gain - penalty) / penalty), 4)
        assert marker.confidence == pytest.approx(expected, abs=1e-4), (
            f"marker at {marker.at_s}s reports confidence {marker.confidence}, "
            f"but its final gain {gain:.2f} over penalty {penalty:.2f} gives {expected}"
        )
