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
[if] the Windows (``msvcrt.locking``) lock is in force [then] a second handle
    is refused and a release lets the next acquire in, [else stop].
[if] a Windows handle sits at a nonzero file position [then] it still locks
    and unlocks byte 0, so two handles cannot lock different bytes, [else stop].
[if] ``msvcrt.locking`` fails with anything but EACCES [then] the error
    propagates instead of reading as "another holder", [else stop].
"""

from __future__ import annotations

import errno
import functools
import os
from pathlib import Path

import pytest

from apps.shared.sync_runtime_gates import DEFER_REASON_SYNC_IN_PROGRESS, SyncDeferredError
from apps.sync_hub import maintenance, single_flight
from apps.sync_hub import status as sync_status

from .enrollment_transport import TestClientTransport

# `hub` and `spoke_a` are conftest.py fixtures (re-exported from
# test_hub_sync.py there) -- no local import needed, and importing them here
# too would shadow the fixture names as unused-import/redefinition warnings
# (see conftest.py's own comment on this exact trap).

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


def test_maintenance_sync_journals_success_while_still_holding_the_flock(
    spoke_a: Path, hub: TestClientTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a sync round completes successfully [then] its journal write
    happens BEFORE the cross-process flock is released (Sol review, PR #3831,
    P1/BLOCKING): the round-2 fix released the flock BEFORE journaling the
    success, so a second process could finish and journal an ``error`` in the
    gap, and this round's now-stale success would silently overwrite it on
    the next line -- the exact "later failure gets masked by an earlier,
    now-stale success" this fix closes.

    Proven directly, not by timing: ``sync_status.write_result`` is spied so
    that AT THE MOMENT it is called, it tries to acquire a fresh flock handle
    on the SAME data dir. While ``maintenance.sync``'s own flock is genuinely
    still held, that second acquire must be refused -- if the journal write
    ever moves back outside the ``with single_flight.sync_flock_for(...)``
    block, this spy's own acquire would succeed and the test would fail.

    Drives a REAL hub over the real ASGI router (the ``hub`` fixture from
    ``test_hub_sync``), not a mock: the property under test is the ORDERING
    of the flock release relative to the journal write, which only exists
    once ``client.run_sync`` has genuinely completed.
    """
    checked = {"write_result_ran_under_the_flock": False}
    real_write_result = sync_status.write_result

    def _spy_write_result(data_dir: Path, result: sync_status.SyncResult) -> None:
        with (
            pytest.raises(single_flight.SyncInProgressError),
            single_flight.sync_flock_for(data_dir),
        ):
            pass  # pragma: no cover -- must never be reached
        checked["write_result_ran_under_the_flock"] = True
        real_write_result(data_dir, result)

    monkeypatch.setattr(sync_status, "write_result", _spy_write_result)

    result = maintenance.sync(
        spoke_a, "http://hub.invalid", transport=hub, name="spoke-a", force=True
    )

    assert checked["write_result_ran_under_the_flock"] is True
    assert result.digest  # sanity: a real completed round came back
    # And the flock is genuinely free again once sync() has returned.
    with single_flight.sync_flock_for(spoke_a):
        pass


# ----- Windows branch (Sol, PR #3831, P1/BLOCKING) -----------------------------
# No Windows interpreter runs in CI, so these drive the msvcrt primitives with
# a stand-in that keeps the real semantics this code depends on: a lock is on
# the byte at the handle's CURRENT position, a byte another handle holds
# refuses with EACCES, and only the holding handle can unlock it.


class _FakeMsvcrt:
    LK_UNLCK, LK_NBLCK = 0, 2

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], int] = {}

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        assert nbytes == 1
        key = (os.fstat(fd).st_ino, os.lseek(fd, 0, os.SEEK_CUR))
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(errno.EACCES, "byte already locked")
            self.held[key] = fd
        elif mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise OSError(errno.EACCES, "byte not locked by this handle")
            del self.held[key]
        else:
            raise AssertionError(f"unexpected msvcrt mode {mode}")


def _api(fake: _FakeMsvcrt) -> single_flight._MsvcrtLocking:
    return single_flight._MsvcrtLocking(fake.locking, fake.LK_NBLCK, fake.LK_UNLCK)


def test_windows_lock_refuses_a_second_handle_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeMsvcrt()
    try_lock = functools.partial(single_flight._try_lock_nt, api=_api(fake))
    unlock = functools.partial(single_flight._unlock_nt, api=_api(fake))
    monkeypatch.setattr(single_flight, "_try_lock", try_lock)
    monkeypatch.setattr(single_flight, "_unlock", unlock)

    with single_flight.sync_flock_for(tmp_path):
        assert len(fake.held) == 1
        busy = pytest.raises(single_flight.SyncInProgressError)
        with busy, single_flight.sync_flock_for(tmp_path):
            pass
    assert fake.held == {}
    with single_flight.sync_flock_for(tmp_path):
        assert len(fake.held) == 1


def test_windows_lock_locks_byte_zero_whatever_the_handle_position(tmp_path: Path) -> None:
    fake = _FakeMsvcrt()
    lock_path = tmp_path / single_flight.SYNC_LOCK_FILENAME
    first = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(lock_path, os.O_RDWR)
    try:
        os.write(first, b"xyz")
        assert single_flight._try_lock_nt(first, _api(fake))
        os.lseek(second, 2, os.SEEK_SET)
        assert not single_flight._try_lock_nt(second, _api(fake)), (
            "two handles at different positions both got a lock: byte 0 is not the lock byte"
        )
        os.lseek(first, 3, os.SEEK_SET)
        single_flight._unlock_nt(first, _api(fake))
        assert fake.held == {}
    finally:
        os.close(first)
        os.close(second)


def test_windows_lock_raises_an_unexpected_os_error(tmp_path: Path) -> None:
    def _bad_handle(fd: int, mode: int, nbytes: int) -> None:
        raise OSError(errno.EBADF, "bad file descriptor")

    fd = os.open(tmp_path / single_flight.SYNC_LOCK_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(OSError) as raised:
            single_flight._try_lock_nt(fd, single_flight._MsvcrtLocking(_bad_handle, 2, 0))
        assert raised.value.errno == errno.EBADF
    finally:
        os.close(fd)
