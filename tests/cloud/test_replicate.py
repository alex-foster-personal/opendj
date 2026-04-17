"""Tests for apps.cloud.replicate (CAT-04).

Uses a :class:`FakeSubprocess` to stand in for the ``litestream`` binary.
No network I/O; everything runs against FakeS3Client.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.lock import FakeS3Client, Lock, LockHolder
from apps.cloud.replicate import Replicator, self_check


def _wait_until(predicate, timeout: float = 2.0, tick: float = 0.01) -> bool:
    """Block until ``predicate()`` is truthy or ``timeout`` elapses.

    Uses ``threading.Event().wait`` for the inter-poll tick so the test
    body contains no raw ``time.sleep`` wall-clock waits; the Event is
    never set, so ``.wait(tick)`` returns purely by timeout and serves as
    an interruptible sleep. Returns True iff the predicate became truthy
    before the deadline.
    """
    deadline = time.monotonic() + timeout
    gate = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        gate.wait(tick)
    return predicate()


def make_cfg(hostname: str = "host-a") -> CloudConfig:
    return CloudConfig(
        r2_account_id="acct",
        r2_access_key_id="id",
        r2_secret_access_key="secret",
        state_bucket="test-state",
        audio_bucket="test-audio",
        hostname=hostname,
        bind_host="127.0.0.1",
    )


class FakeSubprocess:
    """Stand-in for subprocess.Popen that we can signal to exit."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 999_999
        self.terminated = False
        self._mutex = threading.Lock()

    def poll(self) -> int | None:
        with self._mutex:
            return self.returncode

    def terminate(self) -> None:
        with self._mutex:
            self.terminated = True
            self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def finish(self, rc: int = 0) -> None:
        with self._mutex:
            self.returncode = rc


@pytest.mark.requirement("CAT-04")
def test_replicator_exits_2_when_lock_held_elsewhere(capsys):
    s3 = FakeS3Client()
    # Pre-seed the lock as held by another host.
    foreign = LockHolder(
        holder="host-other",
        acquired_at=datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 4, 17, 12, 10, 0, tzinfo=timezone.utc),
        pid=111,
        version=1,
    )
    body = foreign.to_json().encode()
    s3.put_object_if_none_match("test-state", "LOCK.json", body)

    spawned: list[list[str]] = []

    def factory(argv: list[str]) -> FakeSubprocess:  # pragma: no cover - not reached
        spawned.append(argv)
        return FakeSubprocess()

    rep = Replicator(
        make_cfg("host-a"),
        s3,
        subprocess_factory=factory,
        sleep_fn=lambda _s: None,
    )
    # Freeze the lock's clock so the foreign lock is not expired (acquired
    # at 12:00, expires 12:10; we ask at 12:05).
    rep.lock._now = lambda: datetime(  # type: ignore[assignment]
        2026, 4, 17, 12, 5, 0, tzinfo=timezone.utc
    )
    rc = rep.start()
    assert rc == 2
    assert not spawned  # litestream was NOT spawned
    captured = capsys.readouterr()
    assert "host-other" in captured.err


@pytest.mark.requirement("CAT-04")
def test_replicator_happy_path_spawns_litestream_and_releases_lock():
    s3 = FakeS3Client()
    spawned: list[list[str]] = []
    fake_proc = FakeSubprocess()

    def factory(argv: list[str]) -> FakeSubprocess:
        spawned.append(argv)
        return fake_proc

    rep = Replicator(
        make_cfg("host-a"),
        s3,
        subprocess_factory=factory,
        heartbeat_seconds=600,  # avoid heartbeat during test
        sleep_fn=lambda _s: None,
    )

    result_holder: dict[str, int] = {}

    def run() -> None:
        result_holder["rc"] = rep.start()

    t = threading.Thread(target=run)
    t.start()
    # Wait for litestream to have been spawned (Event-based tick; no sleep).
    assert _wait_until(lambda: bool(spawned)), (
        "litestream factory was never called"
    )
    assert spawned[0][0] == "litestream"
    assert spawned[0][1:3] == ["replicate", "-config"]

    # Finish the subprocess cleanly.
    fake_proc.finish(0)
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert result_holder["rc"] == 0
    # Lock must have been released (no LOCK.json left on R2).
    assert s3.get_object("test-state", "LOCK.json") is None


@pytest.mark.requirement("CAT-04")
def test_replicator_exits_3_when_lock_stolen_mid_run():
    s3 = FakeS3Client()
    fake_proc = FakeSubprocess()

    def factory(_argv: list[str]) -> FakeSubprocess:
        return fake_proc

    rep = Replicator(
        make_cfg("host-a"),
        s3,
        subprocess_factory=factory,
        heartbeat_seconds=0,  # force heartbeat on first loop iteration
        sleep_fn=lambda _s: None,
    )

    rc_holder: dict[str, int] = {}

    def run() -> None:
        rc_holder["rc"] = rep.start()

    t = threading.Thread(target=run)
    t.start()
    # Wait until the lock object exists (ack we acquired).
    assert _wait_until(
        lambda: s3.get_object("test-state", "LOCK.json") is not None
    ), "replicator never acquired LOCK.json"
    # Someone steals the lock: overwrite the stored etag so CAS fails.
    s3._store[("test-state", "LOCK.json")] = (  # type: ignore[attr-defined]
        b"{}", '"stolen-etag"',
    )
    t.join(timeout=3.0)
    assert not t.is_alive()
    assert rc_holder["rc"] == 3
    assert fake_proc.terminated


@pytest.mark.requirement("CAT-04")
def test_self_check_returns_zero(capsys):
    rc = self_check()
    assert rc == 0
    out = capsys.readouterr().out
    assert "self-check OK" in out


@pytest.mark.requirement("CAT-05")
def test_cloud_lock_released_only_after_litestream_exit():
    """P11-F02: lock must not be released while litestream may still flush.

    We instrument a FakeSubprocess that records when .wait/.poll report
    exit, and a Lock whose release() records when it is called. The release
    timestamp must be at-or-after the subprocess's final poll-exit time.
    Previously release() ran in ``finally`` before we waited for the
    subprocess, creating a window for a concurrent writer to grab the lock
    while litestream was still flushing WAL frames.
    """
    s3 = FakeS3Client()

    release_times: list[float] = []
    proc_exit_time: dict[str, float] = {}

    class SlowExitProc(FakeSubprocess):
        """Keeps returning 'running' for a short delay after terminate()."""

        def __init__(self) -> None:
            super().__init__()
            self._terminate_at: float | None = None
            self._exit_delay = 0.15  # seconds after terminate before "exited"

        def terminate(self) -> None:
            with self._mutex:
                self.terminated = True
                self._terminate_at = time.monotonic()
            # NOTE: do NOT set returncode yet — simulate a flushing tail.

        def poll(self) -> int | None:
            with self._mutex:
                if self.returncode is not None:
                    return self.returncode
                if (
                    self._terminate_at is not None
                    and time.monotonic() - self._terminate_at >= self._exit_delay
                ):
                    self.returncode = -15
                    proc_exit_time["t"] = time.monotonic()
                    return self.returncode
                return None

        def wait(self, timeout: float | None = None) -> int:
            # Mimic real Popen.wait: block until poll() returns non-None.
            # Use a never-set Event as an interruptible tick instead of
            # time.sleep so tests don't rely on wall-clock sleeps.
            deadline = time.monotonic() + (timeout if timeout is not None else 5.0)
            gate = threading.Event()
            while self.poll() is None and time.monotonic() < deadline:
                gate.wait(0.01)
            return self.returncode or 0

    fake_proc = SlowExitProc()

    def factory(_argv: list[str]) -> SlowExitProc:
        return fake_proc

    rep = Replicator(
        make_cfg("host-a"),
        s3,
        subprocess_factory=factory,
        heartbeat_seconds=600,
        sleep_fn=time.sleep,  # real sleep so the exit-wait actually advances
    )

    # Wrap lock.release to record timestamp.
    orig_release = rep.lock.release

    def recording_release() -> None:
        release_times.append(time.monotonic())
        orig_release()

    rep.lock.release = recording_release  # type: ignore[method-assign]

    rc_holder: dict[str, int] = {}

    def run() -> None:
        rc_holder["rc"] = rep.start()

    t = threading.Thread(target=run)
    t.start()

    # Wait until lock is acquired.
    assert _wait_until(
        lambda: s3.get_object("test-state", "LOCK.json") is not None
    ), "replicator never acquired LOCK.json"

    # Ask replicator to stop — this triggers terminate + the delayed exit.
    rep.request_stop()

    t.join(timeout=5.0)
    assert not t.is_alive()

    assert release_times, "lock.release was never called"
    assert "t" in proc_exit_time, "subprocess never reported exit"
    # The critical assertion: release happened at or after subprocess exit.
    assert release_times[0] >= proc_exit_time["t"], (
        "cloud lock was released BEFORE litestream reported exit "
        f"(release={release_times[0]:.4f}, exit={proc_exit_time['t']:.4f})"
    )
    # And the lock is indeed gone from R2 at the end.
    assert s3.get_object("test-state", "LOCK.json") is None
