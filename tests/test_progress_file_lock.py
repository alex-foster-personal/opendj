"""Real-process acceptance tests for the bounded progress-ledger sidecar lock.

[if] the progress sidecar lock fails to reclaim, time out or hand off within its bound [then] fail, [else stop].

Companion to (private path) (which exercises the full
PATCH transaction under contention); this file targets
`_progress_file_lock`/`_acquire_progress_lock_bounded` directly so the
bounded-wait CONTRACT itself is proven, independent of the HTTP route. No
mocked OS locking anywhere -- every test uses a real sidecar file and real
processes.

Regression one-liners:
  - if a crashed (os._exit) holder is not reclaimed within a bounded time
    then the fix regressed to the old unbounded shape
  - if a live-but-wedged holder does not make a contender raise TimeoutError
    naming the lock path within its ceiling then broken
  - if a TimeoutError'd contender leaves the sidecar unusable for the real
    holder's own later release then broken
  - if a normal (non-crash, non-wedged) holder->contender handoff regresses
    then broken
  - if PROGRESS_LOCK_TIMEOUT_S=0 does not make a contended acquire fail
    immediately then this file's own guard does not bite
"""
from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from apps.webui.server.routes import progress as progress_module

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]


def _seed_ledger(ledger_path: Path) -> None:
    ledger_path.write_text("areas: []\nmeta: {}\n", encoding="utf-8")


def _hold_then_die(ledger_path: str, acquired: Any, release: Any) -> None:
    """Acquire the real lock, signal held, then os._exit while holding it."""
    import os

    path = Path(ledger_path)
    with progress_module._progress_file_lock(path):
        acquired.set()
        release.wait(timeout=10)
        os._exit(70)


def _hold_then_sleep_then_release(
    ledger_path: str, acquired: Any, sleep_s: float,
) -> None:
    """Acquire the real lock, signal held, sleep (simulated wedge), release
    normally. Exits 0 -- this process never crashes, it just outlives a
    contender's short ceiling, which is the 'alive but wedged' shape.
    """
    path = Path(ledger_path)
    with progress_module._progress_file_lock(path):
        acquired.set()
        time.sleep(sleep_s)


def _hold_then_release_promptly(ledger_path: str, acquired: Any) -> None:
    """Acquire, signal, release almost immediately -- the normal handoff."""
    path = Path(ledger_path)
    with progress_module._progress_file_lock(path):
        acquired.set()
        time.sleep(0.05)


def _release_after_delay(release: Any, delay_s: float) -> None:
    time.sleep(delay_s)
    release.set()


def _spawn_holder(target: Any, args: tuple[Any, ...]) -> Any:
    return multiprocessing.get_context("spawn").Process(target=target, args=args)


def test_crashed_holder_reclaimed_within_bounded_time(tmp_path: Path) -> None:
    """(a) A contender already blocked when the holder crashes is granted
    the lock within a bounded time, not left waiting indefinitely."""
    ledger_path = tmp_path / "progress-tree.yaml"
    _seed_ledger(ledger_path)
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    release = ctx.Event()
    holder = _spawn_holder(_hold_then_die, (str(ledger_path), acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=10)
        # Trigger the crash from a background thread 0.3s in, so the main
        # thread's lock attempt below is genuinely blocked (contending)
        # at the moment the holder dies, not started fresh afterward.
        releaser = threading.Thread(target=_release_after_delay, args=(release, 0.3))
        releaser.start()

        start = time.monotonic()
        with progress_module._progress_file_lock(ledger_path):
            elapsed = time.monotonic() - start

        releaser.join(timeout=5)
        holder.join(timeout=10)
        assert holder.exitcode == 70
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert elapsed < 5.0, f"reclaim took {elapsed:.2f}s, expected under 5s"


def test_live_wedged_holder_times_out_and_holder_still_releases_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A live holder that outlives a small ceiling makes the contender
    raise TimeoutError naming the lock path -- and the holder's own later
    release still succeeds (the contender's failure does not wedge the
    sidecar for the real owner)."""
    ledger_path = tmp_path / "progress-tree.yaml"
    _seed_ledger(ledger_path)
    lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
    monkeypatch.setattr(progress_module, "PROGRESS_LOCK_TIMEOUT_S", 1.0)

    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    # Sleeps well past the 1.0s ceiling above before releasing normally --
    # simulates "alive but wedged", distinct from a crash.
    holder = _spawn_holder(
        _hold_then_sleep_then_release, (str(ledger_path), acquired, 3.0),
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10)

        with (
            pytest.raises(TimeoutError) as exc_info,
            progress_module._progress_file_lock(ledger_path),
        ):
            pass  # pragma: no cover -- must not be reached
        assert str(lock_path) in str(exc_info.value)
        assert "1s" in str(exc_info.value) or "1.0" in str(exc_info.value) or (
            f"{1.0:.0f}s" in str(exc_info.value)
        )

        # The still-alive, still-wedged holder now wakes up and releases
        # through its own normal context-manager exit -- must succeed.
        holder.join(timeout=10)
        assert not holder.is_alive()
        assert holder.exitcode == 0
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    # Control: the sidecar is genuinely usable afterward, not left wedged
    # by the contender's own failed attempt.
    monkeypatch.undo()
    start = time.monotonic()
    with progress_module._progress_file_lock(ledger_path):
        pass
    assert time.monotonic() - start < 2.0


def test_normal_handoff_still_works(tmp_path: Path) -> None:
    """(c) Control: an ordinary holder->contender handoff (no crash, no
    wedge) still completes quickly under the bounded implementation."""
    ledger_path = tmp_path / "progress-tree.yaml"
    _seed_ledger(ledger_path)
    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    holder = _spawn_holder(_hold_then_release_promptly, (str(ledger_path), acquired))
    holder.start()
    try:
        assert acquired.wait(timeout=10)
        start = time.monotonic()
        with progress_module._progress_file_lock(ledger_path):
            elapsed = time.monotonic() - start
        holder.join(timeout=10)
        assert holder.exitcode == 0
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert elapsed < 2.0, f"normal handoff took {elapsed:.2f}s, expected under 2s"


def test_zero_ceiling_breaks_reclaim_then_restored_ceiling_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutate the guard both ways (verification.md): a ceiling of 0s must
    make even a same-instant-freeable contended lock fail loud (proving
    this file's tests actually bite), and restoring the real ceiling must
    make the identical scenario succeed again (proving the mutation, not
    something else, was the cause)."""
    ledger_path = tmp_path / "progress-tree.yaml"
    _seed_ledger(ledger_path)
    ctx = multiprocessing.get_context("spawn")

    # --- mutated: ceiling=0 must break a currently-held lock ---
    monkeypatch.setattr(progress_module, "PROGRESS_LOCK_TIMEOUT_S", 0.0)
    acquired = ctx.Event()
    release = ctx.Event()
    holder = _spawn_holder(_hold_then_die, (str(ledger_path), acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=10)
        with (
            pytest.raises(TimeoutError),
            progress_module._progress_file_lock(ledger_path),
        ):
            pass  # pragma: no cover -- must not be reached
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    # --- restored: the identical crashed-holder scenario now succeeds ---
    monkeypatch.undo()
    acquired2 = ctx.Event()
    release2 = ctx.Event()
    holder2 = _spawn_holder(_hold_then_die, (str(ledger_path), acquired2, release2))
    holder2.start()
    try:
        assert acquired2.wait(timeout=10)
        releaser = threading.Thread(
            target=_release_after_delay, args=(release2, 0.2),
        )
        releaser.start()
        with progress_module._progress_file_lock(ledger_path):
            pass
        releaser.join(timeout=5)
        holder2.join(timeout=10)
        assert holder2.exitcode == 70
    finally:
        if holder2.is_alive():
            holder2.terminate()
            holder2.join(timeout=5)
