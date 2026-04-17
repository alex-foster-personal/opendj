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
    # Wait for litestream to have been spawned.
    deadline = time.monotonic() + 2.0
    while not spawned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spawned, "litestream factory was never called"
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
    deadline = time.monotonic() + 2.0
    while s3.get_object("test-state", "LOCK.json") is None and time.monotonic() < deadline:
        time.sleep(0.01)
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
