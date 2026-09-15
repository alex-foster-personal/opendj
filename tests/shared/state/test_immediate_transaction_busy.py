"""Unit tests for SQLITE_BUSY handling in immediate_transaction (issue #2915).

Regression one-liners:
  - if BEGIN IMMEDIATE hits a held lock with busy_timeout=0 then StateStoreBusyError
  - if the lock is released within busy_timeout then the transaction commits
  - if OperationalError is not a lock failure then it propagates unchanged
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.shared.state.writer_common import immediate_transaction


def test_immediate_transaction_raises_state_store_busy_when_begin_locked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    holder = state_db.open_rw(path, apply_schema=True)
    waiter = state_db.open_rw(path, check_same_thread=False)
    try:
        holder.execute("BEGIN IMMEDIATE")
        waiter.execute("PRAGMA busy_timeout = 0")
        with pytest.raises(state_db.StateStoreBusyError):
            with immediate_transaction(waiter):
                pass
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        waiter.close()


def test_immediate_transaction_waits_and_succeeds_when_lock_released(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, v TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    holder = state_db.open_rw(path, check_same_thread=False)
    waiter = state_db.open_rw(path, check_same_thread=False)
    released = threading.Event()

    def _hold_then_release() -> None:
        holder.execute("BEGIN IMMEDIATE")
        released.set()
        time.sleep(0.15)
        holder.execute("ROLLBACK")
        holder.close()

    thread = threading.Thread(target=_hold_then_release)
    thread.start()
    released.wait(timeout=2.0)

    try:
        with immediate_transaction(waiter) as tx:
            tx.execute("INSERT INTO probe (v) VALUES (?)", ("ok",))
    finally:
        waiter.close()
        thread.join(timeout=5.0)

    verify = state_db.open_rw(path)
    try:
        row = verify.execute("SELECT v FROM probe").fetchone()
        assert row is not None and row[0] == "ok"
    finally:
        verify.close()


def test_immediate_transaction_non_busy_operational_error_unchanged(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            with immediate_transaction(conn):
                conn.execute("SELECT 1 FROM definitely_missing_table")
    finally:
        conn.close()
