"""Engine singleton lock + boot preflight refusals.

If lock contention silently succeeded, two engines would share one jobs db
and each would treat the other's rows as orphans to reap. Every assertion
here is about the refusal being loud and NAMING the incumbent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from apps.engine_core.lock import EngineLock, EngineLockError


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
