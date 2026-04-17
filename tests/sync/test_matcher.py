"""Regression tests for ``apps.sync.matcher.match_tracks`` key-lookup scoring.

Codex Phase 02 review flagged that when multiple Rekordbox candidates share
the same normalised ``title|||artist`` key, only ``rb_candidates[0]`` was
being scored, causing djay tracks to be mis-routed to an inferior RB match.
The regression guard below feeds three candidates where the first is the
worst and the third is the best, and asserts the matcher picks the third.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.shared.djay_db import DjayTrack
from apps.sync.matcher import match_tracks


pytestmark = pytest.mark.requirement("SYNC-02")


@dataclass(slots=True)
class _FakeRB:
    id: str
    title: str
    artist: str
    file_path: Path | None = None
    duration_s: float | None = None
    isrc: str = ""


def _dj(
    *,
    uuid: str,
    title: str,
    artist: str,
    isrc: str = "",
    file_path: Path | None = None,
    duration_s: float | None = None,
) -> DjayTrack:
    return DjayTrack(
        uuid=uuid,
        title=title,
        artist=artist,
        isrc=isrc,
        source_uri="",
        file_path=file_path,
        is_local=file_path is not None,
        rating=0,
        duration_s=duration_s,
        play_count=0,
        color_index=None,
    )


def test_match_tracks_scores_all_key_candidates_and_picks_best(
    tmp_path: Path,
) -> None:
    """Given 3 RB rows colliding on the normalised title+artist key, the
    matcher must score every candidate and pick the highest-scoring one.

    Candidate layout (worst -> best):

    * ``rb-worst``     -- only the fuzzy key fires (1 signal).
    * ``rb-mid``       -- key + duration fire (2 signals).
    * ``rb-best``      -- key + duration + filename_exact fire (3 signals,
      crossing the auto-accept gate).

    The djay track has ``file_path`` and ``duration_s`` set so signals can
    discriminate between candidates, but no ISRC (so the ISRC fast-path is
    skipped) and its RB-side counterpart's ``file_path`` differs enough
    that the path fast-path also misses -- forcing the key lookup.
    """
    # dj track file lives in a sub-directory so the NFC-absolute path does
    # NOT match any RB candidate's file_path (path fast-path is bypassed).
    dj_dir = tmp_path / "djay"
    dj_dir.mkdir()
    dj_file = dj_dir / "Song.mp3"
    dj_file.touch()

    # RB "best" lives in a different directory but with the SAME basename,
    # so only the filename_exact signal fires (not filename path equality).
    rb_best_dir = tmp_path / "rb_best"
    rb_best_dir.mkdir()
    rb_best_file = rb_best_dir / "Song.mp3"
    rb_best_file.touch()

    rb_mid_dir = tmp_path / "rb_mid"
    rb_mid_dir.mkdir()
    rb_mid_file = rb_mid_dir / "Other.mp3"
    rb_mid_file.touch()

    rb_worst_dir = tmp_path / "rb_worst"
    rb_worst_dir.mkdir()
    rb_worst_file = rb_worst_dir / "Different.flac"
    rb_worst_file.touch()

    # Order matters: candidate[0] is the WORST, candidate[2] is the BEST.
    # If the matcher only scored candidates[0] (the bug), it would pick
    # the worst and route the djay track to an inferior RB match.
    rb_worst = _FakeRB(
        id="rb-worst",
        title="Song",
        artist="Artist",
        file_path=rb_worst_file,
        duration_s=999.0,  # duration delta >> 0.5s
        isrc="",
    )
    rb_mid = _FakeRB(
        id="rb-mid",
        title="Song",
        artist="Artist",
        file_path=rb_mid_file,
        duration_s=180.0,  # duration matches dj
        isrc="",
    )
    rb_best = _FakeRB(
        id="rb-best",
        title="Song",
        artist="Artist",
        file_path=rb_best_file,  # basename == dj basename ("Song.mp3")
        duration_s=180.0,  # duration matches dj
        isrc="",
    )

    dj = _dj(
        uuid="dj-1",
        title="Song",
        artist="Artist",
        isrc="",  # no ISRC -> ISRC fast-path skipped
        file_path=dj_file,
        duration_s=180.0,
    )

    # Feed candidates in worst-first order (the order they'd land in
    # ``key_to_rb`` given iteration order of ``rb_tracks``).
    result = match_tracks([rb_worst, rb_mid, rb_best], [dj])

    # There must be exactly one pair and it must point to rb-best.
    pairs = list(result.matched) + list(result.review)
    assert len(pairs) == 1, (
        f"expected exactly one scored pair, got {len(pairs)}: {pairs!r}"
    )
    assert pairs[0].rb_id == "rb-best", (
        "matcher picked an inferior RB candidate -- it is only scoring "
        "rb_candidates[0] instead of all key-colliding candidates "
        f"(got rb_id={pairs[0].rb_id!r}, expected 'rb-best')"
    )
    # The pair must be at least review-quality (status != "drop") -- the
    # key regression guard is *which* RB was chosen, not the exact status.
    assert pairs[0].status in {"matched", "review"}
    # rb-worst and rb-mid must never be mistaken for the djay track --
    # they should not appear as the scored candidate on any emitted pair.
    paired_rb_ids = {p.rb_id for p in pairs}
    assert "rb-worst" not in paired_rb_ids
    assert "rb-mid" not in paired_rb_ids
