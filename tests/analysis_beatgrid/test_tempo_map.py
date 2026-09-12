"""Acceptance tests for the v2 piecewise-constant tempo map fitter."""

from __future__ import annotations

import itertools
import random

import numpy as np
import pytest

from apps.analysis_beatgrid.activations import ActivationsMissing, fit_dynamic_grid
from apps.analysis_beatgrid.bpm import least_squares_bpm
from apps.analysis_beatgrid.tempo_change import (
    MIN_SEGMENT_BEATS,
    TempoAnalysis,
    detect_tempo_changes,
)
from apps.analysis_beatgrid.tempo_map import (
    TempoAnchor,
    TempoMap,
    apply_tempo_map,
    fit_tempo_map,
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


def _ramped_beat_times(
    bpm: float,
    seconds: float,
    ramp_at_s: float,
    ramp_over_s: float,
    factor: float,
) -> list[float]:
    times: list[float] = []
    t = 0.0
    while t < seconds:
        times.append(t)
        if t < ramp_at_s:
            current = bpm
        elif t >= ramp_at_s + ramp_over_s:
            current = bpm * factor
        else:
            progress = (t - ramp_at_s) / ramp_over_s
            current = bpm * (1.0 + (factor - 1.0) * progress)
        t += 60.0 / current
    return times


def _ramped_4pct() -> list[float]:
    return _ramped_beat_times(128.0, 200.0, ramp_at_s=90.0, ramp_over_s=30.0, factor=1.04)


def _ref(tmp_path) -> dict[str, str]:
    path = tmp_path / "tiny.npz"
    np.savez_compressed(
        path,
        beat=np.zeros(8, dtype=np.float16),
        downbeat=np.zeros(8, dtype=np.float16),
        fps=np.int32(50),
    )
    return {"npz": str(path), "fps": 50}


def _jittered(times: list[float], sigma_s: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    out = [t + rng.gauss(0.0, sigma_s) for t in times]
    return sorted(out)


def v1_static_grid(times: list[float]) -> list[float]:
    bpm, _ = least_squares_bpm(times)
    n = len(times)
    mean_i, mean_t = (n - 1) / 2.0, sum(times) / n
    period = 60.0 / bpm
    intercept = mean_t - period * mean_i
    return [intercept + period * i for i in range(n)]


def _assert_anchor_invariants(tempo_map: TempoMap) -> None:
    anchors = tempo_map.anchors
    assert anchors, "expected at least one anchor"
    assert anchors[0].beat_index == 0
    for anchor in anchors:
        assert 0 < anchor.confidence <= 1.0
        assert anchor.residual_rms_s >= 0.0
    for earlier, later in itertools.pairwise(anchors):
        assert later.at_s > earlier.at_s
        assert later.beat_index > earlier.beat_index


def _assert_brackets_ramp(tempo_map: TempoMap, *, ramp_at_s: float = 90.0, ramp_end_s: float = 120.0) -> None:
    tolerance_s = 15.0
    anchors = tempo_map.anchors
    assert len(anchors) >= 2
    assert any(a.at_s <= ramp_at_s for a in anchors)
    later = [a for a in anchors if ramp_at_s - tolerance_s <= a.at_s <= ramp_end_s + tolerance_s]
    assert later, f"no anchor near ramp window; anchors at {[a.at_s for a in anchors]}"
    opening_bpm = anchors[0].bpm
    ramp_anchor = later[-1]
    assert ramp_anchor.bpm > opening_bpm
    assert ramp_anchor.bpm / opening_bpm == pytest.approx(1.04, rel=0.005)


def test_detect_tempo_changes_still_returns_v1_tempo_analysis_not_tempo_anchor(tmp_path) -> None:
    """[if] the 4 percent ramp series is segmented [then] v1 still returns TempoAnalysis."""
    times = _ramped_4pct()
    result = detect_tempo_changes(times)
    assert isinstance(result, TempoAnalysis)
    assert not isinstance(result, TempoMap)
    if result.markers:
        marker = result.markers[0]
        assert hasattr(marker, "bpm_before")
        assert hasattr(marker, "bpm_after")
        assert not isinstance(marker, TempoAnchor)


def test_four_percent_ramp_emits_anchors_bracketing_the_ramp(tmp_path) -> None:
    """[if] a fixture has a real 4 percent tempo ramp between 1:30 and 2:00 [then] bracket."""
    times = _ramped_4pct()
    intervals = [b - a for a, b in itertools.pairwise(times)]
    steps = [abs(b - a) / a for a, b in itertools.pairwise(intervals)]
    assert max(steps) < 0.01, "fixture must ramp, not step"

    tempo_map = fit_tempo_map(_ref(tmp_path), times)
    _assert_brackets_ramp(tempo_map)


def test_fixed_tempo_returns_exactly_one_anchor(tmp_path) -> None:
    """[if] a track is fixed tempo [then] the fitter returns exactly one anchor."""
    times = _steady(128.0, 400)
    tempo_map = fit_tempo_map(_ref(tmp_path), times)
    assert len(tempo_map.anchors) == 1

    for seed in range(8):
        jittered = _jittered(_steady(128.0, 400), sigma_s=0.010, seed=seed)
        jittered_map = fit_tempo_map(_ref(tmp_path), jittered)
        assert len(jittered_map.anchors) == 1, f"false split on seed {seed}"


def test_every_emitted_anchor_has_confidence_residual_and_monotone_indices(tmp_path) -> None:
    """[if] an anchor is emitted [then] confidence, residual, and strict monotonicity hold."""
    fixtures = [
        fit_tempo_map(_ref(tmp_path), _ramped_4pct()),
        fit_tempo_map(_ref(tmp_path), _steady(128.0, 400)),
        fit_tempo_map(_ref(tmp_path), _two_tempos(128.0, 200, 128.0 * 1.03, 200)),
        fit_tempo_map(_ref(tmp_path), _steady(128.0, 2 * MIN_SEGMENT_BEATS)),
    ]
    for tempo_map in fixtures:
        _assert_anchor_invariants(tempo_map)


def test_single_anchor_apply_matches_v1_static_grid(tmp_path) -> None:
    """[if] a single-anchor map is applied to the same beats as v1 [then] times match."""
    for times in (_steady(128.0, 400), _jittered(_steady(128.0, 400), 0.010, seed=3)):
        tempo_map = fit_tempo_map(_ref(tmp_path), times)
        assert len(tempo_map.anchors) == 1
        anchor = tempo_map.anchors[0]
        fit = least_squares_bpm(times)
        assert fit is not None
        assert anchor.bpm == pytest.approx(fit[0], abs=1e-9)
        assert anchor.residual_rms_s == pytest.approx(fit[1], abs=1e-9)
        reconstructed = apply_tempo_map(tempo_map, len(times))
        expected = v1_static_grid(times)
        assert reconstructed == pytest.approx(expected, abs=1e-9)


def test_refuse_without_activations() -> None:
    """[if] activations are missing [then] the fitter refuses with ActivationsMissing."""
    beats = _steady(128.0, 64)
    with pytest.raises(ActivationsMissing):
        fit_tempo_map({"features_blob": {}}, beats)
    with pytest.raises(ActivationsMissing):
        fit_dynamic_grid({"features_blob": {}})


def test_short_track_returns_one_anchor(tmp_path) -> None:
    """[if] the track cannot hold two minimum segments [then] exactly one anchor."""
    times = _steady(128.0, 2 * MIN_SEGMENT_BEATS)
    tempo_map = fit_tempo_map(_ref(tmp_path), times)
    assert len(tempo_map.anchors) == 1


def test_fit_dynamic_grid_returns_tempo_map_not_activation_arrays(tmp_path) -> None:
    """[if] activations and beats are present [then] fit_dynamic_grid returns a TempoMap."""
    ref = _ref(tmp_path)
    beats = _steady(128.0, 64)
    result = fit_dynamic_grid(ref, beats)
    assert isinstance(result, TempoMap)
    assert not isinstance(result, dict)
