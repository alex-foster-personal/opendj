"""Schema v32: labelling tables and karma ledger (issue #11192).

[if] apply_migrations runs on a v31 db [then] label tables exist, [else stop].
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import migrations_v32
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("PLATFORM-12033")


def _ladder_to(path: Path, version: int) -> None:
    conn = sqlite3.connect(str(path))
    state_schema._ensure_meta(conn)
    for step_index in range(version):
        for statement in state_schema.MIGRATIONS[step_index]:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, 'then')",
            (step_index + 1,),
        )
    conn.commit()
    conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_v32_is_the_next_ladder_rung() -> None:
    assert state_schema.SCHEMA_VERSION == 32
    assert state_schema.MIGRATIONS[31] is migrations_v32._V32


def test_a_v31_database_gains_labelling_tables(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _ladder_to(path, 31)
    conn = sqlite3.connect(str(path))
    try:
        assert "label_task" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert state_schema.apply_migrations(conn) == 32
        assert {
            "id",
            "experiment_id",
            "hypothesis_ids",
            "modality",
            "payload",
            "reward_karma",
            "reward_credits",
            "max_wait_s",
            "batch_size",
            "created_at",
        } == _columns(conn, "label_task")
        assert "user_id" in _columns(conn, "label_response")
        assert "karma_delta" in _columns(conn, "contribution_ledger")
        triggers = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert migrations_v32.LABEL_RESPONSE_KARMA_TRIGGER in triggers
        assert state_schema.apply_migrations(conn) == 32
    finally:
        conn.close()
