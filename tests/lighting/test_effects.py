"""LIGHT-10570 beat-synced effect generator acceptance tests (issue #9891).

[if] pulse, chase, colour_step, sweep or strobe break acceptance or purity rules [then] fail, [else stop].
"""

from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path

import pytest

from apps.lighting.effects import (
    EFFECTS,
    ChaseParams,
    ColourStepParams,
    EffectOutput,
    LookValues,
    PulseParams,
    StrobeParams,
    SweepParams,
    chase,
    colour_step,
    compose,
    pulse,
    strobe,
    sweep,
)

pytestmark = [pytest.mark.requirement("LIGHT-10570")]


def test_pulse_is_full_on_the_beat_and_below_a_tenth_at_0_99() -> None:
    """[if] pulse at beat phase 0 and 0.99 [then] 1.0 at 0 and below 0.1 at 0.99."""
    assert pulse(0.0, 0.0, 0, 1).intensity == pytest.approx(1.0)
    assert pulse(0.99, 0.0, 0, 1).intensity < 0.1
    assert pulse(7.0, 0.0, 0, 1).intensity == pytest.approx(1.0)


def test_pulse_on_full_look_composes_to_zero_in_dark_phase() -> None:
    """[if] pulse on a full look [then] composed intensity reaches 0 in the dark phase."""
    look = LookValues(intensity=1.0, colour=(1.0, 1.0, 1.0))
    intensities = [
        compose(look, pulse(b, 0.0, 0, 1)).intensity for b in [i * 0.01 for i in range(101)]
    ]
    assert min(intensities) == 0.0
    assert max(intensities) == pytest.approx(1.0)
    assert all(c <= look.intensity for c in intensities)
    assert compose(LookValues(intensity=0.5), EffectOutput(intensity=0.5)).intensity == 0.25


@pytest.mark.parametrize(
    ("direction", "expected_heads"),
    [
        ("forward", [0, 1, 2, 3]),
        ("reverse", [3, 2, 1, 0]),
    ],
)
def test_chase_lights_each_of_four_fixtures_exactly_once_over_four_beats(
    direction: str, expected_heads: list[int]
) -> None:
    """[if] chase over 4 fixtures for 4 beats [then] each fixture is lit exactly once."""
    params = ChaseParams(direction=direction)  # type: ignore[arg-type]
    for beat, head in zip(range(4), expected_heads, strict=True):
        sample = beat + 0.5
        lit = [
            i
            for i in range(4)
            if chase(sample, 0.0, i, 4, params).intensity == 1.0
        ]
        assert lit == [head]


def test_chase_width_two_each_fixture_lit_twice_over_four_beats() -> None:
    """[if] chase width 2 on 4 fixtures for 4 beats [then] each fixture lit exactly twice."""
    params = ChaseParams(width=2)
    counts = [0, 0, 0, 0]
    for beat in range(4):
        sample = beat + 0.5
        for i in range(4):
            if chase(sample, 0.0, i, 4, params).intensity == 1.0:
                counts[i] += 1
    assert counts == [2, 2, 2, 2]


def test_strobe_3hz_on_intervals_start_at_the_same_instants_for_four_fixtures() -> None:
    """[if] 3 Hz strobe on 4 fixtures [then] on-intervals start at the same instants."""
    bpm = 120.0
    rate_hz = 3.0
    duty = 0.5
    params = StrobeParams(rate_hz=rate_hz, bpm=bpm, duty=duty)
    dt_s = 0.001
    n_samples = int(2.0 / dt_s)

    rising_by_fixture: list[list[int]] = []
    for fixture in range(4):
        on_prev = False
        rising: list[int] = []
        for idx in range(n_samples):
            t = idx * dt_s
            beat_phase = t * bpm / 60.0
            out = strobe(beat_phase, 0.0, fixture, 4, params)
            on = out.intensity == 1.0
            if on and not on_prev:
                rising.append(idx)
            on_prev = on
        rising_by_fixture.append(rising)

    assert len(rising_by_fixture) == 4
    assert all(rising == rising_by_fixture[0] for rising in rising_by_fixture[1:])
    assert rising_by_fixture[0]
    assert len(rising_by_fixture[0]) == 6
    for edge_idx in rising_by_fixture[0]:
        t_edge = edge_idx * dt_s
        k = round(t_edge * rate_hz)
        assert abs(t_edge - k / rate_hz) <= dt_s + 1e-12


@pytest.mark.parametrize("centre_deg", [0.0, 90.0])
def test_sweep_two_fixtures_offset_half_are_mirror_images(centre_deg: float) -> None:
    """[if] sweep with 2 fixtures and offset 0.5 [then] pan values mirror at every sample."""
    params = SweepParams(offset=0.5, centre_deg=centre_deg)
    n_bar_samples = 200
    for i in range(n_bar_samples):
        bar_phase = 8.0 * i / (n_bar_samples - 1)
        out0 = sweep(0.0, bar_phase, 0, 2, params)
        out1 = sweep(0.0, bar_phase, 1, 2, params)
        assert out0.tilt is None
        assert out1.tilt is None
        assert out0.pan is not None and out1.pan is not None
        assert out0.pan - centre_deg == pytest.approx(-(out1.pan - centre_deg), abs=1e-9)

    tilt_params = SweepParams(offset=0.5, axis="tilt")
    out = sweep(0.0, 1.0, 0, 2, tilt_params)
    assert out.tilt is not None
    assert out.pan is None


def test_colour_step_advances_every_n_bars() -> None:
    """[if] colour_step every 2 bars [then] palette index advances on schedule."""
    palette = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    params = ColourStepParams(palette=palette, every_bars=2)
    expectations = [
        (0.0, palette[0]),
        (1.0, palette[0]),
        (2.0, palette[1]),
        (3.0, palette[1]),
        (4.0, palette[2]),
        (5.0, palette[2]),
        (6.0, palette[0]),
    ]
    for bar, expected in expectations:
        assert colour_step(0.0, bar, 0, 1, params).colour == expected


def test_effects_are_deterministic() -> None:
    """[if] same inputs twice [then] each effect returns equal outputs."""
    phases = [0.0, 0.37, 1.5, 7.25]
    bar_phases = [0.0, 2.5, 16.0]
    for name, fn in EFFECTS.items():
        for beat_phase, bar_phase in itertools.product(phases, bar_phases):
            for fixture_index, fixture_count in ((0, 1), (1, 3), (2, 5)):
                a = fn(beat_phase, bar_phase, fixture_index, fixture_count, None)
                b = fn(beat_phase, bar_phase, fixture_index, fixture_count, None)
                assert a == b, name


def test_invalid_params_and_fixture_index_raise() -> None:
    """[if] invalid params or fixture index [then] ValueError."""
    with pytest.raises(ValueError):
        PulseParams(decay=0.0)
    with pytest.raises(ValueError):
        ColourStepParams(palette=())
    with pytest.raises(ValueError):
        StrobeParams(duty=1.0)
    with pytest.raises(ValueError):
        StrobeParams(rate_hz=0.0)
    with pytest.raises(ValueError):
        pulse(0.0, 0.0, 0, 0)
    with pytest.raises(ValueError):
        pulse(0.0, 0.0, 1, 1)
    with pytest.raises(ValueError):
        EffectOutput(intensity=1.5)


def test_module_imports_only_stdlib_and_no_clock() -> None:
    """[if] effects imports outside stdlib/apps.lighting or reads a clock [then] fail."""
    import apps.lighting.effects as module

    tree = ast.parse(Path(module.__file__).read_text())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
    banned = {"time", "datetime", "random"}
    for name in roots:
        top = name.split(".")[0]
        assert top not in banned, name
        assert top in sys.stdlib_module_names or name.startswith("apps.lighting"), name
