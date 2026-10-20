"""Shared helpers for the pairings CLI entry points.

Isolates SQLite path resolution + repo construction so the three
entrypoints (``add`` / ``remove`` / ``list``) stay thin.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from apps.shared.pairings import PairingsRepo, ensure_phase08_tables


def _default_state_db() -> Path:
    # Resolve through apps.shared.paths so the live location stays in one
    # place (apps/shared/paths.py is the append-only shared file).
    try:
        from apps.shared.paths import STATE_DB
        return Path(STATE_DB)
    except Exception:  # pragma: no cover - very early bootstraps
        return Path("(private path)")


def open_state_db(path: Path | None = None) -> sqlite3.Connection:
    """Open the state DB rw and ensure Phase 08 tables exist.

    ``path=None`` picks the canonical :data:`apps.shared.paths.STATE_DB`.
    Parent directory is created if missing.
    """
    target = path if path is not None else _default_state_db()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    ensure_phase08_tables(conn)
    return conn


def build_repo(path: Path | None = None) -> tuple[PairingsRepo, sqlite3.Connection]:
    conn = open_state_db(path)
    return PairingsRepo(conn, ensure_schema=False), conn


def add_db_argument(parser: argparse.ArgumentParser) -> None:
    """Register the shared ``--db`` override used by all pairings CLIs."""
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="override state DB path (default: apps.shared.paths.STATE_DB)",
    )


__all__ = ["add_db_argument", "open_state_db", "build_repo"]
