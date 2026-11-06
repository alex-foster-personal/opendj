"""S-KEY window schedule and bar emissions (repo venv, no torch, no weights)."""

from __future__ import annotations

import pytest

from apps.analysis_key import segments
from apps.analysis_key.giantsteps_scoring import score_segment_block
from apps.analysis_key.skey_windows import (
    DEFAULT_HOP_S,
    WINDOW_S,
    segment_from_skey_windows,
    window_bounds,
)

BAR_S = 2.0


def _grid(n_bars: int) -> segments.BarGrid:
    starts = tuple(i * BAR_S for i in range(n_bars))
    return segments.BarGrid(starts=starts, ends=(*starts[1:], n_bars * BAR_S))


def test_window_bounds_120_covers_full_duration() -> None:
    bounds = window_bounds(120.0)
    assert bounds[0][0] == pytest.approx(0.0)
    assert bounds[-1][1] == pytest.approx(120.0)
    for start, end in bounds[:-1]:
        assert end - start == pytest.approx(WINDOW_S)
        assert end - start >= WINDOW_S - 1e-9
    for i in range(len(bounds) - 1):
        assert bounds[i + 1][0] - bounds[i][0] == pytest.approx(DEFAULT_HOP_S)


def test_short_file_is_single_window() -> None:
    bounds = window_bounds(10.0)
    assert bounds == ((0.0, 10.0),)


def test_skey_windows_segment_scores_hit_and_miss() -> None:
    grid = _grid(32)
    duration_s = 64.0
    wins = (
        {
            "start_s": 0.0,
            "end_s": 32.0,
            "key_rekordbox_style": "C",
            "label": "C Major",
            "error": None,
        },
        {
            "start_s": 32.0,
            "end_s": 64.0,
            "key_rekordbox_style": "F#",
            "label": "F# Major",
            "error": None,
        },
    )
    block = segment_from_skey_windows(wins, grid, duration_s=duration_s)
    assert block.status == "ok"
    assert len(block.segments) == 2

    hit = score_segment_block(block, [32.0], grid)
    assert hit.n_matched == 1

    miss = score_segment_block(block, [36.0], grid)
    assert miss.n_matched == 0
