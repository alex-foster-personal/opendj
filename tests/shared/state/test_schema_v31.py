"""Schema v31: library HQ stems + lyric-match completeness config (LIBM-12060).

[if] migration v31 applies [then] the completeness cfg row is seeded once, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.library_completeness_config import CFG, read_expect_hq_stems_and_lyric_match
from apps.shared.state import db as state_db
from apps.shared.state import migrations_v31
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("LIBM-12060")


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for statement in state_schema.MIGRATIONS[step_idx]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')",
            (step_idx + 1,),
        )
    conn.commit()
    conn.close()


def test_v31_is_the_next_shared_and_engine_store_rung() -> None:
    """[if] the ladder is at v30 [then] v31 is the completeness config step, [else stop]."""
    assert state_schema.MIGRATIONS[30] is migrations_v31._V31
    assert store_schema.SCHEMA_VERSION == 20


def test_a_brand_new_empty_state_db_migrated_leaves_the_flag_false(tmp_path: Path) -> None:
    """[if] a brand-new empty state.db is migrated [then] the completeness flag is stored false, [else stop]."""
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
        assert read_expect_hq_stems_and_lyric_match(conn) is False
    finally:
        conn.close()


def test_an_existing_library_upgrades_the_flag_to_true_without_a_click(tmp_path: Path) -> None:
    """[if] an existing library is upgraded [then] the completeness flag is stored true, [else stop]."""
    path = tmp_path / "state.db"
    _ladder_to(path, 30)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES ('sid1', 'inferred', 't', '/music/t.wav', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    opened = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(opened) == state_schema.SCHEMA_VERSION
        assert read_expect_hq_stems_and_lyric_match(opened) is True
    finally:
        opened.close()


def test_cfg_default_is_false_when_the_row_is_missing() -> None:
    """[if] the flag is read through CFG after a fresh install [then] the default is false, [else stop]."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        f"""
        CREATE TABLE {CFG.table} (
            id INTEGER PRIMARY KEY,
            {CFG.column} INTEGER NOT NULL,
            seeded_at TEXT NOT NULL
        )
        """
    )
    try:
        assert read_expect_hq_stems_and_lyric_match(conn) is False
    finally:
        conn.close()


def test_reapplying_v31_leaves_the_stored_flag_unchanged(tmp_path: Path) -> None:
    """[if] the migration is re-run on a db already at the new version [then] the stored flag value is unchanged, [else stop]."""
    path = tmp_path / "state.db"
    _ladder_to(path, 30)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO tracks(stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES ('sid1', 'inferred', 't', '/music/t.wav', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    opened = state_db.open_rw(path)
    try:
        assert state_schema.apply_migrations(opened) == state_schema.SCHEMA_VERSION
        seeded = opened.execute(f"SELECT {CFG.column} FROM {CFG.table} WHERE id = 1").fetchone()[0]
        assert state_schema.apply_migrations(opened) == state_schema.SCHEMA_VERSION
        again = opened.execute(f"SELECT {CFG.column} FROM {CFG.table} WHERE id = 1").fetchone()[0]
        assert again == seeded
    finally:
        opened.close()


def test_the_fresh_engine_store_contains_the_completeness_table() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        store_schema.apply_migrations(conn)
        assert CFG.table in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
