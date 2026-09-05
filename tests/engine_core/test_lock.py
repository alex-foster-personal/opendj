"""Engine singleton lock + boot preflight refusals.

If lock contention silently succeeded, two engines would share one jobs db
and each would treat the other's rows as orphans to reap. Every assertion
here is about the refusal being loud and NAMING the incumbent.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.engine_core import lock as lock_module
from apps.engine_core.config import (
    CONCURRENCY_ENV,
    PROGRESS_LEDGER_NAME,
    EngineBootError,
    EngineConfig,
    apply_env_contract,
    assert_no_progress_ledger,
    assert_single_worker,
    build_config,
)
from apps.engine_core.lock import (
    HEARTBEAT_TTL_S,
    EngineLock,
    EngineLockError,
    _swap_reason,
)


def test_second_boot_refuses_and_names_the_holder(tmp_path: Path) -> None:
    """If a second engine can take the lock then the singleton is a lie."""
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, boot_id="boot-a") as holder:
        with pytest.raises(EngineLockError) as refusal:
            EngineLock(lock_path, boot_id="boot-b").acquire()
        message = str(refusal.value)
        assert "boot-a" in message, message
        assert str(holder.path) in message, message
        assert "heartbeat_age=" in message, message
        assert "boot-b" not in message, message


def test_a_healthy_holder_is_not_something_the_operator_is_told_to_kill(
    tmp_path: Path,
) -> None:
    """The refusal must not prescribe killing a perfectly good engine.

    The old wording ("Stop it before booting a second engine") read as an
    instruction, and the person acting on it would take down the healthy
    instance that is currently serving. A live holder means nothing is wrong;
    the correct action is to use it.
    """
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, boot_id="boot-a"), pytest.raises(EngineLockError) as refusal:
        EngineLock(lock_path, boot_id="boot-b").acquire()
    message = str(refusal.value)
    assert "alive and healthy" in message, message
    assert "already running" in message, message
    for kill_word in ("Stop it", "kill it", "Kill it"):
        assert kill_word not in message, (
            f"the healthy-holder refusal tells the operator to {kill_word!r}: "
            f"{message}"
        )


def test_the_wedged_holder_refusal_still_says_to_kill_it(
    tmp_path: Path,
) -> None:
    """A holder that stopped heartbeating IS the case that needs a kill."""
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, boot_id="boot-a"):
        # Age the recorded heartbeat past the TTL without touching the flock,
        # which is what a wedged-but-live engine looks like from outside.
        stale = json.loads(lock_path.read_text(encoding="utf-8"))
        stale["heartbeat_at"] = (
            datetime.now(UTC) - timedelta(seconds=HEARTBEAT_TTL_S * 2)
        ).isoformat(timespec="milliseconds")
        _overwrite_without_unlinking(lock_path, json.dumps(stale))

        with pytest.raises(EngineLockError) as refusal:
            EngineLock(lock_path, boot_id="boot-b").acquire()
    message = str(refusal.value)
    assert "stale" in message, message
    assert "wedged" in message, message
    assert "kill it before booting" in message, message


def test_acquire_refuses_a_lock_file_swapped_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock is inode-based, so a lock on a replaced file guards nothing.

    The hazard is a swap landing between the open and the flock: the acquirer
    ends up holding an exclusive lock on an inode that no longer has a name,
    believes it is the singleton, and boots alongside whoever locks the new
    file. The swap is driven from inside flock() so the race is exact rather
    than hoped for.
    """
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text("{}", encoding="utf-8")
    real_flock = fcntl.flock

    def _swap_then_lock(fd: int, operation: int) -> None:
        real_flock(fd, operation)
        if operation & fcntl.LOCK_EX:
            lock_path.unlink()
            lock_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(lock_module.fcntl, "flock", _swap_then_lock)

    lock = EngineLock(lock_path, boot_id="boot-b")
    with pytest.raises(EngineLockError) as refusal:
        lock.acquire()
    message = str(refusal.value)
    assert "replaced" in message, message
    assert "Refusing to start" in message, message
    assert not lock.held, "a refused acquire must not leave the lock held"


def test_swap_detection_names_both_shapes(tmp_path: Path) -> None:
    """The guard itself: replaced inode and vanished name are both refusals."""
    lock_path = tmp_path / ".engine.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert _swap_reason(fd, lock_path) is None, "the intact case must pass"

        lock_path.unlink()
        lock_path.write_text("{}", encoding="utf-8")
        replaced = _swap_reason(fd, lock_path)

        lock_path.unlink()
        vanished = _swap_reason(fd, lock_path)
    finally:
        os.close(fd)

    assert replaced is not None and "replaced" in replaced, replaced
    assert vanished is not None and "unlinked" in vanished, vanished


def _overwrite_without_unlinking(path: Path, blob: str) -> None:
    """Rewrite in place: unlinking would trip the inode check under test."""
    fd = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, blob.encode("utf-8"))
    finally:
        os.close(fd)


def test_lock_is_reusable_after_a_clean_release(tmp_path: Path) -> None:
    lock_path = tmp_path / ".engine.lock"
    first = EngineLock(lock_path, boot_id="boot-a")
    first.acquire()
    first.release()
    second = EngineLock(lock_path, boot_id="boot-b")
    second.acquire()
    try:
        recorded = json.loads(lock_path.read_text(encoding="utf-8"))
        assert recorded["boot_id"] == "boot-b"
    finally:
        second.release()


def test_lock_records_the_loopback_endpoint_for_live_tier_discovery(
    tmp_path: Path,
) -> None:
    """A live probe must find the exact engine, never guess a port."""
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(
        lock_path,
        boot_id="boot-a",
        host="127.0.0.1",
        port=8585,
    ):
        recorded = json.loads(lock_path.read_text(encoding="utf-8"))

    assert recorded["host"] == "127.0.0.1"
    assert recorded["port"] == 8585
    assert recorded["role"] == "opendj-engine"


def test_heartbeat_refreshes_the_holder_record(tmp_path: Path) -> None:
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, boot_id="boot-a") as lock:
        first = json.loads(lock_path.read_text(encoding="utf-8"))
        lock.heartbeat()
        second = json.loads(lock_path.read_text(encoding="utf-8"))
    assert second["heartbeat_at"] >= first["heartbeat_at"]
    assert second["started_at"] == first["started_at"]


def test_heartbeat_on_an_unheld_lock_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EngineLockError):
        EngineLock(tmp_path / ".engine.lock").heartbeat()


def test_multiple_workers_are_refused() -> None:
    with pytest.raises(EngineBootError) as refusal:
        assert_single_worker(2, environ={})
    assert "single-worker" in str(refusal.value)


def test_web_concurrency_is_refused() -> None:
    with pytest.raises(EngineBootError) as refusal:
        assert_single_worker(1, environ={CONCURRENCY_ENV: "4"})
    assert CONCURRENCY_ENV in str(refusal.value)
    assert_single_worker(1, environ={CONCURRENCY_ENV: ""})


def test_progress_ledger_in_the_data_dir_blocks_boot(tmp_path: Path) -> None:
    (tmp_path / PROGRESS_LEDGER_NAME).write_text("nodes: []\n", encoding="utf-8")
    with pytest.raises(EngineBootError) as refusal:
        assert_no_progress_ledger(tmp_path)
    assert PROGRESS_LEDGER_NAME in str(refusal.value)


def test_relative_data_dir_is_refused() -> None:
    with pytest.raises(EngineBootError):
        build_config("data", "127.0.0.1", 8585)


def test_env_contract_sets_data_dir_and_defaults_the_backend(
    tmp_path: Path,
) -> None:
    cfg = EngineConfig(data_dir=tmp_path)
    environ: dict[str, str] = {}
    apply_env_contract(cfg, environ=environ)
    assert environ["MDT_DATA_DIR"] == str(tmp_path)
    assert environ["MUSIC_DJ_STATE_BACKEND"] == "sqlite"

    chosen = {"MUSIC_DJ_STATE_BACKEND": "memory"}
    apply_env_contract(cfg, environ=chosen)
    assert chosen["MUSIC_DJ_STATE_BACKEND"] == "memory"
