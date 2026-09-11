"""Three-tier matcher tests: tier order, ambiguity, collisions, determinism.

[if] a song matches 0/1/many tracks [then] match_songs resolves/is ambiguous, [else stop].
"""
from __future__ import annotations

import sqlite3
import unicodedata

import pytest

from apps.mik import match as matcher
from apps.mik.mikdb import MikSong
from apps.shared.state.locations import upsert_location

pytestmark = pytest.mark.requirement("META-01")


def _song(
    row_id: int,
    *,
    path: str | None = None,
    artist: str | None = None,
    title: str | None = None,
    confidence: float | None = 0.9,
) -> MikSong:
    return MikSong(
        row_id=row_id,
        title=title,
        artist=artist,
        album=None,
        path=path,
        path_error=None,
        key_camelot="8A",
        key_confidence=confidence,
        energy=6,
        bpm=124.0,
        loudness=-11.0,
        clipped_peak_count=0,
        analysed_at="2024-01-01T00:00:00+00:00",
    )


def test_exact_path_wins_over_fuzzy_tiers(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, file_path="/Users/user/Music/x.mp3", title="X", artist="Y")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(
        [_song(1, path="/Users/user/Music/x.mp3", artist="Y", title="X")], index
    )
    assert report.matches[1].tier == "exact_path"


def test_basename_tier_matches_a_moved_file(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """No MIK path resolves on disk, so basename is load-bearing, not optional."""
    add_track("a" * 40, file_path="/Volumes/NEW/x.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs([_song(1, path="/Users/old/Music/x.mp3")], index)
    assert report.matches[1].tier == "basename"
    assert report.matches[1].is_fuzzy is True


def test_exact_path_tier_finds_a_track_locations_alternate(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """P1 regression (PR #383 review): a recovered copy recorded only in
    track_locations (this machine's alternate path) must still be findable
    by the exact_path tier, matching what apps.mik.availability.probe
    already recognizes as playable through the shared location picker."""
    add_track("a" * 40, file_path="/nope/legacy.mp3")
    upsert_location(
        state_conn, stable_id="a" * 40, kind="local", file_path="/found/alt.mp3"
    )
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs([_song(1, path="/found/alt.mp3")], index)
    assert report.matches[1].tier == "exact_path"
    assert report.matches[1].stable_id == "a" * 40


def test_artist_title_tier_strips_the_energy_prefix(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, title="Control (Extended Mix)", artist="Eelke Kleijn")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(
        [_song(1, artist="Eelke Kleijn", title="7 - Control (Extended Mix)")], index
    )
    assert report.matches[1].tier == "artist_title"


def test_ambiguous_tier_yields_no_stable_id(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, file_path="/one/x.mp3")
    add_track("b" * 40, file_path="/two/x.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs([_song(1, path="/three/x.mp3")], index)
    assert 1 not in report.matches
    assert report.unmatched[1].reason == "ambiguous_candidates"


def test_collision_winner_is_deterministic_and_losers_are_recorded(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    songs = [
        _song(7, path="/elsewhere/x.mp3", confidence=0.4),  # basename tier
        _song(3, path="/Users/user/x.mp3", confidence=0.1),  # exact path tier
        _song(9, path="/other/x.mp3", confidence=0.99),  # basename tier
    ]
    report = matcher.match_songs(songs, index)
    assert list(report.matches) == [3]  # exact path beats higher confidence
    assert report.unmatched[7].reason == "lost_collision"
    assert report.unmatched[9].reason == "lost_collision"
    # Same tier: highest key confidence wins.
    report2 = matcher.match_songs(songs[::2], index)
    assert set(report2.matches) == {9}


def test_every_song_lands_in_exactly_one_bucket(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, file_path="/Users/user/x.mp3")
    songs = [
        _song(1, path="/Users/user/x.mp3"),
        _song(2, path="/Users/user/nope.mp3"),
        _song(3),
    ]
    report = matcher.match_songs(songs, index=matcher.TrackIndex.from_conn(state_conn))
    assert len(report.matches) + len(report.unmatched) == len(songs)
    assert set(report.matches) & set(report.unmatched) == set()


def test_exact_path_only_mode_refuses_the_fuzzy_tiers(
    state_conn: sqlite3.Connection, add_track
) -> None:
    add_track("a" * 40, file_path="/Volumes/NEW/x.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs(
        [_song(1, path="/Users/old/x.mp3")], index, allow_fuzzy=False
    )
    assert report.matches == {}
    assert report.unmatched[1].reason == "no_candidate"


def test_matching_is_deterministic(state_conn: sqlite3.Connection, add_track) -> None:
    add_track("a" * 40, file_path="/one/x.mp3")
    add_track("b" * 40, file_path="/Users/user/x.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    songs = [_song(i, path=f"/mik/{i}/x.mp3") for i in range(1, 6)]
    first = matcher.match_songs(songs, index)
    second = matcher.match_songs(list(reversed(songs)), index)
    assert first.matches == second.matches
    assert {k: v.reason for k, v in first.unmatched.items()} == {
        k: v.reason for k, v in second.unmatched.items()
    }


def test_fuzzy_keys_normalise_case() -> None:
    assert matcher.basename_key("/a/THE TRACK.MP3") == matcher.basename_key(
        "/b/the track.mp3"
    )


def test_fuzzy_keys_normalise_nfd_to_nfc() -> None:
    """macOS hands out NFD filenames; MIK blobs and state.db can disagree."""
    name = "Cafe\u0301 del Mar.mp3"
    nfc = unicodedata.normalize("NFC", name)
    nfd = unicodedata.normalize("NFD", name)
    assert nfc != nfd  # different bytes, same name
    assert matcher.basename_key(f"/a/{nfc}") == matcher.basename_key(f"/b/{nfd}")
    assert matcher.norm_path(f"/a/{nfd}") == f"/a/{nfc}"


def test_exact_path_tier_preserves_case(
    state_conn: sqlite3.Connection, add_track
) -> None:
    """Paths are data: two paths differing only in case are not the same path."""
    add_track("a" * 40, file_path="/Users/user/X.mp3")
    index = matcher.TrackIndex.from_conn(state_conn)
    report = matcher.match_songs([_song(1, path="/Users/user/x.mp3")], index)
    assert report.matches[1].tier == "basename"
