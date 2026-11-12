"""Shell command broker for desktop-shell work the engine cannot perform.

The installed shell polls ``GET /api/v1/commands/next?consumer=shell`` and
executes commands such as ``apply-update`` (the release channel) and
``install-local-update`` (a local build an agent flagged ready, INSTALL-40)
that require Tauri plugins. The performance-page command bus is separate so
the two pollers never steal work.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request

from .utc_timestamps import utc_now_iso_ms

#: Wire vocabulary for apply refusal when the shell broker is misconfigured.
#: Mirrors ``UpdateStatus`` in ``apps.engine_core.update_channel_contract``
#: without importing engine_core (import-linter: webui must not import domain packages).
ShellBrokerFaultStatus = Literal["shell-broker-unavailable", "shell-broker-invalid"]

log = logging.getLogger(__name__)

SHELL_COMMAND_BROKER_STATE_ATTR: str = "shell_command_broker"
SHELL_COMPLETED_RETENTION: int = 20

#: The two shell commands that replace the installed bundle. At most one of
#: either may be in flight: two installers racing for one .app is a corrupt
#: bundle, whichever channel each came from.
APPLY_UPDATE_TYPE: str = "apply-update"
INSTALL_LOCAL_UPDATE_TYPE: str = "install-local-update"
_INSTALL_TYPES: frozenset[str] = frozenset({APPLY_UPDATE_TYPE, INSTALL_LOCAL_UPDATE_TYPE})


class ShellCommandConflictError(RuntimeError):
    """An install command (apply-update or install-local-update) is in flight."""

    def __init__(self, command_id: str) -> None:
        super().__init__(command_id)
        self.command_id = command_id


@dataclass
class _PendingShellCommand:
    command: dict[str, Any]
    enqueued_at_utc: str
    claimed: bool = False
    claimed_at_utc: str | None = None
    #: INSTALL-43: the shell may not claim this before ``time.monotonic()``
    #: reaches it, so the in-app countdown can be cancelled. None = claim now.
    not_before_monotonic: float | None = None
    countdown_ends_at_utc: str | None = None

    def counting_down(self, now: float) -> bool:
        return (
            not self.claimed
            and self.not_before_monotonic is not None
            and now < self.not_before_monotonic
        )


@dataclass
class _CompletedShellCommand:
    command_id: str
    command: dict[str, Any]
    result: dict[str, Any]
    enqueued_at_utc: str
    claimed_at_utc: str
    completed_at_utc: str
    countdown_ends_at_utc: str | None = None


class _ShellCommandBroker:
    def __init__(self) -> None:
        self.pending: dict[str, _PendingShellCommand] = {}
        self.completed: deque[_CompletedShellCommand] = deque(
            maxlen=SHELL_COMPLETED_RETENTION
        )

    def inflight_apply_id(self) -> str | None:
        for command_id, pending in self.pending.items():
            if pending.command.get("type") in _INSTALL_TYPES:
                return command_id
        return None

    def _enqueue_install(self, command: dict[str, Any], countdown_s: float = 0.0) -> str:
        existing = self.inflight_apply_id()
        if existing is not None:
            raise ShellCommandConflictError(existing)
        command_id = uuid4().hex
        pending = _PendingShellCommand(command=command, enqueued_at_utc=utc_now_iso_ms())
        if countdown_s > 0:
            pending.not_before_monotonic = time.monotonic() + countdown_s
            pending.countdown_ends_at_utc = (
                (datetime.now(UTC) + timedelta(seconds=countdown_s))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        self.pending[command_id] = pending
        return command_id

    def enqueue_apply(self, available_version: str) -> str:
        return self._enqueue_install(
            {"type": APPLY_UPDATE_TYPE, "available_version": available_version}
        )

    def enqueue_install_local(
        self, manifest_url: str, git_sha_full: str, *, countdown_s: float
    ) -> str:
        """Order the shell to install the flagged local build (INSTALL-40).

        ``countdown_s`` holds the order back from the shell (INSTALL-43): the
        app shows the countdown, and :meth:`cancel` withdraws it until then.
        """
        return self._enqueue_install(
            {
                "type": INSTALL_LOCAL_UPDATE_TYPE,
                "manifest_url": manifest_url,
                "git_sha_full": git_sha_full,
            },
            countdown_s,
        )

    def cancel(self, command_id: str, reason: str) -> dict[str, Any]:
        """Withdraw an install still counting down (INSTALL-43).

        Raises KeyError for an unknown id and RuntimeError once the shell has
        claimed it: an install already under way is not interrupted. Returns
        the withdrawn command.
        """
        pending = self.pending.get(command_id)
        if pending is None:
            raise KeyError(command_id)
        if pending.claimed:
            raise RuntimeError(f"shell command {command_id} is already installing")
        now_utc = utc_now_iso_ms()
        self.completed.append(
            _CompletedShellCommand(
                command_id=command_id,
                command=pending.command,
                result={"status": "failed", "outcome": "cancelled", "error": reason},
                enqueued_at_utc=pending.enqueued_at_utc,
                claimed_at_utc=now_utc,
                completed_at_utc=now_utc,
                countdown_ends_at_utc=pending.countdown_ends_at_utc,
            )
        )
        self.pending.pop(command_id)
        return pending.command

    def latest_of_type(self, command_type: str) -> str | None:
        """The newest command of ``command_type``, pending first, else completed."""
        for command_id, pending in reversed(self.pending.items()):
            if pending.command.get("type") == command_type:
                return command_id
        for record in reversed(self.completed):
            if record.command.get("type") == command_type:
                return record.command_id
        return None

    def claim(self) -> tuple[str, dict[str, Any]] | None:
        now = time.monotonic()
        for command_id, pending in self.pending.items():
            if not pending.claimed and not pending.counting_down(now):
                pending.claimed = True
                pending.claimed_at_utc = utc_now_iso_ms()
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
        completed_at_utc = utc_now_iso_ms()
        self.completed.append(
            _CompletedShellCommand(
                command_id=command_id,
                command=pending.command,
                result=result,
                enqueued_at_utc=pending.enqueued_at_utc,
                claimed_at_utc=pending.claimed_at_utc,
                completed_at_utc=completed_at_utc,
                countdown_ends_at_utc=pending.countdown_ends_at_utc,
            )
        )
        self.pending.pop(command_id, None)
        if result.get("status") == "failed":
            log.warning(
                "shell %s %s failed: outcome=%s error=%s",
                pending.command.get("type"),
                command_id,
                result.get("outcome"),
                result.get("error"),
            )

    def require_status(self, command_id: str) -> dict[str, Any]:
        status = self.get_status(command_id)
        if status is None:
            raise KeyError(command_id)
        return status

    def get_status(self, command_id: str) -> dict[str, Any] | None:
        pending = self.pending.get(command_id)
        if pending is not None:
            remaining = (
                None
                if pending.not_before_monotonic is None or pending.claimed
                else round(max(0.0, pending.not_before_monotonic - time.monotonic()), 3)
            )
            return {
                "command_id": command_id,
                "state": "claimed" if pending.claimed else "pending",
                "outcome": None,
                "error": None,
                "enqueued_at_utc": pending.enqueued_at_utc,
                "claimed_at_utc": pending.claimed_at_utc,
                "completed_at_utc": None,
                "countdown_ends_at_utc": pending.countdown_ends_at_utc,
                "countdown_remaining_s": remaining,
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
                    "countdown_ends_at_utc": record.countdown_ends_at_utc,
                    "countdown_remaining_s": None,
                }
        return None

    def pending_count(self) -> int:
        return len(self.pending)


class ShellBrokerUnavailableError(Exception):
    """The app never installed ``app.state.shell_command_broker``."""

    status: ShellBrokerFaultStatus = "shell-broker-unavailable"

    def __init__(self) -> None:
        self.detail = "shell command broker is not installed on this app"


class ShellBrokerInvalidError(Exception):
    """``app.state.shell_command_broker`` is present but not a broker instance."""

    status: ShellBrokerFaultStatus = "shell-broker-invalid"

    def __init__(self) -> None:
        self.detail = "app.state.shell_command_broker has an invalid type"


def install_shell_command_broker(app: FastAPI) -> _ShellCommandBroker:
    """Install one shell broker on ``app.state`` for apply and command polling."""
    broker = _ShellCommandBroker()
    setattr(app.state, SHELL_COMMAND_BROKER_STATE_ATTR, broker)
    return broker


def shell_broker(request: Request) -> _ShellCommandBroker:
    broker = getattr(request.app.state, SHELL_COMMAND_BROKER_STATE_ATTR, None)
    if broker is None:
        raise ShellBrokerUnavailableError()
    if not isinstance(broker, _ShellCommandBroker):
        raise ShellBrokerInvalidError()
    return broker
