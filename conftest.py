"""Root conftest — loads the reqs plugin + shared test fixtures.

Fixtures exposed:

  * ``rb_plain_db_path`` (session) — Path to the committed fixture DB.
  * ``rb_plain_conn`` (function) — sqlite3.Connection to a **copy** of the
    fixture. Each test gets its own throwaway copy so writes don't leak.
  * ``tmp_rb_db`` (function) — Path to a fresh mutable copy (for apply.py
    write-path testing). Never the committed fixture itself.

Tests can trust the copy-per-test contract: no test can mutate the shared
master fixture even transiently.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

# Register the reqs plugin (coverage-matrix.md writer + --live-db gate).
pytest_plugins = ["scripts.pytest_reqs_plugin"]


REPO_ROOT: Path = Path(__file__).resolve().parent
FIXTURE_DIR: Path = REPO_ROOT / "tests" / "fixtures"
RB_FIXTURE: Path = FIXTURE_DIR / "rekordbox" / "master.plain.db"


@pytest.fixture(scope="session")
def rb_plain_db_path() -> Path:
    """Absolute path to the committed rekordbox fixture.

    Session-scoped + read-only: callers MUST NOT open this path in
    read-write mode. Use ``tmp_rb_db`` or ``rb_plain_conn`` for any test
    that needs to mutate.
    """
    if not RB_FIXTURE.exists():
        pytest.skip(
            f"Rekordbox fixture missing at {RB_FIXTURE}. "
            "Run `python -m scripts.make_rb_fixture --force` first."
        )
    return RB_FIXTURE


@pytest.fixture
def rb_plain_conn(rb_plain_db_path: Path, tmp_path: Path):
    """Yield a sqlite3 Connection to a per-test copy of the fixture."""
    dst = tmp_path / "master.plain.db"
    shutil.copy2(rb_plain_db_path, dst)
    con = sqlite3.connect(dst)
    try:
        yield con
    finally:
        con.close()


@pytest.fixture
def tmp_rb_db(rb_plain_db_path: Path, tmp_path: Path) -> Path:
    """Return a writable per-test Path to a fresh copy of the fixture.

    Tests can open + mutate this path freely; nothing leaks across tests
    because ``tmp_path`` is unique per test.
    """
    dst = tmp_path / "master.plain.db"
    shutil.copy2(rb_plain_db_path, dst)
    return dst
