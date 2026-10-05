"""STATE-16: the state.db WAL is reset by a periodic TRUNCATE checkpoint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.webui.server import state_maintenance as sm

pytestmark = pytest.mark.requirement("STATE-16")


@pytest.fixture
def held_db(tmp_path: Path):
    db = tmp_path / "state.db"
    keeper = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)")
    for i in range(400):
        keeper.execute("INSERT INTO t VALUES (?, ?)", (i, "x" * 2000))
    yield db, keeper
    keeper.close()


def test_large_wal_is_truncated(held_db, monkeypatch) -> None:
    """[if] the WAL is over the threshold and no reader pins it [then] it is truncated, [else stop]."""
    db, _keeper = held_db
    monkeypatch.setattr(sm.CFG, "WAL_TRUNCATE_BYTES", 64 * 1024)
    assert sm.wal_bytes(db) > 64 * 1024
    result = sm.StateMaintenance(state_db_path=db).tick()
    assert result is not None and result.busy is False
    assert result.wal_bytes_before > 64 * 1024 and result.wal_bytes_after == 0
    assert sm.wal_bytes(db) == 0


def test_small_wal_is_left_alone(held_db, monkeypatch) -> None:
    """[if] the WAL is under the threshold [then] no checkpoint runs and it keeps its size, [else stop]."""
    db, _keeper = held_db
    before = sm.wal_bytes(db)
    monkeypatch.setattr(sm.CFG, "WAL_TRUNCATE_BYTES", before + 1)
    assert sm.StateMaintenance(state_db_path=db).tick() is None
    assert sm.wal_bytes(db) == before


def test_pinning_reader_reports_busy_and_later_resets(held_db, monkeypatch) -> None:
    """[if] a reader pins an old snapshot [then] busy is reported, and the next tick resets, [else stop]."""
    db, keeper = held_db
    monkeypatch.setattr(sm.CFG, "WAL_TRUNCATE_BYTES", 1024)
    monkeypatch.setattr(sm.CFG, "CHECKPOINT_BUSY_MS", 100)
    reader = sqlite3.connect(db, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM t").fetchone()
    keeper.execute("INSERT INTO t VALUES (100000, 'late')")
    maintenance = sm.StateMaintenance(state_db_path=db)
    busy = maintenance.tick()
    assert busy is not None and busy.busy is True
    assert sm.wal_bytes(db) > 0
    reader.execute("COMMIT")
    reader.close()
    done = maintenance.tick()
    assert done is not None and done.busy is False
    assert sm.wal_bytes(db) == 0


def test_writer_lock_held_reports_busy_not_raise(held_db, monkeypatch) -> None:
    """[if] another connection holds the writer lock [then] busy is reported, not raised, [else stop]."""
    db, keeper = held_db
    monkeypatch.setattr(sm.CFG, "WAL_TRUNCATE_BYTES", 1024)
    monkeypatch.setattr(sm.CFG, "CHECKPOINT_BUSY_MS", 100)
    keeper.execute("BEGIN IMMEDIATE")
    try:
        result = sm.checkpoint_truncate(db, busy_ms=100)
    finally:
        keeper.execute("COMMIT")
    assert result.busy is True


def test_missing_db_is_never_created(tmp_path: Path) -> None:
    """[if] state.db does not exist [then] a tick creates nothing, [else stop]."""
    db = tmp_path / "state.db"
    assert sm.StateMaintenance(state_db_path=db).tick() is None
    assert not db.exists()


def test_should_checkpoint_threshold_is_strict() -> None:
    """[if] the WAL is exactly at the threshold [then] no checkpoint, one byte over runs one, [else stop]."""
    assert sm.should_checkpoint(100, 100) is False
    assert sm.should_checkpoint(101, 100) is True
