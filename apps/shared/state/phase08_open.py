"""Open the state DB with Phase 08 pairings tables (CLI helpers)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.shared.pairings import ensure_phase08_tables
from apps.shared.state.schema import apply_migrations


def default_state_db_path() -> Path:
    try:
        from apps.shared.paths import STATE_DB

        return Path(STATE_DB)
    except Exception:  # pragma: no cover - very early bootstraps  # noqa: BLE001
        return Path("(private path)")


def open_phase08_state_db(path: Path | None = None) -> sqlite3.Connection:
    """Open rw, apply WAL pragmas, the Phase 5 schema (``events``) and Phase 08 tables."""
    target = path if path is not None else default_state_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    apply_migrations(conn)
    ensure_phase08_tables(conn)
    return conn


__all__ = ["default_state_db_path", "open_phase08_state_db"]
