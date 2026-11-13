"""Render fixture state to DMX (LIGHT-44).

[if] a FixtureState renders through a generic profile mode [then] channel bytes follow the hygiene rules, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lighting.fixtures import load_profile
from apps.lighting.render_dmx import FixtureState, render_dmx

GENERIC = Path(__file__).resolve().parents[2] / "apps" / "lighting" / "fixtures" / "generic"


def _mode(profile_id: str):
    return load_profile(GENERIC / f"{profile_id}.json").modes[0]


def test_rgbw_saturated_colour_keeps_rgb() -> None:
    out = render_dmx(FixtureState(intensity=1.0, rgb=(255, 128, 0)), _mode("generic-rgbw"))
    assert tuple(out) == (255, 128, 0, 0)


def test_rgbw_grey_moves_to_white() -> None:
    out = render_dmx(FixtureState(rgb=(200, 200, 200)), _mode("generic-rgbw"))
    assert tuple(out) == (0, 0, 0, 200)


def test_pan_16bit_split() -> None:
    out = render_dmx(FixtureState(pan_deg=270), _mode("generic-moving-head-14"))
    assert out[0] in (127, 128)
    assert abs(out[0] * 256 + out[1] - 32767) <= 1


def test_strobe_over_cap_raises() -> None:
    with pytest.raises(ValueError, match="3 per second"):
        render_dmx(FixtureState(strobe_hz=10), _mode("generic-moving-head-14"))


def test_no_intensity_channel_scales_colour() -> None:
    out = render_dmx(FixtureState(intensity=0.5, rgb=(200, 100, 50)), _mode("generic-rgb"))
    assert tuple(out) == (100, 50, 25)
