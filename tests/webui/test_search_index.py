"""FTS5 search index unit tests (global-fts5-search node).

Builds its own fixture ``state.db`` under ``tmp_path`` via the real
``apps.shared.state`` schema/writer (never touches the repo's real
(private path) -- absent in CI/sandbox anyway, see test_search.py's
503 test). Exercises tokenisation/prefix-matching, bm25 ranking, EAV
(genre/comments/notes/tags) indexing, pagination, and lazy refresh when
SQLite's main database or active WAL changes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from apps.genre_infer import findings
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
            ("s1", "Levitating", ["Dua Lipa"], "Future Nostalgia", "deep-house", "great opener", ["banger"]),
            ("s2", "Blinding Lights", ["The Weeknd"], "After Hours", "synthwave", "peak time", ["retro"]),
            ("s3", "Bad Guy", ["Billie Eilish"], "When We All Fall Asleep", "pop", "moody", ["dark-pop"]),
            ("s4", "Weekend Vibes", ["Nobody"], "Lo-Fi Sundays", "lofi", "chill background", []),
            (
                "s5",
                "Strobe",
                ["deadmau5"],
                "For Lack of a Better Name",
                "progressive-house",
                "closer track",
                ["classic"],
            ),
            ("s6", "House Anthem", ["DJ House"], "Club Cuts", "trance", "banger opener", []),
        ]
        for sid, title, artists, album, genre, comments, tags in tracks:
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=artists,
                album=album,
                isrc=None,
                duration_ms=200_000,
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


@pytest.mark.requirement("SEARCH-NAV-14754")
def test_build_fts_query_nul_only_returns_none() -> None:
    """[if] build_fts_query gets NUL-only [then] None, [else stop]."""
    assert search_index.build_fts_query("\x00") is None


@pytest.mark.requirement("SEARCH-NAV-14754")
def test_build_fts_query_strips_nul_without_sqlite_error(fixture_db: Path) -> None:
    """[if] build_fts_query gets mixed NUL [then] strip and search ok, [else stop]."""
    assert search_index.build_fts_query("a\x00b") is not None
    search_index.search(fixture_db, "a\x00b", limit=10)


@pytest.mark.requirement("LIBUX-10721")
def test_search_indexes_album_and_rebuilds_stale_schema_version(fixture_db: Path) -> None:
    """[if] stale fts schema [then] rebuild indexes album with quoted context, [else stop]."""
    index_path = search_index.ensure_index(fixture_db)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("UPDATE search_meta SET schema = 4 WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    hits, total = search_index.search(fixture_db, "nostalgia", limit=10)
    assert total == 1
    assert hits[0][0] == "s1"
    assert "nostalgia" in hits[0][1].lower()

    conn = sqlite3.connect(index_path)
    try:
        schema = int(conn.execute("SELECT schema FROM search_meta WHERE id = 1").fetchone()[0])
    finally:
        conn.close()
    assert schema == 6


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


@pytest.mark.requirement("GENRE-04")
def test_search_indexes_rekordbox_genre_over_imported_file_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] rb and import genres differ [then] FTS indexes rb first, [else stop]."""
    import apps.adapters.rekordbox.config as rb_mod

    rb_genre = "Garage"
    imported_genre = "Zyxtagimport9038"
    vendor_id = "vid-rb-fts9038"

    state_path = tmp_path / "state.db"
    master_path = tmp_path / "master.plain.db"
    monkeypatch.setattr(rb_mod, "MASTER_PLAIN_DB", master_path, raising=False)

    master = sqlite3.connect(master_path)
    try:
        master.execute(
            "CREATE TABLE djmdContent (ID TEXT PRIMARY KEY, GenreID TEXT, rb_local_deleted INTEGER NOT NULL DEFAULT 0)"
        )
        master.execute(
            "CREATE TABLE djmdGenre (ID TEXT PRIMARY KEY, Name TEXT, rb_local_deleted INTEGER NOT NULL DEFAULT 0)"
        )
        gid = "g-fts9038"
        master.execute(
            "INSERT INTO djmdGenre (ID, Name, rb_local_deleted) VALUES (?, ?, 0)",
            (gid, rb_genre),
        )
        master.execute(
            "INSERT INTO djmdContent (ID, GenreID, rb_local_deleted) VALUES (?, ?, 0)",
            (vendor_id, gid),
        )
        master.commit()
    finally:
        master.close()

    conn = state_db.open_rw(state_path)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s-rb",
            stable_id_tier="inferred",
            title="Neutralsignal",
            artists=["Anon"],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path="/music/s-rb.mp3",
        )
        writer.set_vendor_id("s-rb", "rekordbox", vendor_id)
        _set_eav_field(conn, "s-rb", "genre", imported_genre)
    finally:
        writer.close()
        conn.close()

    hits, total = search_index.search(state_path, "garage", limit=10)
    assert total == 1
    assert [sid for sid, _ in hits] == ["s-rb"]

    hidden, _ = search_index.search(state_path, "Zyxtagimport", limit=10)
    assert hidden == []


@pytest.mark.requirement("GENRE-04")
def test_search_indexes_and_refreshes_only_the_precedence_selected_genre(fixture_db: Path) -> None:
    """[if] genre state changes [then] search follows precedence, [else stop]."""
    conn = state_db.open_rw(fixture_db)
    try:
        findings.ensure_schema(conn)
        findings.put_finding(
            conn,
            findings.Finding(
                stable_id="s1",
                provider="beatport",
                status=findings.STATUS_OK,
                genre="Nebula Breaks",
                confidence=1.0,
            ),
        )
        findings.update_config(conn, {"precedence": ["beatport", "library-tag"]})
    finally:
        conn.close()

    hits, _ = search_index.search(fixture_db, "nebula", limit=10)
    hidden, _ = search_index.search(fixture_db, "deep", limit=10)
    assert [sid for sid, _ in hits] == ["s1"]
    assert hidden == []

    conn = state_db.open_rw(fixture_db)
    try:
        findings.put_finding(
            conn,
            findings.Finding(
                stable_id="s1",
                provider="beatport",
                status=findings.STATUS_OK,
                genre="Aurora Jungle",
                confidence=1.0,
            ),
        )
    finally:
        conn.close()

    old_finding, _ = search_index.search(fixture_db, "nebula", limit=10)
    new_finding, _ = search_index.search(fixture_db, "aurora", limit=10)
    assert old_finding == []
    assert [sid for sid, _ in new_finding] == ["s1"]

    conn = state_db.open_rw(fixture_db)
    try:
        findings.update_config(conn, {"precedence": ["library-tag", "beatport"]})
    finally:
        conn.close()

    hidden_finding, _ = search_index.search(fixture_db, "aurora", limit=10)
    restored_library_tag, _ = search_index.search(fixture_db, "deep", limit=10)
    assert hidden_finding == []
    assert [sid for sid, _ in restored_library_tag] == ["s1"]


def test_search_matches_comments_eav_field(fixture_db: Path) -> None:
    hits, total = search_index.search(fixture_db, "chill", limit=10)
    assert total == 1
    assert hits[0][0] == "s4"


def test_search_matches_writable_notes_eav_field(fixture_db: Path) -> None:
    conn = state_db.open_rw(fixture_db)
    try:
        _set_eav_field(conn, "s4", "notes", "tailored outro annotation")
    finally:
        conn.close()

    hits, total = search_index.search(fixture_db, "tailored", limit=10)

    assert total == 1
    assert hits[0][0] == "s4"


def test_search_matches_tags_eav_field(fixture_db: Path) -> None:
    # "banger" is s1's tag AND a word in s6's comments ("banger opener") --
    # both are real matches. Keep this established ordering while adding
    # notes as an equal-weight searchable annotation.
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
    assert [sid for sid, _ in page1] + [sid for sid, _ in page2] == [sid for sid, _ in hits_all][:4]


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
            stable_id="s7",
            stable_id_tier="inferred",
            title="Phoenix",
            artists=["Delta"],
            album=None,
            isrc=None,
            duration_ms=180_000,
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


def test_index_refreshes_for_committed_wal_write_without_main_db_mtime_change(
    fixture_db: Path,
) -> None:
    search_index.ensure_index(fixture_db)
    main_db_mtime = fixture_db.stat().st_mtime_ns
    conn = state_db.open_rw(fixture_db)
    bus = FakeEventBus()
    writer = StateWriter(conn, bus=bus, actor="test")
    try:
        writer.upsert_track(
            stable_id="s7",
            stable_id_tier="inferred",
            title="WAL Arrival",
            artists=["Delta"],
            album=None,
            isrc=None,
            duration_ms=180_000,
            file_path="/music/s7.mp3",
        )
        assert fixture_db.stat().st_mtime_ns == main_db_mtime

        hits, total = search_index.search(fixture_db, "arrival", limit=10)
    finally:
        writer.close()
        conn.close()

    assert total == 1
    assert hits[0][0] == "s7"


def test_index_rebuild_commits_schema_and_metadata_atomically(
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    original_open_index = search_index._open_index

    def open_index_with_trace(index_db_path: Path) -> sqlite3.Connection:
        conn = original_open_index(index_db_path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(search_index, "_open_index", open_index_with_trace)

    search_index.ensure_index(fixture_db)

    begin = statements.index("BEGIN IMMEDIATE")
    first_drop = next(i for i, sql in enumerate(statements) if sql.startswith("DROP TABLE"))
    metadata = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO search_meta"))
    commit = statements.index("COMMIT")
    assert begin < first_drop < metadata < commit


def test_concurrent_cold_searches_return_the_same_hits(fixture_db: Path) -> None:
    start = threading.Barrier(2)
    results: list[tuple[list[tuple[str, str]], int]] = []
    errors: list[Exception] = []

    def search_cold_index() -> None:
        try:
            start.wait(timeout=10)
            results.append(search_index.search(fixture_db, "levi", limit=10))
        except Exception as exc:  # noqa: BLE001 - asserted on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=search_cold_index) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads), "search thread leaked"
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0][1] == 1
    assert [stable_id for stable_id, _ in results[0][0]] == ["s1"]


def test_concurrent_cold_searches_rebuild_once(
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = threading.Barrier(2)
    both_saw_cold_meta = threading.Barrier(2)
    rebuild_calls: list[Path] = []
    results: list[tuple[list[tuple[str, str]], int]] = []
    errors: list[Exception] = []
    original_current_meta = search_index._current_meta
    original_rebuild = search_index._rebuild

    def current_meta_after_both_cold_checks(
        conn: sqlite3.Connection,
    ) -> tuple[int, str] | None:
        meta = original_current_meta(conn)
        if not conn.in_transaction:
            both_saw_cold_meta.wait(timeout=10)
        return meta

    def rebuild_once(conn: sqlite3.Connection, state_db_path: Path) -> None:
        rebuild_calls.append(state_db_path)
        original_rebuild(conn, state_db_path)

    monkeypatch.setattr(search_index, "_current_meta", current_meta_after_both_cold_checks)
    monkeypatch.setattr(search_index, "_rebuild", rebuild_once)

    def search_cold_index() -> None:
        try:
            start.wait(timeout=10)
            results.append(search_index.search(fixture_db, "levi", limit=10))
        except Exception as exc:  # noqa: BLE001 - asserted on the main thread
            errors.append(exc)

    threads = [threading.Thread(target=search_cold_index) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads), "search thread leaked"
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert rebuild_calls == [fixture_db]


def test_concurrent_first_opens_of_a_new_index_both_succeed(tmp_path: Path) -> None:
    # Switching a new file to WAL from two connections at once makes SQLite
    # answer one SQLITE_BUSY without its busy handler; _open_index must absorb it.
    errors: list[Exception] = []
    for attempt in range(50):
        target = tmp_path / f"idx-{attempt}.db"
        start = threading.Barrier(2)

        def open_new_index(target: Path = target, start: threading.Barrier = start) -> None:
            try:
                start.wait(timeout=10)
                search_index._open_index(target).close()
            except Exception as exc:  # noqa: BLE001 - asserted on the main thread
                errors.append(exc)

        threads = [threading.Thread(target=open_new_index) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads), "open thread leaked"
    assert errors == []


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
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        conn.close()
    assert "tracks_fts" not in names
    assert "search_meta" not in names


def test_search_works_while_the_library_is_being_written_to(
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a second connection commits to state.db during every index read
    [then] fail, [else stop]: search must answer, not 503."""
    writer = sqlite3.connect(fixture_db, check_same_thread=False)
    writer.execute("PRAGMA journal_mode=WAL")
    commits: list[str] = []
    real_connect = sqlite3.connect

    def commit_during_read(sql: str) -> None:
        if sql.lstrip().upper().startswith("SELECT"):
            writer.execute("UPDATE tracks SET updated_at = ? WHERE stable_id = 's2'", (str(len(commits)),))
            writer.commit()
            commits.append(sql)

    def connect_with_writer(
        database: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        conn = real_connect(database, uri=uri, timeout=timeout)
        if "mode=ro" in database:
            conn.set_trace_callback(commit_during_read)
        return conn

    monkeypatch.setattr(search_index.sqlite3, "connect", connect_with_writer)
    try:
        hits, total = search_index.search(fixture_db, "levi", limit=10)
    finally:
        writer.close()

    assert commits, "the control: a real commit must have landed during the source read"
    assert total == 1
    assert [sid for sid, _ in hits] == ["s1"]


def _commit_on_another_thread(
    state_db_path: Path,
) -> tuple[threading.Event, threading.Event, threading.Event, threading.Thread, list[int], list[Exception]]:
    """BEGIN IMMEDIATE + UPDATE + COMMIT on a second connection until released.

    ``committed`` is set after every commit, so a caller that clears it can wait
    for the next real commit instead of sleeping.
    """
    held, release, committed = threading.Event(), threading.Event(), threading.Event()
    commits: list[int] = []
    errors: list[Exception] = []

    def _hold() -> None:
        conn = sqlite3.connect(str(state_db_path), isolation_level=None)
        try:
            n = 0
            while not release.is_set():
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE tracks SET updated_at = ? WHERE stable_id = 's2'",
                    (str(n),),
                )
                conn.execute("COMMIT")
                n += 1
                commits.append(n)
                committed.set()
                held.set()
        except Exception as exc:  # noqa: BLE001 - re-raised on the main thread below
            errors.append(exc)
            held.set()
        finally:
            conn.close()

    thread = threading.Thread(target=_hold)
    thread.start()
    return held, release, committed, thread, commits, errors


def test_search_answers_while_another_thread_writes(
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real second-thread writer commits to state.db while search rebuilds the index."""
    held, release, committed, thread, commits, errors = _commit_on_another_thread(fixture_db)
    assert held.wait(10), "the writer thread never committed"
    real_ensure_index = search_index.ensure_index

    def ensure_index_after_a_fresh_commit(state_db_path: Path, index_db_path: Path | None = None) -> Path:
        # Deterministic control: inside search(), wait for the writer to land a
        # NEW commit, so a real concurrent commit is guaranteed whatever the
        # scheduler does (a bare tight loop lost the race under CI load).
        committed.clear()
        assert committed.wait(5), "the writer thread could not commit while search ran"
        return real_ensure_index(state_db_path, index_db_path)

    try:
        before = len(commits)
        monkeypatch.setattr(search_index, "ensure_index", ensure_index_after_a_fresh_commit)
        started = time.monotonic()
        hits, total = search_index.search(fixture_db, "levi", limit=10)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"search waited {elapsed:.2f}s on the writer"
        assert len(commits) > before, "the control: a real commit must have landed on the other thread during search"
        assert total == 1
        assert [sid for sid, _ in hits] == ["s1"]
    finally:
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "writer thread leaked"
        if errors:
            raise errors[0]


def test_index_source_is_read_from_one_snapshot(
    fixture_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a commit lands between the tracks read and the fields read
    [then] the index must not mix the two states, [else stop]."""
    statements: list[str] = []
    real_connect = sqlite3.connect

    def connect_with_trace(
        database: str,
        *,
        uri: bool = False,
        timeout: float = 5.0,
    ) -> sqlite3.Connection:
        conn = real_connect(database, uri=uri, timeout=timeout)
        if "mode=ro" in database:
            conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(search_index.sqlite3, "connect", connect_with_trace)

    search_index.search(fixture_db, "levi", limit=10)

    begin = next(i for i, sql in enumerate(statements) if sql.strip().upper() == "BEGIN")
    reads = [i for i, sql in enumerate(statements) if sql.lstrip().upper().startswith("SELECT")]
    assert reads, "the control: the source read must have been traced"
    assert all(i > begin for i in reads), statements


pytestmark = pytest.mark.rb_parity
