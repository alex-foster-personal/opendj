"""STATE-22: a library-jobs tick that loses the writer lock warns once and retries.

[if] the writer lock is busy at a tick [then] a WARNING and a retry, no ERROR, [else stop].
"""
from __future__ import annotations

import functools
import logging
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server import library_jobs_autostart
from apps.webui.server.library_jobs_autostart import LibraryJobsWatcher

pytestmark = pytest.mark.requirement("STATE-22")


def _watcher(tmp_path: Path) -> LibraryJobsWatcher:
    data_dir = tmp_path / "data"
    return LibraryJobsWatcher(
        state_db=data_dir / "state" / "state.db",
        stems_root=data_dir / "state" / "stems",
        data_dir=data_dir,
        enabled=True,
    )


def _errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.requirement("STATE-22")
def test_a_busy_tick_warns_without_a_traceback_and_the_next_tick_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] a writer holds the lock past busy_timeout [then] WARN with a count, retry, [else stop]."""
    monkeypatch.setattr(
        state_db, "open_rw", functools.partial(state_db.open_rw, busy_timeout_s=0.2)
    )
    watcher = _watcher(tmp_path)
    caplog.set_level(logging.WARNING, logger=library_jobs_autostart.__name__)
    watcher.run_tick()
    assert not _errors(caplog), "control: an idle tick must succeed"

    holder = sqlite3.connect(str(watcher.state_db), isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        watcher.run_tick()
        watcher.run_tick()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert not _errors(caplog), [r.getMessage() for r in _errors(caplog)]
    warnings = [r for r in caplog.records if "busy tick" in r.getMessage()]
    assert [r.exc_info for r in warnings] == [None, None], "a busy tick logged a traceback"
    assert "(1 busy tick(s)" in warnings[0].getMessage()
    assert "(2 busy tick(s)" in warnings[1].getMessage()

    caplog.clear()
    watcher.run_tick()
    assert watcher.busy_ticks == 2
    assert not caplog.records, "the tick after the lock freed did not run cleanly"


@pytest.mark.requirement("STATE-22")
def test_any_other_tick_error_still_logs_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[if] a tick fails for any reason but a busy lock [then] ERROR with traceback, [else stop]."""
    watcher = _watcher(tmp_path)

    def broken() -> None:
        raise sqlite3.OperationalError("no such table: analysis_queue_batch")

    monkeypatch.setattr(watcher, "tick", broken)
    caplog.set_level(logging.WARNING, logger=library_jobs_autostart.__name__)
    watcher.run_tick()

    errors = _errors(caplog)
    assert len(errors) == 1 and errors[0].exc_info is not None
    assert watcher.busy_ticks == 0
