"""Colour conversion helpers (issue #14511).

[if] colour helpers convert RGB to RGBW, CMY and HSV [then] values match and round-trip, [else stop].
"""

from __future__ import annotations

import math

import pytest

from apps.lighting.colour_convert import (
    cmy_to_rgb,
    hsv_to_rgb,
    rgb_to_cmy,
    rgb_to_hsv,
    rgb_to_rgbw,
)

TOL = 1.0 / 255.0
pytestmark = [pytest.mark.requirement("LIGHT-14527")]

GRID = [i / 15.0 for i in range(16)]  # 16**3 = 4096 points


def test_rgbw_white_and_red() -> None:
    """[if] white or pure red is split to RGBW [then] white gives W=1 RGB=0 and red keeps W=0, [else stop]."""
    assert rgb_to_rgbw((1.0, 1.0, 1.0)) == (0.0, 0.0, 0.0, 1.0)
    assert rgb_to_rgbw((1.0, 0.0, 0.0)) == (1.0, 0.0, 0.0, 0.0)


def test_cmy_round_trip() -> None:
    """[if] rgb goes to cmy and back over a grid [then] every channel is within 1/255, [else stop]."""
    for rgb in ((r, g, b) for r in GRID for g in GRID for b in GRID):
        back = cmy_to_rgb(rgb_to_cmy(rgb))
        assert all(abs(a - b) <= TOL for a, b in zip(rgb, back, strict=True))


def test_hsv_round_trip_4096_grid() -> None:
    """[if] rgb goes to hsv and back over a 4096-point grid [then] it round-trips and hue is in [0, 360), [else stop]."""
    points = [(r, g, b) for r in GRID for g in GRID for b in GRID]
    assert len(points) == 4096
    for rgb in points:
        h, s, v = rgb_to_hsv(rgb)
        assert 0.0 <= h < 360.0
        back = hsv_to_rgb((h, s, v))
        assert all(abs(a - b) <= 1e-9 for a, b in zip(rgb, back, strict=True))
    assert hsv_to_rgb((360.0, 1.0, 1.0)) == pytest.approx(hsv_to_rgb((0.0, 1.0, 1.0)))
    assert hsv_to_rgb((-120.0, 1.0, 1.0)) == pytest.approx((0.0, 0.0, 1.0))


@pytest.mark.parametrize(
    "call",
    [
        lambda: rgb_to_rgbw((math.nan, 0.0, 0.0)),
        lambda: rgb_to_rgbw((1.5, 0.0, 0.0)),
        lambda: rgb_to_cmy((0.0, -0.1, 0.0)),
        lambda: cmy_to_rgb((0.0, 0.0, math.inf)),
        lambda: rgb_to_hsv((0.0, 0.0, 2.0)),
        lambda: hsv_to_rgb((math.inf, 1.0, 1.0)),
        lambda: hsv_to_rgb((0.0, 1.1, 1.0)),
    ],
)
def test_invalid_inputs_raise(call) -> None:
    """[if] an input is non-finite or out of range [then] ValueError is raised, [else stop]."""
    with pytest.raises(ValueError):
        call()
