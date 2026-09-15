"""SQLite busy-lock classification helpers (issue #2790).

[if] SQLite raises locked or busy operational errors [then] is_sqlite_busy classifies them, [else stop].
"""
from __future__ import annotations

import sqlite3

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
