"""open_rw post-migration AGENTS.md hook -- real sqlite, no mocks."""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from apps.database import regenerate_agents_md_if_writable
from apps.database.generate_agents_md import (
    GENERATOR_VERSION,
    ForeignAgentsMdError,
    MissingColumnDocsError,
    agents_md_cache_line,
    agents_md_cache_marker,
    write_agents_md,
)
from apps.shared.state import schema as state_schema
from apps.shared.state import schema_markers, sync_stamp
from apps.shared.state.agents_md_cache import regenerate_agents_md_cached
from apps.shared.state.db import open_dry_run, open_rw

_AGENTS_HEADER = "# state.db -- generated table reference"


def test_open_rw_writes_agents_md_with_real_table_heading(tmp_path: Path) -> None:
    """if open_rw migrates a fresh DB then AGENTS.md is written with real headings - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.close()

    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.is_file()
    text = agents_md.read_text(encoding="utf-8")
    assert _AGENTS_HEADER in text
    assert "## `tracks`" in text


def test_open_rw_without_schema_does_not_write_agents_md(tmp_path: Path) -> None:
    """if open_rw is called with apply_schema=False then AGENTS.md is not written - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path, apply_schema=False)
    try:
        pass
    finally:
        conn.close()

    assert not (tmp_path / "AGENTS.md").exists()


def test_regenerate_skips_when_state_dir_unwritable(tmp_path: Path) -> None:
    """if state_dir is unwritable then regenerate skips and writes nothing - broken"""
    # WAL pragmas need a writable parent, so this skip is tested on the hook
    # itself with an already-open connection. Reopening via open_rw is the
    # next test: sqlite fails at PRAGMA, before the hook runs.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"

    conn = open_rw(db_path)
    try:
        agents_md = state_dir / "AGENTS.md"
        if agents_md.exists():
            agents_md.unlink()

        original_mode = state_dir.stat().st_mode
        state_dir.chmod(0o555)
        try:
            if os.access(state_dir, os.W_OK):
                pytest.fail("chmod did not make state_dir unwritable (running as root?)")
            assert regenerate_agents_md_if_writable(conn, state_dir) is False
            assert not agents_md.exists()
        finally:
            state_dir.chmod(original_mode)
    finally:
        conn.close()


def test_open_rw_raises_when_state_dir_unwritable(tmp_path: Path) -> None:
    """if state_dir is unwritable then open_rw fails at sqlite pragmas - broken"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"

    conn = open_rw(db_path)
    conn.close()

    original_mode = state_dir.stat().st_mode
    state_dir.chmod(0o555)
    try:
        if os.access(state_dir, os.W_OK):
            pytest.fail("chmod did not make state_dir unwritable (running as root?)")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            open_rw(db_path)
    finally:
        state_dir.chmod(original_mode)


def test_regenerate_raises_on_undocumented_table(tmp_path: Path) -> None:
    """if an undocumented table exists then regenerate_agents_md_if_writable raises - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    try:
        conn.execute("CREATE TABLE undocumented_xyz (id INTEGER PRIMARY KEY)")
        with pytest.raises(MissingColumnDocsError):
            regenerate_agents_md_if_writable(conn, tmp_path)
    finally:
        conn.close()


def test_open_rw_propagates_missing_column_docs_error_on_reopen(tmp_path: Path) -> None:
    """if reopen hits a docs gap on an owned table then open_rw raises - broken

    Doubles as the cache-vs-drift-guard regression test for issue #4015: the
    ALTER TABLE below never touches ``schema.SCHEMA_VERSION`` or
    ``owned_tables``, so if the AGENTS.md cache marker were keyed on the
    app's own schema version instead of sqlite's ``PRAGMA schema_version``,
    this reopen would hit the marker cache, skip regeneration entirely, and
    this test would go from "raises" to "silently reuses the stale file" --
    which is exactly the regression the first draft of this fix introduced
    and this test caught.
    """
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.execute("ALTER TABLE tracks ADD COLUMN totally_fake_injected_column TEXT")
    conn.close()

    with pytest.raises(MissingColumnDocsError) as excinfo:
        open_rw(db_path)
    assert "tracks.totally_fake_injected_column" in str(excinfo.value)


def test_open_rw_omits_legacy_leftover_tables_and_still_opens(tmp_path: Path) -> None:
    """if leftover lyric_*_legacy tables exist then open_rw still writes AGENTS.md - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.execute(
        "CREATE TABLE lyric_verdict_legacy (stable_id TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE lyric_word_legacy ("
        "stable_id TEXT NOT NULL, idx INTEGER NOT NULL, PRIMARY KEY (stable_id, idx))"
    )
    conn.close()

    reopened = open_rw(db_path)
    try:
        live = {
            row[0]
            for row in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "lyric_verdict_legacy" in live
        assert "lyric_word_legacy" in live
    finally:
        reopened.close()

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert _AGENTS_HEADER in text
    assert "## `tracks`" in text
    assert "lyric_verdict_legacy" not in text
    assert "lyric_word_legacy" not in text


def test_open_rw_refuses_to_overwrite_foreign_agents_md(tmp_path: Path) -> None:
    """if parent AGENTS.md is not a generated sidecar then open_rw does not clobber it - broken"""
    db_path = tmp_path / "state.db"
    agents_md = tmp_path / "AGENTS.md"
    handwritten = "# apps/database\n\nhand-authored -- do not destroy\n"
    agents_md.write_text(handwritten, encoding="utf-8")

    with pytest.raises(ForeignAgentsMdError, match="not a generated"):
        open_rw(db_path)

    assert agents_md.read_text(encoding="utf-8") == handwritten


def test_open_rw_replaces_generated_agents_md_sidecar(tmp_path: Path) -> None:
    """if parent AGENTS.md already has the generated header then open_rw rewrites it - broken"""
    db_path = tmp_path / "state.db"
    agents_md = tmp_path / "AGENTS.md"
    stale = _AGENTS_HEADER + "\n\nGENERATED FILE. stale sidecar\n"
    agents_md.write_text(stale, encoding="utf-8")

    conn = open_rw(db_path)
    conn.close()

    text = agents_md.read_text(encoding="utf-8")
    assert text.startswith(_AGENTS_HEADER)
    assert "stale sidecar" not in text
    assert "## `tracks`" in text


def test_open_rw_keeps_foreign_authority_tables_in_agents_md(tmp_path: Path) -> None:
    """if launcher_meta exists then open_rw documents it instead of dropping it - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.execute(
        "CREATE TABLE launcher_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.close()

    reopened = open_rw(db_path)
    reopened.close()

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "## `tracks`" in text
    assert "## `launcher_meta`" in text
    assert "## `schema_meta`" in text


def test_open_dry_run_does_not_create_agents_md(tmp_path: Path) -> None:
    """if open_dry_run runs on a DB without AGENTS.md then it does not create one - broken"""
    db_path = tmp_path / "state.db"
    raw = sqlite3.connect(str(db_path))
    try:
        state_schema.apply_migrations(raw)
        sync_stamp.backfill_local_machine_id(raw)
    finally:
        raw.close()
    assert not (tmp_path / "AGENTS.md").exists()

    dry = open_dry_run(db_path)
    try:
        pass
    finally:
        dry.close()

    assert not (tmp_path / "AGENTS.md").exists()


def _marker_for(conn: sqlite3.Connection, owned_tables: frozenset[str], version: int) -> str:
    return agents_md_cache_marker(
        sqlite_schema_version=conn.execute("PRAGMA schema_version").fetchone()[0],
        owned_tables=owned_tables,
        generator_version=version,
    )


def test_agents_md_cache_marker_changes_with_each_real_input(tmp_path: Path) -> None:
    """if schema version, owned tables, or generator version differ, the marker differs - broken"""
    tracks_only = frozenset({"tracks"})
    tracks_and_playlists = frozenset({"tracks", "playlists"})

    def marker(schema: int, tables: frozenset[str], version: int) -> str:
        return agents_md_cache_marker(
            sqlite_schema_version=schema, owned_tables=tables, generator_version=version
        )

    base = marker(19, tracks_only, GENERATOR_VERSION)
    assert base == marker(19, tracks_only, GENERATOR_VERSION)
    assert base != marker(20, tracks_only, GENERATOR_VERSION)
    assert base != marker(19, tracks_and_playlists, GENERATOR_VERSION)
    assert base != marker(19, tracks_only, GENERATOR_VERSION + 1)


def test_open_rw_reopen_on_unchanged_schema_skips_regeneration(tmp_path: Path) -> None:
    """if schema and owned tables are unchanged then a reopen does not rewrite AGENTS.md - broken"""
    db_path = tmp_path / "state.db"
    open_rw(db_path).close()
    agents_md = tmp_path / "AGENTS.md"
    conn = open_rw(db_path)
    try:
        marker = _marker_for(conn, state_schema.ALL_KNOWN_TABLES, GENERATOR_VERSION)
        assert schema_markers.has_marker(conn, marker)
    finally:
        conn.close()
    assert agents_md.read_text(encoding="utf-8").endswith(agents_md_cache_line(marker) + "\n")

    # write_agents_md always lands via os.replace of a fresh temp file, so a
    # regeneration ALWAYS changes the inode: an unchanged inode after reopen
    # is positive proof the write did not run.
    before = agents_md.stat().st_ino
    open_rw(db_path).close()
    assert agents_md.stat().st_ino == before


def test_regenerate_reruns_when_owned_tables_input_changes(tmp_path: Path) -> None:
    """if owned_tables differs from the cached marker then regenerate writes again - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    try:
        agents_md = tmp_path / "AGENTS.md"
        before = agents_md.stat().st_ino
        # Same call shape open_rw already made: the cache HIT this fix exists for.
        assert regenerate_agents_md_cached(
            conn, tmp_path, owned_tables=state_schema.ALL_KNOWN_TABLES
        ) is False
        assert agents_md.stat().st_ino == before

        # A genuinely different owned_tables set is a different real input,
        # so it must MISS: the gate does not just always skip.
        narrowed = frozenset({"tracks"})
        assert narrowed != state_schema.ALL_KNOWN_TABLES
        assert regenerate_agents_md_cached(conn, tmp_path, owned_tables=narrowed) is True
        assert agents_md.stat().st_ino != before
    finally:
        conn.close()


def test_regenerate_reruns_for_a_marker_an_older_generator_wrote(tmp_path: Path) -> None:
    """if the DB only holds an older generator's marker then regenerate writes again - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    try:
        tables = state_schema.ALL_KNOWN_TABLES
        current = _marker_for(conn, tables, GENERATOR_VERSION)
        older = _marker_for(conn, tables, GENERATOR_VERSION - 1)
        # Real on-disk state of a DB last opened by the previous generator:
        # its marker row, and no row for the current one.
        conn.execute(f"DELETE FROM {schema_markers.MARKER_TABLE} WHERE marker = ?", (current,))
        schema_markers.insert_marker(conn, older)
        conn.commit()
        assert not schema_markers.has_marker(conn, current)

        assert regenerate_agents_md_cached(conn, tmp_path, owned_tables=tables) is True
        assert schema_markers.has_marker(conn, current)
    finally:
        conn.close()


def test_open_rw_regenerates_a_missing_sidecar_despite_the_db_marker(tmp_path: Path) -> None:
    """if state.db carries the marker but AGENTS.md is gone then open_rw rewrites it - broken"""
    db_path = tmp_path / "state.db"
    open_rw(db_path).close()
    agents_md = tmp_path / "AGENTS.md"
    agents_md.unlink()

    # The marker row is still in the DB (it travels with a restored copy).
    # This reopen also re-records an ALREADY-present marker, the exact path
    # two concurrent missing opens take: it must not abort on the key.
    open_rw(db_path).close()

    assert agents_md.is_file()
    assert _AGENTS_HEADER in agents_md.read_text(encoding="utf-8")


def test_open_rw_regenerates_a_sidecar_written_for_another_schema(tmp_path: Path) -> None:
    """if a copied state.db lands beside another DB's AGENTS.md then open_rw rewrites it - broken"""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    open_rw(source_dir / "state.db").close()

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    shutil.copy2(source_dir / "state.db", dest_dir / "state.db")
    # A real generated sidecar for a different input (one owned table),
    # carrying ITS OWN cache line, sits where the copy is restored.
    conn = sqlite3.connect(dest_dir / "state.db")
    try:
        narrowed = frozenset({"tracks"})
        foreign_marker = _marker_for(conn, narrowed, GENERATOR_VERSION)
        write_agents_md(
            conn, dest_dir / "AGENTS.md", owned_tables=narrowed, cache_marker=foreign_marker
        )
    finally:
        conn.close()
    assert "## `playlists`" not in (dest_dir / "AGENTS.md").read_text(encoding="utf-8")

    open_rw(dest_dir / "state.db").close()

    text = (dest_dir / "AGENTS.md").read_text(encoding="utf-8")
    assert "## `playlists`" in text
    assert agents_md_cache_line(foreign_marker) not in text


def test_insert_marker_if_absent_keeps_one_row_where_insert_marker_aborts(tmp_path: Path) -> None:
    """if two opens record the same cache marker then neither aborts - broken"""
    conn = open_rw(tmp_path / "state.db")
    try:
        schema_markers.insert_marker_if_absent(conn, "cache-probe")
        schema_markers.insert_marker_if_absent(conn, "cache-probe")
        count = conn.execute(
            f"SELECT COUNT(*) FROM {schema_markers.MARKER_TABLE} WHERE marker = ?",
            ("cache-probe",),
        ).fetchone()[0]
        assert count == 1
        # Control: the table really enforces the key, so the idempotent
        # insert is what kept this quiet, not a missing constraint.
        with pytest.raises(sqlite3.IntegrityError):
            schema_markers.insert_marker(conn, "cache-probe")
    finally:
        conn.close()


def test_open_rw_never_waits_on_a_peer_writer_to_record_the_cache_marker(
    tmp_path: Path,
) -> None:
    """if a peer holds the writer lock after a schema change then open_rw does not wait - broken"""
    db_path = tmp_path / "state.db"
    open_rw(db_path).close()
    agents_md = tmp_path / "AGENTS.md"

    # A real schema change (sqlite's schema_version moves) with no table
    # change, so the next open MISSES the cache exactly as a lazily created
    # analysis table makes a request's open miss.
    peer = sqlite3.connect(str(db_path), isolation_level=None)
    peer.execute("CREATE INDEX cache_probe_idx ON tracks(title)")
    peer.execute("DROP INDEX cache_probe_idx")
    # WAL: BEGIN IMMEDIATE takes the writer lock and still lets readers in.
    peer.execute("BEGIN IMMEDIATE")
    try:
        before = agents_md.stat().st_ino
        started = time.monotonic()
        conn = open_rw(db_path, busy_timeout_s=3.0)
        waited_s = time.monotonic() - started
        try:
            marker = _marker_for(conn, state_schema.ALL_KNOWN_TABLES, GENERATOR_VERSION)
            # The open neither blocked for the 3 s busy timeout nor raised.
            assert waited_s < 1.5, waited_s
            # The sidecar still regenerated for the new schema: the miss ran.
            assert agents_md.stat().st_ino != before
            assert agents_md.read_text(encoding="utf-8").endswith(
                agents_md_cache_line(marker) + "\n"
            )
            # The row could not be written under the peer's lock.
            assert not schema_markers.has_marker(conn, marker)
            # The zero-wait attempt did not leak: the handle keeps the
            # caller's busy timeout for its own later writes.
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
        finally:
            conn.close()
    finally:
        peer.rollback()
        peer.close()

    # Control against the overshoot "never record": once the lock is free,
    # the next open records the marker, so the cache still converges.
    conn = open_rw(db_path)
    try:
        assert schema_markers.has_marker(conn, marker)
    finally:
        conn.close()


def test_open_dry_run_does_not_overwrite_existing_agents_md(tmp_path: Path) -> None:
    """if AGENTS.md already exists then open_dry_run leaves its bytes unchanged - broken"""
    db_path = tmp_path / "state.db"
    raw = sqlite3.connect(str(db_path))
    try:
        state_schema.apply_migrations(raw)
        sync_stamp.backfill_local_machine_id(raw)
    finally:
        raw.close()

    sentinel = b"sentinel-agents-md-must-not-change\n"
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_bytes(sentinel)

    dry = open_dry_run(db_path)
    try:
        pass
    finally:
        dry.close()

    assert agents_md.read_bytes() == sentinel
