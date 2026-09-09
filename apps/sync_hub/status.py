"""CloudSync's small, truthful status surface and bounded result journal.

The status endpoint and CLI read this module. Configuration is deliberately
explicit: a scheduled sync is enabled only when ``MDT_CLOUDSYNC_SCHEDULER=1``
and ``MDT_CLOUDSYNC_HUB_URL`` names its hub. No configuration means off, not a
plausible healthy response.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SCHEDULER_ENV: str = "MDT_CLOUDSYNC_SCHEDULER"
ENDPOINT_ENV: str = "MDT_CLOUDSYNC_HUB_URL"
SIGNED_IN_AS_ENV: str = "MDT_CLOUDSYNC_SIGNED_IN_AS"
STATUS_FILENAME: str = "cloudsync-status.json"
MAX_RECENT_RESULTS: int = 5

ResultStatus = Literal["ok", "error"]


class CloudSyncStatusError(RuntimeError):
    """The local CloudSync result journal is malformed or inaccessible."""


@dataclass(frozen=True)
class SyncResult:
    """One completed push/pull attempt, recorded locally and JSON-safe."""

    finished_at: str
    status: ResultStatus
    message: str
    pushed: int
    pulled: int

    @classmethod
    def from_wire(cls, payload: object, *, source: Path) -> SyncResult:
        if not isinstance(payload, dict):
            raise CloudSyncStatusError(f"{source} result is not an object")
        expected = {"finished_at", "status", "message", "pushed", "pulled"}
        if set(payload) != expected:
            raise CloudSyncStatusError(
                f"{source} result fields are {sorted(payload)}, expected {sorted(expected)}"
            )
        if payload["status"] not in ("ok", "error"):
            raise CloudSyncStatusError(f"{source} result status is invalid")
        text_keys = ("finished_at", "message")
        if not all(isinstance(payload[key], str) and payload[key] for key in text_keys):
            raise CloudSyncStatusError(f"{source} result requires non-empty text fields")
        count_keys = ("pushed", "pulled")
        if not all(isinstance(payload[key], int) and payload[key] >= 0 for key in count_keys):
            raise CloudSyncStatusError(f"{source} result counts must be non-negative integers")
        return cls(**payload)

    def to_wire(self) -> dict[str, Any]:
        return {
            "finished_at": self.finished_at,
            "status": self.status,
            "message": self.message,
            "pushed": self.pushed,
            "pulled": self.pulled,
        }


@dataclass(frozen=True)
class CloudSyncStatus:
    """The common wire object for HTTP, UI, and the operator CLI."""

    enabled: bool
    reason: str | None
    signed_in_as: str | None
    last_push_at: str | None
    last_pull_at: str | None
    last_result: dict[str, str] | None
    rows_pending: int | None
    endpoint: str | None
    recent_results: tuple[SyncResult, ...]

    def to_wire(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "signed_in_as": self.signed_in_as,
            "last_push_at": self.last_push_at,
            "last_pull_at": self.last_pull_at,
            "last_result": self.last_result,
            "rows_pending": self.rows_pending,
            "endpoint": self.endpoint,
            "recent_results": [result.to_wire() for result in self.recent_results],
        }


def status_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATUS_FILENAME


def _configured(env: Mapping[str, str]) -> tuple[bool, str | None, str | None]:
    scheduler = env.get(SCHEDULER_ENV, "")
    endpoint = env.get(ENDPOINT_ENV, "").strip() or None
    if scheduler not in ("", "0", "1"):
        return False, f"{SCHEDULER_ENV} must be 0 or 1.", endpoint
    if scheduler != "1":
        return False, "CloudSync is not configured.", endpoint
    if endpoint is None:
        return False, f"{SCHEDULER_ENV}=1 but {ENDPOINT_ENV} is unset.", None
    return True, None, endpoint


def read_results(data_dir: Path) -> tuple[SyncResult, ...]:
    """Read the bounded newest-first history, never turning bad data into ok."""
    path = status_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise CloudSyncStatusError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CloudSyncStatusError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"results"}:
        raise CloudSyncStatusError(f"{path} must be an object with only results")
    rows = payload["results"]
    if not isinstance(rows, list):
        raise CloudSyncStatusError(f"{path} results must be a list")
    parsed = [SyncResult.from_wire(row, source=path) for row in rows]
    ordered = sorted(parsed, key=lambda result: result.finished_at, reverse=True)
    return tuple(ordered[:MAX_RECENT_RESULTS])


def write_result(data_dir: Path, result: SyncResult) -> tuple[SyncResult, ...]:
    """Prepend one result and atomically retain only the last five attempts."""
    results = (result, *read_results(data_dir))[:MAX_RECENT_RESULTS]
    path = status_path(data_dir)
    temp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"results": [row.to_wire() for row in results]})
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        raise CloudSyncStatusError(f"cannot write {path}: {exc}") from exc
    return results


def read_status(data_dir: Path, *, env: Mapping[str, str] | None = None) -> CloudSyncStatus:
    """Return this machine's actual configured status and recorded outcomes."""
    source = os.environ if env is None else env
    enabled, reason, endpoint = _configured(source)
    results = read_results(Path(data_dir))
    latest = results[0] if results else None
    return CloudSyncStatus(
        enabled=enabled,
        reason=reason,
        signed_in_as=source.get(SIGNED_IN_AS_ENV, "").strip() or None,
        last_push_at=None if latest is None else latest.finished_at,
        last_pull_at=None if latest is None else latest.finished_at,
        last_result=(
            None
            if latest is None
            else {"status": latest.status, "message": latest.message}
        ),
        # A successful attempt's pushed/pulled counts describe movement in
        # opposite directions, not a backlog. Until the sync engine publishes
        # its watermark-derived count, claiming arithmetic here would lie.
        rows_pending=None,
        endpoint=endpoint,
        recent_results=results,
    )


__all__ = [
    "MAX_RECENT_RESULTS",
    "CloudSyncStatus",
    "CloudSyncStatusError",
    "SyncResult",
    "read_status",
    "status_path",
    "write_result",
]
