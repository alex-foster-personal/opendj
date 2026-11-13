"""Order a patch group by stage position so ``effects.chase`` indices follow the rig.

``effects.chase`` walks fixtures by index 0..N-1. ``order_group`` returns a group's fixtures in a
stage order (x runs left to right from the audience) and ``chase_indices`` maps each fixture id to
its chase index. The group centre is the midpoint of its leftmost and rightmost fixture.
"""

from __future__ import annotations

import random
from typing import Literal

from apps.lighting.patch import Patch, PatchedFixture

FixtureOrder = Literal["left_to_right", "right_to_left", "outside_in", "inside_out", "shuffle"]
ORDERS: tuple[FixtureOrder, ...] = ("left_to_right", "right_to_left", "outside_in", "inside_out", "shuffle")


def _centre(fixtures: tuple[PatchedFixture, ...]) -> float:
    xs = [f.position.x for f in fixtures]
    return (min(xs) + max(xs)) / 2.0


def order_group(patch: Patch, group: str, order: FixtureOrder, seed: int = 0) -> tuple[PatchedFixture, ...]:
    """Return the fixtures of ``group`` in ``order``; unknown group or order raises ValueError."""
    fixtures = patch.group(group)
    if not fixtures:
        known = sorted({g for f in patch.fixtures for g in f.groups})
        raise ValueError(f"patch {patch.name!r} has no group {group!r}; groups: {known}")
    if order not in ORDERS:
        raise ValueError(f"unknown fixture order {order!r}; orders: {list(ORDERS)}")
    by_id = sorted(fixtures, key=lambda f: f.id)
    if order == "shuffle":
        random.Random(seed).shuffle(by_id)
        return tuple(by_id)
    if order == "left_to_right":
        return tuple(sorted(by_id, key=lambda f: f.position.x))
    if order == "right_to_left":
        return tuple(sorted(by_id, key=lambda f: -f.position.x))
    centre = _centre(fixtures)
    sign = -1.0 if order == "outside_in" else 1.0
    return tuple(sorted(by_id, key=lambda f: (sign * round(abs(f.position.x - centre), 9), f.position.x)))


def chase_indices(patch: Patch, group: str, order: FixtureOrder, seed: int = 0) -> dict[str, int]:
    """Map each fixture id in ``group`` to its ``effects.chase`` index 0..N-1."""
    return {f.id: index for index, f in enumerate(order_group(patch, group, order, seed))}
