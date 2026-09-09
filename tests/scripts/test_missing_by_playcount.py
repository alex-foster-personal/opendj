"""Gap-ranking tests for D3 (commit 7753f158).

Covers both halves of the unit: scripts/missing_by_playcount.py ranks the
library's missing files by how often the maintainer actually played them, and
scripts/gap_sheet.py turns that ranking into the choice sheet. No live
Rekordbox DB and no gws call -- the query runs against a real sqlite file
built here with the same djmdContent/djmdArtist shape, and the sheet is
checked at the grid it would upload.

Regression lines:
  - if the query stops ordering by DJPlayCount DESC then the "most played
    missing track" is no longer at the top and the ranking is worthless
  - if --min-plays stops filtering then never-played tracks pad the sheet
  - if a track whose file is present is not dropped then the gap list reports
    tracks that need no action
  - if a dead-machine path is classified as gone (or present) then the
    remediation for it is wrong
  - if the rating is not folded from Rekordbox's 0/51/../255 to 0-5 then the
    sheet shows 255-star tracks
  - if two runs of the CSV writer differ byte for byte then the output cannot
    be diffed between sweeps
  - if the sheet's first column stops being the empty 'buy' choice column
    then the maintainer has nowhere to record the decision
  - if the sheet's store columns stop coming from SOURCE_TEMPLATES then the
    sheet and the in-repo acquisition queue drift
"""

from __future__ import annotations

import csv
import sqlite3

import pytest

from apps.spotify.acquisition import SOURCE_TEMPLATES
from scripts import gap_sheet
from scripts import missing_by_playcount as mbp

#: Fake, obviously-not-real stale-home prefix used by tests. Never a real
#: machine name (#910) -- the value only has to be consistent within a test.
_TEST_DEAD_HOME = "/Users/test-dead-home/"

# ----- fixtures -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _dead_home_prefix_configured(monkeypatch):
    """Most tests below don't care about dead-home matching at all, but
    scripts.missing_by_playcount._classify reads MDT_DEAD_HOME_PREFIXES for
    every non-empty path, so it must be set for those tests to run. The
    fail-fast test explicitly deletes it again.
    """
    monkeypatch.setenv(mbp.DEAD_HOME_PREFIXES_ENV, _TEST_DEAD_HOME)


def _make_db(tmp_path, rows):
    """A real sqlite file with the two Rekordbox tables the query joins."""
    db = tmp_path / "master.plain.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE djmdContent (ID TEXT, DJPlayCount INTEGER, Rating INTEGER, "
        "Title TEXT, ArtistID TEXT, FolderPath TEXT)"
    )
    con.execute("CREATE TABLE djmdArtist (ID TEXT, Name TEXT)")
    for artist_id, name in {r[4]: r[5] for r in rows}.items():
        con.execute("INSERT INTO djmdArtist VALUES (?, ?)", (artist_id, name))
    for i, (plays, rating, title, folder, artist_id, _name) in enumerate(rows):
        con.execute(
            "INSERT INTO djmdContent VALUES (?, ?, ?, ?, ?, ?)",
            (str(i), plays, rating, title, artist_id, folder),
        )
    con.commit()
    con.close()
    return db


# ----- residency classification ---------------------------------------------


def test_a_real_local_file_is_present(tmp_path) -> None:
    track = tmp_path / "track.mp3"
    track.write_bytes(b"audio bytes")
    assert mbp._classify(str(track)) == "present"


def test_a_path_that_does_not_exist_is_gone(tmp_path) -> None:
    assert mbp._classify(str(tmp_path / "nope.mp3")) == "missing-gone"


def test_an_empty_path_is_its_own_status() -> None:
    assert mbp._classify("") == "missing-no-path"
    assert mbp._classify(None) == "missing-no-path"


def test_a_dead_machine_home_is_flagged_before_the_filesystem_is_consulted() -> None:
    """The configured dead-home prefix is matched -- never a live stat."""
    assert mbp._classify(f"{_TEST_DEAD_HOME}Music/x.mp3") == "missing-dead-machine"


def test_dead_home_prefix_env_unset_fails_fast(monkeypatch) -> None:
    """No hardcoded fallback (#910): an unset var must not silently match
    zero rows, it must stop the run.
    """
    monkeypatch.delenv(mbp.DEAD_HOME_PREFIXES_ENV, raising=False)
    with pytest.raises(SystemExit):
        mbp._classify("/Users/whoever/Music/x.mp3")


def test_dead_home_prefix_env_set_to_test_value_matches() -> None:
    """The match mechanism itself (not just the env plumbing) still works
    once a real value is supplied at runtime.
    """
    assert mbp._classify(f"{_TEST_DEAD_HOME}Music/x.mp3") == "missing-dead-machine"
    assert mbp._classify("/Users/someone-else/Music/x.mp3") != "missing-dead-machine"


def test_a_directory_is_not_a_present_file(tmp_path) -> None:
    assert mbp._classify(str(tmp_path)) == "missing-stub"


# ----- ranking --------------------------------------------------------------


def test_query_ranks_by_play_count_descending(tmp_path) -> None:
    db = _make_db(tmp_path, [
        (3, 51, "Middle", "/gone/b.mp3", "a1", "Artist One"),
        (9, 102, "Most played", "/gone/a.mp3", "a1", "Artist One"),
        (1, 0, "Least", "/gone/c.mp3", "a2", "Artist Two"),
    ])
    raw = mbp._fetch_played_tracks(db, 1)
    assert [r[0] for r in raw] == [9, 3, 1]
    assert raw[0][2] == "Most played"


def test_min_plays_filters_out_never_played_tracks(tmp_path) -> None:
    db = _make_db(tmp_path, [
        (5, 0, "Played", "/gone/a.mp3", "a1", "A"),
        (0, 0, "Never", "/gone/b.mp3", "a1", "A"),
    ])
    assert [r[2] for r in mbp._fetch_played_tracks(db, 1)] == ["Played"]


def test_a_missing_decrypted_copy_exits_rather_than_returning_empty(tmp_path) -> None:
    with pytest.raises(SystemExit):
        mbp._fetch_played_tracks(tmp_path / "not-there.db", 1)


def test_present_tracks_are_dropped_from_the_gap_rows(tmp_path) -> None:
    here = tmp_path / "here.mp3"
    here.write_bytes(b"x")
    rows = mbp._build_rows([
        (9, 255, "Owned", "Artist", str(here)),
        (4, 51, "Missing", "Artist", "/gone/x.mp3"),
    ])
    assert [r.title for r in rows] == ["Missing"]


def test_rekordbox_rating_is_folded_to_stars() -> None:
    rows = mbp._build_rows([
        (1, 255, "Five star", "A", "/gone/a.mp3"),
        (1, 51, "One star", "A", "/gone/b.mp3"),
        (1, 0, "Unrated", "A", "/gone/c.mp3"),
    ])
    assert [r.rating for r in rows] == [5, 1, 0]


def test_gap_rows_preserve_the_query_ranking(tmp_path) -> None:
    db = _make_db(tmp_path, [
        (2, 0, "Second", "/gone/b.mp3", "a1", "A"),
        (7, 0, "First", "/gone/a.mp3", "a1", "A"),
    ])
    rows = mbp._build_rows(mbp._fetch_played_tracks(db, 1))
    assert [r.plays for r in rows] == [7, 2]


# ----- csv ------------------------------------------------------------------


def test_csv_header_and_rows(tmp_path) -> None:
    out = tmp_path / "out" / "gaps.csv"
    rows = mbp._build_rows([(6, 153, "T", "A", "/gone/t.mp3")])
    mbp._write_csv(rows, out)
    parsed = list(csv.reader(out.open()))
    assert parsed[0] == list(mbp.CSV_COLUMNS)
    assert parsed[1] == ["6", "3", "T", "A", "missing-gone", "/gone/t.mp3"]


def test_csv_is_byte_identical_across_two_runs(tmp_path) -> None:
    rows = mbp._build_rows([
        (6, 153, "T", "A", "/gone/t.mp3"),
        (2, 0, "U", "B", "/gone/u.mp3"),
    ])
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    mbp._write_csv(rows, first)
    mbp._write_csv(rows, second)
    assert first.read_bytes() == second.read_bytes()


# ----- choice sheet ---------------------------------------------------------


def _csv_with(tmp_path, rows) -> "object":
    path = tmp_path / "missing-by-playcount.csv"
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(mbp.CSV_COLUMNS)
        writer.writerows(rows)
    return path


def test_sheet_rows_respect_min_plays_and_limit(tmp_path) -> None:
    path = _csv_with(tmp_path, [
        [9, 5, "A", "Artist", "missing-gone", "/x/a.mp3"],
        [3, 4, "B", "Artist", "missing-gone", "/x/b.mp3"],
        [1, 0, "C", "Artist", "missing-gone", "/x/c.mp3"],
    ])
    assert [r["title"] for r in gap_sheet._load_rows(path, 2, 10)] == ["A", "B"]
    assert [r["title"] for r in gap_sheet._load_rows(path, 1, 1)] == ["A"]


def test_missing_csv_exits_naming_the_script_to_run(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        gap_sheet._load_rows(tmp_path / "absent.csv", 1, 10)
    assert "missing_by_playcount" in str(exc.value)


def test_grid_puts_an_empty_buy_column_first_and_freezes_nothing_else(tmp_path) -> None:
    rows = gap_sheet._load_rows(
        _csv_with(tmp_path, [[9, 5, "Title", "Artist", "missing-gone", "/x/a.mp3"]]), 1, 10
    )
    grid = gap_sheet._build_grid(rows)
    assert grid[0][0] == "buy"
    assert grid[1][0] == ""
    assert grid[1][1:6] == ["9", "5", "Title", "Artist", "missing-gone"]


def test_grid_store_columns_come_from_source_templates(tmp_path) -> None:
    rows = gap_sheet._load_rows(
        _csv_with(tmp_path, [[9, 5, "Title", "Artist", "missing-gone", "/x/a.mp3"]]), 1, 10
    )
    grid = gap_sheet._build_grid(rows)
    store_names = [name for name, _key, _t in SOURCE_TEMPLATES]
    assert grid[0][len(gap_sheet.BASE_COLUMNS):] == store_names
    links = grid[1][len(gap_sheet.BASE_COLUMNS):]
    assert len(links) == len(SOURCE_TEMPLATES)
    assert all(cell.startswith("=HYPERLINK(") for cell in links)


def test_store_links_url_encode_the_artist_and_title() -> None:
    links = gap_sheet._store_links("Bicep", "Glue (Extended)")
    assert all("Bicep+Glue" in url for url in links)
    assert all(" " not in url for url in links)
