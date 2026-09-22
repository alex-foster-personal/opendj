"""Phase 4 SYNC-05/06: conflict resolver tests."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.sync.conflict import resolve_conflict

pytestmark = [pytest.mark.requirement("SYNC-05"), pytest.mark.requirement("SYNC-06")]


def test_equal_values_no_change():
    assert resolve_conflict("rating", 3, 3) == "no_change"


def test_one_side_empty_accepts_other():
    assert resolve_conflict("bpm", None, 128.0) == "accept_djay"
    assert resolve_conflict("bpm", 128.0, None) == "accept_rb"


def test_rating_zero_rule_djay_wins():
    assert resolve_conflict("rating", 0, 3) == "accept_djay"


def test_rating_zero_rule_rb_wins():
    assert resolve_conflict("rating", 5, 0) == "accept_rb"


def test_rating_both_nonzero_follows_prefer_rb():
    assert resolve_conflict("rating", 5, 3, prefer="rb") == "accept_rb"


def test_rating_both_nonzero_follows_prefer_djay():
    assert resolve_conflict("rating", 5, 3, prefer="djay") == "accept_djay"


def test_newest_within_window_prefers_newer():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=3)
    res = resolve_conflict("rating", 2, 5, rb_modified_at=older, djay_modified_at=now)
    assert res == "accept_djay"


def test_newest_when_rb_is_newer():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=2)
    res = resolve_conflict("rating", 5, 2, rb_modified_at=now, djay_modified_at=older)
    assert res == "accept_rb"


def test_outside_window_defaults_to_rb():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=30)
    res = resolve_conflict("bpm", 128.0, 130.0, rb_modified_at=now, djay_modified_at=older)
    assert res == "accept_rb"


def test_prefer_rb_when_rb_older_is_conflict():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=1)
    res = resolve_conflict(
        "rating", 5, 3, rb_modified_at=older, djay_modified_at=now, prefer="rb"
    )
    assert res == "conflict"


def test_prefer_djay_when_djay_older_is_conflict():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=1)
    res = resolve_conflict(
        "rating", 5, 3, rb_modified_at=now, djay_modified_at=older, prefer="djay"
    )
    assert res == "conflict"


def test_manual_bpm_priority_when_one_side_empty():
    assert resolve_conflict("manual_bpm", None, 128.5) == "accept_djay"


def test_float_equality_tolerates_small_epsilon():
    assert resolve_conflict("bpm", 128.0, 128.0000001) == "no_change"


def test_string_empty_is_empty():
    assert resolve_conflict("tags", "", "house") == "accept_djay"
    assert resolve_conflict("tags", "techno", "") == "accept_rb"


def test_both_timestamps_none_within_window_fallback():
    res = resolve_conflict("rating", 5, 3)
    assert res == "accept_rb"


def test_string_equality_no_change():
    assert resolve_conflict("key_camelot", "8A", "8A") == "no_change"


def test_unknown_field_still_resolves():
    res = resolve_conflict("xyz", None, "abc")
    assert res == "accept_djay"


def test_prefer_rb_default_when_no_timestamps():
    res = resolve_conflict("energy", 3, 5, prefer="rb")
    assert res == "accept_rb"


def test_window_boundary_seven_days_exact():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=7)
    res = resolve_conflict("rating", 2, 5, rb_modified_at=older, djay_modified_at=now)
    assert res == "accept_djay"


def test_window_boundary_eight_days_out():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    older = now - timedelta(days=8)
    res = resolve_conflict("bpm", 128.0, 130.0, rb_modified_at=older, djay_modified_at=now)
    assert res == "conflict"


def test_rating_both_nonzero_newest_tie():
    now = datetime(2026, 4, 17, tzinfo=UTC)
    res = resolve_conflict(
        "rating", 5, 3, rb_modified_at=now, djay_modified_at=now, prefer="newest"
    )
    assert res == "conflict"
