"""GiantSteps+ loader malformed-interval coverage (issue #13198).

[if] a GiantSteps+ interval is malformed [then] the loader rejects it and keeps valid rows, [else stop].
"""

from __future__ import annotations

import math

import pytest

from apps.analysis_key.giantsteps_plus_loader import KeySegment, load_segments

pytestmark = pytest.mark.requirement("NATIVE-13198")


@pytest.mark.parametrize(
    ("starts", "ends", "segment"),
    [
        ([20], [10], 0),
        ([0, 30], [30, 25], 1),
    ],
)
def test_annotation_ending_before_it_begins_is_rejected(starts: list[float], ends: list[float], segment: int) -> None:
    """[if] an annotation ends before it begins [then] load_segments names the segment and refuses, [else stop]."""
    keys = ["A minor", "C major"][: len(starts)]
    with pytest.raises(ValueError, match=rf"segment {segment} start_s must be less than end_s"):
        load_segments({"key": keys, "start_times": starts, "end_times": ends})


@pytest.mark.parametrize(
    ("starts", "ends", "segment"),
    [
        ([math.nan], [10.0], 0),
        ([0.0], [math.nan], 0),
        ([0.0], [math.inf], 0),
        ([-math.inf], [10.0], 0),
        (["nan"], ["10"], 0),
        ([0.0, 30.0], [30.0, "inf"], 1),
    ],
)
def test_annotation_with_non_finite_timestamp_is_rejected(
    starts: list[float | str], ends: list[float | str], segment: int
) -> None:
    """[if] an annotation has a non-finite timestamp [then] load_segments names the segment and refuses, [else stop]."""
    keys = ["A minor", "C major"][: len(starts)]
    with pytest.raises(ValueError, match=rf"segment {segment} timestamps must be finite"):
        load_segments({"key": keys, "start_times": starts, "end_times": ends})


def test_valid_bounded_interval_and_key_is_retained() -> None:
    """[if] an annotation has a valid bounded interval and key [then] it is kept as a KeySegment, [else stop]."""
    rows = load_segments({"key": ["F# minor"], "start_times": [1.5], "end_times": [118.25]})

    assert rows == (KeySegment(start_s=1.5, end_s=118.25, key="F# minor"),)
