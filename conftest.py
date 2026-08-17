"""Root conftest -- loads the reqs plugin + shared test fixtures.

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

import os
import shutil
import sqlite3
from pathlib import Path

# Tests default to local so an agentbox root .env with remote mode cannot
# leak into resolution. Individual tests set remote explicitly.
os.environ.setdefault("MDT_LIBRARY_MODE", "local")

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


# ---------------------------------------------------------------------------
# External-host fixtures (LaCie). See tests/fixtures/_resolver.py.
#
# We expose ``big_usb_fixture`` at the repo-root conftest so it's visible to
# every test under tests/, not just tests/fixtures/. It returns the resolved
# Path to the 1586-track Pioneer USB export hosted on LaCie, or skips cleanly
# when the external host isn't mounted.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def big_usb_fixture():
    """Yield the Path to the LaCie-hosted rb-usb-export-big fixture.

    Session-scoped so downstream module-scoped parsers (e.g. the big
    reader test) can cache a single ``read_usb_export`` call rather
    than re-parsing the 1586-track export per test. Skips the test
    (rather than erroring) if the external host isn't mounted or the
    subpath is missing — cloning the repo on a fresh machine must not
    break the suite.
    """
    # Local import so tests/fixtures/_resolver.py stays optional: a
    # fresh clone without the resolver module (unlikely but defensible)
    # still collects the rest of the suite.
    from tests.fixtures._resolver import (  # type: ignore[import-not-found]
        FixtureNotAvailable,
        fixture_path,
    )

    try:
        return fixture_path("rb-usb-export-big")
    except (FixtureNotAvailable, FileNotFoundError) as exc:
        pytest.skip(f"rb-usb-export-big not available: {exc}")


@pytest.fixture
def rb_pyrekordbox_db(tmp_rb_db: Path):
    """Yield a pyrekordbox Rekordbox6Database over a per-test fixture copy.

    Opens with ``unlock=False`` because the fixture is already plain
    SQLite — attempting SQLCipher unlock would fail. Callers get a real
    ORM handle so they can iterate ``get_content()`` / ``get_playlist()``.
    """
    from pyrekordbox import Rekordbox6Database  # local import → optional dep

    db = Rekordbox6Database(path=str(tmp_rb_db), unlock=False)
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:
            pass
