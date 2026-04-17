"""Unit tests for :mod:`apps.sync.ratings` direction policy (SYNC-06)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.sync.matcher import MatchResult, MatchedPair
from apps.sync.ratings import RatingDiff, compute_rating_diffs, summarise_diffs


pytestmark = pytest.mark.requirement("SYNC-06")


@dataclass(slots=True)
class _FakeRB:
    id: str
    rating: int


@dataclass(slots=True)
class _FakeDJ:
    uuid: str
    rating: int


def _pair(rb_id: str, djay_uuid: str, status: str = "matched") -> MatchedPair:
    return MatchedPair(
        rb_id=rb_id,
        djay_uuid=djay_uuid,
        rb_title="t",
        rb_artist="a",
        djay_title="t",
        djay_artist="a",
        confidence=1.0,
        signals=("isrc_exact", "filename_exact", "duration"),
        rationale="test",
        status=status,
    )


class TestDirections:
    def test_both_unrated_is_nop(self) -> None:
        mr = MatchResult(matched=[_pair("rb-1", "u-1")])
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 0)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 0)},
        )
        assert len(diffs) == 1
        assert diffs[0].direction == "nop"
        assert diffs[0].new_djay_rating is None

    def test_rb_rated_djay_unrated_writes_rb_to_djay(self) -> None:
        mr = MatchResult(matched=[_pair("rb-1", "u-1")])
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 4)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 0)},
        )
        assert diffs[0].direction == "rb->djay"
        assert diffs[0].new_djay_rating == 4

    def test_djay_rated_rb_unrated_skips_phase2(self) -> None:
        mr = MatchResult(matched=[_pair("rb-1", "u-1")])
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 0)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 3)},
        )
        assert diffs[0].direction == "djay->rb"
        assert diffs[0].new_djay_rating is None
        assert diffs[0].new_rb_rating == 3  # proposed but NOT written

    def test_equal_ratings_is_nop(self) -> None:
        mr = MatchResult(matched=[_pair("rb-1", "u-1")])
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 5)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 5)},
        )
        assert diffs[0].direction == "nop"

    def test_differing_ratings_is_conflict(self) -> None:
        mr = MatchResult(matched=[_pair("rb-1", "u-1")])
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 4)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 3)},
        )
        assert diffs[0].direction == "conflict"
        assert diffs[0].new_djay_rating is None


class TestReviewBucket:
    def test_review_pairs_are_conflict_regardless_of_ratings(self) -> None:
        mr = MatchResult(
            matched=[],
            review=[_pair("rb-1", "u-1", status="review")],
        )
        diffs = compute_rating_diffs(
            mr,
            rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 5)},
            dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 0)},
            include_review=True,
        )
        assert diffs[0].direction == "conflict"
        assert "review" in diffs[0].rationale


class TestSummarise:
    def test_summary_counts_directions(self) -> None:
        diffs = [
            RatingDiff("u1", "r1", 5, 0, "rb->djay", 5, None, ""),
            RatingDiff("u2", "r2", 4, 3, "conflict", None, None, ""),
            RatingDiff("u3", "r3", 0, 0, "nop", None, None, ""),
            RatingDiff("u4", "r4", 5, 0, "rb->djay", 5, None, ""),
        ]
        s = summarise_diffs(diffs)
        assert s["rb->djay"] == 2
        assert s["conflict"] == 1
        assert s["nop"] == 1
        assert s["total"] == 4
