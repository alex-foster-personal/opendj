"""The CloudSync scheduler's liveness evidence: ``<data-dir>/cloudsync-heartbeat.json``.

A FILE rather than in-process state so every reader sees the same evidence:
the HTTP status route inside the engine, and ``python -m apps.sync_hub status``
in a separate process. The scheduler rewrites it every
``CFG.BEAT_INTERVAL_S`` while it is configured and alive, and deletes it on a
clean stop. A crashed process leaves the file behind; it goes stale after
``CFG.STALE_AFTER_S`` and then reads as NOT running, which is the point:
status.enabled means "a loop is beating", never "someone set an env var".
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from apps.shared.state import sync_stamp
from apps.sync_hub.atomic_json import write_json_atomic

HEARTBEAT_FILENAME: str = "cloudsync-heartbeat.json"


@dataclass(frozen=True)
class _Cfg:
    #: How often a live scheduler rewrites the file, in seconds.
    BEAT_INTERVAL_S: float = 15.0
    #: Age past which a beat no longer proves a live loop: three missed beats.
    STALE_AFTER_S: float = 45.0


CFG = _Cfg()


class CloudSyncHeartbeatError(RuntimeError):
    """The heartbeat file exists but cannot be read or parsed."""


@dataclass(frozen=True)
class Heartbeat:
    beat_at: str
    pid: int
    hub_url: str

    def age_s(self, now: datetime) -> float:
        return (now - sync_stamp.parse_canonical(self.beat_at)).total_seconds()

    def is_fresh(self, now: datetime) -> bool:
        """True iff the beat is no older than the stale bound and not from the future.

        A beat more than one stale bound in the future is a clock fault, not
        evidence of life, so it does not count either.
        """
        age = self.age_s(now)
        return -CFG.STALE_AFTER_S <= age <= CFG.STALE_AFTER_S

    def to_wire(self) -> dict[str, Any]:
        return {"beat_at": self.beat_at, "pid": self.pid, "hub_url": self.hub_url}


def heartbeat_path(data_dir: Path) -> Path:
    return Path(data_dir) / HEARTBEAT_FILENAME


def beat(data_dir: Path, *, hub_url: str, now: datetime | None = None) -> Heartbeat:
    """Record one beat for this process, atomically."""
    stamp = sync_stamp.canonical_now() if now is None else sync_stamp.canonical_from(now)
    current = Heartbeat(beat_at=stamp, pid=os.getpid(), hub_url=hub_url)
    write_json_atomic(heartbeat_path(data_dir), current.to_wire())
    return current


def read(data_dir: Path) -> Heartbeat | None:
    """The last beat, or ``None`` when no scheduler has one on file."""
    path = heartbeat_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudSyncHeartbeatError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"beat_at", "pid", "hub_url"}:
        raise CloudSyncHeartbeatError(f"{path} must hold exactly beat_at, pid, hub_url")
    if not isinstance(payload["pid"], int) or not isinstance(payload["hub_url"], str):
        raise CloudSyncHeartbeatError(f"{path} has a non-integer pid or non-text hub_url")
    try:
        sync_stamp.parse_canonical(str(payload["beat_at"]))
    except sync_stamp.SyncStampError as exc:
        raise CloudSyncHeartbeatError(f"{path} beat_at is not a timestamp: {exc}") from exc
    return Heartbeat(beat_at=payload["beat_at"], pid=payload["pid"], hub_url=payload["hub_url"])


def clear(data_dir: Path) -> None:
    """Remove the beat on a clean stop, so status flips to not-running at once."""
    heartbeat_path(data_dir).unlink(missing_ok=True)


__all__ = [
    "CFG",
    "HEARTBEAT_FILENAME",
    "CloudSyncHeartbeatError",
    "Heartbeat",
    "beat",
    "clear",
    "heartbeat_path",
    "read",
]
