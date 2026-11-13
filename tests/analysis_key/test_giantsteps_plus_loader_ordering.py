"""Ordering contract for GiantSteps+ key-segment rows (issue #13200).

The loader's documented ordering is source order, and source order must already be
chronological: rows come back exactly as written when start times strictly increase,
and any unordered or tied source is refused with a named ``ValueError`` rather than
silently re-sorted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.analysis_key.giantsteps_plus_loader import (
    load_segments,
    load_verified_manifest_tracks,
)


def _payload(keys: list[str], starts: list[float], ends: list[float]) -> dict[str, object]:
    """Build a dataset-shaped annotation from parallel arrays."""
    return {"key": keys, "start_times": starts, "end_times": ends}


def test_unordered_rows_are_refused_not_resorted() -> None:
    """A shuffled tiling of valid segments is refused instead of being silently sorted."""
    with pytest.raises(ValueError, match="adjacent segments must be contiguous"):
        load_segments(_payload(["A", "C", "B"], [0, 45, 20], [20, 90, 45]))


def test_unordered_manifest_fixture_is_refused(tmp_path: Path) -> None:
    """The manifest path applies the same refusal to an unordered fixture row."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "fixture-unordered",
                        "key_sequence": ["F major", "D minor"],
                        "start_times_s": [20, 0],
                        "end_times_s": [40, 20],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        load_verified_manifest_tracks(manifest_path)


@pytest.mark.parametrize(
    ("starts", "ends"),
    [
        ([0, 0], [30, 30]),
        ([0, 0], [10, 30]),
        ([0, 30, 30], [30, 60, 90]),
    ],
    ids=["identical-intervals", "same-start-different-end", "tie-after-first-row"],
)
def test_same_start_timestamp_is_refused_as_ambiguous(starts: list[float], ends: list[float]) -> None:
    """Two intervals that start at the same timestamp are an explicit refusal, not a tie-break."""
    keys = [f"key-{index}" for index in range(len(starts))]

    with pytest.raises(ValueError, match="start_times must be strictly increasing"):
        load_segments(_payload(keys, starts, ends))
