"""Engine singleton lock.

An exclusive ``flock`` on ``<data_dir>/.engine.lock`` whose contents are the
holder's identity JSON ``{pid, role, port, boot_id, started_at, heartbeat_at}``. The
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

    def __init__(
        self,
        path: Path,
        *,
        boot_id: str | None = None,
        host: str | None = None,
        port: int | None = None,
        role: str = "opendj-engine",
    ) -> None:
        self.path = Path(path)
        self.boot_id = boot_id or str(uuid.uuid4())
        self.host = host
        self.port = port
        self.role = role
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
        swapped = _swap_reason(fd, self.path)
        if swapped is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise EngineLockError(swapped)
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
        data: dict[str, Any] = {
            "pid": os.getpid(),
            "role": self.role,
            "boot_id": self.boot_id,
            "started_at": self.started_at,
            "heartbeat_at": _now(),
        }
        if self.host is not None:
            data["host"] = self.host
        if self.port is not None:
            data["port"] = self.port
        blob = json.dumps(data, sort_keys=True).encode("utf-8")
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


def _swap_reason(fd: int, path: Path) -> str | None:
    """Did the path stop referring to the inode we just locked?

    flock is attached to an INODE, not to a name. ``rm .engine.lock`` followed
    by a fresh create leaves this process holding an exclusive lock on an
    inode with no name, while the next engine opens the new file, locks it
    uncontended, and boots -- two live engines on one jobs db, which is the
    exact outcome this lock exists to prevent. So the held fd and the path are
    compared after the flock succeeds, and a mismatch refuses the boot.
    """
    held = os.fstat(fd)
    try:
        current = os.stat(path)
    except FileNotFoundError:
        return (
            f"engine lock {path} was unlinked while it was being acquired, so "
            "the flock now guards a deleted inode and would not stop a second "
            "engine from booting. Refusing to start. Retry once nothing is "
            "deleting the lock file."
        )
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        return (
            f"engine lock {path} was replaced while it was being acquired "
            f"(inode {held.st_ino} -> {current.st_ino}), so the flock guards "
            "the old inode and would not stop a second engine from booting. "
            "Refusing to start. Retry once nothing is recreating the lock file."
        )
    return None


def _refusal(path: Path, holder: LockHolder) -> str:
    if holder.is_live:
        # Nothing is wrong here: the other engine is doing its job. Telling an
        # operator to stop a HEALTHY engine is advice that costs them their
        # running instance for no reason.
        return (
            f"engine lock {path} is held by another engine that is alive and "
            f"healthy: {holder.describe()}. This boot must not proceed -- two "
            "engines on one jobs db is the corruption this lock prevents. Use "
            "the engine that is already running."
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
