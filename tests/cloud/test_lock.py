"""Tests for apps.cloud.lock (CAT-04).

Uses :class:`apps.cloud.lock.FakeS3Client` (no network) and a deterministic
clock so TTL expiry is testable without sleeping.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from apps.cloud.config import CloudConfig
from apps.cloud.lock import (
    LOCK_KEY,
    FakeS3Client,
    Lock,
    LockHolder,
    LockLostError,
)


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


class Clock:
    """Deterministic clock fixture for TTL tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 4, 17, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.mark.requirement("CAT-04")
def test_acquire_on_empty_bucket_succeeds():
    s3 = FakeS3Client()
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    result = lock.try_acquire()
    assert result.success is True
    assert result.holder is not None
    assert result.holder.holder == "host-a"
    # The object must exist on R2 now.
    assert s3.get_object("test-state", LOCK_KEY) is not None


@pytest.mark.requirement("CAT-04")
def test_acquire_fails_when_other_holder_active():
    s3 = FakeS3Client()
    clock = Clock()
    lock_a = Lock(make_cfg("host-a"), s3, now_fn=clock)
    assert lock_a.try_acquire().success is True
    # A second host tries to acquire.
    lock_b = Lock(make_cfg("host-b"), s3, now_fn=clock)
    result = lock_b.try_acquire()
    assert result.success is False
    assert result.holder is not None
    assert result.holder.holder == "host-a"


@pytest.mark.requirement("CAT-04")
def test_acquire_over_expired_lock_succeeds():
    s3 = FakeS3Client()
    clock = Clock()
    lock_a = Lock(make_cfg("host-a"), s3, now_fn=clock, ttl_seconds=60)
    assert lock_a.try_acquire().success is True
    # Jump 61 seconds later; host-a's lock is now expired.
    clock.advance(61)
    lock_b = Lock(make_cfg("host-b"), s3, now_fn=clock, ttl_seconds=60)
    result = lock_b.try_acquire()
    assert result.success is True
    assert result.holder is not None
    assert result.holder.holder == "host-b"


@pytest.mark.requirement("CAT-04")
def test_heartbeat_refreshes_expires_at():
    s3 = FakeS3Client()
    clock = Clock()
    lock = Lock(make_cfg("host-a"), s3, now_fn=clock, ttl_seconds=120)
    assert lock.try_acquire().success is True
    held = lock.current_holder()
    assert held is not None
    original_expiry = held.expires_at
    clock.advance(30)
    lock.heartbeat()
    refreshed = lock.current_holder()
    assert refreshed is not None
    assert refreshed.expires_at > original_expiry


@pytest.mark.requirement("CAT-04")
def test_heartbeat_with_stale_etag_raises_lock_lost():
    s3 = FakeS3Client()
    clock = Clock()
    lock = Lock(make_cfg("host-a"), s3, now_fn=clock, ttl_seconds=120)
    assert lock.try_acquire().success is True
    # Simulate someone else stomping on the object (bypass CAS).
    evil = LockHolder(
        holder="host-evil",
        acquired_at=clock(),
        expires_at=clock() + timedelta(seconds=120),
        pid=999,
        version=1,
    )
    s3._store[("test-state", LOCK_KEY)] = (  # type: ignore[attr-defined]
        evil.to_json().encode(),
        '"evil-etag"',
    )
    with pytest.raises(LockLostError):
        lock.heartbeat()


@pytest.mark.requirement("CAT-04")
def test_release_deletes_the_object():
    s3 = FakeS3Client()
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    assert lock.try_acquire().success is True
    assert s3.get_object("test-state", LOCK_KEY) is not None
    assert lock.release() is True
    assert s3.get_object("test-state", LOCK_KEY) is None
    # Second release is a no-op.
    assert lock.release() is False


@pytest.mark.requirement("CAT-04")
def test_race_only_one_acquire_succeeds():
    """Two threads call try_acquire concurrently; exactly one wins."""
    s3 = FakeS3Client()
    clock = Clock()
    winners: list[bool] = []
    errors: list[Exception] = []

    def worker(host: str) -> None:
        try:
            lock = Lock(make_cfg(host), s3, now_fn=clock)
            res = lock.try_acquire()
            winners.append(res.success)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"host-{i}",)) for i in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert winners.count(True) == 1
    assert winners.count(False) == 4


@pytest.mark.requirement("CAT-04")
def test_corrupt_lock_object_is_overwritten():
    s3 = FakeS3Client()
    s3._store[("test-state", LOCK_KEY)] = (  # type: ignore[attr-defined]
        b"{not valid json",
        '"garbage-etag"',
    )
    # Prime the etag so CAS overwrite works against our fake etag.
    body, _etag = s3.get_object("test-state", LOCK_KEY)  # type: ignore[misc]
    # Replace with actual etag that FakeS3 computes.
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    # Because FakeS3 indexes by body hash, the stored etag is wrong. Manually
    # fix so the test reflects real S3 behaviour.
    import hashlib
    real_etag = '"' + hashlib.sha1(body).hexdigest() + '"'
    s3._store[("test-state", LOCK_KEY)] = (body, real_etag)  # type: ignore[attr-defined]
    result = lock.try_acquire()
    assert result.success is True


@pytest.mark.requirement("CAT-04")
def test_current_holder_returns_none_when_absent():
    s3 = FakeS3Client()
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    assert lock.current_holder() is None


@pytest.mark.requirement("CAT-04")
def test_current_holder_returns_none_on_corrupt_body():
    s3 = FakeS3Client()
    s3._store[("test-state", LOCK_KEY)] = (  # type: ignore[attr-defined]
        b"garbage",
        '"etag"',
    )
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    assert lock.current_holder() is None


@pytest.mark.requirement("CAT-04")
def test_lock_holder_json_round_trip():
    h = LockHolder(
        holder="mbp",
        acquired_at=datetime(2026, 4, 17, 14, 2, 3, tzinfo=UTC),
        expires_at=datetime(2026, 4, 17, 14, 12, 3, tzinfo=UTC),
        pid=12345,
        version=1,
    )
    rebuilt = LockHolder.from_json(h.to_json())
    assert rebuilt == h


@pytest.mark.requirement("CAT-04")
def test_install_signal_handlers_does_not_throw():
    """Smoke test; signal wiring must not crash even when not on main thread."""
    s3 = FakeS3Client()
    lock = Lock(make_cfg("host-a"), s3, now_fn=Clock())
    assert lock.try_acquire().success is True
    lock.install_signal_handlers()
    # Force the safe release path.
    lock._safe_release()
