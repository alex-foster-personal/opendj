"""Shared state.db setup for set-notes route and CLI tests."""

from __future__ import annotations

from pathlib import Path

from apps.shared.state import db as state_db


def init_empty_state_db(db_path: Path) -> None:
    """Create parent dirs and touch an empty state.db at ``db_path``."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    state_db.open_rw(db_path).close()
