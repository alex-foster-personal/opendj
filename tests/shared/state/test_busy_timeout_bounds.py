"""State-db handles carry an explicit, bounded busy wait.

Single-line intent:
  - if a lock is held past a handle's busy wait then it raises within that
    bound [broken if the wait is unbounded or silently inherited]
  - if a handle is asked to wait forever, or not at all, then it refuses
    [broken if an unbounded wait can be configured]
  - if a handle is opened with a bound then SQLite reports exactly that bound
    [broken if a later PRAGMA quietly overrides it]
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server import sqlite_backend
from tests.shared.state.peer_lock import current_wal_state_db, hold_exclusive_lock

pytestmark = pytest.mark.requirement("GUARD-09")


@pytest.mark.parametrize(
    "open_with_bound",
    [
        pytest.param(
            lambda path, bound: sqlite_backend.read_tracks_schema_version(
                path, busy_timeout_s=bound,
            ),
            id="read_tracks_schema_version",
        ),
        pytest.param(
            lambda path, bound: state_db.open_rw(path, busy_timeout_s=bound).close(),
            id="open_rw",
        ),
        pytest.param(
            lambda path, bound: state_db.open_ro(path, busy_timeout_s=bound)
            .execute("SELECT COUNT(*) FROM sqlite_master")
            .fetchone(),
            id="open_ro",
        ),
    ],
)
def test_a_peer_lock_held_past_the_bound_raises_within_it(
    tmp_path: Path, open_with_bound: Callable[[Path, float], object],
) -> None:
    """[if] a peer holds the lock past a handle's busy wait [then] the open
    raises ``database is locked`` no sooner than the bound and no later than
    the bound plus slack, [else stop]. Locked is an error, never "not stale".
    """
    bound_s, slack_s = 0.5, 2.0
    db_path = current_wal_state_db(tmp_path / "state.db")
    holder = hold_exclusive_lock(db_path, bound_s + slack_s + 3)
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            open_with_bound(db_path, bound_s)
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.communicate(timeout=30)
    assert bound_s <= elapsed < bound_s + slack_s, (
        f"raised after {elapsed:.2f}s against a {bound_s}s bound"
    )


@pytest.mark.parametrize("bound", [math.inf, math.nan, 0.0, -1.0])
def test_an_unbounded_or_zero_busy_wait_is_refused(tmp_path: Path, bound: float) -> None:
    """[if] a handle is asked to wait forever, or not at all [then] it
    refuses before opening, [else stop]."""
    db_path = current_wal_state_db(tmp_path / "state.db")
    for opener in (state_db.open_rw, state_db.open_ro):
        with pytest.raises(ValueError, match="busy_timeout_s"):
            opener(db_path, busy_timeout_s=bound)
    assert math.isfinite(state_db.BOOT_BUSY_TIMEOUT_S)
    assert state_db.BOOT_BUSY_TIMEOUT_S > state_db.DEFAULT_BUSY_TIMEOUT_S


@pytest.mark.parametrize(
    "bound", [0.5, state_db.DEFAULT_BUSY_TIMEOUT_S, state_db.BOOT_BUSY_TIMEOUT_S],
)
def test_each_handle_carries_the_bound_it_was_asked_for(
    tmp_path: Path, bound: float,
) -> None:
    """[if] a handle is opened with a busy wait [then] SQLite reports that
    exact wait for every later statement, [else stop]. The first statement
    alone cannot show this: a later PRAGMA could shorten the wait after it."""
    db_path = current_wal_state_db(tmp_path / "state.db")
    for opener in (state_db.open_rw, state_db.open_ro):
        conn = opener(db_path, busy_timeout_s=bound)
        try:
            effective_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        assert effective_ms == round(bound * 1000), opener.__name__
