"""Engine singleton lock + boot preflight refusals.

If lock contention silently succeeded, two engines would share one jobs db
and each would treat the other's rows as orphans to reap. Every assertion
here is about the refusal being loud and NAMING the incumbent.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.engine_core import lock as lock_module
from apps.engine_core.config import (
    CONCURRENCY_ENV,
    EngineBootError,
    EngineConfig,
    apply_env_contract,
    assert_single_worker,
    build_config,
)
from apps.engine_core.lock import (
    HEARTBEAT_TTL_S,
    EngineLock,
    EngineLockError,
    _swap_reason,
    inspect_lock,
    read_holder,
)

UNLINKS_AN_OPEN_FILE = pytest.mark.skipif(
    sys.platform == "win32",
    reason="unlinks the lock file while it is open; Windows refuses to delete an open file",
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
            f"the healthy-holder refusal tells the operator to {kill_word!r}: {message}"
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


@UNLINKS_AN_OPEN_FILE
def test_acquire_refuses_a_lock_file_swapped_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock is inode-based, so a lock on a replaced file guards nothing.

    The hazard is a swap landing between the open and the flock: the acquirer
    ends up holding an exclusive lock on an inode that no longer has a name,
    believes it is the singleton, and boots alongside whoever locks the new
    file. The swap is driven from inside the lock call so the race is exact
    rather than hoped for.
    """
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text("{}", encoding="utf-8")
    real_try_lock = lock_module.file_lock.try_lock

    def _lock_then_swap(fd: int) -> bool:
        taken = real_try_lock(fd)
        lock_path.unlink()
        lock_path.write_text("{}", encoding="utf-8")
        return taken

    monkeypatch.setattr(lock_module.file_lock, "try_lock", _lock_then_swap)

    lock = EngineLock(lock_path, boot_id="boot-b")
    with pytest.raises(EngineLockError) as refusal:
        lock.acquire()
    message = str(refusal.value)
    assert "replaced" in message, message
    assert "Refusing to start" in message, message
    assert not lock.held, "a refused acquire must not leave the lock held"


@UNLINKS_AN_OPEN_FILE
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


def _read_raw_until(lock_path: Path, stop: threading.Event, seen: list[str]) -> None:
    # A real concurrent reader, as the CLI and the shells read the lock: by
    # path, without the flock. It keeps every raw read for the assertions.
    while not stop.is_set():
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError:
            continue
        try:
            raw = os.read(fd, 4096)
            if raw:
                seen.append(raw.decode("utf-8"))
        finally:
            os.close(fd)


def _unparsable(reads: list[str]) -> list[str]:
    bad = []
    for raw in reads:
        try:
            json.loads(raw)
        except ValueError:
            bad.append(f"{raw[:40]!r}...{raw[-40:]!r}")
    return bad


@pytest.mark.requirement("ENGLOCK-6443")
def test_a_reader_mid_heartbeat_always_sees_a_whole_record(tmp_path: Path) -> None:
    """
    [if] a reader reads the lock during heartbeats [then] each read parses whole, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    seen: list[str] = []
    stop = threading.Event()
    with EngineLock(lock_path, boot_id="boot-a", port=8585) as lock:
        reader = threading.Thread(target=_read_raw_until, args=(lock_path, stop, seen))
        reader.start()
        # Heartbeat until the reader has sampled enough, not a fixed count: on a
        # loaded runner a fixed 2000 heartbeats once left the reader only 45 reads.
        deadline = time.monotonic() + 60
        beats = 0
        try:
            while (beats < 300 or len(seen) < 200) and time.monotonic() < deadline:
                lock.heartbeat()
                beats += 1
        finally:
            stop.set()
            reader.join()

    assert len(seen) > 100, f"the reader ran too few times to measure anything: {len(seen)}"
    assert _unparsable(seen) == [], "a reader saw an empty or torn lock"
    for raw in seen:
        holder = json.loads(raw)
        assert holder["boot_id"] == "boot-a" and holder["port"] == 8585, holder
        datetime.fromisoformat(holder["heartbeat_at"])


def _place_leftover(lock_path: Path) -> None:
    # An older writer's longer record, swapped in by rename so the reader never
    # catches the fixture itself half-written.
    staging = lock_path.with_suffix(".staging")
    staging.write_text(
        json.dumps(
            {
                "pid": 1,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": 8585,
                "boot_id": "boot-old",
                "pad": "x" * 3000,
            }
        ),
        encoding="utf-8",
    )
    os.replace(staging, lock_path)


@pytest.mark.requirement("ENGLOCK-6443")
def test_read_holder_retries_an_unparsable_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    [if] read_holder sees spaces once [then] it retries and parses, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    valid = json.dumps({"pid": 1, "port": 8585, "boot_id": "boot-a"}).encode("utf-8")
    reads = iter([b"   ", valid])

    def _read(fd: int, count: int) -> bytes:
        chunk = next(reads, valid)
        return chunk[:count] if len(chunk) > count else chunk

    lock_path.write_bytes(valid)
    monkeypatch.setattr(os, "read", _read)
    monkeypatch.setattr(lock_module.time, "sleep", lambda _s: None)
    holder = read_holder(lock_path)
    assert holder is not None
    assert holder.port == 8585


@pytest.mark.requirement("ENGLOCK-6443")
def test_read_holder_accepts_unpadded_longer_record(tmp_path: Path) -> None:
    """
    [if] the lock is valid but st_size != RECORD_BYTES [then] read_holder parses, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    blob = json.dumps({"pid": 1, "port": 8585, "boot_id": "boot-a", "pad": "x" * 200})
    lock_path.write_text(blob, encoding="utf-8")
    assert lock_path.stat().st_size != lock_module.RECORD_BYTES
    holder = read_holder(lock_path)
    assert holder is not None
    assert holder.port == 8585


@pytest.mark.requirement("ENGLOCK-6443")
def test_inspect_lock_retries_before_naming_no_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    [if] inspect_lock reads spaces then the real holder [then] it names boot-a not pid=None, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, boot_id="boot-a", host="127.0.0.1", port=8585):
        good_record = lock_path.read_text(encoding="utf-8")
        sleep_calls = 0

        def _sleep(_s: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 1:
                _overwrite_without_unlinking(lock_path, good_record)

        monkeypatch.setattr(lock_module.time, "sleep", _sleep)
        _overwrite_without_unlinking(lock_path, " " * lock_module.RECORD_BYTES)
        inspected = inspect_lock(lock_path)
        assert inspected != "free"
        assert inspected.boot_id == "boot-a"
        assert inspected.pid == os.getpid()

        with pytest.raises(EngineLockError) as refusal:
            EngineLock(lock_path, boot_id="boot-b").acquire()
        message = str(refusal.value)
        assert "boot-a" in message
        assert "pid=None" not in message


@pytest.mark.requirement("ENGLOCK-6443")
@UNLINKS_AN_OPEN_FILE
def test_the_record_is_fixed_size_and_trims_a_longer_leftover(tmp_path: Path) -> None:
    """
    [if] a longer leftover lock is taken over [then] reads parse and it ends RECORD_BYTES, [else stop].
    """
    from apps.shared.engine_origin import EngineNotRunning, resolve_origin

    lock_path = tmp_path / ".engine.lock"
    invalid_json_reads: list[str] = []
    origins: list[str] = []
    stop = threading.Event()

    def _origin_reader() -> None:
        while not stop.is_set():
            try:
                origin = resolve_origin(lock_path)
            except EngineNotRunning as error:
                if "is not valid JSON" in str(error):
                    invalid_json_reads.append(str(error))
                continue
            except FileNotFoundError:
                continue
            origins.append(origin.boot_id or "")
            assert origin.port == 8585
            assert origin.boot_id in {"boot-old", "boot-a"}

    reader = threading.Thread(target=_origin_reader)
    reader.start()
    takeovers = 0
    deadline = time.monotonic() + 20
    recorded: dict[str, object] = {}
    try:
        while time.monotonic() < deadline and (
            takeovers < 30 or len(origins) < 100
        ):
            _place_leftover(lock_path)
            with EngineLock(
                lock_path, boot_id="boot-a", host="127.0.0.1", port=8585
            ) as lock:
                assert lock_path.stat().st_size == lock_module.RECORD_BYTES
                lock.heartbeat()
                assert lock_path.stat().st_size == lock_module.RECORD_BYTES
                recorded = json.loads(lock_path.read_text(encoding="utf-8"))
            takeovers += 1
    finally:
        stop.set()
        reader.join()

    assert invalid_json_reads == [], invalid_json_reads
    assert recorded["boot_id"] == "boot-a"
    assert "pad" not in recorded
    assert lock_path.stat().st_size == lock_module.RECORD_BYTES
    assert takeovers >= 30 or len(origins) >= 100


@pytest.mark.requirement("ENGLOCK-6443")
def test_an_oversized_record_refuses_and_releases_the_lock(tmp_path: Path) -> None:
    """
    [if] the record exceeds RECORD_BYTES [then] acquire refuses, lock reads free, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    with pytest.raises(EngineLockError) as refusal:
        EngineLock(lock_path, boot_id="boot-a", host="h" * 2000).acquire()
    assert "fixed" in str(refusal.value)
    assert inspect_lock(lock_path) == "free"


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


def test_relative_data_dir_is_refused() -> None:
    with pytest.raises(EngineBootError):
        build_config("data", "127.0.0.1", 8585)


@pytest.mark.requirement("INSTALL-14")
def test_lock_json_records_parent_pid_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    [if] OPENDJ_PARENT_PID is set [then] the lock json records parent_pid and port, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    monkeypatch.setenv("OPENDJ_PARENT_PID", "4242")
    with EngineLock(lock_path, host="127.0.0.1", port=9001, boot_id="boot-a"):
        recorded = json.loads(lock_path.read_text(encoding="utf-8"))
    assert recorded["parent_pid"] == 4242
    assert recorded["host"] == "127.0.0.1"
    assert recorded["port"] == 9001


@pytest.mark.requirement("INSTALL-14")
def test_read_holder_does_not_take_the_exclusive_lock(tmp_path: Path) -> None:
    """
    [if] read_holder peeks a held lock [then] it never blocks or steals the lock, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, host="127.0.0.1", port=8585, boot_id="boot-a"):
        peeked = read_holder(lock_path)
        assert peeked is not None
        assert peeked.pid == os.getpid()
        assert peeked.port == 8585
        nested = read_holder(lock_path)
        assert nested is not None
        assert nested.pid == peeked.pid


@pytest.mark.requirement("INSTALL-14")
def test_inspect_lock_reports_free_after_release(tmp_path: Path) -> None:
    """
    [if] a lock is acquired then released [then] inspect_lock reports free again after, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    assert inspect_lock(lock_path) == "free"
    lock = EngineLock(lock_path, boot_id="boot-a")
    lock.acquire()
    assert inspect_lock(lock_path) != "free"
    lock.release()
    assert inspect_lock(lock_path) == "free"


@pytest.mark.requirement("INSTALL-14")
def test_inspect_lock_reports_the_holder_while_held(tmp_path: Path) -> None:
    """
    [if] a lock is held [then] inspect_lock reports the holder's pid and port, [else stop].
    """
    lock_path = tmp_path / ".engine.lock"
    with EngineLock(lock_path, host="127.0.0.1", port=7777, boot_id="boot-a"):
        inspected = inspect_lock(lock_path)
        assert inspected != "free"
        assert inspected.pid == os.getpid()
        assert inspected.port == 7777


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
