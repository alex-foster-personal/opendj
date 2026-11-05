"""Open the state DB and run one additive schema migration hook."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from apps.shared.state import db as state_db


def open_rw_migrated(
    db: Path,
    migrate: Callable[[sqlite3.Connection], object],
) -> sqlite3.Connection:
    conn = state_db.open_rw(db)
    migrate(conn)
    return conn


__all__ = ["open_rw_migrated"]
