"""SQLite busy-lock classification helpers (issue #2790).

[if] SQLite raises locked or busy operational errors [then] is_sqlite_busy classifies them, [else stop].
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from apps.shared.state import db as state_db

pytestmark = pytest.mark.requirement("LIBM-09")


def test_is_sqlite_busy_recognizes_locked_database() -> None:
    locked = sqlite3.OperationalError("database is locked")
    busy = sqlite3.OperationalError("database is busy")
    assert state_db.is_sqlite_busy(locked) is True
    assert state_db.is_sqlite_busy(busy) is True

    coded = sqlite3.OperationalError("busy")
    coded.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
    assert state_db.is_sqlite_busy(coded) is True


def test_is_sqlite_busy_rejects_other_operational_errors() -> None:
    assert state_db.is_sqlite_busy(sqlite3.OperationalError("no such table: foo")) is False
    assert state_db.is_sqlite_busy(ValueError("database is locked")) is False


def _fresh_db_with_a_writer(path: Path) -> sqlite3.Connection:
    """A rollback-journal database (as a fresh state.db is before its first
    WAL switch) with another connection holding a RESERVED write lock."""
    writer = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    writer.execute("CREATE TABLE holder(x)")
    assert writer.execute("PRAGMA journal_mode").fetchone() == ("delete",), "control: not yet in WAL"
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO holder VALUES (1)")
    return writer


def test_open_rw_waits_out_a_writer_while_a_fresh_db_switches_to_wal(tmp_path: Path) -> None:
    """[if] another connection holds a write lock while a fresh state.db is switched to WAL
    [then] open_rw waits for it inside busy_timeout_s instead of raising at once, [else stop].

    Main CI, Fri 16 Oct 2026: the folder-import worker's open_rw died with
    "database is locked" on PRAGMA journal_mode = WAL while the engine held
    the fresh DB, because SQLite answers that pragma without its busy handler.
    """
    path = tmp_path / "state.db"
    writer = _fresh_db_with_a_writer(path)
    # Control: the raw pragma fails at once, so the wait below is open_rw's own.
    probe = sqlite3.connect(str(path), timeout=2.0, isolation_level=None)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        probe.execute("PRAGMA journal_mode = WAL")
    assert time.monotonic() - started < 1.0, "control: SQLite did not skip its busy handler here"
    probe.close()

    release = threading.Timer(0.3, lambda: writer.execute("COMMIT"))
    release.start()
    try:
        conn = state_db.open_rw(path, apply_schema=False, busy_timeout_s=5.0)
    finally:
        release.join()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()
        writer.close()


def test_open_rw_still_gives_up_on_a_writer_that_outlasts_busy_timeout_s(tmp_path: Path) -> None:
    """[if] the writer holds its lock past busy_timeout_s [then] open_rw raises database is locked
    after about that long, not sooner and not never, [else stop]."""
    path = tmp_path / "state.db"
    writer = _fresh_db_with_a_writer(path)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        state_db.open_rw(path, apply_schema=False, busy_timeout_s=0.5)
    waited = time.monotonic() - started
    writer.close()
    assert 0.4 < waited < 3.0, f"open_rw gave up after {waited:.2f} s for a 0.5 s busy_timeout_s"


def test_open_rw_keeps_its_bound_when_the_writer_turns_exclusive_late(tmp_path: Path) -> None:
    """[if] the writer holds RESERVED for most of busy_timeout_s, then takes EXCLUSIVE
    [then] the attempt that meets EXCLUSIVE waits only the time left, so open_rw
    still raises at about busy_timeout_s, not near twice it, [else stop].

    Codex on #7108: an attempt given the handle's full timeout ran a 0.5 s open
    to about 0.95 s.
    """
    path = tmp_path / "state.db"
    writer = _fresh_db_with_a_writer(path)
    writer.execute("PRAGMA cache_size = 1")

    def turn_exclusive() -> None:
        # Spilling a one-page cache mid-transaction makes the writer take EXCLUSIVE.
        writer.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 400) "
            "INSERT INTO holder SELECT randomblob(4096) FROM n"
        )

    upgrade = threading.Timer(0.7, turn_exclusive)
    started = time.monotonic()
    upgrade.start()
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            state_db.open_rw(path, apply_schema=False, busy_timeout_s=1.0)
        waited = time.monotonic() - started
    finally:
        upgrade.join()
    # Control: the writer really holds EXCLUSIVE now, so even a read is refused.
    reader = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        reader.execute("SELECT count(*) FROM holder").fetchone()
    reader.close()
    writer.close()
    assert 0.9 < waited < 1.4, f"open_rw gave up after {waited:.2f} s for a 1.0 s busy_timeout_s"
