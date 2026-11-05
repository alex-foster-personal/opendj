"""Unit tests for ``bind_upsert_many`` lazy path and upsert semantics."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from apps.webui.server.sqlite_sidecar_store import bind_upsert_many

_SCHEMA = """
CREATE TABLE IF NOT EXISTS t (
    stable_id TEXT PRIMARY KEY,
    n INTEGER NOT NULL
)
"""
_COLUMNS = ("stable_id", "n")


@dataclass(frozen=True)
class _Row:
    stable_id: str
    n: int


def test_bind_upsert_many_empty_batch_skips_path_and_file(tmp_path: Path) -> None:
    called = False

    def path_fn() -> Path:
        nonlocal called
        called = True
        return tmp_path / "sidecar.db"

    upsert_many = bind_upsert_many(path_fn, _SCHEMA, "t", _COLUMNS)
    upsert_many([])
    assert not called
    assert not (tmp_path / "sidecar.db").exists()


def test_bind_upsert_many_uses_path_at_write_time(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    current = {"path": first}

    def path_fn() -> Path:
        return current["path"]

    upsert_many = bind_upsert_many(path_fn, _SCHEMA, "t", _COLUMNS)
    current["path"] = second
    upsert_many([_Row("a", 1)])
    assert not first.exists()
    assert second.is_file()


def test_bind_upsert_many_insert_or_replace(tmp_path: Path) -> None:
    db = tmp_path / "sidecar.db"

    def path_fn() -> Path:
        return db

    upsert_many = bind_upsert_many(path_fn, _SCHEMA, "t", _COLUMNS)
    upsert_many([_Row("a", 1)])
    upsert_many([_Row("a", 2)])

    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT stable_id, n FROM t WHERE stable_id = ?", ("a",)).fetchone()
        assert row == ("a", 2)
    finally:
        conn.close()
