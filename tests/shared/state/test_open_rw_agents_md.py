"""open_rw post-migration AGENTS.md hook -- real sqlite, no mocks."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from apps.database import regenerate_agents_md_if_writable
from apps.database.generate_agents_md import (
    GENERATOR_VERSION,
    ForeignAgentsMdError,
    MissingColumnDocsError,
    agents_md_cache_marker,
)
from apps.shared.state import schema as state_schema
from apps.shared.state import schema_markers, sync_stamp
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


def test_agents_md_cache_marker_changes_with_each_real_input(tmp_path: Path) -> None:
    """if schema version, owned tables, or generator version differ, the marker differs - broken"""
    tracks_only = frozenset({"tracks"})
    tracks_and_playlists = frozenset({"tracks", "playlists"})
    base = agents_md_cache_marker(sqlite_schema_version=19, owned_tables=tracks_only)
    other_schema = agents_md_cache_marker(sqlite_schema_version=20, owned_tables=tracks_only)
    other_tables = agents_md_cache_marker(
        sqlite_schema_version=19, owned_tables=tracks_and_playlists
    )
    same_again = agents_md_cache_marker(sqlite_schema_version=19, owned_tables=tracks_only)

    assert base == same_again
    assert base != other_schema
    assert base != other_tables
    # A live bump of GENERATOR_VERSION must also move the marker -- this
    # assertion is the mutation check for that path: revert the constant to
    # 1 and this line goes red, because a marker gate that ignores the
    # generator's own version would keep serving a stale AGENTS.md forever
    # after the render logic or curated docs change underneath it.
    assert f"v{GENERATOR_VERSION}" in base


def test_open_rw_reopen_on_unchanged_schema_skips_regeneration(tmp_path: Path) -> None:
    """if schema and owned tables are unchanged then a reopen does not rewrite AGENTS.md - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.close()

    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.is_file()
    verify_conn = open_rw(db_path)
    try:
        sqlite_schema_version = verify_conn.execute("PRAGMA schema_version").fetchone()[0]
        marker = agents_md_cache_marker(
            sqlite_schema_version=sqlite_schema_version,
            owned_tables=state_schema.ALL_KNOWN_TABLES,
        )
        assert schema_markers.has_marker(verify_conn, marker)
    finally:
        verify_conn.close()

    # Delete the file so a real regeneration is the ONLY way it comes back --
    # this is the positive-presence proof the fleet verification rule asks
    # for: absence after reopen is not "nothing happened to look the same",
    # it is "regeneration provably did not run", because if it had run the
    # file would exist again.
    agents_md.unlink()

    reopened = open_rw(db_path)
    reopened.close()

    assert not agents_md.exists()


def test_regenerate_reruns_when_owned_tables_input_changes(tmp_path: Path) -> None:
    """if owned_tables differs from the cached marker then regenerate writes again - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    try:
        agents_md = tmp_path / "AGENTS.md"
        agents_md.unlink()

        # Same call shape open_rw already made (schema_version unchanged,
        # same owned_tables) -- this is the cache HIT this fix exists for.
        assert regenerate_agents_md_if_writable(
            conn, tmp_path, owned_tables=state_schema.ALL_KNOWN_TABLES
        ) is False
        assert not agents_md.exists()

        # A genuinely different owned_tables set is a different real input,
        # so it must be a cache MISS: this is the direct counterpart to the
        # skip test above, proving the gate does not just always skip.
        narrowed = frozenset({"tracks"})
        assert narrowed != state_schema.ALL_KNOWN_TABLES
        assert regenerate_agents_md_if_writable(
            conn, tmp_path, owned_tables=narrowed
        ) is True
        assert agents_md.is_file()
    finally:
        conn.close()


def test_regenerate_reruns_when_generator_version_bumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """if GENERATOR_VERSION bumps then a previously-cached marker misses - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    try:
        agents_md = tmp_path / "AGENTS.md"
        agents_md.unlink()

        assert regenerate_agents_md_if_writable(
            conn, tmp_path, owned_tables=state_schema.ALL_KNOWN_TABLES
        ) is False
        assert not agents_md.exists()

        monkeypatch.setattr(
            "apps.database.generate_agents_md.GENERATOR_VERSION", GENERATOR_VERSION + 1
        )
        assert regenerate_agents_md_if_writable(
            conn, tmp_path, owned_tables=state_schema.ALL_KNOWN_TABLES
        ) is True
        assert agents_md.is_file()
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
