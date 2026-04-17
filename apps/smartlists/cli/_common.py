"""Shared CLI helpers -- state DB path + repo construction."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.pairings import ensure_phase08_tables
from apps.smartlists.repo import SmartlistsRepo


def _default_state_db() -> Path:
    try:
        from apps.shared.paths import STATE_DB
        return Path(STATE_DB)
    except Exception:  # pragma: no cover
        return Path("data/state/state.db")


def open_state_db(path: Path | None = None) -> sqlite3.Connection:
    target = path if path is not None else _default_state_db()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    ensure_phase08_tables(conn)
    return conn


def build_repo(path: Path | None = None) -> tuple[SmartlistsRepo, sqlite3.Connection]:
    conn = open_state_db(path)
    return SmartlistsRepo(conn, ensure_schema=False), conn


__all__ = ["open_state_db", "build_repo"]
