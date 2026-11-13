"""SYNC-12993: the rating conflict window is configurable through the
``metadata-diff`` CLI (``apps.audit.sync_diff``) and reaches the resolver.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

import pytest

from apps.audit import sync_diff
from apps.audit.sync_diff import build_analysis_diff
from apps.shared.normalised import NormalisedAnalysis
from apps.sync.conflict import parse_window_days


def _rating_resolution(window_days: int, gap_days: int) -> str:
    """Resolve one RB=3 vs djay=5 rating pair where djay is newer by ``gap_days``."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
    rb = NormalisedAnalysis(uuid_or_id="1", source="rb", modified_at=now - timedelta(days=gap_days))
    dj = NormalisedAnalysis(uuid_or_id="uuid-1", source="djay", modified_at=now)
    _, rating_rows = build_analysis_diff(
        {"1": rb},
        {"uuid-1": dj},
        {"1": 3},
        {"uuid-1": 5},
        [("1", "uuid-1")],
        prefer="newest",
        rating_window_days=window_days,
    )
    assert len(rating_rows) == 1
    return rating_rows[0].resolution


@pytest.mark.requirement("SYNC-12993")
@pytest.mark.parametrize("raw, expected", [("1", 1), ("7", 7), ("30", 30)])
def test_parse_window_days_accepts_positive_whole_days(raw: str, expected: int) -> None:
    """[if] a positive whole-day window is given [then] it parses to that many days, [else stop]."""
    assert parse_window_days(raw) == expected


@pytest.mark.requirement("SYNC-12993")
@pytest.mark.parametrize("raw", ["0", "-1", "-30", "1.5", "week", ""])
def test_parse_window_days_rejects_non_positive_or_non_integer(raw: str) -> None:
    """[if] the window is zero, negative or not an integer [then] it is refused, [else stop]."""
    with pytest.raises(argparse.ArgumentTypeError):
        parse_window_days(raw)


@pytest.mark.requirement("SYNC-12993")
@pytest.mark.parametrize("raw", ["0", "-3"])
def test_cli_rejects_non_positive_window(raw: str, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI gets a non-positive window [then] it exits 2 before any DB opens, [else stop]."""
    with pytest.raises(SystemExit) as exc:
        sync_diff.main(["--rating-conflict-window-days", raw])
    assert exc.value.code == 2
    assert "conflict window" in capsys.readouterr().err


@pytest.mark.requirement("SYNC-12993")
def test_rating_conflict_inside_selected_window_newest_wins() -> None:
    """[if] a 3-day gap is inside a 5-day window [then] the newer djay rating wins, [else stop]."""
    assert _rating_resolution(window_days=5, gap_days=3) == "accept_djay"


@pytest.mark.requirement("SYNC-12993")
def test_rating_conflict_outside_selected_window_is_reported() -> None:
    """[if] a 3-day gap is outside a 2-day window [then] the row is a conflict, [else stop]."""
    assert _rating_resolution(window_days=2, gap_days=3) == "conflict"


@pytest.mark.requirement("SYNC-12993")
def test_rating_window_defaults_to_seven_days() -> None:
    """[if] no window is passed [then] the 7-day default still applies, [else stop]."""
    now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)
    rb = NormalisedAnalysis(uuid_or_id="1", source="rb", modified_at=now - timedelta(days=6))
    dj = NormalisedAnalysis(uuid_or_id="uuid-1", source="djay", modified_at=now)
    _, rating_rows = build_analysis_diff({"1": rb}, {"uuid-1": dj}, {"1": 3}, {"uuid-1": 5}, [("1", "uuid-1")])
    assert rating_rows[0].resolution == "accept_djay"
