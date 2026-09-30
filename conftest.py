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
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

# The interpreter contract is checked before anything else here. `uv run`
# falls back to a PATH command when that command is missing from the project
# environment, and a PATH `pytest` brings its own interpreter with it; pytest
# lives in the `dev` extra, so a plain `uv sync` leaves it out. Under an older
# interpreter the visible symptom is an unrelated module failing with
# `ImportError: cannot import name 'UTC' from 'datetime'`, which sends the
# reader after the wrong bug. Name the real cause instead.
from scripts.interpreter_contract import assert_interpreter_satisfies_floor

assert_interpreter_satisfies_floor(Path(__file__).resolve().parent / "pyproject.toml")

# Tests default to local so an agentbox root .env with remote mode cannot
# leak into resolution. Individual tests set remote explicitly.
os.environ.setdefault("MDT_LIBRARY_MODE", "local")

# The analyze-on-import reconcile loop shells out to apps.analysis.run, and
# the real daemon entry points arm it from this variable. Building an app in
# a test must never spawn an analyzer subprocess as a side effect, so the
# suite forces it off rather than merely defaulting it: a developer who runs
# the daemon in the same shell exports MUSIC_DJ_AUTO_ANALYZE=on, and
# setdefault would honor that export and let a TestClient lifespan launch
# real analyzer subprocesses. The loop's own tests do not read this variable
# at all - they construct the watcher enabled by hand
# (apps.webui.server.analysis_autostart.build(enabled=True)).
os.environ["MUSIC_DJ_AUTO_ANALYZE"] = "off"

import pytest

# Register the reqs plugin (coverage-matrix.md writer + --live-db gate), and the
# SMARTEST-CI tier plugins (specs/ci-fail-fast.md round 6a). They are registered
# HERE and not with `-p` on the command line because `.venv/bin/pytest` (the
# console script ci.yml runs) does not put the checkout on sys.path, so
# `-p scripts.x` dies with "No module named 'scripts'" before any option is
# parsed; a rootdir conftest is imported with rootdir on sys.path. Both plugins
# are inert unless one of their options is given.
pytest_plugins = [
    "scripts.pytest_reqs_plugin",
    "scripts.pytest_fast_tier",
    "scripts.pytest_tier_floor",
    "tests.support.spawned_servers",
]


REPO_ROOT: Path = Path(__file__).resolve().parent
FIXTURE_DIR: Path = REPO_ROOT / "tests" / "fixtures"
RB_FIXTURE: Path = FIXTURE_DIR / "rekordbox" / "master.plain.db"
RB_FIXTURE_MANIFEST: Path = FIXTURE_DIR / "rekordbox.manifest.json"


def _fail_closed_missing_fixture(detail: str) -> NoReturn:
    """Fail unless ``MDT_ALLOW_MISSING_FIXTURES=1`` explicitly opts out.

    AGENTS.md: "Never silently skip acceptance because data ... is
    missing." Once ``tests/fixtures/rekordbox/`` leaves the repo (the
    history rewrite ``rekordbox.extern`` is staged for), a missing fixture
    host becomes the NORMAL state for CI and fresh public clones -- every
    ingest/reconciliation/removal/dedup/writeback-safety test built on
    ``rb_plain_db_path`` would silently skip and the run would still read
    green. Default to failing loud; a developer machine that knowingly
    lacks the fixture host opts out explicitly rather than by a hidden
    default.
    """
    if os.environ.get("MDT_ALLOW_MISSING_FIXTURES") == "1":
        pytest.skip(f"{detail} (MDT_ALLOW_MISSING_FIXTURES=1 set)")
    else:
        pytest.fail(
            f"{detail} Set MDT_ALLOW_MISSING_FIXTURES=1 to explicitly skip on "
            "a machine that knowingly lacks the fixture host; unset, this "
            "fails closed rather than silently dropping ingest/"
            "reconciliation/removal/dedup/writeback-safety coverage."
        )
    raise AssertionError("unreachable: pytest.skip/pytest.fail always raise")


def _verify_rb_fixture_checksum(candidate: Path) -> None:
    """Verify the resolved rekordbox fixture DB against its committed manifest.

    ``fixture_path()`` only confirms the resolved target is a directory:
    once ``rekordbox.extern`` can point anywhere (LaCie, ``MUX_FIXTURE_HOST``,
    a regenerated copy), that proves nothing about the file's CONTENT. A
    stale or partially regenerated ``master.plain.db`` would otherwise be
    accepted silently and change test results (AGENTS.md: "Verify canonical
    fixtures by version, manifest, and checksum before use"). Always fails
    hard on mismatch -- this is a data-integrity error, not a "missing
    fixture" case, so ``MDT_ALLOW_MISSING_FIXTURES`` does not apply.
    """
    import json

    from apps.shared.hashing import sha256_file

    manifest = json.loads(RB_FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("contract_version") != 1 or manifest.get("fixture") != "rekordbox":
        pytest.fail(
            f"Unsupported rekordbox fixture manifest at {RB_FIXTURE_MANIFEST}: "
            f"expected contract_version=1, fixture='rekordbox', got "
            f"contract_version={manifest.get('contract_version')!r}, "
            f"fixture={manifest.get('fixture')!r}. A manifest from an "
            "unsupported contract revision must not be accepted as canonical "
            "just because it retains the same key and digest."
        )
    expected = manifest["files"]["master.plain.db"]
    actual = sha256_file(candidate)
    if actual != expected:
        pytest.fail(
            f"Rekordbox fixture checksum mismatch at {candidate}: expected "
            f"{expected}, got {actual}. The resolved file does not match "
            f"{RB_FIXTURE_MANIFEST} -- regenerate the fixture "
            "(`python -m scripts.make_rb_fixture --force`) or fix "
            "MUX_FIXTURE_HOST/rekordbox.extern to point at the canonical copy."
        )


@pytest.fixture(scope="session")
def rb_plain_db_path() -> Path:
    """Absolute path to the rekordbox fixture DB, wherever it now lives.

    Session-scoped + read-only: callers MUST NOT open this path in
    read-write mode. Use ``tmp_rb_db`` or ``rb_plain_conn`` for any test
    that needs to mutate.

    Resolution goes through ``tests.fixtures._resolver`` rather than a
    hard-coded path, because this fixture is leaving the repository: it
    carries real track titles, real Spotify IDs and real filesystem paths
    from the maintainer's library (docs/oss-going-public-checklist.md, blocker 2).
    The resolver prefers the committed directory when it is present and
    falls back to the ``rekordbox.extern`` marker afterwards, so this keeps
    working on both sides of the history rewrite with no flag day.

    Unavailability fails closed (see ``_fail_closed_missing_fixture``) and
    content is checksum-verified against ``rekordbox.manifest.json`` (see
    ``_verify_rb_fixture_checksum``) before the path is handed to callers.
    """
    from tests.fixtures._resolver import FixtureNotAvailable, fixture_path

    try:
        candidate = fixture_path("rekordbox") / "master.plain.db"
    except FixtureNotAvailable as exc:
        _fail_closed_missing_fixture(f"Rekordbox fixture host not available: {exc}")
    except FileNotFoundError:
        _fail_closed_missing_fixture(
            f"Rekordbox fixture missing at {RB_FIXTURE} and no "
            "'rekordbox.extern' marker resolved. Mount the fixture host "
            "(MUX_FIXTURE_HOST), or regenerate with `python -m "
            "scripts.make_rb_fixture --force` and move the result onto the "
            "fixture host yourself -- do not leave a regenerated copy "
            "sitting in tests/fixtures/rekordbox/ once that directory is no "
            "longer tracked (`.gitignore` guards against re-adding it, but "
            "the file would still carry real track titles, Spotify IDs and "
            "filesystem paths -- see docs/oss-going-public-checklist.md "
            "blocker 2)."
        )
    if not candidate.is_file():
        _fail_closed_missing_fixture(f"Rekordbox fixture DB missing inside {candidate.parent}")
    _verify_rb_fixture_checksum(candidate)
    return candidate


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


# ----- one-way rekordbox import gate ---------------------------------------
# apps.shared.rekordbox_writeback ships OFF: no code path may write toward the
# real rekordbox library, share directory, or a USB export. The test suite
# inherits that default, so a suite with a forgotten monkeypatch is refused by
# the gate instead of reaching a real target.
#
# Suites that exercise live-write MECHANICS (the seven safety rails, backup and
# readback, USB plan/apply) against tmp fixtures opt back in per module with::
#
#     pytestmark = pytest.mark.rekordbox_writeback
#
# which is deliberately visible at the top of the file, so "this module runs
# with rekordbox writes enabled" is never an invisible property.


@pytest.fixture(autouse=True)
def _error_sink_log_is_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep OBS-01 sink writes inside the test tmp dir, never ~/jobs/logs."""
    monkeypatch.setenv("OPENDJ_ERROR_SINK_LOG", str(tmp_path / "opendj-error-sink.jsonl"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--standalone",
        action="store_true",
        default=False,
        help=(
            "Fail when MDT_DATA_DIR contains master.plain.db or state/anlz-cache "
            "(STANDALONE-01 isolation instrument)"
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--standalone"):
        from tests.standalone.guard import assert_data_dir_clean

        assert_data_dir_clean()


@pytest.fixture(autouse=True)
def _rekordbox_writeback_gate(request, monkeypatch):
    """Gate OFF by default; ON only for modules that opt in by marker."""
    from apps.shared.rekordbox_writeback import REKORDBOX_WRITEBACK_ENABLED_ENV

    if request.node.get_closest_marker("rekordbox_writeback") is not None:
        monkeypatch.setenv(REKORDBOX_WRITEBACK_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(REKORDBOX_WRITEBACK_ENABLED_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_threads(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail the test that leaves a background thread it started running.

    sentry_sdk-owned threads are checked everywhere; non-daemon threads only in
    modules marked ``no_leaked_threads``. Rules and the draining-monitor
    exemption: tests/support/thread_leaks.py.
    """
    from tests.support.thread_leaks import leaked_threads

    baseline = set(threading.enumerate())
    yield
    leaks = leaked_threads(
        baseline,
        include_non_daemon=request.node.get_closest_marker("no_leaked_threads") is not None,
    )
    if leaks:
        pytest.fail(
            f"{request.node.nodeid} left {len(leaks)} background thread(s) running: "
            + "; ".join(leaks)
            + ". Close what the test started (a sentry_sdk client: "
            "tests/support/sentry_client.close_sentry_client).",
            pytrace=False,
        )
