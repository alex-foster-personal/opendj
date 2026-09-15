"""Shell command broker for desktop-shell work the engine cannot perform.

The installed shell polls ``GET /api/v1/commands/next?consumer=shell`` and
executes commands such as ``apply-update`` that require Tauri plugins. The
performance-page command bus is separate so the two pollers never steal work.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import Request

SHELL_COMMAND_BROKER_STATE_ATTR: str = "shell_command_broker"


class ShellCommandConflictError(RuntimeError):
    """An apply-update command is already pending or claimed."""

    def __init__(self, command_id: str) -> None:
        super().__init__(command_id)
        self.command_id = command_id


@dataclass
class _PendingShellCommand:
    command: dict[str, Any]
    claimed: bool = False


class _ShellCommandBroker:
    def __init__(self) -> None:
        self.pending: dict[str, _PendingShellCommand] = {}

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
            command={"type": "apply-update", "available_version": available_version}
        )
        return command_id

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        for command_id, pending in self.pending.items():
            if not pending.claimed:
                pending.claimed = True
                return command_id, pending.command
        return None

    def complete(self, command_id: str, result: dict[str, Any]) -> None:
        pending = self.pending.get(command_id)
        if pending is None:
            raise KeyError(command_id)
        if not pending.claimed:
            raise RuntimeError(f"shell command {command_id} was not claimed")
        self.pending.pop(command_id, None)

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
