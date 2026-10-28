"""``apps.dedup.schema.ensure_schema``: the migrated connection it returns (Codex, PR #6182)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.dedup import contract
from apps.dedup import schema as dedup_schema


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    names = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {name: [str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")] for name in names}


def test_an_in_memory_database_keeps_its_tables() -> None:
    # It lives only as long as its connection, so the returned one must be the one migrated.
    conn = dedup_schema.ensure_schema(Path(":memory:"))
    try:
        assert "duplicate_clusters" in _tables(conn)
        assert "flagged_manual_review" in _tables(conn)["duplicate_clusters"]
    finally:
        conn.close()


def test_the_contract_refuses_an_in_memory_database(tmp_path: Path) -> None:
    # Codex, PR #6182: the contract closes the connection it migrates, so a caller's own
    # connection to ``:memory:`` opened an empty database and failed on its first insert.
    with pytest.raises(ValueError, match="in-memory"):
        contract.ensure_schema(Path(":memory:"))
    # Control: a file keeps its tables for the caller's own connection.
    contract.ensure_schema(tmp_path / "file.sqlite")
    with sqlite3.connect(tmp_path / "file.sqlite") as conn:
        assert "tag_provenance" in _tables(conn)


def test_the_internal_and_contract_migrations_agree(tmp_path: Path) -> None:
    contract.ensure_schema(tmp_path / "contract.sqlite")
    internal = dedup_schema.ensure_schema(tmp_path / "internal.sqlite")
    via_contract = sqlite3.connect(tmp_path / "contract.sqlite")
    try:
        assert _tables(internal) == _tables(via_contract)
        assert _tables(internal), "read no tables at all: the comparison is over nothing"
    finally:
        internal.close()
        via_contract.close()


def test_an_old_database_gains_the_review_flag(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as old:
        old.executescript(dedup_schema.SCHEMA_SQL)
        old.execute("ALTER TABLE duplicate_clusters DROP COLUMN flagged_manual_review")  # as before the flag
    with sqlite3.connect(db) as old:
        assert "flagged_manual_review" not in _tables(old)["duplicate_clusters"]
    conn = dedup_schema.ensure_schema(db)
    try:
        assert "flagged_manual_review" in _tables(conn)["duplicate_clusters"]
    finally:
        conn.close()
