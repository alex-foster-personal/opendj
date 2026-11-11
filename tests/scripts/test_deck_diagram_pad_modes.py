"""Older midi.json maps (no per-row 'layer') must still generate pad modes without KeyError."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "tools" / "deck-diagrams" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from generate_html import _modes_for  # noqa: E402


def test_modes_for_rows_without_layer() -> None:
    rows = [
        {"type": "note", "name": "pad1_hotcue", "ch": 7, "code": 0},
        {"type": "note", "name": "pad1_roll", "ch": 7, "code": 16},
    ]
    modes = _modes_for(rows, 1, 7)
    assert [m["mode"] for m in modes] == ["hotcue", "roll"]


def test_modes_for_shift_layer_still_paired() -> None:
    rows = [
        {"type": "note", "name": "pad1_hotcue", "ch": 6, "code": 0, "layer": "base"},
        {"type": "note", "name": "pad1_hotcue_shift", "ch": 6, "code": 8, "layer": "shift"},
    ]
    (m,) = _modes_for(rows, 1, 6)
    assert m["shift_code"] == 8


def test_modes_for_shift_on_adjacent_channel() -> None:
    """DDJ-FLX10-style maps put shift pad rows on ch+1, not the same ch as base."""
    rows = [
        {"type": "note", "name": "pad1_hot_cue_page1", "ch": 8, "code": 0},
        {"type": "note", "name": "pad1_hot_cue_page1_shift", "ch": 9, "code": 0},
        {"type": "note", "name": "pad1_beat_jump_page1", "ch": 8, "code": 4},
        {"type": "note", "name": "pad1_beat_jump_page1_shift", "ch": 9, "code": 4},
    ]
    by_mode = {m["mode"]: m for m in _modes_for(rows, 1, 8)}
    assert by_mode["hot_cue_page1"]["shift_code"] == 0
    assert by_mode["beat_jump_page1"]["shift_code"] == 4
