"""Pure anchor/BPM grid transform (issue #11138).

[if] beats are retempoed [then] times stay strictly increasing with n in 1..4, [else stop].
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from apps.shared.state import grid_edit
from apps.shared.state.grid_edit_bpm import rebuild_beats_at_bpm

pytestmark = pytest.mark.requirement("BEATGRID-11949")


def _grid(times: list[float], bpm: float = 120.0) -> list[dict[str, Any]]:
    return [{"n": (i % 4) + 1, "bpm": bpm, "t": t} for i, t in enumerate(times)]


FOUR_ON_FLOOR = _grid([0.5 * i for i in range(16)], 120.0)


def test_120_bpm_four_on_floor_rebuilt_at_128_around_in_grid_anchor() -> None:
    """[if] 120 BPM grid is rebuilt at 128 BPM at an in-grid anchor [then] anchor line time is fixed, [else stop]."""
    anchor_t = 4.0
    out = rebuild_beats_at_bpm(FOUR_ON_FLOOR, anchor_t, 128.0)
    i_anchor = next(i for i, b in enumerate(FOUR_ON_FLOOR) if b["t"] == 4.0)
    period = 60.0 / 128.0
    assert out[i_anchor]["t"] == pytest.approx(4.0)
    assert out[i_anchor]["bpm"] == 128.0
    for i, beat in enumerate(out):
        assert beat["t"] == pytest.approx(4.0 + (i - i_anchor) * period)


def test_rebuilt_times_increase_and_every_n_is_in_the_bar() -> None:
    """[if] a grid is retempoed [then] times strictly increase and each n is 1..4, [else stop]."""
    out = rebuild_beats_at_bpm(FOUR_ON_FLOOR, 0.0, 100.0)
    times = [b["t"] for b in out]
    assert times == sorted(times) and len(times) == len(set(times))
    assert all(1 <= b["n"] <= 4 for b in out)
    assert [b["n"] for b in out] == [b["n"] for b in FOUR_ON_FLOOR]


def test_beats_that_would_land_before_zero_are_dropped() -> None:
    """[if] retempo would put a beat before 0 s [then] that beat is dropped, [else stop]."""
    grid = _grid([float(i) for i in range(20)], 120.0)
    out = rebuild_beats_at_bpm(grid, 19.0, 40.0)
    assert all(b["t"] >= 0 for b in out)
    assert len(out) < len(grid)
    assert out[0]["t"] == pytest.approx(1.0)


def test_non_finite_non_positive_bpm_or_empty_beats_are_refused() -> None:
    """[if] bpm is bad or beats empty [then] transform is refused, [else stop]."""
    with pytest.raises(grid_edit.GridEditError) as empty:
        rebuild_beats_at_bpm([], 1.0, 120.0)
    assert empty.value.code == "GRID_EDIT_INVALID"
    for bpm in (0.0, -1.0, math.nan, math.inf):
        with pytest.raises(grid_edit.GridEditError):
            rebuild_beats_at_bpm(FOUR_ON_FLOOR, 1.0, bpm)


def test_grid_edit_reexports_the_transform() -> None:
    assert grid_edit.rebuild_beats_at_bpm is rebuild_beats_at_bpm
