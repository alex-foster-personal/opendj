"""PERF-RB-05: an unchanged probe answer does not rewrite its path_availability row.

Regression one-liners:
  - if a fresh row is probed again with the same size then nothing is written
  - if the size changed then the row is written at once
  - if the stored row is older than the rewrite window then it is written
  - if a path is new then it is inserted
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.rb_vendor_pkg import path_index

NS = "ns-test"
PATH = "/Music/a.mp3"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = tmp_path / "state" / "state.db"
    connection = state_db.open_rw(db_path, apply_schema=True)
    try:
        yield connection
    finally:
        connection.close()


def _row(conn: sqlite3.Connection) -> tuple[int | None, str]:
    return conn.execute(
        "SELECT materialised_size, checked_at FROM path_availability "
        "WHERE resolver_namespace = ? AND logical_path = ?",
        (NS, PATH),
    ).fetchone()


def _age_row(conn: sqlite3.Connection, seconds: float) -> str:
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")
    conn.execute("UPDATE path_availability SET checked_at = ?", (stamp,))
    conn.commit()
    return stamp


def _writes(conn: sqlite3.Connection, rows: list[tuple[str, int | None]]) -> int:
    before = conn.total_changes
    path_index.upsert_rows(conn, NS, rows)
    conn.commit()
    return conn.total_changes - before


@pytest.mark.requirement("PERF-RB-05")
def test_unchanged_fresh_answer_writes_nothing(conn: sqlite3.Connection) -> None:
    """[if] a fresh row is re-probed unchanged [then] no row is written, [else stop]."""
    _writes(conn, [(PATH, 100)])
    stamp = _age_row(conn, 60)

    assert _writes(conn, [(PATH, 100)]) == 0
    assert _row(conn) == (100, stamp)


@pytest.mark.requirement("PERF-RB-05")
def test_changed_size_is_written_at_once(conn: sqlite3.Connection) -> None:
    """[if] the probed size changed [then] the row is rewritten, [else stop]."""
    _writes(conn, [(PATH, 100)])
    stamp = _age_row(conn, 60)

    assert _writes(conn, [(PATH, None)]) == 1
    size, checked_at = _row(conn)
    assert size is None and checked_at > stamp


@pytest.mark.requirement("PERF-RB-05")
def test_unchanged_row_past_the_window_is_rewritten(conn: sqlite3.Connection) -> None:
    """[if] an unchanged row is older than the window [then] it is rewritten, [else stop]."""
    _writes(conn, [(PATH, 100)])
    stamp = _age_row(conn, path_index.UNCHANGED_REWRITE_AFTER_S + 60)

    assert _writes(conn, [(PATH, 100)]) == 1
    assert _row(conn)[1] > stamp


@pytest.mark.requirement("PERF-RB-05")
def test_new_path_is_inserted(conn: sqlite3.Connection) -> None:
    """[if] a path has no row yet [then] it is inserted, [else stop]."""
    assert _writes(conn, [(PATH, 100)]) == 1
    assert _row(conn)[0] == 100
