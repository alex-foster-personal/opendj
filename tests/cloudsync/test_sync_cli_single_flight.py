"""CLOUDSYNC-14: the CLI must share single-flight protection, not just claim to.

Found by review on PR #3831 (Codex, Wed 24 Sep 2026): the shipped
CLOUDSYNC-14 acceptance line claims the CLI's ``sync --force`` shares
single-flight protection with the scheduler and the operator's Sync now, but
``apps.sync_hub.single_flight.sync_lock_for`` is a plain ``threading.Lock`` --
visible only inside the ONE process that holds it. ``python -m apps.sync_hub
sync`` is its OWN process and never even imports that lock, so it could start
a second concurrent round against the SAME ``state.db`` while the engine's
scheduler or "Sync now" was already mid-round. ``sync_flock_for`` (an
``fcntl.flock`` on a file in the data dir, the same cross-process pattern
``apps.engine_core.lock.EngineLock`` already uses for the engine singleton)
closes that gap, and ``apps.sync_hub.maintenance.sync`` -- the one function
the scheduler, the HTTP route, and the CLI all funnel through to reach
``client.run_sync`` -- now wraps that call with it.

[if] another holder already holds a data dir's sync flock [then] a second
    acquire raises SyncInProgressError without blocking, [else stop].
[if] a sync flock's holder releases it [then] a later acquire succeeds, [else stop].
[if] maintenance.sync is called while the flock is held [then] it defers with
    sync_in_progress before any hub I/O, [else stop].
[if] the caller passed force=True [then] the flock still refuses a
    concurrent round -- force only skips the Gig/deck-playing gate, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.sync_runtime_gates import DEFER_REASON_SYNC_IN_PROGRESS, SyncDeferredError
from apps.sync_hub import maintenance, single_flight

pytestmark = pytest.mark.requirement("CLOUDSYNC-14")


def test_sync_flock_for_refuses_a_concurrent_holder(tmp_path: Path) -> None:
    """A second acquire against the SAME data dir raises immediately -- it
    must never block, since callers (the scheduler, the HTTP route, the CLI)
    all need a fast refusal, not a hang."""
    with (
        single_flight.sync_flock_for(tmp_path),
        pytest.raises(single_flight.SyncInProgressError),
        single_flight.sync_flock_for(tmp_path),
    ):
        pass  # pragma: no cover -- must never be reached


def test_sync_flock_for_is_released_when_the_with_block_exits(tmp_path: Path) -> None:
    """[if] the first holder's ``with`` block exits [then] a later acquire
    succeeds -- proves this is a real release, not a lock that leaks."""
    with single_flight.sync_flock_for(tmp_path):
        pass
    with single_flight.sync_flock_for(tmp_path):
        pass  # no SyncInProgressError this time


def test_maintenance_sync_defers_when_another_process_holds_the_flock(
    tmp_path: Path,
) -> None:
    """[if] another holder already has the data dir's sync flock [then]
    ``maintenance.sync`` defers with ``sync_in_progress`` -- and does so
    BEFORE any hub I/O (claude-review / Codex, PR #3831, P1/BLOCKING): the
    hub URL below is deliberately unreachable, the same
    "unreachable-proves-no-I/O-was-attempted" technique
    ``test_cli_sync_defers_for_a_playing_deck_seen_on_a_live_engine`` already
    uses for the Gig/deck-playing gate above this one.
    """
    with single_flight.sync_flock_for(tmp_path), pytest.raises(SyncDeferredError) as excinfo:
        maintenance.sync(tmp_path, "http://127.0.0.1:1", force=False)
    assert excinfo.value.reason == DEFER_REASON_SYNC_IN_PROGRESS


def test_maintenance_sync_force_does_not_bypass_the_sync_flock(tmp_path: Path) -> None:
    """[if] --force is set AND another holder has the flock [then]
    ``maintenance.sync`` still defers with ``sync_in_progress`` (claude-review
    / Codex, PR #3831, P1/BLOCKING): force skips only the Gig-posture /
    playing-deck safety judgement above -- two concurrent rounds writing the
    same ``state.db`` is a data-integrity failure, not a call an operator can
    override.
    """
    with single_flight.sync_flock_for(tmp_path), pytest.raises(SyncDeferredError) as excinfo:
        maintenance.sync(tmp_path, "http://127.0.0.1:1", force=True)
    assert excinfo.value.reason == DEFER_REASON_SYNC_IN_PROGRESS


def test_maintenance_sync_cli_exit_code_for_a_concurrent_round(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end through ``main()``: the CLI reports the documented deferred
    exit code and reason, exactly like the Gig/deck-playing defer above it,
    rather than a hub connection error or an uncaught exception."""
    with single_flight.sync_flock_for(tmp_path):
        exit_code = maintenance.main(
            ["sync", "--data-dir", str(tmp_path), "--hub", "http://127.0.0.1:1", "--force"]
        )
    assert exit_code == maintenance.EXIT_SYNC_DEFERRED
    assert f"DEFERRED: {DEFER_REASON_SYNC_IN_PROGRESS}" in capsys.readouterr().err
