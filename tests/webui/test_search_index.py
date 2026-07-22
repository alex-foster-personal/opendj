"""FTS5 search index unit tests (global-fts5-search node).

Builds its own fixture ``state.db`` under ``tmp_path`` via the real
``apps.shared.state`` schema/writer (never touches the repo's real
data/state/state.db -- absent in CI/sandbox anyway, see test_search.py's
503 test). Exercises tokenisation/prefix-matching, bm25 ranking, EAV
(genre/comments/tags) indexing, pagination, and lazy rebuild-on-mtime.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server import search_index

_MODIFIED_AT = "2026-04-17T10:00:00.000000Z"


def _set_eav_field(conn: sqlite3.Connection, stable_id: str, field_name: str, value: object) -> None:
    # track_fields is a generic EAV table (schema.py) -- genre/comments/tags
    # are not in provenance.py's WRAPPED_FIELDS whitelist (that whitelist
    # only gates apps.shared.state.writer.StateWriter.set_field, an
    # application-level restriction, not a schema constraint), so ingest
    # paths that populate them write the row directly, same as here.
    conn.execute(
        "INSERT INTO track_fields(stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES (?, ?, ?, 'manual', NULL, ?)",
        (stable_id, field_name, json.dumps(value), _MODIFIED_AT),
    )


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """6 tracks with title/artist + genre/comments/tags EAV fields."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        tracks = [
            ("s1", "Levitating", ["Dua Lipa"], "deep-house", "great opener", ["banger"]),
            ("s2", "Blinding Lights", ["The Weeknd"], "synthwave", "peak time", ["retro"]),
            ("s3", "Bad Guy", ["Billie Eilish"], "pop", "moody", ["dark-pop"]),
            ("s4", "Weekend Vibes", ["Nobody"], "lofi", "chill background", []),
            ("s5", "Strobe", ["deadmau5"], "progressive-house", "closer track", ["classic"]),
            ("s6", "House Anthem", ["DJ House"], "trance", "banger opener", []),
        ]
        for sid, title, artists, genre, comments, tags in tracks:
            writer.upsert_track(
                stable_id=sid, stable_id_tier="inferred", title=title,
                artists=artists, album=None, isrc=None, duration_ms=200_000,
                file_path=f"/music/{sid}.mp3",
            )
            _set_eav_field(conn, sid, "genre", genre)
            _set_eav_field(conn, sid, "comments", comments)
            if tags:
                _set_eav_field(conn, sid, "tags", tags)
    finally:
        writer.close()
        conn.close()
    return path


def test_build_fts_query_empty_is_none() -> None:
    assert search_index.build_fts_query("") is None
    assert search_index.build_fts_query("   ") is None


def test_build_fts_query_quotes_and_prefixes_terms() -> None:
    assert search_index.build_fts_query("dua lip") == '"dua"* "lip"*'


def test_build_fts_query_strips_embedded_quotes() -> None:
    assert search_index.build_fts_query('"dua"') == '"dua"*'


def test_search_matches_title_prefix(fixture_db: Path) -> None:
    hits, total = search_index.search(fixture_db, "levi", limit=10)
    assert total == 1
    assert [sid for sid, _ in hits] == ["s1"]


def test_search_matches_artist(fixture_db: Path) -> None:
    hits, total = search_index.search(fixture_db, "eilish", limit=10)
    assert total == 1
    assert hits[0][0] == "s3"


def test_search_matches_genre_eav_field(fixture_db: Path) -> None:
    # "house" prefix-matches genre tokens on s1 ("deep-house") and s5
    # ("progressive-house"), plus title+artist on s6 ("House Anthem" /
    # "DJ House").
    hits, total = search_index.search(fixture_db, "house", limit=10)
    ids = {sid for sid, _ in hits}
    assert ids == {"s1", "s5", "s6"}
    assert total == 3


def test_search_matches_comments_eav_field(fixture_db: Path) -> None:
    hits, total = search_index.search(fixture_db, "chill", limit=10)
    assert total == 1
    assert hits[0][0] == "s4"


def test_search_matches_tags_eav_field(fixture_db: Path) -> None:
    # "banger" is s1's tag AND a word in s6's comments ("banger opener") --
    # both are real matches, s1 via tags (weight 1) and s6 via comments
    # (weight 2), so s6 ranks first.
    hits, total = search_index.search(fixture_db, "banger", limit=10)
    assert total == 2
    assert {sid for sid, _ in hits} == {"s1", "s6"}
    assert hits[0][0] == "s6"


def test_search_title_and_artist_beat_genre_only_in_rank(fixture_db: Path) -> None:
    # s6 matches "house" on both title (weight 10) and artist (weight 5);
    # s1/s5 only match on genre (weight 3). s6 must rank first.
    hits, _total = search_index.search(fixture_db, "house", limit=10)
    assert hits[0][0] == "s6"


def test_search_returns_match_context_snippet(fixture_db: Path) -> None:
    hits, _total = search_index.search(fixture_db, "levitating", limit=10)
    assert hits, "expected at least one hit"
    _sid, context = hits[0]
    assert "levitating" in context.lower()


def test_search_empty_query_returns_empty(fixture_db: Path) -> None:
    assert search_index.search(fixture_db, "", limit=10) == ([], 0)
    assert search_index.search(fixture_db, "   ", limit=10) == ([], 0)


def test_search_pagination_limit_and_offset(fixture_db: Path) -> None:
    # Prefix "d" matches a token on 4 of the 6 fixture tracks: "dua"
    # (s1 artist), "deep" (s1 genre), "dark" (s3 tags), "deadmau5"
    # (s5 artist), "dj" (s6 artist) -- s1 twice over, still one hit.
    hits_all, total = search_index.search(fixture_db, "d", limit=100)
    assert total == len(hits_all) == 4
    assert {sid for sid, _ in hits_all} == {"s1", "s3", "s5", "s6"}

    page1, total1 = search_index.search(fixture_db, "d", limit=2, offset=0)
    page2, total2 = search_index.search(fixture_db, "d", limit=2, offset=2)
    assert total1 == total2 == total
    assert len(page1) == 2
    assert [sid for sid, _ in page1] != [sid for sid, _ in page2]
    # Pages tile the full ranked result set in order.
    assert (
        [sid for sid, _ in page1] + [sid for sid, _ in page2]
        == [sid for sid, _ in hits_all][:4]
    )


def test_ensure_index_unavailable_when_source_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "state.db"
    with pytest.raises(search_index.SearchIndexUnavailable):
        search_index.ensure_index(missing)


def test_search_unavailable_when_source_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "state.db"
    with pytest.raises(search_index.SearchIndexUnavailable):
        search_index.search(missing, "anything", limit=10)


def test_index_rebuilds_when_source_mtime_changes(fixture_db: Path) -> None:
    hits_before, _ = search_index.search(fixture_db, "phoenix", limit=10)
    assert hits_before == []

    # mtime resolution on some filesystems is 1s; force it forward so the
    # rebuild-on-mtime-change comparison is unambiguous.
    conn = state_db.open_rw(fixture_db)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s7", stable_id_tier="inferred", title="Phoenix",
            artists=["Delta"], album=None, isrc=None, duration_ms=180_000,
            file_path="/music/s7.mp3",
        )
    finally:
        writer.close()
        conn.close()
    future = time.time() + 5
    import os
    os.utime(fixture_db, (future, future))

    hits_after, total = search_index.search(fixture_db, "phoenix", limit=10)
    assert total == 1
    assert hits_after[0][0] == "s7"


def test_index_not_rebuilt_when_source_unchanged(fixture_db: Path) -> None:
    index_path = search_index.ensure_index(fixture_db)
    first_mtime = index_path.stat().st_mtime
    # A second ensure_index with no source change must not touch the file
    # (rebuild is a DROP+recreate, which would bump the index db's mtime).
    time.sleep(0.01)
    search_index.ensure_index(fixture_db)
    assert index_path.stat().st_mtime == first_mtime


def test_index_stored_separately_from_state_db(fixture_db: Path) -> None:
    index_path = search_index.ensure_index(fixture_db)
    assert index_path != fixture_db
    assert index_path.exists()
    # state.db's own tables are untouched -- no tracks_fts / search_meta
    # leaked into the source file.
    conn = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
    try:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
    finally:
        conn.close()
    assert "tracks_fts" not in names
    assert "search_meta" not in names
