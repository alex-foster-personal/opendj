"""Fixture order for chases over a patch group.

[if] a patch group is ordered by stage position [then] chase indices follow the rig, [else stop].
"""

from __future__ import annotations

import pytest

from apps.lighting.effects import chase
from apps.lighting.fixture_order import chase_indices, order_group
from apps.lighting.patch import Patch, PatchedFixture, StagePosition

pytestmark = [pytest.mark.requirement("LIGHT-14724")]


def _fx(fid: str, x: float, groups: tuple[str, ...] = ("pars",)) -> PatchedFixture:
    return PatchedFixture(fid, "test-rgb-par-3", "3ch", 1, 1, groups, StagePosition(x, 0.5))


PATCH = Patch(
    "club",
    (
        _fx("d", 0.9),
        _fx("b", 0.3),
        _fx("a", 0.1),
        _fx("c", 0.7),
        _fx("tie-b", 0.5),
        _fx("tie-a", 0.5),
        _fx("mover", 0.2, ("movers",)),
    ),
)


def _ids(fixtures: tuple[PatchedFixture, ...]) -> list[str]:
    return [f.id for f in fixtures]


def test_left_right_orders_by_x_with_id_ties() -> None:
    """[if] left_to_right / right_to_left order a group [then] it sorts by stage x, ties by id, [else stop]."""
    assert _ids(order_group(PATCH, "pars", "left_to_right")) == ["a", "b", "tie-a", "tie-b", "c", "d"]
    assert _ids(order_group(PATCH, "pars", "right_to_left")) == ["d", "c", "tie-a", "tie-b", "b", "a"]


def test_outside_in_and_inside_out_are_symmetric_about_centre() -> None:
    """[if] outside_in / inside_out order a group [then] it orders by distance from the centre, [else stop]."""
    assert _ids(order_group(PATCH, "pars", "outside_in")) == ["a", "d", "b", "c", "tie-a", "tie-b"]
    assert _ids(order_group(PATCH, "pars", "inside_out")) == ["tie-a", "tie-b", "b", "c", "a", "d"]


def test_seeded_shuffle_is_repeatable_and_seed_dependent() -> None:
    """[if] a seeded shuffle runs [then] the same seed repeats and another seed differs, [else stop]."""
    first = _ids(order_group(PATCH, "pars", "shuffle", seed=7))
    assert first == _ids(order_group(PATCH, "pars", "shuffle", seed=7))
    assert first != _ids(order_group(PATCH, "pars", "shuffle", seed=8))
    assert sorted(first) == sorted(_ids(PATCH.group("pars")))


def test_order_maps_to_chase_indices_and_unknown_group_raises() -> None:
    """[if] an order maps to chase indices [then] they are 0..N-1 and an unknown group raises ValueError, [else stop]."""
    indices = chase_indices(PATCH, "pars", "left_to_right")
    assert sorted(indices.values()) == list(range(6))
    assert indices["a"] == 0
    assert chase(0.0, 0.0, indices["a"], len(indices)).intensity == 1.0
    assert chase(0.0, 0.0, indices["d"], len(indices)).intensity == 0.0
    with pytest.raises(ValueError, match="no group"):
        chase_indices(PATCH, "lasers", "left_to_right")
