"""Migration v26: library organization foundation on top of main's v25.

[if] a v25 state.db (transition memory) is opened [then] it reaches v26 with its playlists and saved
transitions intact, every playlist reading kind 'playlist', [else stop].
[if] the v26 step runs [then] the three tag types are seeded and every new synced table has a write
token and its three triggers, [else stop].
[if] a db the reverted #7886 stamped v25 (library tables, no pair_transitions) is opened [then] it reaches
v26 with pair_transitions created and its library rows, tokens and triggers untouched, [else stop].
[if] a v26 step fails halfway [then] the db stays at v25 with no new column, [else stop].
[if] a newer-than-code db is opened [then] the ladder writes nothing, [else stop].
[if] the engine store migrates fresh or adopts a finished v26 db [then] it holds both the v25 and the v26 tables, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.engine_core.store import schema as store_schema
from apps.shared.state import migrations_v26, migrations_v27, migrations_v28, migrations_v29, migrations_v30
from apps.shared.state import schema as state_schema

pytestmark = [
    pytest.mark.requirement("LIBORG-7926"),
    pytest.mark.requirement("LIBORG-7926b"),
    pytest.mark.requirement("LIBORG-7926c"),
    pytest.mark.requirement("LIBORG-7926d"),
]

_LIBRARY_TABLES = (
    "playlist_section",
    "tag_type",
    "tag_meta",
    "tag_synonym",
    "work",
    "work_member",
    "work_dismissal",
    "work_pref",
    "undo_journal",
)


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_idx in range(version):
        for stmt in state_schema.MIGRATIONS[step_idx]:
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')", (step_idx + 1,))
    conn.commit()
    conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _playlist_columns(conn: sqlite3.Connection) -> list[str]:
    return [str(r[1]) for r in conn.execute("PRAGMA table_info(playlists)")]


def _seed_v25_rows(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
        "VALUES ('pl-1', 'Warmup', 'opendj', 'pl-1', 't', 't')"
    )
    conn.execute(
        "INSERT INTO pair_transitions (out_track_id, in_track_id, plan_json, source, style, created_at, updated_at) "
        "VALUES ('a', 'b', '{}', 'captured', 'blend', 't', 't')"
    )
    conn.commit()
    conn.close()


def test_the_ladder_is_linear_with_library_org_at_26() -> None:
    assert state_schema.SCHEMA_VERSION == 30, "if the state ladder target is not 30 then broken"
    assert len(state_schema.MIGRATIONS) == 30, "if the ladder has a gap or a duplicate step then broken"
    assert state_schema.MIGRATIONS[24] is state_schema._V25, "if v25 is not transition memory then broken"
    assert state_schema.MIGRATIONS[25] is migrations_v26._V26, "if v26 is not library organization then broken"
    assert state_schema.MIGRATIONS[26] is migrations_v27._V27, "if v27 is not path_availability mtime then broken"
    assert state_schema.MIGRATIONS[27] is migrations_v28._V28, "if v28 is not AutoPlay decisions then broken"
    assert state_schema.MIGRATIONS[28] is migrations_v29._V29, (
        "if v29 is not the live-by-stable membership index then broken"
    )
    assert state_schema.MIGRATIONS[29] is migrations_v30._V30, (
        "if v30 is not track_edit + playlist_memberships.edit_id then broken"
    )


def test_a_v25_db_upgrades_to_v26_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 25)
    _seed_v25_rows(path)
    before = sqlite3.connect(str(path))
    assert "kind" not in _playlist_columns(before)  # negative control: v25 has no hierarchy
    assert not set(_LIBRARY_TABLES) & _tables(before)
    before.close()

    conn = sqlite3.connect(str(path))
    try:
        assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION
        assert set(_LIBRARY_TABLES) <= _tables(conn), "if a library table is missing after v26 then broken"
        assert conn.execute("SELECT name, kind, parent_id FROM playlists").fetchall() == [("Warmup", "playlist", None)]
        assert conn.execute("SELECT count(*) FROM pair_transitions").fetchone()[0] == 1, (
            "if v26 loses a saved transition then broken"
        )
        assert [r[0] for r in conn.execute("SELECT id FROM tag_type ORDER BY sort_index")] == [
            "general",
            "instrument",
            "mood",
        ]
        tokens = {str(r[0]) for r in conn.execute("SELECT table_name FROM sync_write_tokens")}
        assert set(migrations_v26.V26_WRITE_TOKEN_TABLES) <= tokens
        triggers = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert set(migrations_v26.EXPECTED_TRIGGERS) <= triggers
    finally:
        conn.close()


def test_a_db_stamped_v25_by_the_reverted_7886_gains_transition_memory(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 24)
    conn = sqlite3.connect(str(path))
    for stmt in migrations_v26.LIBRARY_ORG_DDL:  # what #7886 ran as its v25
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (25, 'then')")
    conn.execute(
        "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at, kind) "
        "VALUES ('f-1', 'Crates', 'opendj', 'f-1', 't', 't', 'folder')"
    )
    conn.execute("UPDATE tag_type SET label = 'Vibes' WHERE id = 'mood'")
    conn.commit()
    tokens_before = conn.execute("SELECT table_name, token FROM sync_write_tokens ORDER BY 1").fetchall()
    assert "pair_transitions" not in _tables(conn)  # negative control: the gap #7961 names
    try:
        assert state_schema.apply_migrations(conn) == state_schema.SCHEMA_VERSION, (
            "if a #7886-stamped db cannot reach v26 then broken"
        )
        assert "pair_transitions" in _tables(conn), "if v26 does not repair the missing transition memory then broken"
        assert conn.execute("SELECT kind FROM playlists WHERE playlist_id = 'f-1'").fetchone() == ("folder",)
        assert conn.execute("SELECT count(*), max(label = 'Vibes') FROM tag_type").fetchone() == (3, 1), (
            "if v26 re-seeds over an edited tag type then broken"
        )
        tokens_after = conn.execute("SELECT table_name, token FROM sync_write_tokens ORDER BY 1").fetchall()
        kept = [row for row in tokens_after if row[0] in {name for name, _token in tokens_before}]
        assert kept == tokens_before, "if v26 re-mints tokens the 7886 db already held then broken"
        stored = {
            str(r[0]): str(r[1]) for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        }
        assert all(stored.get(name) == sql for name, sql in migrations_v26.EXPECTED_TRIGGERS.items())
    finally:
        conn.close()


def test_a_fresh_v26_db_stores_the_same_ddl_as_7886_did(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 26)
    conn = sqlite3.connect(str(path))
    try:
        stored = {
            str(r[0]): str(r[1]) for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'")
        }
        assert all(stored.get(name) == sql for name, sql in migrations_v26.EXPECTED_TRIGGERS.items()), (
            "if the idempotent triggers store text the digest gate does not expect then broken"
        )
        assert "IF NOT EXISTS" not in str(conn.execute("SELECT group_concat(sql) FROM sqlite_master").fetchone()[0])
    finally:
        conn.close()


def test_a_failed_v26_step_leaves_the_db_at_v25(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 25)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE undo_journal (id TEXT PRIMARY KEY)")  # wrong shape: the v26 index on it fails
    conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError):
            state_schema.apply_migrations(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == 25
        assert "kind" not in _playlist_columns(conn), "if a failed v26 leaves a half-applied playlists then broken"
    finally:
        conn.close()


def test_a_db_newer_than_the_code_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 26)
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO schema_meta(version, applied_at) VALUES (31, 'future')")
    conn.commit()
    schema_before = conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    try:
        assert state_schema.apply_migrations(conn) == 31
        assert conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
    finally:
        conn.close()


def test_the_engine_store_holds_both_rungs_fresh_and_adopted(tmp_path: Path) -> None:
    assert store_schema.SCHEMA_VERSION == 19
    fresh = sqlite3.connect(":memory:")
    store_schema.apply_migrations(fresh)
    assert {"pair_transitions", *_LIBRARY_TABLES} <= _tables(fresh)
    assert "kind" in _playlist_columns(fresh)
    fresh.close()

    # Adoption takes a db the legacy ladder has already finished, as the runner's own refusal says.
    path = tmp_path / "state.db"
    _ladder_to(path, store_schema.LEGACY_SHARED_STATE_VERSION)
    adopted = sqlite3.connect(str(path))
    try:
        store_schema.apply_migrations(adopted)
        assert {"pair_transitions", *_LIBRARY_TABLES} <= _tables(adopted)
        assert "kind" in _playlist_columns(adopted)
    finally:
        adopted.close()
