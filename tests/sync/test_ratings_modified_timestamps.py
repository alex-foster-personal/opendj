"""Rating conflict resolution uses modification timestamps (SYNC-12979).

[if] matched ratings differ [then] last-modified-wins inside the window decides the direction, [else stop].
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.audit.sync_diff import build_analysis_diff
from apps.shared.djay_db import DjayTrack
from apps.sync.matcher import MatchedPair, MatchResult, _unified_from_dj, _unified_from_rb
from apps.sync.ratings import compute_rating_diffs

pytestmark = pytest.mark.requirement("SYNC-12979")

_NOW = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)


@dataclass(slots=True)
class _FakeRB:
    id: str
    rating: int
    updated_at: datetime | None = None


@dataclass(slots=True)
class _FakeDJ:
    uuid: str
    rating: int
    modified_at: datetime | None = None


def _diff(rb_mtime: datetime | None, dj_mtime: datetime | None, *, window_days: int = 7):
    """Return the single RatingDiff for an RB=5 vs djay=3 matched pair."""
    pair = MatchedPair(
        rb_id="rb-1",
        djay_uuid="u-1",
        rb_title="t",
        rb_artist="a",
        djay_title="t",
        djay_artist="a",
        confidence=1.0,
        signals=("isrc_exact",),
        rationale="test",
        status="matched",
    )
    diffs = compute_rating_diffs(
        MatchResult(matched=[pair]),
        rb_tracks_by_id={"rb-1": _FakeRB("rb-1", 5, rb_mtime)},
        dj_tracks_by_uuid={"u-1": _FakeDJ("u-1", 3, dj_mtime)},
        window_days=window_days,
    )
    assert len(diffs) == 1
    return diffs[0]


def test_rb_newer_within_window_writes_rb_to_djay() -> None:
    """[if] both timestamps sit inside the window and RB is newer [then] rb->djay carries RB's rating, [else stop]."""
    d = _diff(_NOW, _NOW - timedelta(days=2))
    assert d.direction == "rb->djay"
    assert d.new_djay_rating == 5
    assert d.rb_modified_at == _NOW
    assert d.djay_modified_at == _NOW - timedelta(days=2)


def test_djay_newer_within_window_proposes_djay_to_rb() -> None:
    """[if] both timestamps sit inside the window and djay is newer [then] djay->rb carries djay's rating, [else stop]."""
    d = _diff(_NOW - timedelta(days=1), _NOW)
    assert d.direction == "djay->rb"
    assert d.new_rb_rating == 3
    assert d.new_djay_rating is None


def test_one_missing_timestamp_stays_conflict() -> None:
    """[if] one matched track lacks a modification timestamp [then] the row stays a no-write conflict, [else stop]."""
    d = _diff(_NOW, None)
    assert d.direction == "conflict"
    assert d.new_djay_rating is None and d.new_rb_rating is None
    assert "no modification timestamp on djay" in d.rationale


def test_timestamps_outside_window_stay_conflict() -> None:
    """[if] the two timestamps are further apart than the window [then] the row stays a conflict, [else stop]."""
    d = _diff(_NOW, _NOW - timedelta(days=10), window_days=7)
    assert d.direction == "conflict"
    assert "outside 7-day window" in d.rationale
    assert _diff(_NOW, _NOW - timedelta(days=10), window_days=14).direction == "rb->djay"


def test_matcher_unified_tracks_carry_modified_at() -> None:
    """[if] source tracks expose a modification stamp [then] UnifiedTrack carries it, [else stop]."""
    assert _unified_from_rb(_FakeRB("rb-1", 5, _NOW)).modified_at == _NOW
    dj = DjayTrack(
        uuid="u-1",
        title="t",
        artist="a",
        isrc="",
        source_uri="file:///Users/dev/Music/t.mp3",
        file_path=Path("/Users/dev/Music/t.mp3"),
        is_local=True,
    )
    assert _unified_from_dj(dj).modified_at is None


def test_sync_diff_rating_rows_use_track_timestamps() -> None:
    """[if] the metadata plan passes per-track rating timestamps [then] newest-wins decides the row, [else stop]."""
    _ans, rows = build_analysis_diff(
        {},
        {},
        {"1": 2},
        {"a": 5},
        [("1", "a")],
        prefer="newest",
        rb_rating_modified_by_id={"1": _NOW - timedelta(days=1)},
        djay_rating_modified_by_uuid={"a": _NOW},
    )
    assert rows[0].resolution == "accept_djay"
