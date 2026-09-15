"""Shell command broker for desktop-shell work the engine cannot perform.

The installed shell polls ``GET /api/v1/commands/next?consumer=shell`` and
executes commands such as ``apply-update`` that require Tauri plugins. The
performance-page command bus is separate so the two pollers never steal work.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import Request

log = logging.getLogger(__name__)

SHELL_COMMAND_BROKER_STATE_ATTR: str = "shell_command_broker"
SHELL_COMPLETED_RETENTION: int = 20


class ShellCommandConflictError(RuntimeError):
    """An apply-update command is already pending or claimed."""

    def __init__(self, command_id: str) -> None:
        super().__init__(command_id)
        self.command_id = command_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class _PendingShellCommand:
    command: dict[str, Any]
    enqueued_at_utc: str
    claimed: bool = False
    claimed_at_utc: str | None = None


@dataclass
class _CompletedShellCommand:
    command_id: str
    command: dict[str, Any]
    result: dict[str, Any]
    enqueued_at_utc: str
    claimed_at_utc: str
    completed_at_utc: str


class _ShellCommandBroker:
    def __init__(self) -> None:
        self.pending: dict[str, _PendingShellCommand] = {}
        self.completed: deque[_CompletedShellCommand] = deque(
            maxlen=SHELL_COMPLETED_RETENTION
        )

    def inflight_apply_id(self) -> str | None:
        for command_id, pending in self.pending.items():
            if pending.command.get("type") == "apply-update":
                return command_id
        return None

    def enqueue_apply(self, available_version: str) -> str:
        existing = self.inflight_apply_id()
        if existing is not None:
            raise ShellCommandConflictError(existing)
        command_id = uuid4().hex
        self.pending[command_id] = _PendingShellCommand(
            command={"type": "apply-update", "available_version": available_version},
            enqueued_at_utc=_utc_now(),
        )
        return command_id

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        for command_id, pending in self.pending.items():
            if not pending.claimed:
                pending.claimed = True
                pending.claimed_at_utc = _utc_now()
                return command_id, pending.command
        return None

    def complete(self, command_id: str, result: dict[str, Any]) -> None:
        pending = self.pending.get(command_id)
        if pending is None:
            raise KeyError(command_id)
        if not pending.claimed:
            raise RuntimeError(f"shell command {command_id} was not claimed")
        if pending.claimed_at_utc is None:
            raise RuntimeError(f"shell command {command_id} has no claimed_at_utc")
        completed_at_utc = _utc_now()
        self.completed.append(
            _CompletedShellCommand(
                command_id=command_id,
                command=pending.command,
                result=result,
                enqueued_at_utc=pending.enqueued_at_utc,
                claimed_at_utc=pending.claimed_at_utc,
                completed_at_utc=completed_at_utc,
            )
        )
        self.pending.pop(command_id, None)
        if result.get("status") == "failed":
            log.warning(
                "shell apply-update %s failed: outcome=%s error=%s",
                command_id,
                result.get("outcome"),
                result.get("error"),
            )

    def get_status(self, command_id: str) -> dict[str, Any] | None:
        pending = self.pending.get(command_id)
        if pending is not None:
            if pending.claimed:
                return {
                    "command_id": command_id,
                    "state": "claimed",
                    "outcome": None,
                    "error": None,
                    "enqueued_at_utc": pending.enqueued_at_utc,
                    "claimed_at_utc": pending.claimed_at_utc,
                    "completed_at_utc": None,
                }
            return {
                "command_id": command_id,
                "state": "pending",
                "outcome": None,
                "error": None,
                "enqueued_at_utc": pending.enqueued_at_utc,
                "claimed_at_utc": None,
                "completed_at_utc": None,
            }
        for record in reversed(self.completed):
            if record.command_id == command_id:
                status = record.result.get("status")
                if status not in {"succeeded", "failed"}:
                    raise RuntimeError(
                        f"shell command {command_id} has invalid completed status: {status!r}"
                    )
                error = record.result.get("error")
                return {
                    "command_id": command_id,
                    "state": status,
                    "outcome": record.result.get("outcome"),
                    "error": error if isinstance(error, str) else None,
                    "enqueued_at_utc": record.enqueued_at_utc,
                    "claimed_at_utc": record.claimed_at_utc,
                    "completed_at_utc": record.completed_at_utc,
                }
        return None

    def pending_count(self) -> int:
        return len(self.pending)


def shell_broker(request: Request) -> _ShellCommandBroker:
    broker = getattr(request.app.state, SHELL_COMMAND_BROKER_STATE_ATTR, None)
    if broker is None:
        broker = _ShellCommandBroker()
        request.app.state.shell_command_broker = broker
    if not isinstance(broker, _ShellCommandBroker):
        raise TypeError("app.state.shell_command_broker has an invalid type")
    return broker
