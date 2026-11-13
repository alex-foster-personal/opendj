"""VARIANT-13902: energy curve and movement variants behind lighting settings (issue #10107).

[if] a curve, movement or out-of-list value breaks its acceptance line [then] fail, [else stop].
"""

from __future__ import annotations

import itertools
import math
from pathlib import Path
from statistics import mean

import pytest

from apps.lighting.effects import StrobeParams, movement_sweep, strobe
from apps.lighting.safety import MAX_FLASHES_PER_SECOND
from apps.lighting.sections import resolve_sections
from apps.lighting.settings import ENERGY_CURVE_KEY, MOVEMENT_KEY, SETTINGS, LightingSettings
from apps.lighting.show_plan import EffectBand, SectionVariants, plan_show, shape_plan
from tests.lighting.test_palettes import _client
from tests.lighting.test_show_plan import _data, _profile, _TestMap

pytestmark = [pytest.mark.requirement("VARIANT-13902")]

CURVES = ("gentle", "matched", "punchy")
PINNED_MEANS: list[float] = [0.297, 0.379, 0.474]
PINNED_PEAKS: list[float] = [0.588, 0.744, 0.907]


def _shaped(curve: str):
    data = _data()
    profile = _profile(data)
    sections = resolve_sections(data["anlz"])
    plan = plan_show(profile, sections, _TestMap(), 7, moments=data["anlz"]["phrase_moments"])
    return plan, shape_plan(profile, sections, plan, curve)


def test_curves_order_mean_intensity_and_hit_peaks() -> None:
    """[if] gentle < matched < punchy fails for mean intensity or hit peak, or the pinned numbers move [then] fail, [else stop]."""
    means, peaks = [], []
    for curve in CURVES:
        plan, shaped = _shaped(curve)
        means.append(mean(c.intensity for c in shaped))
        peaks.append(max(s.intensity for p, s in zip(plan, shaped, strict=True) if p.effects is EffectBand.HIT))
    assert means[0] < means[1] < means[2]
    assert peaks[0] < peaks[1] < peaks[2]
    assert [round(m, 3) for m in means] == PINNED_MEANS
    assert [round(p, 3) for p in peaks] == PINNED_PEAKS


def test_wide_is_three_times_small_and_half_the_period() -> None:
    """[if] wide amplitude is not 3x small or its sweep period is not half [then] fail, [else stop]."""
    small, wide = movement_sweep("small", range_deg=540.0), movement_sweep("wide", range_deg=540.0)
    assert math.isclose(wide.amplitude_deg, 3 * small.amplitude_deg)
    assert math.isclose(wide.bars, small.bars / 2)


def test_live_change_applies_at_next_section_boundary(tmp_path: Path) -> None:
    """[if] a live curve or movement change alters the running section or misses the next boundary [then] fail, [else stop]."""
    settings = LightingSettings(pick_log=tmp_path / "picks.ndjson")
    variants = SectionVariants(settings)
    assert variants.current == ("matched", "small")
    settings.set(ENERGY_CURVE_KEY, "punchy")
    settings.set(MOVEMENT_KEY, "wide")
    assert variants.current == ("matched", "small")
    assert variants.section_boundary() == ("punchy", "wide")


@pytest.mark.parametrize("curve", CURVES)
def test_no_curve_exceeds_three_flashes_per_second(curve: str) -> None:
    """[if] any curve's strobe on the drop-heavy fixture flashes more than 3 times a second [then] fail, [else stop]."""
    _, shaped = _shaped(curve)
    rates = [c.strobe_hz for c in shaped if c.strobe_hz > 0]
    assert rates
    for rate in rates:
        bpm = 128.0
        frames = [
            strobe(i / 240 * bpm / 60, 0.0, 0, 1, StrobeParams(rate_hz=rate, bpm=bpm)).intensity for i in range(240 * 4)
        ]
        onsets = sum(1 for a, b in itertools.pairwise(frames) if b > a)
        assert onsets / 4 <= MAX_FLASHES_PER_SECOND


@pytest.mark.parametrize("key", ["energy_curve", "movement"])
def test_out_of_list_value_answers_400_naming_allowed(key: str, tmp_path: Path) -> None:
    """[if] PUT of a value outside the list is not 400 or omits the allowed values [then] fail, [else stop]."""
    client, _runtime, _log = _client(tmp_path)
    response = client.put(f"/api/v1/lighting/settings/{key}", json={"value": "huge"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["allowed"] == list(SETTINGS[f"lighting.{key}"].allowed)
    assert all(v in detail["message"] for v in detail["allowed"])
