"""LIBM-178: the non-music classifier tells rekordbox Sampler content from songs.

[if] a row is rekordbox Sampler content by path or genre [then] it is non-music, [else stop].

the maintainer, Tue 6 Oct 2026: "This isn't a music track - I thought we auto-filtered
non-music out of the library?" The row was a 13 s GROOVE CIRCUIT
preset. The path shapes below are the three Sampler banks CORE
measured on the preview library (38 live rows: MERGE FX, GROOVE CIRCUIT,
OSC_SAMPLER).

Regression one-liners:
  - if a Sampler path or a sample genre is classified as music then broken
  - if a short song, or a song whose title or folder says sampler, is non-music then broken
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apps.shared.non_music import (
    SHORT_CANDIDATE_MS,
    is_non_music,
    is_short_candidate,
    non_music_reason,
)

pytestmark = pytest.mark.requirement("LIBM-178")

RB = "/Users/dev/Music/rekordbox/Sampler"


@dataclass
class _Row:
    file_path: str | None = None
    genre: str | None = None
    duration_ms: int | None = 180_000


@pytest.mark.parametrize(
    ("path", "genre"),
    [
        (f"{RB}/GROOVE CIRCUIT/PRESET/Quillfen Kit/Quillfen Loop Nine.wav", "Loop Samples"),
        (f"{RB}/GROOVE CIRCUIT/PRESET/Quillfen Kit/Quillfen Loop Nine.wav", None),
        (f"{RB}/MERGE FX/Riser/MERGE FX 01.wav", None),
        (f"{RB}/OSC_SAMPLER/PRESET/ONESHOT/Horn.wav", None),
        ("C:\\Users\\dev\\Music\\rekordbox\\Sampler\\MERGE FX\\Riser 02.wav", None),
        ("/Users/dev/Music/REKORDBOX/sampler/x.wav", None),
        ("/Volumes/USB/elsewhere/Quillfen Loop Nine.wav", "Loop Samples"),
        ("/Volumes/USB/elsewhere/Horn.wav", " one shot samples "),
    ],
)
def test_sampler_content_is_non_music(path: str, genre: str | None) -> None:
    """[if] a Sampler path or sample genre [then] non-music, [else stop]."""
    assert is_non_music(_Row(file_path=path, genre=genre, duration_ms=13_000))


def test_reason_names_the_rule() -> None:
    """[if] path and genre both match [then] the path rule is named first, [else stop]."""
    assert non_music_reason(_Row(f"{RB}/MERGE FX/a.wav", "Loop Samples")) == "sampler-path"
    assert non_music_reason(_Row("/m/a.wav", "Loop Samples")) == "sample-genre"
    assert non_music_reason(_Row("/m/a.wav", "House")) is None


def test_listing_genre_overrides_the_tracks_own() -> None:
    """[if] the display genre is a sample genre [then] non-music, [else stop]."""
    assert is_non_music(_Row("/m/a.wav", None), "Loop Samples")
    assert not is_non_music(_Row("/m/a.wav", None), "Techno")


def test_a_given_genre_replaces_a_stale_track_genre() -> None:
    """[if] the caller's genre is music [then] a stale sample genre on the track is ignored, [else stop]."""
    assert not is_non_music(_Row("/m/a.wav", "Loop Samples"), "House")
    assert is_non_music(_Row("/m/a.wav", "Loop Samples"))


@pytest.mark.parametrize(
    "row",
    [
        _Row("/Users/dev/Music/Radio Edits/Intro.mp3", "House", 12_000),
        _Row("/Users/dev/Music/rekordbox/Quillfen Loop Nine.wav", "Breaks"),
        _Row("/Users/dev/Music/Sampler/Sampler Track.mp3", "Techno"),
        _Row("/Users/dev/Music/rekordbox-sampler-tribute/Song.mp3", "Techno"),
        _Row("/Users/dev/Music/Loop Samples - Song.mp3", "Loop Samples Remix"),
        _Row(None, None, None),
    ],
)
def test_songs_are_music(row: _Row) -> None:
    """[if] a song, however short or sampler-titled [then] music, [else stop]."""
    assert not is_non_music(row)


def test_short_rows_are_candidates_only() -> None:
    """[if] a music row under 30 s [then] a candidate, never non-music, [else stop]."""
    short = _Row("/m/intro.mp3", "House", SHORT_CANDIDATE_MS - 1)
    assert is_short_candidate(short)
    assert not is_non_music(short)
    assert not is_short_candidate(_Row("/m/song.mp3", "House", SHORT_CANDIDATE_MS))
    assert not is_short_candidate(_Row(f"{RB}/MERGE FX/a.wav", None, 5_000))
