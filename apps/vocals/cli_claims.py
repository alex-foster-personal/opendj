"""Track-claim lease locking for vocal worker runs (split from
``apps.vocals.cli``).

-Claude
"""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.process_groups import WorkerCleanupError
from apps.vocals.cli_worker import WORKER_TERMINATE_GRACE_S

# ----- CFG ---------------------------------------------------------------------
LOCK_LEASE_S: float = 30
LOCK_HEARTBEAT_S: float = 5


@dataclass(frozen=True)
class TrackClaim:
    """One immutable lock owner with a renewable, process-independent lease."""

    path: Path
    owner_token: str
    lease_s: float
    stop_heartbeat: threading.Event
    heartbeat_thread: threading.Thread
    heartbeat_errors: list[BaseException]


@contextmanager
def _claim_record_guard(lock: Path) -> Iterator[None]:
    """Serialize short claim-record transitions across processes."""
    mutex_path = lock.with_name(f"{lock.name}.mutex")
    mutex_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(mutex_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _read_claim_record(lock: Path) -> dict[str, Any] | None:
    if not lock.exists():
        return None
    try:
        record = json.loads(lock.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"invalid vocals claim record: {lock}") from exc
    if not isinstance(record, dict):
        raise RuntimeError(  # noqa: TRY004
            f"invalid vocals claim record: {lock}"
        )
    if (
        not isinstance(record.get("owner_token"), str)
        or not record["owner_token"]
        or not isinstance(record.get("pid"), int)
        or isinstance(record["pid"], bool)
        or not isinstance(record.get("heartbeat_at"), (int, float))
        or isinstance(record["heartbeat_at"], bool)
        or not math.isfinite(record["heartbeat_at"])
        or not isinstance(record.get("lease_s"), (int, float))
        or isinstance(record["lease_s"], bool)
        or not math.isfinite(record["lease_s"])
        or record["lease_s"] <= 0
        or not isinstance(record.get("released"), bool)
        or not isinstance(record.get("manual_recovery_required", False), bool)
    ):
        raise RuntimeError(f"invalid vocals claim record: {lock}")
    return record


def _write_claim_record(lock: Path, record: dict[str, Any]) -> None:
    descriptor, tmp_name = tempfile.mkstemp(
        dir=str(lock.parent),
        prefix=f".{lock.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(record, file_handle, sort_keys=True)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_name, lock)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _claim_is_active(record: dict[str, Any], now: float) -> bool:
    return not record["released"] and (
        record.get("manual_recovery_required", False) or now - record["heartbeat_at"] <= record["lease_s"]
    )


def _heartbeat_claim(
    lock: Path,
    owner_token: str,
    lease_s: float,
    stop: threading.Event,
    errors: list[BaseException],
) -> None:
    interval_s = min(LOCK_HEARTBEAT_S, lease_s / 3)
    while not stop.wait(interval_s):
        try:
            with _claim_record_guard(lock):
                record = _read_claim_record(lock)
                if record is None or record["owner_token"] != owner_token:
                    return
                record["heartbeat_at"] = time.time()
                _write_claim_record(lock, record)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
            return


def _claim_track(
    cache_file: Path,
    lease_s: float = LOCK_LEASE_S,
) -> TrackClaim | None:
    """Claim one uncached track without waiting behind another CLI process.

    The persisted lease duration and heartbeat are independent of any
    contender's worker timeout. Every transition is serialized, and the
    immutable owner token prevents an expired owner from releasing its
    successor's lease.
    """
    if lease_s <= 0:
        raise ValueError(f"claim lease must be > 0 seconds, got {lease_s}")
    lock = cache_file.with_suffix(".json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    owner_token = uuid.uuid4().hex
    with _claim_record_guard(lock):
        record = _read_claim_record(lock)
        if record is not None and _claim_is_active(record, now):
            return None
        _write_claim_record(
            lock,
            {
                "owner_token": owner_token,
                "pid": os.getpid(),
                "heartbeat_at": now,
                "lease_s": lease_s,
                "released": False,
                "manual_recovery_required": False,
            },
        )
    stop = threading.Event()
    heartbeat_errors: list[BaseException] = []
    heartbeat_thread = threading.Thread(
        target=_heartbeat_claim,
        args=(lock, owner_token, lease_s, stop, heartbeat_errors),
        name=f"vocals-claim-{owner_token[:8]}",
        daemon=True,
    )
    heartbeat_thread.start()
    return TrackClaim(
        path=lock,
        owner_token=owner_token,
        lease_s=lease_s,
        stop_heartbeat=stop,
        heartbeat_thread=heartbeat_thread,
        heartbeat_errors=heartbeat_errors,
    )


@contextmanager
def _owned_track_claim(claim: TrackClaim) -> Iterator[None]:
    if claim.heartbeat_errors:
        raise RuntimeError("vocals claim heartbeat failed") from claim.heartbeat_errors[0]
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if record is None or record["owner_token"] != claim.owner_token or not _claim_is_active(record, time.time()):
            raise RuntimeError("vocals track claim expired or changed owner")
        yield


def _assert_track_claim(claim: TrackClaim) -> None:
    with _owned_track_claim(claim):
        return


def _release_track_claim(claim: TrackClaim) -> None:
    claim.stop_heartbeat.set()
    claim.heartbeat_thread.join(timeout=WORKER_TERMINATE_GRACE_S)
    if claim.heartbeat_thread.is_alive():
        raise RuntimeError("vocals claim heartbeat thread did not stop")
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if record is None or record["owner_token"] != claim.owner_token:
            return
        if record.get("manual_recovery_required", False):
            raise RuntimeError(f"vocals claim requires manual recovery and cannot be released: {claim.path}")
        record["released"] = True
        record["heartbeat_at"] = time.time()
        _write_claim_record(claim.path, record)


def _retain_track_claim_for_manual_recovery(
    claim: TrackClaim,
    cleanup_error: WorkerCleanupError,
) -> None:
    """Make an unverified-cleanup claim non-expiring, then stop its heartbeat."""
    with _claim_record_guard(claim.path):
        record = _read_claim_record(claim.path)
        if record is None or record["owner_token"] != claim.owner_token:
            raise RuntimeError("cannot retain unverified worker claim because ownership changed") from cleanup_error
        record["manual_recovery_required"] = True
        record["cleanup_error"] = str(cleanup_error)
        record["heartbeat_at"] = time.time()
        _write_claim_record(claim.path, record)
    print(
        f"[ERROR] worker cleanup unverified; claim retained at {claim.path}; "
        "verify the worker tree before manual recovery",
        file=sys.stderr,
    )
    claim.stop_heartbeat.set()
    claim.heartbeat_thread.join(timeout=WORKER_TERMINATE_GRACE_S)
    if claim.heartbeat_thread.is_alive():
        raise RuntimeError("vocals claim heartbeat thread did not stop for manual recovery") from cleanup_error


@contextmanager
def _managed_track_claim(claim: TrackClaim) -> Iterator[None]:
    release_claim = True
    try:
        yield
    except WorkerCleanupError as exc:
        release_claim = False
        _retain_track_claim_for_manual_recovery(claim, exc)
        raise
    finally:
        if release_claim:
            _release_track_claim(claim)
