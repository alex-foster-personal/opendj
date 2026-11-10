"""The shared client core that checks and applies an update through a running engine.

Part of the update channel split out of one oversized module (#9688). Import
from ``apps.engine_core.update_channel``, the public facade, not from here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from apps.engine_core.build_info import (
    BUILD_INFO_PATH,
)
from apps.engine_core.origin import EngineNotRunning
from apps.engine_core.update_channel_contract import (
    ACTIONABLE_STATUSES,
    APPLY_TIMEOUT_S,
    EXIT_APPLIED,
    EXIT_APPLY_FAILED,
    EXIT_NOT_APPLIED,
    REQUEST_TIMEOUT_S,
    STATUS_ENGINE_MALFORMED,
    STATUS_ENGINE_UNREACHABLE,
    UPDATE_APPLY_PATH,
    UPDATE_APPLY_STATUS_PATH,
    UPDATE_CHECK_PATH,
    UpdateCheckError,
    facade,
)
from apps.engine_core.update_channel_feed import (
    parse_version,
)


# ----- outcomes -----------------------------------------------------------
@dataclass(frozen=True)
class CheckOutcome:
    """What one check against a running engine found.

    ``document`` is the engine's own answer, passed through unchanged: the
    surfaces render it, they do not re-derive it, so three callers cannot
    print three different stories about one channel.
    """

    status: str
    detail: str | None
    document: dict[str, Any]

    @property
    def actionable(self) -> bool:
        return self.status in ACTIONABLE_STATUSES


@dataclass(frozen=True)
class ApplyOutcome:
    """What one apply did, and what it can honestly claim.

    ``code`` is the exit code every surface returns, and it is ``EXIT_APPLIED``
    only for an install the before/after read PROVES: the announced version is
    running and the build identity moved. ``reason`` is the machine-readable
    half; ``status`` and ``detail`` are the channel's own words where it named
    them, so an agent never has to parse prose to find out what happened.
    """

    code: int
    reason: str
    status: str | None = None
    detail: str | None = None
    check: dict[str, Any] = field(default_factory=dict)
    before: dict[str, str | None] = field(default_factory=dict)
    after: dict[str, str | None] = field(default_factory=dict)
    shell_status: dict[str, Any] | None = None
    command_id: str | None = None
    advertised_version: str | None = None

    @property
    def applied(self) -> bool:
        return self.code == EXIT_APPLIED


# ----- the shared client core ---------------------------------------------
def check_via_engine(origin: str, *, client: httpx.Client | None = None) -> CheckOutcome:
    """Ask a RUNNING engine what the channel offers.

    The engine answers for the build IT is running, which is the only identity
    that matters to a caller on a user machine: there is no checkout to
    resolve locally, and resolving one would compare the channel against a
    different build than the app the user is looking at. Every way this can
    fail to get an answer is a named fault carrying the URL, never a
    reassurance.
    """
    if client is not None:
        return _check_with(client, origin)
    with httpx.Client() as owned:
        return _check_with(owned, origin)


def _check_with(client: httpx.Client, origin: str) -> CheckOutcome:
    url = f"{origin.rstrip('/')}{UPDATE_CHECK_PATH}"
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return _fault(STATUS_ENGINE_UNREACHABLE, f"{url}: {exc}")
    try:
        document = response.json()
    except ValueError:
        return _fault(
            STATUS_ENGINE_MALFORMED,
            f"{url} answered HTTP {response.status_code} with a body that is not JSON",
        )
    status_field = document.get("status") if isinstance(document, dict) else None
    if not isinstance(status_field, str) or status_field == "":
        return _fault(
            STATUS_ENGINE_MALFORMED,
            f"{url} answered HTTP {response.status_code} with no 'status' field: {json.dumps(document)[:500]}",
        )
    detail = document.get("detail")
    return CheckOutcome(
        status=status_field,
        detail=detail if isinstance(detail, str) and detail != "" else None,
        document=document,
    )


def _fault(status: str, detail: str) -> CheckOutcome:
    return CheckOutcome(status=status, detail=detail, document={"status": status, "detail": detail})


def apply_via_engine(
    origin: str,
    timeout_s: float = APPLY_TIMEOUT_S,
    *,
    lock_path: Path | None = None,
    progress: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
) -> ApplyOutcome:
    """Install the announced release, then PROVE the relaunched app is it.

    THE whole relaunch-and-compare path, in one place. ``apply`` is the call
    whose correctness matters most, and a second copy of it is how two
    surfaces drift: the packaged CLI and the module entry point both call
    here, and neither can quietly acquire its own idea of what "installed"
    means.

    A zero exit is a CLAIM, so it is earned twice over: ``app_version`` must
    be the version the channel announced, and ``git_sha_full`` must have
    moved off the build that was running. Anything less is named and
    non-zero. Every wait is bounded by ``timeout_s``, counted from the moment
    the order is posted, because an install that never completes must fail
    loudly rather than hang the agent that asked for it.
    """
    if client is not None:
        return _apply_with(client, origin, timeout_s, lock_path, progress)
    with httpx.Client() as owned:
        return _apply_with(owned, origin, timeout_s, lock_path, progress)


def _apply_with(
    client: httpx.Client,
    origin: str,
    timeout_s: float,
    lock_path: Path | None,
    progress: Callable[[str], None] | None,
) -> ApplyOutcome:
    base = origin.rstrip("/")
    before = _read_identity(client, f"{base}{BUILD_INFO_PATH}")
    if isinstance(before, ApplyOutcome):
        return before

    check = check_via_engine(base, client=client)
    announced_version = _announced_version(check)
    if check.status != "update-available" or announced_version is None:
        shortfall = _check_shortfall(check)
        return _contextualise(shortfall, check, before, announced_version, None)

    # ONE deadline from here, so the install and the restart can never add up
    # to twice the wait a caller asked for.
    deadline = time.monotonic() + timeout_s
    command_id = _start_install(client, base)
    if isinstance(command_id, ApplyOutcome):
        return _contextualise(command_id, check, before, announced_version, None)
    if progress is not None:
        progress(f"enqueued {announced_version} (command_id={command_id})")

    installed = _await_install(client, base, command_id, deadline, timeout_s)
    if isinstance(installed, ApplyOutcome):
        return _contextualise(installed, check, before, announced_version, command_id)
    if installed.relaunch_detected:
        # read via the facade: tests patch it there
        relaunch_deadline = time.monotonic() + facade().RELAUNCH_BUILD_INFO_TIMEOUT_S
        if progress is not None:
            progress("engine relaunch detected; polling build-info via lock file")
    else:
        relaunch_deadline = time.monotonic() + timeout_s
        if progress is not None:
            progress(f"waiting for the relaunched app to report {announced_version}")

    after = _await_relaunch(client, base, before, lock_path, relaunch_deadline)
    return _verdict(before, after, check, announced_version, command_id, timeout_s)


def _read_identity(client: httpx.Client, url: str) -> dict[str, str | None] | ApplyOutcome:
    """The running build's identity, or why it could not be read."""
    try:
        response = client.get(url, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return _not_applied("engine-unreachable", detail=f"{url}: {exc}")
    if response.status_code != 200:
        return _not_applied(
            "build-info-unavailable",
            detail=(f"{url} answered HTTP {response.status_code}: {response.text[:500]}"),
        )
    return _build_identity_slice(response.json())


def _announced_version(check: CheckOutcome) -> str | None:
    announced = check.document.get("available_version")
    return announced if isinstance(announced, str) and announced != "" else None


def _check_shortfall(check: CheckOutcome) -> ApplyOutcome:
    """Why the channel is not offering an installable release."""
    if check.status != "update-available":
        return _not_applied(
            "check-not-update-available",
            status=check.status,
            detail=check.detail or f"update check reported {check.status}",
        )
    return _not_applied(
        "announced-version-missing",
        status=check.status,
        detail="update-available without available_version",
    )


def _start_install(client: httpx.Client, base: str) -> str | ApplyOutcome:
    """Post the order and return its command id, or why nothing was posted."""
    apply_url = f"{base}{UPDATE_APPLY_PATH}"
    try:
        response = client.post(apply_url, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return _not_applied("apply-unreachable", detail=f"{apply_url}: {exc}")
    if response.status_code != 202:
        refusal = _refusal_body(response)
        return _not_applied(
            "apply-refused",
            status=refusal.get("status"),
            detail=refusal.get("detail") or f"{apply_url} answered HTTP {response.status_code}: {response.text[:500]}",
        )
    accepted = _json_object(response)
    command_id = accepted.get("command_id") if accepted is not None else None
    if not isinstance(command_id, str) or command_id == "":
        return _not_applied(
            "apply-response-missing-command-id",
            detail=f"{apply_url} answered 202 without a command_id",
        )
    return command_id


@dataclass(frozen=True)
class _InstallWait:
    """What the install wait saw: the shell's word, or the engine going away.

    A relaunch kills the engine that was serving the status route, so a
    refusal AFTER the order was claimed is evidence the install started, not a
    failure. Telling those apart is the whole reason this is a value rather
    than a status document.
    """

    status: dict[str, Any] | None = None
    relaunch_detected: bool = False


def _await_install(
    client: httpx.Client,
    base: str,
    command_id: str,
    deadline: float,
    timeout_s: float,
    status_path_template: str = UPDATE_APPLY_STATUS_PATH,
) -> _InstallWait | ApplyOutcome:
    """Wait for the shell to report the order done, bounded by the deadline.

    Returns what the shell said, or the outcome that says why the wait ended.
    The shell is the only component that may install an update, so this is the
    only evidence that an install was even attempted. ``status_path_template``
    names the route that reports the order: the release channel's apply, or a
    local build's install (INSTALL-40); both read the one shell broker.
    """
    status_url = f"{base}{status_path_template.format(command_id=command_id)}"
    seen_claimed = False
    while True:
        if time.monotonic() >= deadline:
            return replace(
                _not_applied(
                    "install-timeout",
                    status="update-available",
                    detail=(f"the shell did not report {command_id} installed within {timeout_s:g}s"),
                ),
                code=EXIT_APPLY_FAILED,
            )
        try:
            response = client.get(status_url, timeout=REQUEST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            if seen_claimed:
                return _InstallWait(relaunch_detected=True)
            return _not_applied("install-status-unreachable", detail=f"{status_url}: {exc}")
        if response.status_code == 404 and seen_claimed:
            return _InstallWait(relaunch_detected=True)
        status_body = _json_object(response)
        if status_body is None:
            return _not_applied(
                "install-status-malformed",
                detail=(f"{status_url} answered HTTP {response.status_code}: {response.text[:500]}"),
            )
        state = status_body.get("state")
        if state in ("claimed", "succeeded"):
            seen_claimed = True
        if state == "failed":
            error = status_body.get("error")
            return replace(
                _not_applied(
                    "shell-failed",
                    status="failed",
                    detail=(
                        error
                        if isinstance(error, str) and error != ""
                        else f"the shell reported command {command_id} failed"
                    ),
                ),
                code=EXIT_APPLY_FAILED,
                shell_status=status_body,
            )
        if state == "succeeded":
            return _InstallWait(status=status_body)
        # read via the facade: tests patch it there
        time.sleep(facade().APPLY_STATUS_POLL_INTERVAL_S)


def _await_relaunch(
    client: httpx.Client,
    base: str,
    before: dict[str, str | None],
    lock_path: Path | None,
    deadline: float,
    arrived: Callable[[dict[str, str | None], dict[str, str | None]], bool] | None = None,
) -> dict[str, str | None]:
    """Poll build-info until the running identity moves, or the deadline passes.

    The origin is re-resolved every pass: the relaunched app writes its own
    lock, and it may well bind a different port than the one it replaced.
    ``arrived`` decides when the identity has moved far enough: by default a
    strictly higher semver (the release channel); a local build that keeps
    its semver passes its own predicate on ``git_sha_full`` (INSTALL-40).
    """
    has_arrived = _version_higher if arrived is None else arrived
    after = before
    backoff = 1.0
    while time.monotonic() < deadline:
        poll_base = base
        try:
            # read via the facade: tests patch it there
            poll_base = facade().resolve_origin(lock_path).base_url
        except EngineNotRunning:
            poll_base = base
        try:
            response = client.get(f"{poll_base}{BUILD_INFO_PATH}", timeout=REQUEST_TIMEOUT_S)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            after = _build_identity_slice(response.json())
            if has_arrived(before, after):
                return after
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 10.0)
    return after


def _not_applied(reason: str, *, status: str | None = None, detail: str) -> ApplyOutcome:
    """The apply was never attempted: the channel or the engine said no."""
    return ApplyOutcome(code=EXIT_NOT_APPLIED, reason=reason, status=status, detail=detail)


def _apply_failed(reason: str, *, detail: str) -> ApplyOutcome:
    """The apply was attempted and did not land the announced build."""
    return ApplyOutcome(
        code=EXIT_APPLY_FAILED,
        reason=reason,
        status="update-available",
        detail=detail,
    )


def _contextualise(
    outcome: ApplyOutcome,
    check: CheckOutcome,
    before: dict[str, str | None],
    announced_version: str | None,
    command_id: str | None,
    after: dict[str, str | None] | None = None,
) -> ApplyOutcome:
    """Attach the run's context to a bare outcome, so every surface has it."""
    return replace(
        outcome,
        check=check.document,
        before=before,
        after=before if after is None else after,
        command_id=command_id,
        advertised_version=announced_version,
    )


def _verdict(
    before: dict[str, str | None],
    after: dict[str, str | None],
    check: CheckOutcome,
    announced_version: str,
    command_id: str,
    timeout_s: float,
) -> ApplyOutcome:
    """Did the relaunch land the announced build? Name the shortfall if not."""
    shortfall = _shortfall(before, after, announced_version, timeout_s)
    if shortfall is not None:
        return _contextualise(shortfall, check, before, announced_version, command_id, after)
    applied = ApplyOutcome(
        code=EXIT_APPLIED,
        reason="applied",
        status="update-available",
        detail=f"{announced_version} is running",
    )
    return _contextualise(applied, check, before, announced_version, command_id, after)


def _shortfall(
    before: dict[str, str | None],
    after: dict[str, str | None],
    announced_version: str,
    timeout_s: float,
) -> ApplyOutcome | None:
    """Why the relaunch is not the install that was asked for, if it is not."""
    if not _version_higher(before, after):
        return _apply_failed(
            "relaunch-timeout",
            detail=(
                f"the app still reports {before.get('app_version')} "
                f"({before.get('git_sha_full')}) {timeout_s:g}s after the shell "
                "reported the install done"
            ),
        )
    if after.get("app_version") != announced_version:
        return _apply_failed(
            "version-not-announced",
            detail=(
                f"the relaunched app reports app_version {after.get('app_version')!r}, "
                f"and the channel announced {announced_version!r}"
            ),
        )
    if after.get("git_sha_full") == before.get("git_sha_full"):
        return _apply_failed(
            "build-unchanged",
            detail=(
                f"the relaunched app reports the same git_sha_full "
                f"{after.get('git_sha_full')!r} as the build it replaced"
            ),
        )
    return None


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _refusal_body(response: httpx.Response) -> dict[str, Any]:
    body = _json_object(response)
    return body if body is not None else {}


def _build_identity_slice(body: dict[str, object]) -> dict[str, str | None]:
    app_version = body.get("app_version")
    git_sha_full = body.get("git_sha_full")
    return {
        "app_version": app_version if isinstance(app_version, str) else None,
        "git_sha_full": git_sha_full if isinstance(git_sha_full, str) else None,
    }


def _version_higher(before: dict[str, str | None], after: dict[str, str | None]) -> bool:
    """True when after.app_version parses and is strictly greater than before."""
    before_ver = before.get("app_version")
    after_ver = after.get("app_version")
    if not isinstance(before_ver, str) or not isinstance(after_ver, str):
        return False
    try:
        here = parse_version(before_ver, "pre-apply app_version")
        there = parse_version(after_ver, "post-apply app_version")
    except UpdateCheckError:
        return False
    return there > here
