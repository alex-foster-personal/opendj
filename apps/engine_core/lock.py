"""Engine singleton lock.

An exclusive ``flock`` on ``<data_dir>/.engine.lock`` whose contents are the
holder's identity JSON ``{pid, boot_id, started_at, heartbeat_at}``. The
flock is the authority -- the kernel drops it when the holder dies -- and the
JSON exists so a refusal can NAME the holder instead of saying "busy".
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# A holder whose heartbeat is younger than this is live. An older heartbeat
# under a still-held flock means the process is alive but wedged -- also a
# refusal, with a different message.
HEARTBEAT_TTL_S: float = 120.0


class EngineLockError(RuntimeError):
    """Another engine holds the lock. The message names pid/boot_id/age."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _age_s(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (datetime.now(UTC) - parsed).total_seconds()


@dataclass(frozen=True)
class LockHolder:
    """What the incumbent wrote about itself.

    Fields are Optional because a holder can die between ``open`` and its
    first write, leaving an empty file behind.
    """

    pid: int | None
    boot_id: str | None
    started_at: str | None
    heartbeat_at: str | None
    heartbeat_age_s: float | None

    @property
    def is_live(self) -> bool:
        return (
            self.heartbeat_age_s is not None
            and self.heartbeat_age_s < HEARTBEAT_TTL_S
        )

    def describe(self) -> str:
        age = (
            "unknown" if self.heartbeat_age_s is None
            else f"{self.heartbeat_age_s:.1f}s"
        )
        return f"pid={self.pid} boot_id={self.boot_id} heartbeat_age={age}"


class EngineLock:
    """Context manager around the exclusive lock file."""

    def __init__(self, path: Path, *, boot_id: str | None = None) -> None:
        self.path = Path(path)
        self.boot_id = boot_id or str(uuid.uuid4())
        self.started_at = _now()
        self._fd: int | None = None

    # ----- acquire / release --------------------------------------------
    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = _read_holder(fd)
            os.close(fd)
            raise EngineLockError(_refusal(self.path, holder)) from exc
        self._fd = fd
        self._write()

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def heartbeat(self) -> None:
        """Refresh ``heartbeat_at``. Driven on a timer by the app lifespan."""
        if self._fd is None:
            raise EngineLockError(f"heartbeat on an unheld lock {self.path}")
        self._write()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> EngineLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    # ----- internals -----------------------------------------------------
    def _write(self) -> None:
        fd = self._fd
        if fd is None:
            raise EngineLockError(f"write on an unheld lock {self.path}")
        blob = json.dumps(
            {
                "pid": os.getpid(),
                "boot_id": self.boot_id,
                "started_at": self.started_at,
                "heartbeat_at": _now(),
            },
            sort_keys=True,
        ).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, blob)
        os.fsync(fd)


def _read_holder(fd: int) -> LockHolder:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4096)
    data: dict[str, Any] = {}
    if raw:
        try:
            loaded = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    heartbeat_at = data.get("heartbeat_at")
    return LockHolder(
        pid=data.get("pid"),
        boot_id=data.get("boot_id"),
        started_at=data.get("started_at"),
        heartbeat_at=heartbeat_at,
        heartbeat_age_s=_age_s(heartbeat_at),
    )


def _refusal(path: Path, holder: LockHolder) -> str:
    if holder.is_live:
        return (
            f"engine already running on {path}: {holder.describe()}. "
            "Stop it before booting a second engine."
        )
    # The flock is still held, so the holder process is alive even though it
    # stopped heartbeating. Stealing the lock from a wedged-but-live engine
    # would put two writers on one jobs db, so this is still a refusal.
    return (
        f"engine lock {path} is held but stale: {holder.describe()} "
        f"(heartbeat older than {HEARTBEAT_TTL_S:.0f}s). The holder is alive "
        "and wedged; kill it before booting."
    )


__all__ = [
    "HEARTBEAT_TTL_S",
    "EngineLock",
    "EngineLockError",
    "LockHolder",
]
