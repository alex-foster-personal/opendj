"""LIGHT-14589: stage layout JSON loader for the visualiser.

[if] a layout is loaded, checked against the patch and dumped [then] it round-trips with errors and warnings, [else stop].
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from apps.lighting.patch import Patch, PatchedFixture, PatchError, StagePosition
from apps.lighting.stage_layout import FixturePlacement, layout_from_json, layout_to_json

pytestmark = [pytest.mark.requirement("LIGHT-14589")]

PATCH = Patch(
    "club",
    tuple(
        PatchedFixture(fid, "test-rgb-par-3", "3ch", 1, addr, ("pars",), StagePosition(0.5, 0.5))
        for fid, addr in (("par-1", 1), ("par-2", 4))
    ),
)


def _entry(fid: str, x: float = 0.1) -> dict[str, object]:
    return {"id": fid, "x": x, "y": 0.2, "z": 0.9, "rotation": 90.0, "beam_angle": 25.0}


def test_valid_layout_loads_frozen_dataclasses() -> None:
    """[if] a valid layout loads [then] fixtures are frozen dataclasses with x/y/z, rotation, beam angle, [else stop]."""
    result = layout_from_json({"version": 1, "fixtures": [_entry("par-1"), _entry("par-2", 0.8)]}, PATCH)
    first = result.layout.fixtures[0]
    assert first == FixturePlacement("par-1", 0.1, 0.2, 0.9, 90.0, 25.0)
    assert result.warnings == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.x = 0.5  # type: ignore[misc]


def test_fixture_missing_from_patch_is_error() -> None:
    """[if] a layout names a fixture not in the patch [then] loading raises naming it, [else stop]."""
    with pytest.raises(PatchError, match="'ghost' is not in patch 'club'"):
        layout_from_json({"version": 1, "fixtures": [_entry("ghost")]}, PATCH)


def test_same_position_warns() -> None:
    """[if] two fixtures share x/y/z [then] the layout loads with a warning naming both, [else stop]."""
    result = layout_from_json({"version": 1, "fixtures": [_entry("par-1"), _entry("par-2")]}, PATCH)
    assert len(result.layout.fixtures) == 2
    assert len(result.warnings) == 1
    assert "'par-1'" in result.warnings[0]
    assert "'par-2'" in result.warnings[0]


def test_load_dump_round_trips() -> None:
    """[if] a layout is loaded then dumped and reloaded [then] JSON and model are equal, [else stop]."""
    data = {"version": 1, "fixtures": [_entry("par-1"), _entry("par-2", 0.8)]}
    layout = layout_from_json(data, PATCH).layout
    dumped = json.loads(json.dumps(layout_to_json(layout)))
    assert dumped == data
    assert layout_from_json(dumped, PATCH).layout == layout
