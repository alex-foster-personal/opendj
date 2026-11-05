"""The agent half of restart-to-update: ready, status, install (INSTALL-39).

ONE client core for every agent surface. ``opendj update ready|status|install``
and the MCP tools ``app_update_ready``, ``app_update_status`` and
``app_update_install`` all call these three functions, so the CLI and MCP
cannot disagree about what "installed" means.

``install_via_engine`` reuses the release apply's wait loops
(:func:`apps.engine_core.update_channel._await_install` and
``_await_relaunch``) with a different arrival test: a local build usually keeps
the installed semver, so the claim is earned by the relaunched app reporting
the FLAGGED ``git_sha_full``, not a higher version.

Exit codes are the updater's (:data:`EXIT_APPLIED`, :data:`EXIT_NOT_APPLIED`,
:data:`EXIT_APPLY_FAILED`), so a caller branches on the same numbers for the
release channel and for a local build.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from apps.engine_core.local_update import (
    CANCEL_COMBO_LABEL,
    CANCEL_PATH,
    INSTALL_PATH,
    INSTALL_STATUS_PATH,
    READY_PATH,
    STATUS_PATH,
)
from apps.engine_core.update_channel import (
    APPLY_TIMEOUT_S,
    BUILD_INFO_PATH,
    EXIT_APPLIED,
    EXIT_APPLY_FAILED,
    EXIT_NOT_APPLIED,
    RELAUNCH_BUILD_INFO_TIMEOUT_S,
    REQUEST_TIMEOUT_S,
    ApplyOutcome,
    _await_install,
    _await_relaunch,
    _read_identity,
)

#: Reading the archive's stamp and hashing it takes about 2 s for a 500 MB build.
READY_TIMEOUT_S: float = 60.0


@dataclass(frozen=True)
class AppUpdateReply:
    """One engine answer: ok, the document, and the refusal code when not ok."""

    ok: bool
    document: dict[str, Any]
    error: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class InstallOutcome:
    """What one install did. ``code`` is EXIT_APPLIED only when the flagged sha runs."""

    code: int
    reason: str
    detail: str | None
    git_sha_full: str | None = None
    command_id: str | None = None
    before: dict[str, str | None] = field(default_factory=dict)
    after: dict[str, str | None] = field(default_factory=dict)
    #: INSTALL-44: the descent warning the engine attached to the install.
    warning: str | None = None

    @property
    def installed(self) -> bool:
        return self.code == EXIT_APPLIED

    def document(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "reason": self.reason,
            "detail": self.detail,
            "git_sha_full": self.git_sha_full,
            "command_id": self.command_id,
            "before": self.before,
            "after": self.after,
            "exit_code": self.code,
            "warning": self.warning,
        }


def _reply(response: httpx.Response, url: str) -> AppUpdateReply:
    try:
        body = response.json()
    except ValueError:
        return AppUpdateReply(
            False,
            {},
            "engine-response-malformed",
            f"{url} answered HTTP {response.status_code}: {response.text[:300]}",
        )
    if not isinstance(body, dict):
        return AppUpdateReply(False, {}, "engine-response-malformed", f"{url} answered a non-object body")
    if response.is_success:
        return AppUpdateReply(True, body)
    if response.status_code == 404 and body.get("detail") == "Not Found":
        return AppUpdateReply(False, body, "route-missing", f"{url} is not served; the running app predates restart-to-update")
    error = body.get("error")
    detail = body.get("detail")
    return AppUpdateReply(
        False,
        body,
        error if isinstance(error, str) else f"http-{response.status_code}",
        detail if isinstance(detail, str) else str(detail),
    )


def _call(method: str, origin: str, path: str, *, json_body: dict[str, Any] | None = None, timeout: float = REQUEST_TIMEOUT_S, client: httpx.Client | None = None) -> AppUpdateReply:
    url = f"{origin.rstrip('/')}{path}"
    owned = client is None
    active = httpx.Client() if client is None else client
    try:
        response = active.request(method, url, json=json_body, timeout=timeout)
    except httpx.HTTPError as error:
        return AppUpdateReply(False, {}, "engine-unreachable", f"{url}: {error}")
    finally:
        if owned:
            active.close()
    return _reply(response, url)


def ready_via_engine(origin: str, artifact: Path, notes: str | None = None, *, client: httpx.Client | None = None) -> AppUpdateReply:
    """Flag ``artifact`` as the pending update. Installs nothing."""
    body: dict[str, Any] = {"artifact": str(artifact.expanduser().resolve())}
    if notes:
        body["notes"] = notes
    return _call("POST", origin, READY_PATH, json_body=body, timeout=READY_TIMEOUT_S, client=client)


def status_via_engine(origin: str, *, client: httpx.Client | None = None) -> AppUpdateReply:
    """The same document the tray's nudge reads."""
    return _call("GET", origin, STATUS_PATH, client=client)


def cancel_via_engine(origin: str, reason: str, *, client: httpx.Client | None = None) -> AppUpdateReply:
    """Withdraw the install still counting down (INSTALL-43). Same as Esc Space x4."""
    return _call("POST", origin, CANCEL_PATH, json_body={"reason": reason}, client=client)


def install_via_engine(
    origin: str,
    timeout_s: float = APPLY_TIMEOUT_S,
    *,
    lock_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
) -> InstallOutcome:
    """Order the shell to install the flagged build, then PROVE it is running."""
    if client is not None:
        return _install_with(client, origin, timeout_s, lock_path, progress)
    with httpx.Client() as owned:
        return _install_with(owned, origin, timeout_s, lock_path, progress)


def _install_with(
    client: httpx.Client,
    origin: str,
    timeout_s: float,
    lock_path: Path | None,
    progress: Callable[[str], None] | None,
) -> InstallOutcome:
    base = origin.rstrip("/")
    before = _read_identity(client, f"{base}{BUILD_INFO_PATH}")
    if isinstance(before, ApplyOutcome):
        return InstallOutcome(EXIT_NOT_APPLIED, before.reason, before.detail)
    deadline = time.monotonic() + timeout_s
    accepted = _call("POST", base, INSTALL_PATH, client=client)
    if not accepted.ok:
        return InstallOutcome(EXIT_NOT_APPLIED, accepted.error or "install-refused", accepted.detail, before=before, after=before)
    command_id = accepted.document.get("command_id")
    target_sha = accepted.document.get("git_sha_full")
    if not isinstance(command_id, str) or not isinstance(target_sha, str):
        return InstallOutcome(
            EXIT_NOT_APPLIED,
            "install-response-malformed",
            f"{INSTALL_PATH} answered without command_id/git_sha_full: {accepted.document}",
            before=before,
            after=before,
        )
    raw_warning = accepted.document.get("warning")
    warning = raw_warning if isinstance(raw_warning, str) else None
    if progress is not None and warning is not None:
        progress(f"[WARN] {warning}")
    if progress is not None:
        countdown = accepted.document.get("countdown_s")
        progress(
            f"install of {target_sha[:8]} starts in {countdown:g} s (command_id={command_id}); "
            f"cancel with {CANCEL_COMBO_LABEL} in the app or `opendj update cancel`"
            if isinstance(countdown, (int, float)) and countdown > 0
            else f"enqueued install of {target_sha[:8]} (command_id={command_id})"
        )

    waited = _await_install(client, base, command_id, deadline, timeout_s, INSTALL_STATUS_PATH)
    if isinstance(waited, ApplyOutcome):
        if (waited.shell_status or {}).get("outcome") == "cancelled":
            return InstallOutcome(EXIT_NOT_APPLIED, "cancelled", waited.detail, target_sha, command_id, before, before, warning)
        return InstallOutcome(waited.code, waited.reason, waited.detail, target_sha, command_id, before, before, warning)
    if progress is not None:
        progress("waiting for the relaunched app to report the flagged build")
    relaunch_deadline = time.monotonic() + (RELAUNCH_BUILD_INFO_TIMEOUT_S if waited.relaunch_detected else timeout_s)

    def arrived(_before: dict[str, str | None], after: dict[str, str | None]) -> bool:
        return after.get("git_sha_full") == target_sha

    after = _await_relaunch(client, base, before, lock_path, relaunch_deadline, arrived)
    if after.get("git_sha_full") != target_sha:
        return InstallOutcome(
            EXIT_APPLY_FAILED,
            "relaunch-timeout",
            f"the app still reports {after.get('git_sha_full')} after the shell took the install of {target_sha}",
            target_sha,
            command_id,
            before,
            after,
            warning,
        )
    return InstallOutcome(EXIT_APPLIED, "installed", f"{target_sha[:8]} is running", target_sha, command_id, before, after, warning)


__all__ = [
    "AppUpdateReply",
    "InstallOutcome",
    "cancel_via_engine",
    "install_via_engine",
    "ready_via_engine",
    "status_via_engine",
]
