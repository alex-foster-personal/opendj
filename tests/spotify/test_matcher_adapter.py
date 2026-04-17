"""Tests for apps.spotify.matcher_adapter."""
from __future__ import annotations

import json
import sqlite3

import pytest

from apps.spotify.matcher_adapter import (
    DURATION_TOLERANCE_MS,
    LocalTrack,
    load_local_tracks,
    match_spotify_tracks,
    normalise_artist,
    normalise_title,
)
from apps.spotify.client import _parse_track_item

from .fake_spotipy import make_track


def _sp(spotify_id: str, **kwargs):
    return _parse_track_item(make_track(spotify_id=spotify_id, **kwargs))


@pytest.mark.requirement("CAT-01")
class TestNormalise:
    def test_title_casefolds(self) -> None:
        assert normalise_title("Hello World") == "hello world"

    def test_title_strips_remaster(self) -> None:
        assert normalise_title("Song (Remastered 2011)") == "song"

    def test_title_strips_radio_edit(self) -> None:
        assert normalise_title("Song - Radio Edit") == "song"

    def test_title_strips_punctuation(self) -> None:
        assert normalise_title("!!Hey, Hey -- Hey!!") == "hey hey hey"

    def test_artist_casefolds(self) -> None:
        assert normalise_artist("The Prodigy") == "the prodigy"

    def test_artist_strips_punct(self) -> None:
        assert normalise_artist("A$AP Rocky!") == "a ap rocky"

    def test_title_empty(self) -> None:
        assert normalise_title("") == ""
        assert normalise_artist("") == ""


@pytest.mark.requirement("CAT-01")
class TestMatchSpotifyTracks:
    def _local(self, stable_id="s1", **kwargs) -> LocalTrack:
        defaults = dict(
            isrc="USABC2500001",
            title="Test Title",
            artists=("Test Artist",),
            duration_ms=200000,
        )
        defaults.update(kwargs)
        return LocalTrack(stable_id=stable_id, **defaults)  # type: ignore[arg-type]

    def test_isrc_exact_auto_matches(self) -> None:
        src = _sp("t1", isrc="USABC2500001")
        tgt = self._local(isrc="USABC2500001")
        res = match_spotify_tracks([src], [tgt])
        assert len(res.matched) == 1
        pair = res.matched[0]
        assert pair.target is tgt
        assert "isrc" in pair.signals

    def test_isrc_case_insensitive(self) -> None:
        src = _sp("t1", isrc="usabc2500001")
        tgt = self._local(isrc="USABC2500001")
        res = match_spotify_tracks([src], [tgt])
        assert len(res.matched) == 1

    def test_fuzzy_three_signals_lands_in_review(self) -> None:
        # No ISRC; title + artist + duration fire (0.55) but < 0.70 -> review.
        src = _sp("t1", isrc=None, name="Hello", artists=["Artist X"], duration_ms=180000)
        tgt = self._local(
            isrc=None, title="Hello", artists=("artist x",), duration_ms=180500,
        )
        res = match_spotify_tracks([src], [tgt])
        assert len(res.review) == 1
        assert res.review[0].signals == ("title", "artist", "duration")

    def test_two_signals_below_floor_is_unmatched(self) -> None:
        src = _sp("t1", isrc=None, name="Hello", artists=["X"], duration_ms=180000)
        tgt = self._local(
            isrc=None, title="Hello", artists=("Y",), duration_ms=180500,
        )
        res = match_spotify_tracks([src], [tgt])
        assert len(res.unmatched) == 1

    def test_nothing_matches_goes_to_unmatched(self) -> None:
        src = _sp("t1", isrc=None, name="Unknown", artists=["Who"], duration_ms=1000)
        tgt = self._local(isrc="USABC2500001", title="Different", artists=("Else",))
        res = match_spotify_tracks([src], [tgt])
        assert len(res.unmatched) == 1
        assert res.unmatched[0].target is None

    def test_duration_tolerance_is_symmetric(self) -> None:
        base = self._local(isrc=None, title="X", artists=("A",), duration_ms=100000)
        src_ok = _sp("ok", isrc=None, name="X", artists=["A"],
                     duration_ms=100000 + DURATION_TOLERANCE_MS - 1)
        src_bad = _sp("bad", isrc=None, name="X", artists=["A"],
                      duration_ms=100000 + DURATION_TOLERANCE_MS + 1)
        res = match_spotify_tracks([src_ok, src_bad], [base])
        pairs_by_id = {p.source.spotify_id: p for p in res.pairs}
        assert "duration" in pairs_by_id["ok"].signals
        assert "duration" not in pairs_by_id["bad"].signals

    def test_match_rate(self) -> None:
        src1 = _sp("a", isrc="USABC2500001")
        src2 = _sp("b", isrc=None, name="Nope", artists=["Who"], duration_ms=1)
        tgt = self._local(isrc="USABC2500001")
        res = match_spotify_tracks([src1, src2], [tgt])
        assert res.match_rate == pytest.approx(0.5)


@pytest.mark.requirement("CAT-01")
def test_load_local_tracks_from_sqlite(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE tracks (
            stable_id TEXT PRIMARY KEY, stable_id_tier TEXT, title TEXT,
            artists_json TEXT, album TEXT, isrc TEXT, duration_ms INTEGER,
            file_path TEXT, content_hash TEXT,
            created_at TEXT, updated_at TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", "isrc", "Hello", json.dumps(["A", "B"]), "Alb", "USABC2500001",
         200000, "/x.mp3", None, "2026-01-01", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("s2", "inferred", "Bad", "{not json", "", None, 1000, None, None, "x", "x"),
    )
    conn.commit()

    tracks = load_local_tracks(conn)
    by_id = {t.stable_id: t for t in tracks}
    assert by_id["s1"].artists == ("A", "B")
    assert by_id["s1"].isrc == "USABC2500001"
    assert by_id["s2"].artists == ()
