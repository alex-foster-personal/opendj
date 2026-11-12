"""Warp map schema and monotonic validation (issue #11199).

[if] warp map rules are enforced [then] Python matches the TypeScript contract, [else stop].
"""

from __future__ import annotations

import json

import pytest

from apps.shared.state import grid_edit

pytestmark = pytest.mark.requirement("WARP-12034")


def _pqtz() -> list[dict[str, object]]:
    return [
        {"n": 1, "bpm": 120.0, "t": 0.0},
        {"n": 2, "bpm": 120.0, "t": 0.35},
        {"n": 3, "bpm": 120.0, "t": 1.1},
        {"n": 4, "bpm": 120.0, "t": 1.45},
        {"n": 1, "bpm": 128.0, "t": 2.6},
    ]


def test_build_from_pqtz_round_trips_without_mutating_input() -> None:
    """[if] PQTZ builds a map [then] source times stay on anchors, [else stop]."""
    beats = _pqtz()
    before = json.dumps(beats)
    doc = grid_edit.build_warp_map_from_pqtz(beats)
    assert json.dumps(beats) == before
    assert [a.source_s for a in doc.anchors] == [b["t"] for b in beats]


def test_non_monotonic_source_fails_before_schedule() -> None:
    """[if] source_s repeats [then] validate_warp_map refuses, [else stop]."""
    doc = grid_edit.WarpMapDocument(
        anchors=(
            grid_edit.WarpAnchor(0.0, 0.0, "analyzed"),
            grid_edit.WarpAnchor(1.0, 1.0, "locked"),
            grid_edit.WarpAnchor(1.0, 2.0, "creative"),
        )
    )
    with pytest.raises(grid_edit.WarpMapError):
        grid_edit.validate_warp_map(doc)


def test_missing_analysis_does_not_synthesize_fallback() -> None:
    """[if] PQTZ is too short [then] build_warp_map_from_pqtz raises, [else stop]."""
    with pytest.raises(grid_edit.WarpMapError, match="at least 2 beats"):
        grid_edit.build_warp_map_from_pqtz([{"n": 1, "bpm": 120.0, "t": 0.0}])


def test_kinds_survive_blob_parse() -> None:
    """[if] mixed anchor kinds are stored [then] parse_warp_map_blob keeps them, [else stop]."""
    raw = {
        "v": 1,
        "anchors": [
            {"source_s": 0.0, "beat": 0.0, "kind": "analyzed"},
            {"source_s": 0.5, "beat": 1.0, "kind": "locked"},
            {"source_s": 1.0, "beat": 2.0, "kind": "creative"},
        ],
    }
    doc = grid_edit.parse_warp_map_blob(raw)
    assert [a.kind for a in doc.anchors] == ["analyzed", "locked", "creative"]
