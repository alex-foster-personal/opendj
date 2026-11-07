"""Open state.db for set-notes reads and writes (SETNOTE-6340c)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.state import db as state_db

from .schema import apply_set_notes_migrations


def open_set_notes(db_path: Path) -> sqlite3.Connection:
    """Open an existing state.db read-write with the set-notes tables migrated."""
    if not db_path.exists():
        raise FileNotFoundError(f"missing {db_path}")
    conn = state_db.open_rw(db_path)
    apply_set_notes_migrations(conn)
    return conn


__all__ = ["open_set_notes"]
