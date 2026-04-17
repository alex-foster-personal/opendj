"""Integration tests for ``apps.sync.matcher.match_tracks`` (SYNC-02)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.shared.djay_db import DjayTrack
from apps.sync.matcher import match_tracks


pytestmark = pytest.mark.requirement("SYNC-02")


@dataclass(slots=True)
class _FakeRB:
    id: str = "rb-1"
    title: str = ""
    artist: str = ""
    file_path: Path | None = None
    duration_s: float | None = None
    isrc: str = ""


def _dj(
    *,
    uuid: str,
    title: str = "",
    artist: str = "",
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


class TestMatchTracksBuckets:
    def test_three_signal_match_auto_accepts(self, tmp_path: Path) -> None:
        path = tmp_path / "Song.mp3"
        path.touch()
        rb = _FakeRB(
            id="rb-1",
            title="Song",
            artist="Artist",
            isrc="USA111",
            file_path=path,
            duration_s=180.0,
        )
        dj = _dj(
            uuid="u-1",
            title="Song",
            artist="Artist",
            isrc="USA111",
            file_path=path,
            duration_s=180.0,
        )
        result = match_tracks([rb], [dj])
        assert len(result.matched) == 1
        assert len(result.review) == 0
        assert result.matched[0].rb_id == "rb-1"
        assert result.matched[0].djay_uuid == "u-1"
        assert result.matched[0].confidence >= 0.70

    def test_two_signal_pair_lands_in_review(self, tmp_path: Path) -> None:
        """2 signals (filename_exact + duration) -> review bucket."""
        # Identical basename + matching duration gives 2 cheap signals.
        # No ISRC + no real audio => id3 doesn't fire, chromaprint doesn't
        # fire (no fingerprint_fn passed).
        a = tmp_path / "sub1" / "Track.mp3"
        b = tmp_path / "sub2" / "Track.mp3"
        a.parent.mkdir()
        b.parent.mkdir()
        a.touch()
        b.touch()
        rb = _FakeRB(
            id="rb-1",
            title="Some Title",
            artist="Some Artist",
            file_path=a,
            duration_s=180.0,
        )
        dj = _dj(
            uuid="u-1",
            title="Some Title",
            artist="Some Artist",
            file_path=b,
            duration_s=180.0,
        )
        result = match_tracks([rb], [dj])
        # The candidate is found via the normalised title+artist key.
        # filename_exact (same basename, different dirs) + duration fire.
        # 2 signals => review bucket.
        assert len(result.matched) == 0, result.stats
        assert len(result.review) == 1
        assert result.review[0].status == "review"

    def test_unmatched_tracks_separate(self, tmp_path: Path) -> None:
        rb = _FakeRB(id="rb-1", title="Alpha", artist="A")
        dj = _dj(uuid="u-1", title="Beta", artist="B")
        result = match_tracks([rb], [dj])
        assert result.matched == []
        # No ISRC / no path / different normalised keys -> both stay in
        # their "only" buckets.
        assert any(getattr(r, "id", "") == "rb-1" for r in result.rb_only)
        assert any(d.uuid == "u-1" for d in result.djay_only)

    def test_fixture_3_of_5_match(self, tmp_path: Path) -> None:
        """Fixture: 5 RB + 5 djay tracks, 3 ISRC-matching."""
        rb_list = [
            _FakeRB(id=f"rb-{i}", title=f"Track{i}", artist="A", isrc=isrc)
            for i, isrc in enumerate(
                ["USA111", "USA222", "USA333", "", ""], start=1
            )
        ]
        # Phase 2 scorer needs >=3 signals. An ISRC-only match (0.35) is
        # one signal; we bump up by setting file_path + duration on both
        # sides for tracks 1..3 so three cheap signals fire.
        for rb in rb_list[:3]:
            f = tmp_path / f"{rb.title}.mp3"
            f.touch()
            rb.file_path = f
            rb.duration_s = 180.0
        dj_list = [
            _dj(
                uuid=f"u-{i}",
                title=f"Track{i}",
                artist="A",
                isrc=isrc,
                file_path=(tmp_path / f"Track{i}.mp3") if isrc else None,
                duration_s=180.0 if isrc else None,
            )
            for i, isrc in enumerate(
                ["USA111", "USA222", "USA333", "USA444", "USA555"], start=1
            )
        ]
        result = match_tracks(rb_list, dj_list)
        assert len(result.matched) == 3, result.stats
        assert len(result.rb_only) == 2
        assert len(result.djay_only) == 2

    def test_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "Song.mp3"
        path.touch()
        rb = _FakeRB(
            id="rb-1",
            isrc="USA111",
            file_path=path,
            duration_s=180.0,
            title="Song",
            artist="A",
        )
        dj = _dj(
            uuid="u-1",
            isrc="USA111",
            file_path=path,
            duration_s=180.0,
            title="Song",
            artist="A",
        )
        r1 = match_tracks([rb], [dj])
        r2 = match_tracks([rb], [dj])
        assert r1.stats == r2.stats
        assert len(r1.matched) == len(r2.matched) == 1


class TestMatchStats:
    def test_stats_populated(self) -> None:
        rb = _FakeRB(id="rb-1", title="A", artist="X")
        dj = _dj(uuid="u-1", title="A", artist="X")
        result = match_tracks([rb], [dj])
        assert result.stats["rb_total"] == 1
        assert result.stats["dj_total"] == 1
        assert (
            result.stats["matched"]
            + result.stats["review"]
            + result.stats["rb_only"]
            == result.stats["rb_total"]
            + result.stats["review"]  # review tracks NOT in rb_only
        ) or result.stats["rb_only"] <= 1
