"""Ranking + local-index tests for D2 (commit 403259b4).

No Spotify call anywhere. The local half of the sweep -- the index the gap is
measured against, and the ranking that decides which playlist the maintainer is shown
first -- runs entirely off a real state.db built from the repo's own schema
statements. The paged Spotify walk (``_all_playlists`` / ``_playlist_gap``)
is recorded as untested in the audit rather than faked here.

Regression lines:
  - if the ranking stops putting the biggest absolute gap first then the
    sweep's whole point (which playlist to import next) is lost
  - if the ranking ties are not broken by name then two runs of an unchanged
    library emit different CSVs
  - if gap_pct divides by a zero total then an empty playlist crashes the run
  - if the local index stops skipping rows with no file_path then playlists
    are scored against tracks the library cannot actually play
  - if ISRCs are not upper-cased on both sides then every lower-case ISRC
    counts as a gap
  - if the artist|title key stops casefolding then case differences between
    Spotify and Rekordbox read as missing tracks
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from apps.shared.state.schema import apply_migrations
from scripts import spotify_playlist_sweep as sweep

# ----- fixtures -------------------------------------------------------------


def _state_db(tmp_path, rows):
    """Real state.db built by the repo's own migrations, seeded with `rows`.

    rows: (stable_id, title, artists, isrc, file_path)
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    apply_migrations(con)
    for stable_id, title, artists, isrc, file_path in rows:
        con.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "isrc, file_path, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                stable_id,
                "inferred",
                title,
                json.dumps(artists),
                isrc,
                file_path,
                "2026-08-16T00:00:00Z",
                "2026-08-16T00:00:00Z",
            ),
        )
    con.commit()
    con.close()
    return db


def _gap(name: str, missing: int, total: int = 100) -> sweep.PlaylistGap:
    return sweep.PlaylistGap(
        name=name, playlist_id=f"id-{name}", owner="maintainer", total=total, missing=missing
    )


# ----- ranking --------------------------------------------------------------


def test_biggest_absolute_gap_ranks_first() -> None:
    ranked = sweep.rank_gaps([_gap("small", 2), _gap("huge", 90), _gap("mid", 30)])
    assert [g.name for g in ranked] == ["huge", "mid", "small"]


def test_a_fully_owned_playlist_ranks_last() -> None:
    ranked = sweep.rank_gaps([_gap("owned", 0), _gap("one-missing", 1)])
    assert ranked[-1].name == "owned"


def test_ties_break_by_name_so_two_runs_agree() -> None:
    first = sweep.rank_gaps([_gap("zulu", 5), _gap("alpha", 5), _gap("mike", 5)])
    second = sweep.rank_gaps([_gap("mike", 5), _gap("zulu", 5), _gap("alpha", 5)])
    assert [g.name for g in first] == ["alpha", "mike", "zulu"]
    assert [g.name for g in first] == [g.name for g in second]


def test_ranking_prefers_absolute_missing_over_percentage() -> None:
    """A 3-of-3 playlist is 100 percent missing but a smaller job than 90."""
    ranked = sweep.rank_gaps([_gap("tiny-all-missing", 3, total=3), _gap("big", 90)])
    assert ranked[0].name == "big"


def test_ranking_does_not_mutate_its_input() -> None:
    original = [_gap("b", 1), _gap("a", 9)]
    sweep.rank_gaps(original)
    assert [g.name for g in original] == ["b", "a"]


# ----- gap_pct --------------------------------------------------------------


def test_fully_owned_playlist_reports_zero_pct() -> None:
    assert _gap("owned", 0).gap_pct == 0.0


def test_empty_playlist_does_not_divide_by_zero() -> None:
    assert _gap("empty", 0, total=0).gap_pct == 0.0


def test_gap_pct_is_rounded_to_one_decimal() -> None:
    assert _gap("third", 1, total=3).gap_pct == 33.3


# ----- local index ----------------------------------------------------------


def test_tracks_without_a_file_are_not_part_of_the_index(tmp_path) -> None:
    db = _state_db(tmp_path, [
        ("s1", "Playable", ["Bicep"], "GBABC1234567", "/music/a.mp3"),
        ("s2", "Metadata only", ["Bicep"], "GBXYZ7654321", None),
    ])
    isrcs, names = sweep._local_index(db)
    assert "GBABC1234567" in isrcs
    assert "GBXYZ7654321" not in isrcs
    assert "bicep|metadata only" not in names


def test_isrcs_are_upper_cased_for_matching(tmp_path) -> None:
    db = _state_db(tmp_path, [("s1", "T", ["A"], "gbabc1234567", "/music/a.mp3")])
    isrcs, _names = sweep._local_index(db)
    assert isrcs == {"GBABC1234567"}


def test_artist_title_keys_are_casefolded(tmp_path) -> None:
    db = _state_db(tmp_path, [("s1", "Glue", ["Bicep"], None, "/music/a.mp3")])
    _isrcs, names = sweep._local_index(db)
    assert "bicep|glue" in names


def test_only_the_primary_artist_forms_the_key(tmp_path) -> None:
    db = _state_db(tmp_path, [("s1", "Track", ["Lead", "Feature"], None, "/m/a.mp3")])
    _isrcs, names = sweep._local_index(db)
    assert "lead|track" in names


def test_a_track_with_no_title_is_skipped(tmp_path) -> None:
    db = _state_db(tmp_path, [("s1", None, ["A"], "GBABC1234567", "/m/a.mp3")])
    _isrcs, names = sweep._local_index(db)
    assert names == set()


def test_a_missing_state_db_exits_rather_than_scoring_everything_as_a_gap(tmp_path) -> None:
    with pytest.raises(SystemExit):
        sweep._local_index(tmp_path / "absent.db")
