"""The safe tier auto-applies only audited transforms: whitespace trimming is not one of them."""

from __future__ import annotations

import pytest

from apps.tags import title_fix
from apps.tags.title_fix import Track


def _safe(title: str, artist: str) -> bool:
    track = Track("t1", title, artist)
    return title_fix.safe_tier(track, title_fix.regex_propose(track))


@pytest.mark.requirement("TFIX-05")
def test_safe_tier_still_auto_applies_mik_prefix_only_rows() -> None:
    """[if] only a MIK prefix strips on a filled artist [then] safe tier still applies, [else stop]."""
    assert _safe("8A - 6 - Ember Tide", "Vessel Choir"), "split must not change safe-tier projection"


@pytest.mark.requirement("TFIX-05")
def test_a_padded_artist_or_title_never_reaches_the_safe_tier() -> None:
    """[if] the raw artist or title carries leading or trailing whitespace [then] the row is not safe tier, [else stop]."""
    assert _safe("8A - 6 - Ember Tide", "Vessel Choir"), "control: the same row, unpadded, is safe tier"
    assert not _safe("8A - 6 - Ember Tide", " Vessel Choir "), (
        "if trimming rides in the safe tier then an unaudited artist edit auto-applies"
    )
    assert not _safe(" 8A - 6 - Ember Tide ", "Vessel Choir"), (
        "if a padded title is safe tier then trimming is unaudited"
    )
