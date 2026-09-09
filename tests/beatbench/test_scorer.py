"""Acceptance tests for the beat-mapping scorer, on synthetic grids only.

THESE TESTS ARE THE SPEC. The scorer is the one thing every benchmark round
reuses unchanged, so a silent change in what "agreement" means would make two
rounds incomparable while both still looked green. Pinning the semantics on
grids whose correct answer is known by construction is the only way to tell a
scorer regression apart from an analyzer regression.

Synthetic on purpose: a real track cannot prove that a 30 ms constant offset
reads as 30 ms, because no real track has a known-exact answer. A generated
grid does.

Each test states its acceptance criterion in the form "if <scenario> then
<expected>", so a failure names the broken behaviour rather than a bare assert.
"""

from __future__ import annotations

import math

import pytest

from apps.analysis_bench.scorers.beatgrid import (
    BEAT_TOLERANCE_S,
    SCORER_VERSION,
    classify_tempo_relation,
    grid_is_dynamic,
    percentile,
    score_bpm,
    score_downbeats,
    score_positions,
    window_slice,
)

# ----- Synthetic grid builders -------------------------------------------


def click_grid(bpm: float, count: int, offset_s: float = 0.0) -> list[float]:
    """A perfectly regular beat sequence, the reference case."""
    period = 60.0 / bpm
    return [offset_s + i * period for i in range(count)]


def ramp_grid(bpm_start: float, bpm_end: float, count: int) -> list[float]:
    """A grid whose tempo slews linearly, standing in for a dynamic grid."""
    times = [0.0]
    for i in range(1, count):
        bpm = bpm_start + (bpm_end - bpm_start) * (i - 1) / max(count - 2, 1)
        times.append(times[-1] + 60.0 / bpm)
    return times


def as_beats(times: list[float], bpm: float | list[float]) -> list[dict]:
    """Wrap bare times in the rekordbox {n, bpm, t} wire shape."""
    bpms = bpm if isinstance(bpm, list) else [bpm] * len(times)
    return [
        {"n": (i % 4) + 1, "bpm": round(b, 2), "t": round(t, 3)}
        for i, (t, b) in enumerate(zip(times, bpms, strict=False))
    ]


# ----- percentile ---------------------------------------------------------


def test_percentile_matches_linear_interpolation() -> None:
    """If percentile does not interpolate linearly then every p95 is wrong."""
    values = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 50) == pytest.approx(2.0)
    assert percentile(values, 0) == pytest.approx(0.0)
    assert percentile(values, 100) == pytest.approx(4.0)
    assert percentile(values, 75) == pytest.approx(3.0)


def test_percentile_of_empty_is_none() -> None:
    """If an empty series returns a number then a no-data case fakes a score."""
    assert percentile([], 50) is None


# ----- BPM scoring --------------------------------------------------------


def test_exact_bpm_match_passes_every_tolerance() -> None:
    """If a candidate reports rekordbox's stored BPM then all bands pass."""
    verdict = score_bpm(128.0, 128.0)
    assert verdict.within_0_01 and verdict.within_0_1 and verdict.within_1_0
    assert verdict.abs_error == pytest.approx(0.0)
    assert verdict.relation == "same"


def test_bpm_bands_are_nested_and_distinct() -> None:
    """If the bands are not nested then a coarse band could pass while fine fails."""
    near = score_bpm(128.0, 128.05)
    assert not near.within_0_01
    assert near.within_0_1 and near.within_1_0

    loose = score_bpm(128.0, 128.6)
    assert not loose.within_0_01 and not loose.within_0_1
    assert loose.within_1_0


def test_half_and_double_tempo_are_classified_not_counted_as_plain_misses() -> None:
    """If an octave error reads as a plain miss then the failure mode is hidden."""
    assert score_bpm(128.0, 64.0).relation == "half"
    assert score_bpm(128.0, 256.0).relation == "double"
    assert score_bpm(128.0, 64.0).within_1_0 is False
    assert score_bpm(120.0, 180.0).relation == "three_halves"
    assert score_bpm(180.0, 120.0).relation == "two_thirds"


def test_unrelated_tempo_is_relation_none() -> None:
    """If an unrelated tempo picks up an octave label then the label is noise."""
    assert score_bpm(128.0, 97.3).relation == "none"


def test_missing_candidate_bpm_is_none_not_zero() -> None:
    """If a missing BPM scores as 0.0 then a crash reads as a bad answer."""
    verdict = score_bpm(128.0, None)
    assert verdict.abs_error is None
    assert verdict.relation == "absent"
    assert not verdict.within_1_0


def test_classify_tempo_relation_uses_relative_tolerance() -> None:
    """If octave bands used an absolute tolerance then fast tracks would misbind."""
    assert classify_tempo_relation(200.0, 400.5) == "double"
    assert classify_tempo_relation(60.0, 120.1) == "double"


# ----- Beat position scoring ---------------------------------------------


def test_identical_grids_score_zero_offset_and_perfect_f() -> None:
    """If identical grids do not score zero then the metric has a constant bias."""
    ref = click_grid(120.0, 60)
    got = score_positions(ref, list(ref))
    assert got.raw_p50_ms == pytest.approx(0.0, abs=1e-6)
    assert got.raw_p95_ms == pytest.approx(0.0, abs=1e-6)
    assert got.f_measure == pytest.approx(1.0)
    assert got.global_shift_ms == pytest.approx(0.0, abs=1e-6)


def test_constant_offset_shows_as_shift_and_vanishes_when_removed() -> None:
    """If a pure phase offset survives shift removal then jitter is unreadable."""
    ref = click_grid(120.0, 60)
    cand = [t + 0.030 for t in ref]
    got = score_positions(ref, cand)
    assert got.raw_p50_ms == pytest.approx(30.0, abs=0.5)
    assert got.global_shift_ms == pytest.approx(30.0, abs=0.5)
    assert got.shifted_p50_ms == pytest.approx(0.0, abs=1e-6)
    assert got.shifted_p95_ms == pytest.approx(0.0, abs=1e-6)
    assert got.f_measure == pytest.approx(1.0)


def test_offset_beyond_tolerance_destroys_f_measure_but_shift_still_recovers() -> None:
    """If a large phase error hid inside shift removal then the two variants agree wrongly."""
    ref = click_grid(120.0, 60)
    cand = [t + 0.200 for t in ref]
    got = score_positions(ref, cand)
    assert got.raw_p50_ms == pytest.approx(200.0, abs=1.0)
    assert got.f_measure == pytest.approx(0.0)
    assert got.shifted_p50_ms == pytest.approx(0.0, abs=1e-6)


def test_jitter_survives_shift_removal() -> None:
    """If jitter vanished with the shift then noise and phase are conflated."""
    ref = click_grid(120.0, 61)
    cand = [t + (0.020 if i % 2 else -0.020) for i, t in enumerate(ref)]
    got = score_positions(ref, cand)
    assert got.global_shift_ms == pytest.approx(0.0, abs=25.0)
    assert got.shifted_p95_ms > 15.0


def test_half_tempo_candidate_reports_beat_count_ratio() -> None:
    """If a half-tempo grid scored well on offsets then the ratio must expose it."""
    ref = click_grid(120.0, 60)
    cand = click_grid(60.0, 30)
    got = score_positions(ref, cand)
    assert got.beat_count_ratio == pytest.approx(0.5, abs=0.02)
    assert got.f_measure < 0.75


def test_empty_candidate_is_honest_zero_not_a_crash() -> None:
    """If an analyzer returning nothing crashed the scorer then the round dies."""
    got = score_positions(click_grid(120.0, 60), [])
    assert got.f_measure == pytest.approx(0.0)
    assert got.raw_p50_ms is None
    assert got.matched == 0


def test_empty_reference_fails_loud() -> None:
    """If a track with no rekordbox grid scored at all then ground truth is faked."""
    with pytest.raises(ValueError):
        score_positions([], click_grid(120.0, 10))


def test_f_measure_matching_is_one_to_one() -> None:
    """If matching were many-to-one then duplicated beats would inflate recall."""
    ref = click_grid(120.0, 10)
    cand = [t + d for t in ref for d in (-0.005, 0.005)]
    got = score_positions(ref, cand)
    assert got.matched == 10
    assert got.precision == pytest.approx(0.5)
    assert got.recall == pytest.approx(1.0)


# ----- Downbeat scoring ---------------------------------------------------


def test_correct_downbeats_agree_fully() -> None:
    """If aligned downbeats do not score 1.0 then bar-1 agreement is unusable."""
    ref = click_grid(120.0, 16)[::4]
    got = score_downbeats(ref, list(ref))
    assert got.supported is True
    assert got.agreement == pytest.approx(1.0)


def test_downbeats_off_by_one_beat_score_zero() -> None:
    """If an off-by-one-beat bar phase still agreed then downbeat has no meaning."""
    ref = click_grid(120.0, 16)[::4]
    cand = [t + 0.5 for t in ref]
    got = score_downbeats(ref, cand)
    assert got.agreement == pytest.approx(0.0)


def test_analyzer_without_downbeats_is_not_applicable_not_zero() -> None:
    """If a no-downbeat analyzer scored 0.0 then N/A is misreported as failure."""
    got = score_downbeats(click_grid(120.0, 16)[::4], None)
    assert got.supported is False
    assert got.agreement is None


# ----- Dynamic grid detection --------------------------------------------


def test_constant_grid_is_not_dynamic() -> None:
    """If a fixed grid read as dynamic then the split is meaningless."""
    assert grid_is_dynamic(as_beats(click_grid(128.0, 40), 128.0)) is False


def test_multi_tempo_grid_is_dynamic() -> None:
    """If a real tempo change did not read as dynamic then the split misses it."""
    beats = as_beats(click_grid(128.0, 40), [128.0] * 20 + [130.0] * 20)
    assert grid_is_dynamic(beats) is True


def test_tempo_ramp_grid_is_dynamic() -> None:
    """If a slewing tempo did not read as dynamic then ramps hide in the fixed bucket."""
    times = ramp_grid(120.0, 132.0, 60)
    bpms = [60.0 / (times[i + 1] - times[i]) for i in range(len(times) - 1)] + [132.0]
    assert grid_is_dynamic(as_beats(times, bpms)) is True


def test_ramp_reference_is_tracked_by_a_matching_candidate() -> None:
    """If the scorer could not follow a ramp then dynamic tracks are unscoreable."""
    times = ramp_grid(120.0, 132.0, 80)
    got = score_positions(times, list(times))
    assert got.f_measure == pytest.approx(1.0)
    assert got.raw_p95_ms == pytest.approx(0.0, abs=1e-6)


def test_fixed_tempo_candidate_drifts_against_a_ramp() -> None:
    """If a fixed grid matched a ramp perfectly then dynamic error is invisible."""
    times = ramp_grid(120.0, 140.0, 120)
    fixed = click_grid(120.0, 120)
    got = score_positions(times, fixed)
    assert got.raw_p95_ms > 70.0
    assert got.f_measure < 0.9


# ----- Windowing ----------------------------------------------------------


def test_window_slice_is_half_open_and_inclusive_of_start() -> None:
    """If the window bounds drifted then two candidates score different spans."""
    times = click_grid(60.0, 10)
    assert window_slice(times, 2.0, 5.0) == [2.0, 3.0, 4.0]


def test_scorer_version_is_pinned() -> None:
    """If the version is not a concrete string then rounds cannot be compared."""
    assert isinstance(SCORER_VERSION, str) and SCORER_VERSION
    assert math.isclose(BEAT_TOLERANCE_S, 0.070)
