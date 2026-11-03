"""Tempo stability derivation (issue #8339): the lane's quality signal fixtures.

[if] a steady grid gets segments or a real tempo move goes unmarked [then] broken, [else stop].

Hygiene is mimicked, not calibrated, so each test pins a named fixture from
the issue's "Proposed quality signal" table. Every detection test has a
control that must stay quiet, because a classifier that marks everything
would pass a detection-only suite.
"""

from __future__ import annotations

import math

import pytest

from apps.analysis_beatgrid.tempo_change import MIN_SEGMENT_BARS
from apps.analysis_beatgrid.tempo_stability import (
    RAMP_MIN_PCT_PER_BAR,
    tempo_stability,
)

pytestmark = pytest.mark.requirement("TEMPOLANE-8342")

BPM = 128.0
EIGHT_BARS_S = MIN_SEGMENT_BARS * 4 * 60.0 / BPM


def _steady(bpm: float, seconds: float) -> list[float]:
    period = 60.0 / bpm
    return [i * period for i in range(int(seconds / period))]


def _ramped(at_s: float, over_s: float, factor: float, seconds: float = 200.0) -> list[float]:
    """The NATIVE-03 fixture: a click at BPM, ramping by `factor` from `at_s`."""
    times: list[float] = []
    t = 0.0
    while t < seconds:
        times.append(t)
        progress = min(1.0, max(0.0, (t - at_s) / over_s))
        t += 60.0 / (BPM * (1.0 + (factor - 1.0) * progress))
    return times


def _stepped(factor: float) -> list[float]:
    times = _steady(BPM, 100.0)
    period, last = 60.0 / (BPM * factor), times[-1]
    times.extend(last + (i + 1) * period for i in range(200))
    return times


def _rekordbox_dynamic() -> tuple[list[float], list[float]]:
    """A rekordbox-style dynamic grid: per-beat BPM steps, ms-rounded times.

    PQTZ stores a tempo per beat and a time in whole ms, so this is the shape
    such a grid reaches the route in, with no `tempo_changes` key anywhere.
    """
    bpms = [124.0] * 160 + [124.0 + 0.25 * i for i in range(1, 25)] + [130.0] * 200
    times, t = [], 0.0
    for bpm in bpms:
        times.append(round(t, 3))
        t += 60.0 / bpm
    return times, bpms


def test_steady_track_has_no_segments() -> None:
    """[if] a fixed 128 BPM click grid yields any segment [then] the lane paints a steady track, [else stop]."""
    result = tempo_stability(_steady(BPM, 240.0))
    assert result.verdict == "steady"
    assert result.segments == ()
    assert result.nominal_bpm == pytest.approx(BPM)


def test_native03_ramp_marks_one_faster_stretch_within_eight_bars() -> None:
    """[if] the 3 percent ramp at 2:00 gets no faster ramp or jump starting within 8 bars of 2:00 [then] the lane misses it, [else stop]."""
    result = tempo_stability(_ramped(120.0, EIGHT_BARS_S, 1.03))
    moving = [s for s in result.segments if s.kind != "steady"]
    assert moving, "the ramp must yield a non-steady segment"
    first = moving[0]
    assert first.kind in ("ramp", "jump")
    assert abs(first.start_s - 120.0) <= EIGHT_BARS_S
    assert first.direction == "faster"
    assert result.verdict == "changes"
    assert result.largest_change_pct == pytest.approx(3.0, abs=0.1)
    # Control: the stretch before the ramp is steady and marked as such.
    assert result.segments[0].kind == "steady"
    assert result.segments[0].end_s == pytest.approx(first.start_s)


def test_held_step_reads_jump_not_ramp() -> None:
    """[if] a single 5 percent step reads as a ramp [then] jump and ramp are indistinguishable, [else stop]."""
    result = tempo_stability(_stepped(1.05))
    kinds = {s.kind for s in result.segments}
    assert "ramp" not in kinds
    assert "jump" in kinds


def test_slow_drift_reads_ramp_with_direction() -> None:
    """[if] a slow 4 percent slowdown over 32 bars reads anything but a slower ramp [then] drift has no direction, [else stop]."""
    over_s = 4 * EIGHT_BARS_S
    assert 0.04 / (over_s / (4 * 60.0 / BPM)) * 100 >= RAMP_MIN_PCT_PER_BAR
    result = tempo_stability(_ramped(60.0, over_s, 0.96, seconds=240.0))
    ramps = [s for s in result.segments if s.kind == "ramp"]
    assert ramps and all(s.direction == "slower" for s in ramps)
    assert ramps[0].bpm_end < ramps[0].bpm_start


def test_rekordbox_dynamic_grid_yields_a_non_steady_segment() -> None:
    """[if] a rekordbox dynamic grid with no tempo_changes key yields only steady segments [then] dynamic grids stay unmarked, [else stop]."""
    times, bpms = _rekordbox_dynamic()
    result = tempo_stability(times, bpms)
    assert any(s.kind != "steady" for s in result.segments)
    assert result.verdict != "steady"


def _wandering(amplitude: float, period_bars: int, beats: int = 1000) -> list[float]:
    """Tempo wandering sinusoidally by `amplitude`: never 1 percent off, never ramping."""
    times, t = [], 0.0
    for i in range(beats):
        times.append(t)
        t += 60.0 / (BPM * (1.0 + amplitude * math.sin(2 * math.pi * i / (4 * period_bars))))
    return times


def test_variable_tempo_grid_never_reads_steady() -> None:
    """[if] a variable_tempo grid reads steady [then] the #7879 hard veto is broken, [else stop]."""
    result = tempo_stability(_wandering(0.008, 64))
    # Every other gate is quiet on this grid, so only the veto can keep it off steady.
    assert result.change_count == 0
    assert all(segment.kind == "steady" for segment in result.segments)
    assert result.grid_class == "variable_tempo"
    assert result.verdict == "drift"


def test_jittered_beats_read_unstable_only_past_the_wobble_floor() -> None:
    """[if] tracker wobble the detector calls steady reads unstable, or real chaos does not [then] the jitter gate is wrong, [else stop]."""
    import random

    rng = random.Random(7)
    base = _steady(BPM, 200.0)
    wobble = sorted(t + rng.gauss(0.0, 0.004) for t in base)
    assert tempo_stability(wobble).verdict == "steady"
    chaos = base[:200] + sorted(t + rng.gauss(0.0, 0.04) for t in base[200:260]) + base[260:]
    chaos = sorted(set(chaos))
    kinds = [s.kind for s in tempo_stability(chaos).segments]
    assert "unstable" in kinds


def test_half_time_grid_is_a_fold_not_a_tempo_change() -> None:
    """[if] a grid folded to half time reads as a 50 percent tempo change [then] octave errors look like drama, [else stop]."""
    base = _steady(BPM, 90.0)
    period, last = 60.0 / (BPM / 2), base[-1]
    folded = base + [last + (i + 1) * period for i in range(40)]
    period, last = 60.0 / BPM, folded[-1]
    times = folded + [last + (i + 1) * period for i in range(200)]
    result = tempo_stability(times)
    assert result.largest_change_pct < 1.0
    assert result.change_count == 0
    assert result.verdict != "changes"
    assert any(s.kind == "unstable" and s.direction == "none" for s in result.segments)
    assert not any(s.kind in ("jump", "ramp") for s in result.segments)
    # Control: a real 5 percent step is still a change, never mistaken for a fold.
    assert tempo_stability(_stepped(1.05)).change_count >= 1


def test_fewer_than_two_beats_raises() -> None:
    """[if] one beat yields a verdict instead of an error [then] not-analyzed tracks get invented answers, [else stop]."""
    with pytest.raises(ValueError, match="at least 2 beats"):
        tempo_stability([0.0])
