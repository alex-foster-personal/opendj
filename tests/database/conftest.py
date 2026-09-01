"""Shared fixtures for ``tests/database``.

Mirrors ``tests/shared/state/conftest.py`` -- every test that needs a state
DB gets a fresh file under ``tmp_path``, migrated to the current
``SCHEMA_VERSION`` by :func:`apps.shared.state.db.open_rw`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import db as state_db


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def state_conn(state_db_path: Path):
    conn = state_db.open_rw(state_db_path)
    try:
        yield conn
    finally:
        conn.close()
