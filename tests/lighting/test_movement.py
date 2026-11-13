"""Tests for apps.lighting.movement pan/tilt path generators.

[if] a movement shape is sampled over a cycle [then] it stays in limits, loops, spreads and respects speed, [else stop].
"""

from __future__ import annotations

import itertools
import math

import pytest

from apps.lighting.movement import (
    SHAPES,
    MovementLimits,
    MovementParams,
    fixture_offset,
    movement,
    offsets_for,
    path_point,
    slew_limit,
)
from apps.lighting.patch import PatchedFixture, StagePosition

pytestmark = [pytest.mark.requirement("LIGHT-14619")]


def test_outputs_stay_in_unit_range_and_fixture_limits() -> None:
    """[if] each shape is sampled over a cycle [then] pan/tilt stay in 0..1 and inside the limits, [else stop]."""
    limits = MovementLimits(pan_min=0.2, pan_max=0.7, tilt_min=0.1, tilt_max=0.4)
    params = MovementParams(bars=2.0, limits=limits)
    for shape in SHAPES:
        for step in range(200):
            out = movement(shape, step / 50.0, step % 4, 4, params)
            assert out.pan is not None and out.tilt is not None
            assert 0.0 <= out.pan <= 1.0 and 0.0 <= out.tilt <= 1.0
            assert limits.pan_min <= out.pan <= limits.pan_max
            assert limits.tilt_min <= out.tilt <= limits.tilt_max


def test_phase_zero_and_one_give_same_point() -> None:
    """[if] a shape is sampled at cycle phase 0 and 1 [then] both points match, [else stop]."""
    for shape in SHAPES:
        a = path_point(shape, 0.0)
        b = path_point(shape, 1.0)
        assert a == pytest.approx(b, abs=1e-12)


def test_offsets_spread_evenly_across_fixtures() -> None:
    """[if] N fixtures get offsets [then] they are index/N, evenly spaced, also for a patch group, [else stop]."""
    n = 5
    offsets = [fixture_offset(i, n) for i in range(n)]
    gaps = [b - a for a, b in itertools.pairwise([*offsets, 1.0])]
    assert gaps == pytest.approx([1.0 / n] * n)
    fixtures = [
        PatchedFixture(f"m{i}", "p", "m", 1, 1 + i * 16, ("movers",), StagePosition(x=(3 - i) / 4, y=0.5))
        for i in range(4)
    ]
    assert offsets_for(fixtures) == {"m3": 0.0, "m2": 0.25, "m1": 0.5, "m0": 0.75}


def test_step_between_30hz_frames_stays_under_speed_limit() -> None:
    """[if] frames at 30 Hz pass through slew_limit [then] each step is within the speed limit, [else stop]."""
    max_step = 0.02
    bpm = 174.0
    params = MovementParams(bars=1.0)
    bars_per_frame = bpm / 60.0 / 4.0 / 30.0
    for shape in SHAPES:
        current = path_point(shape, 0.0, params)
        for frame in range(1, 300):
            target = path_point(shape, frame * bars_per_frame, params)
            nxt = slew_limit(current, target, max_step)
            assert math.hypot(nxt[0] - current[0], nxt[1] - current[1]) <= max_step + 1e-12
            current = nxt
