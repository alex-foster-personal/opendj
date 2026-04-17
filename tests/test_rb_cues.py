"""Phase 4 SYNC-04: RB cue reader + colour palette tests."""
from __future__ import annotations

import pytest

from apps.shared.rb_color_palette import (
    RB_COLOR_PALETTE,
    color_index_to_rgb,
    rgb_to_color_index,
)
from apps.shared.rekordbox_db import _rb_kind_to_normalised

pytestmark = pytest.mark.requirement("SYNC-04")


def test_palette_contains_zero_as_none():
    assert RB_COLOR_PALETTE[0] is None


def test_palette_has_eight_colours():
    named = [v for v in RB_COLOR_PALETTE.values() if v is not None]
    assert len(named) == 8


def test_color_index_to_rgb_round_trip():
    for idx, rgb in RB_COLOR_PALETTE.items():
        if rgb is None:
            continue
        assert rgb_to_color_index(rgb) == idx


def test_color_index_to_rgb_none_round_trip():
    assert color_index_to_rgb(None) is None
    assert rgb_to_color_index(None) == 0


def test_rgb_nearest_color_for_off_palette():
    # (250, 10, 5) should map to pure red (index 1)
    assert rgb_to_color_index((250, 10, 5)) == 1


def test_kind_zero_is_memory():
    kind, idx = _rb_kind_to_normalised(0, None)
    assert kind == "memory"
    assert idx is None


def test_kind_one_to_eight_maps_hot():
    for k in range(1, 9):
        kind, idx = _rb_kind_to_normalised(k, None)
        assert kind == "hot"
        assert idx == k - 1


def test_kind_none_is_memory():
    kind, _ = _rb_kind_to_normalised(None, None)
    assert kind == "memory"
