"""open_rw post-migration AGENTS.md hook -- real sqlite, no mocks."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from apps.database import regenerate_agents_md_if_writable
from apps.database.generate_agents_md import MissingColumnDocsError
from apps.shared.state import schema as state_schema
from apps.shared.state import sync_stamp
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
    assert "## `tracks`" in text or "## `schema_meta`" in text


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
    """if reopen hits a docs gap then open_rw propagates MissingColumnDocsError - broken"""
    db_path = tmp_path / "state.db"
    conn = open_rw(db_path)
    conn.execute("CREATE TABLE undocumented_xyz (id INTEGER PRIMARY KEY)")
    conn.close()

    with pytest.raises(MissingColumnDocsError):
        open_rw(db_path)


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
