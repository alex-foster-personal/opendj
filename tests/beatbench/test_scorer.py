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

import inspect
import math
from pathlib import Path

import pytest

from apps.analysis_bench import rounds
from apps.analysis_bench.scorers import beatgrid_lane
from apps.analysis_bench.scorers.beatgrid import (
    BEAT_TOLERANCE_S,
    FIXED_TEMPO_F_REGRESSION_TOL,
    SCORER_VERSION,
    WEIGHTS_NOT_RELEASED,
    FixedTempoFRegression,
    apply_fixed_tempo_f_guard,
    classify_tempo_relation,
    evaluate_fixed_tempo_f_shift,
    grid_is_dynamic,
    partition_counts,
    percentile,
    score_bpm,
    score_downbeats,
    score_positions,
    window_slice,
)
from scripts.beatbench import report as beatgrid_report

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


# ----- Fixture triples and eval-time partition (#2046) --------------------


def _fixture_triples(times: list[float], bpms: float | list[float]) -> list[list]:
    """Compact ``[n, t, bpm]`` ref_beats as fixtures carry them."""
    bpm_list = bpms if isinstance(bpms, list) else [bpms] * len(times)
    return [
        [(i % 4) + 1, round(t, 3), round(b, 2)]
        for i, (t, b) in enumerate(zip(times, bpm_list, strict=False))
    ]


def _minimal_fixture(
    *,
    is_dynamic: bool,
    ref_beats: list[list],
    stable_id: str = "t1",
) -> dict:
    return {
        "stable_id": stable_id,
        "is_dynamic": is_dynamic,
        "ref_beats": ref_beats,
        "rb_bpm": ref_beats[0][2],
        "score_start_s": 0.0,
        "score_end_s": 9999.0,
        "window_start_s": 0.0,
    }


def test_grid_is_dynamic_accepts_fixture_triples() -> None:
    """If fixture triples are rejected then eval-time scoring on real fixtures raises."""
    constant = _fixture_triples(click_grid(128.0, 40), 128.0)
    two_tempi = _fixture_triples(click_grid(128.0, 40), [128.0] * 20 + [130.0] * 20)
    assert grid_is_dynamic(constant) is False
    assert grid_is_dynamic(two_tempi) is True


def test_partition_counts_ignore_lying_stored_flag() -> None:
    """If the stored is_dynamic flag drives the split then the re-count is wrong."""
    constant = _fixture_triples(click_grid(128.0, 60), 128.0)
    two_tempi = _fixture_triples(click_grid(128.0, 60), [128.0] * 30 + [130.0] * 30)
    fixtures = [
        _minimal_fixture(is_dynamic=True, ref_beats=constant, stable_id="lies-fixed"),
        _minimal_fixture(is_dynamic=False, ref_beats=two_tempi, stable_id="lies-dynamic"),
    ]
    n_fixed, n_dynamic = partition_counts([f["ref_beats"] for f in fixtures])
    assert (n_fixed, n_dynamic) == (1, 1)
    assert 138 not in (n_fixed, n_dynamic)


def test_fixed_and_dynamic_averages_are_not_blended() -> None:
    """If dynamic rows fold into the fixed average then the lane figure is misleading."""
    fixed_times = click_grid(120.0, 60)
    ramp_times = ramp_grid(120.0, 140.0, 60)
    ramp_bpms = [60.0 / (ramp_times[i + 1] - ramp_times[i]) for i in range(len(ramp_times) - 1)]
    ramp_bpms.append(140.0)
    fixed_fixture = _minimal_fixture(
        is_dynamic=False,
        ref_beats=_fixture_triples(fixed_times, 120.0),
        stable_id="fixed-track",
    )
    dynamic_fixture = _minimal_fixture(
        is_dynamic=True,
        ref_beats=_fixture_triples(ramp_times, ramp_bpms),
        stable_id="dynamic-track",
    )
    perfect = {"beats": fixed_times, "downbeats": None, "native_bpm": 120.0}
    rows = [
        beatgrid_report.score_track(fixed_fixture, perfect),
        beatgrid_report.score_track(dynamic_fixture, perfect),
    ]
    rows = [r for r in rows if r is not None]
    fixed_cell = beatgrid_report.aggregate([r for r in rows if not r["is_dynamic"]])
    dynamic_cell = beatgrid_report.aggregate([r for r in rows if r["is_dynamic"]])
    assert fixed_cell["n"] == 1
    assert dynamic_cell["n"] == 1
    assert fixed_cell["f_measure_mean"] == pytest.approx(1.0)
    assert dynamic_cell["f_measure_mean"] < 0.9
    blended = (fixed_cell["f_measure_mean"] + dynamic_cell["f_measure_mean"]) / 2
    assert fixed_cell["f_measure_mean"] != pytest.approx(blended)
    assert dynamic_cell["f_measure_mean"] != pytest.approx(blended)


def test_rendered_table_shows_weights_not_released() -> None:
    """If masked-diffusion Beat This! is omitted or shows as n=0 then the table hides absence."""
    cells = [("librosa", {"n": 1, "bpm_exact_0_01_pct": 0.0, "bpm_within_0_1_pct": 0.0,
                          "bpm_within_1_0_pct": 0.0, "octave_half_pct": 0.0,
                          "octave_double_pct": 0.0,
                          "f_measure_mean": 0.5, "f_measure_shifted_mean": 0.5,
                          "cmlt_mean": None, "amlt_mean": None, "n_continuity_scored": 0,
                          "raw_p50_ms": None, "raw_p95_ms": None,
                          "shifted_p50_ms": None, "shifted_p95_ms": None,
                          "downbeat_agreement_mean": None}, {"emits_downbeats": False})]
    md = "\n".join(beatgrid_report.render_table("fixed grids", cells))
    assert "Masked Diffusion Beat This!" in md
    assert WEIGHTS_NOT_RELEASED in md
    assert "| Masked Diffusion Beat This! | 0 |" not in md

    lane_md = beatgrid_lane.render_table({
        "arms": {
            "librosa": {
                "role": "negative_control",
                "fixed": cells[0][1],
                "dynamic": {"n": 0},
                "emits_downbeats": False,
            },
        },
    })
    assert "Masked Diffusion Beat This!" in lane_md
    assert WEIGHTS_NOT_RELEASED in lane_md


# ----- Fixed-tempo F regression guard (NATIVE-01, issue #2053) -----------


def _thin_as_raised_threshold(times: list[float], stride: int = 8) -> list[float]:
    """Stand-in for raising the `minimal` keep-threshold: drop every stride-th beat."""
    return [t for i, t in enumerate(times) if i % stride != 0]


def _report(*, candidate_f: float, promotion_figure: float | None) -> dict:
    payload = {
        "lane": "beatgrid",
        "scorer_version": SCORER_VERSION,
        "bundle": {"lane": "beatgrid", "version": "v1", "bundle_id": "abc123"},
        "table": "| candidate | n |\n|---|---|\n| beat_this | 1 |",
        "arms": {
            "beat_this": {"role": "candidate", "fixed": {"f_measure_mean": candidate_f}},
            "truth_offset": {"role": "positive_control"},
            "constant_128": {"role": "negative_control"},
        },
    }
    if promotion_figure is not None:
        payload["promotion_figure"] = promotion_figure
    return payload


@pytest.mark.requirement("NATIVE-01")
def test_evaluate_shift_greater_than_tol_is_regression() -> None:
    """[if] F shift >0.01 [then] regression recorded, promotion skipped, [else stop]."""
    shift = evaluate_fixed_tempo_f_shift(1.0, 0.989)
    assert shift.is_regression is True
    assert shift.delta == pytest.approx(0.011)


@pytest.mark.requirement("NATIVE-01")
def test_evaluate_shift_of_exactly_0_01_is_not_a_regression() -> None:
    """[if] the change moves F by 0.01 or less [then] the round proceeds as today, [else stop]."""
    shift = evaluate_fixed_tempo_f_shift(1.0, 0.99)
    assert shift.is_regression is False
    assert evaluate_fixed_tempo_f_shift(1.0, 1.0).is_regression is False


@pytest.mark.requirement("NATIVE-01")
def test_a_threshold_change_that_moves_fixed_f_by_more_than_0_01_is_a_regression(
    tmp_path: Path,
) -> None:
    """[if] F shift >0.01 [then] regression recorded, promotion skipped, [else stop]."""
    ref = click_grid(128.0, 64)
    baseline_f = score_positions(ref, ref).f_measure
    mutated_f = score_positions(ref, _thin_as_raised_threshold(ref)).f_measure
    assert abs(mutated_f - baseline_f) > FIXED_TEMPO_F_REGRESSION_TOL
    report = _report(candidate_f=mutated_f, promotion_figure=baseline_f)
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n", encoding="utf-8")
    before = log.read_text(encoding="utf-8")
    with pytest.raises(rounds.RoundError) as excinfo:
        rounds.append_round(log, "beatgrid", report, floor=2, host="test")
    assert "regression" in str(excinfo.value).lower()
    assert "promotion" in str(excinfo.value).lower()
    assert report["promotion"]["regression"] is True
    assert report["promotion"]["updated"] is False
    assert report["promotion"]["fixed_tempo_f"] == pytest.approx(baseline_f)
    assert report["promotion"]["delta"] > FIXED_TEMPO_F_REGRESSION_TOL
    assert log.read_text(encoding="utf-8") == before


@pytest.mark.requirement("NATIVE-01")
def test_a_threshold_change_that_moves_fixed_f_by_at_most_0_01_proceeds(
    tmp_path: Path,
) -> None:
    """[if] the change moves F by 0.01 or less [then] the round proceeds as today, [else stop]."""
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n", encoding="utf-8")

    report_identical = _report(candidate_f=1.0, promotion_figure=1.0)
    number = rounds.append_round(log, "beatgrid", report_identical, floor=2, host="test")
    assert number == 2
    assert "### beatgrid bench round 2" in log.read_text(encoding="utf-8")
    assert report_identical["promotion"]["regression"] is False
    assert report_identical["promotion"]["updated"] is True
    assert report_identical["promotion"]["fixed_tempo_f"] == pytest.approx(1.0)

    log.write_text("## Experiment log\n", encoding="utf-8")
    report_boundary = _report(candidate_f=0.99, promotion_figure=1.0)
    rounds.append_round(log, "beatgrid", report_boundary, floor=2, host="test")
    assert report_boundary["promotion"]["regression"] is False
    assert report_boundary["promotion"]["updated"] is True
    assert report_boundary["promotion"]["fixed_tempo_f"] == pytest.approx(0.99)


@pytest.mark.requirement("NATIVE-01")
def test_without_a_promotion_figure_the_round_proceeds_as_today(tmp_path: Path) -> None:
    """[if] the change moves F by 0.01 or less [then] the round proceeds as today, [else stop]."""
    report = _report(candidate_f=0.5, promotion_figure=None)
    log = tmp_path / "log.md"
    log.write_text("## Experiment log\n", encoding="utf-8")
    rounds.append_round(log, "beatgrid", report, floor=2, host="test")
    assert "promotion" not in report


@pytest.mark.requirement("NATIVE-01")
def test_removing_the_fixed_tempo_f_guard_goes_red(tmp_path: Path) -> None:
    """[if] the guard is removed [then] its test goes red (mutation control), [else stop]."""
    ref = click_grid(128.0, 64)
    baseline_f = score_positions(ref, ref).f_measure
    mutated_f = score_positions(ref, _thin_as_raised_threshold(ref)).f_measure
    report = _report(candidate_f=mutated_f, promotion_figure=baseline_f)
    with pytest.raises(FixedTempoFRegression):
        apply_fixed_tempo_f_guard(report)
    source = inspect.getsource(evaluate_fixed_tempo_f_shift)
    assert "FIXED_TEMPO_F_REGRESSION_TOL" in source
    assert "is_regression" in source
    posted = inspect.getsource(rounds.append_round)
    assert "apply_fixed_tempo_f_guard" in posted
