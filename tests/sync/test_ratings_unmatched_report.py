"""Unmatched and review-bucket rated tracks get no-write report rows (SYNC-12985).

[if] a rated track has no match or only a review-bucket match [then] the ratings report carries a no-write row for it, [else stop].
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pytest

from apps.sync.apply_ratings import _plan_rating_targets
from apps.sync.djay_sync_service import unmatched_rating_rows
from apps.sync.matcher import MatchedPair, MatchResult
from apps.sync.ratings import compute_rating_diffs, unmatched_rating_diffs

pytestmark = pytest.mark.requirement("SYNC-12985")


@dataclass(slots=True)
class _FakeRB:
    id: str
    rating: int


@dataclass(slots=True)
class _FakeDJ:
    uuid: str
    rating: int


def _review_pair(rb_id: str, djay_uuid: str) -> MatchedPair:
    """Build a review-bucket pair with weak signals."""
    return MatchedPair(
        rb_id=rb_id,
        djay_uuid=djay_uuid,
        rb_title="t",
        rb_artist="a",
        djay_title="t",
        djay_artist="a",
        confidence=0.6,
        signals=("title_artist", "duration"),
        rationale="test",
        status="review",
    )


def test_rated_rb_track_without_match_is_reported_unmatched() -> None:
    """A rated RB-only track yields an unmatched no-write diff; unrated ones do not."""
    mr = MatchResult(rb_only=[_FakeRB("rb-1", 4), _FakeRB("rb-2", 0)])
    diffs = compute_rating_diffs(mr)
    assert len(diffs) == 1
    d = diffs[0]
    assert (d.rb_id, d.djay_uuid, d.rb_rating, d.direction) == ("rb-1", "", 4, "unmatched")
    assert d.new_djay_rating is None and d.new_rb_rating is None
    assert "no matched djay track" in d.rationale


def test_rated_djay_track_without_match_is_reported_unmatched() -> None:
    """A rated djay-only track yields an unmatched no-write diff."""
    mr = MatchResult(djay_only=[_FakeDJ("u-9", 3)])
    diffs = compute_rating_diffs(mr)
    assert len(diffs) == 1
    d = diffs[0]
    assert (d.rb_id, d.djay_uuid, d.djay_rating, d.direction) == ("", "u-9", 3, "unmatched")
    assert d.new_djay_rating is None and d.new_rb_rating is None


def test_review_pair_with_different_ratings_is_reported_by_default() -> None:
    """A review-bucket pair with differing ratings is a no-write conflict without opting in."""
    mr = MatchResult(review=[_review_pair("rb-1", "u-1")])
    diffs = compute_rating_diffs(
        mr,
        rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 5)},
        dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 2)},
    )
    assert len(diffs) == 1
    assert diffs[0].direction == "conflict"
    assert diffs[0].new_djay_rating is None and diffs[0].new_rb_rating is None
    assert "RB=5 vs djay=2" in diffs[0].rationale


def test_unmatched_rating_diffs_skips_paired_ids() -> None:
    """Tracks that appear in a pair are not reported as unmatched."""
    diffs = unmatched_rating_diffs(
        {"rb-1": 4, "rb-2": 5},
        {"u-1": 3, "u-2": 1},
        [("rb-1", "u-1")],
    )
    assert {(d.rb_id, d.djay_uuid) for d in diffs} == {("rb-2", ""), ("", "u-2")}


def test_service_unmatched_rows_are_never_apply_targets() -> None:
    """Metadata-plan unmatched rows carry resolution 'unmatched' and plan no writes."""
    rows = unmatched_rating_rows({"rb-1": 4, "rb-2": 0}, {"u-2": 3}, [])
    assert [(r.rb_content_id, r.djay_uuid, r.resolution) for r in rows] == [
        ("rb-1", "", "unmatched"),
        ("", "u-2", "unmatched"),
    ]
    assert rows[0].rb_value == "4" and rows[0].djay_value == ""
    csv_rows = [{k: str(v) for k, v in asdict(r).items()} for r in rows]
    assert _plan_rating_targets(csv_rows, None) == ([], [])
