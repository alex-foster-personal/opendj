"""LIGHT-10575: look model with per-group targets and linear crossfades.

[if] crossfade midpoint RGB/intensity, unset pan hold, or unknown-group build regress [then] fail, [else stop].
"""

from __future__ import annotations

import math

import pytest

from apps.lighting.looks import (
    EffectRef,
    FixtureState,
    GroupTarget,
    LookError,
    build_look,
    crossfade,
    fade_progress,
    resolve,
)

pytestmark = [pytest.mark.requirement("LIGHT-10575")]

PATCH = {"front": ["par1", "par2"], "movers": ["mh1"], "empty_group": []}


def _look_a_front() -> object:
    return build_look(
        "a_red",
        "Red wash",
        {"front": GroupTarget(rgb=(255, 0, 0), intensity=1.0)},
        patch=PATCH,
    )


def _look_b_front() -> object:
    return build_look(
        "b_blue",
        "Blue half",
        {"front": GroupTarget(rgb=(0, 0, 255), intensity=0.5)},
        patch=PATCH,
    )


def test_crossfade_red_full_to_blue_half_at_midpoint() -> None:
    """[if] look A red at 1.0 and B blue at 0.5 do not crossfade to ~(128,0,128) at 0.75 intensity [then] fail."""
    a = _look_a_front()
    b = _look_b_front()
    result = crossfade(a, b, 0.5)  # type: ignore[arg-type]
    for fixture_id in ("par1", "par2"):
        state = result[fixture_id]
        assert state.rgb is not None
        r, g, b_ch = state.rgb
        assert abs(r - 128) <= 1
        assert abs(g - 0) <= 1
        assert abs(b_ch - 128) <= 1
        assert state.intensity == pytest.approx(0.75)


def test_unset_pan_holds_previous_look() -> None:
    """[if] look B leaves pan unset [then] crossfade must keep look A's pan."""
    a = build_look(
        "a_mover",
        "A",
        {"movers": GroupTarget(pan_deg=90.0, tilt_deg=10.0)},
        patch=PATCH,
    )
    b = build_look(
        "b_mover",
        "B",
        {"movers": GroupTarget(tilt_deg=50.0)},
        patch=PATCH,
    )
    for progress, expected_tilt in ((0.0, 10.0), (0.5, 30.0), (1.0, 50.0)):
        state = crossfade(a, b, progress)["mh1"]
        assert state.pan_deg == 90.0
        assert state.tilt_deg == pytest.approx(expected_tilt)


def test_unknown_group_raises_naming_group() -> None:
    """[if] a group in a look names no fixture in the patch [then] build_look names the group."""
    with pytest.raises(LookError, match="truss_back"):
        build_look(
            "wash_warm",
            "Warm",
            {"truss_back": GroupTarget(intensity=1.0)},
            patch=PATCH,
        )
    with pytest.raises(LookError, match="ghost"):
        build_look(
            "with_fx",
            "Fx",
            {"front": GroupTarget(intensity=1.0)},
            effects=[EffectRef(effect_id="pulse", groups=("ghost",))],
            patch=PATCH,
        )
    with pytest.raises(LookError, match="empty_group"):
        build_look(
            "empty",
            "Empty",
            {"empty_group": GroupTarget(intensity=1.0)},
            patch=PATCH,
        )


def test_progress_endpoints_equal_resolved_looks() -> None:
    """[if] crossfade at 0/1 does not match resolved endpoints [then] fail."""
    a = _look_a_front()
    b = _look_b_front()
    at_zero = crossfade(a, b, 0.0)  # type: ignore[arg-type]
    assert at_zero == resolve(a)  # type: ignore[arg-type]
    at_one = crossfade(a, b, 1.0)  # type: ignore[arg-type]
    assert at_one == resolve(b, resolve(a))  # type: ignore[arg-type]


def test_progress_out_of_range_refused() -> None:
    """[if] crossfade progress is outside 0..1 or non-finite [then] ValueError."""
    a = _look_a_front()
    b = _look_b_front()
    for bad in (-0.1, 1.1, math.nan):
        with pytest.raises(ValueError):
            crossfade(a, b, bad)  # type: ignore[arg-type]


def test_fade_progress_in_beats() -> None:
    """[if] fade_progress does not map beats to clamped linear progress [then] fail."""
    assert fade_progress(2, 4) == 0.5
    assert fade_progress(8, 4) == 1.0
    assert fade_progress(-1, 4) == 0.0
    assert fade_progress(0, 0) == 1.0
    with pytest.raises(ValueError):
        fade_progress(1, -1)


def test_group_target_validation() -> None:
    """[if] out-of-range GroupTarget fields are accepted [then] fail."""
    with pytest.raises(LookError, match="rgb"):
        GroupTarget(rgb=(256, 0, 0))
    with pytest.raises(LookError, match="intensity"):
        GroupTarget(intensity=1.5)
    with pytest.raises(LookError, match="white"):
        GroupTarget(white=-0.1)


def test_fixture_in_two_groups_later_group_wins() -> None:
    """[if] a fixture in two groups does not take the later group's set fields [then] fail."""
    both = {"overlap": ["par1"], "front": ["par1", "par2"]}
    look = build_look(
        "overlap",
        "Overlap",
        {
            "front": GroupTarget(rgb=(255, 0, 0), intensity=0.8),
            "overlap": GroupTarget(rgb=(0, 255, 0)),
        },
        patch=both,
    )
    state = resolve(look)["par1"]
    assert state.rgb == (0, 255, 0)
    assert state.intensity == pytest.approx(0.8)


def test_untouched_previous_fixture_passes_through() -> None:
    """[if] a fixture only in previous is changed by crossfade [then] fail."""
    a = _look_a_front()
    b = _look_b_front()
    previous = {"side": FixtureState(rgb=(10, 20, 30), intensity=0.2)}
    result = crossfade(a, b, 0.5, previous=previous)  # type: ignore[arg-type]
    assert result["side"] == previous["side"]
