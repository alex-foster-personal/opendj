"""Boundary cases for title-fix duration: long recording (ms) and gig sting length."""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.tags.title_fix import _describe_project, project_rules, provenance_tags
from apps.webui.server.title_fix_service import _scan_track


@pytest.mark.requirement("TFIX-07")
def test_long_recording_over_20_min_tags_dj_mix_not_at_exactly_20_min() -> None:
    """[if] no artist and duration 1_200_500 ms [then] provenance includes dj-mix and not at 1_200_000 ms, [else stop]."""
    track, _ = _scan_track("s1", "Afternoon Mix", "[]", None, 1_200_500, "Library/mix.wav")
    assert track.length_s == 1200.5
    tags_over = provenance_tags(track, False)
    assert "dj-mix" in tags_over
    assert "live-set" not in tags_over

    track_exact, _ = _scan_track("s2", "Afternoon Mix", "[]", None, 1_200_000, "Library/mix.wav")
    assert track_exact.length_s == 1200.0
    tags_exact = provenance_tags(track_exact, False)
    assert "dj-mix" not in tags_exact
    assert "live-set" not in tags_exact

    assert "1200.5 s" in _describe_project(track)


@pytest.mark.requirement("TFIX-07")
def test_lossless_bare_clip_just_under_sting_max_is_still_gig_intro_or_sting() -> None:
    """[if] a lossless bare clip is just under STING_MAX_S [then] gig-intro-or-sting still fires, [else stop]."""
    track, _ = _scan_track("s3", "Segment", "[]", None, 19_999, "Library/clip.wav")
    bare = replace(track, album="", label="", year=0)
    strong, _weak = project_rules(bare)
    assert "gig-intro-or-sting" in strong
