"""``GET /api/v1/update/check`` -- is there a newer Open DJ than this one?

WHY THIS EXISTS SEPARATELY FROM THE TAURI UPDATER

The desktop shell carries ``tauri-plugin-updater``, and that plugin is the
only component that may ever APPLY an update: it re-fetches the manifest,
verifies its minisign signature against the pubkey compiled into the shell,
and swaps the .app bundle. Signature verification and installation must not
be separable, so nothing here downloads or installs anything.

What the plugin CANNOT do is answer the question anywhere except inside the
shell's own webview. An agent driving this app over HTTP has no IPC bridge,
and neither does a browser tab pointed at the engine. Agent-native parity is
a project requirement, so the CHECK is answered here too, over plain HTTP,
from the SAME endpoint constant the shell is configured with. The two read
one URL and cannot drift: ``tests/engine_core/test_update_channel.py`` asserts
this module's ``UPDATE_ENDPOINT`` equals the ``plugins.updater.endpoints[0]``
in ``tauri.conf.json``.

TWO VERSIONS, AND WHY BOTH ARE ON SCREEN

The updater keys on SEMVER (``tauri.conf.json`` ``version``), because that is
what the plugin compares. Today every build Open DJ ships is ``0.1.0``: the
number changes when someone cuts a release, not when someone merges. The
identity that actually distinguishes two builds is the git sha and build time
already stamped into the payload manifest, which this engine serves at
``/build-info``.

So this route reports BOTH, and names the awkward case rather than hiding it:
a remote release carrying the same semver as the running build but a different
sha is ``up-to-date`` by the rule the updater will apply, with
``same_version_different_build`` set. A user staring at "you are up to date"
while running a different commit is exactly the wasted afternoon
``build_info`` was written to end; it is not repeated here.

NO SILENT NO-OP

Every failure to reach or parse the manifest is a named fault carrying the
endpoint and the reason. An update channel that quietly answers "up to date"
when its endpoint is unreachable is worse than no channel: it converts an
outage into a false reassurance. There is no branch in this module that can
produce ``up-to-date`` without having read a manifest.

Requirements (mini-PRD):

- ✔︎ ✅ 🎯 A reachable manifest with a higher semver reports
  ``update-available`` naming both versions. -> :func:`compare_versions`
- ✔︎ ✅ 🎯 An unreachable, refused, or malformed endpoint reports a named
  fault, never ``up-to-date``. -> :func:`resolve_update_check`
- ✔︎ ✅ 🎯 A manifest with no entry for this platform reports
  ``platform-unsupported`` naming the key it looked for.
  -> :func:`_platform_entry`
- ✔︎ ✅ 🎯 Equal semver with a different git sha reports ``up-to-date`` AND
  ``same_version_different_build``. -> :func:`resolve_update_check`
- ✔︎ ✅ 🎯 The endpoint this module reads equals the one compiled into the
  shell. -> ``tests/engine_core/test_update_channel.py``

Acceptance tests:

- [if] the endpoint returns a manifest with version 9.9.9 [then] status is
  ``update-available`` with available_version 9.9.9, [else ⛔️].
- [if] the endpoint refuses the connection [then] status is
  ``endpoint-unreachable`` and the body names the URL, [else ⛔️].
- [if] the endpoint answers 404 [then] status is ``endpoint-refused`` carrying
  404, and NOT ``up-to-date``, [else ⛔️].
- [if] the manifest omits this platform key [then] status is
  ``platform-unsupported`` naming darwin-aarch64, [else ⛔️].
- [if] the manifest's version equals the running one but the notes name a
  different sha [then] status is ``up-to-date`` with
  same_version_different_build true, [else ⛔️].
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    BUILD_INFO_PATH,
    BuildIdentity,
    BuildInfoUnavailable,
    resolve_build_info,
)
from apps.engine_core.origin import EngineNotRunning, resolve_origin
from apps.shared import platform_paths
from apps.shared.semver import Semver as _Version
from apps.shared.semver import parse_semver as _parse_semver
from apps.webui.server.shell_commands import ShellCommandConflictError, shell_broker

#: THE update endpoint. One string, read by this module and compiled into the
#: desktop shell via ``plugins.updater.endpoints`` in tauri.conf.json; a test
#: asserts the two are identical.
#:
#: GitHub Releases is the standard host for a Tauri updater and needs no
#: server to operate. The product repository is private, so its releases are
#: deliberately not used here: unauthenticated Tauri clients receive a 404.
#: ``issue-assets`` is public and holds only signed release artifacts and their
#: manifest. See docs/auto-update.md.
UPDATE_ENDPOINT: str = (
    "https://github.com/alex-foster-personal/issue-assets"
    "/releases/latest/download/latest.json"
)

UPDATE_CHECK_PATH: str = "/api/v1/update/check"
UPDATE_APPLY_PATH: str = "/api/v1/update/apply"
UPDATE_APPLY_STATUS_PATH: str = "/api/v1/update/apply/{command_id}"

#: How often the apply CLI polls command status before checking build-info.
APPLY_STATUS_POLL_INTERVAL_S: float = 1.0

#: After the shell claims apply and the old engine dies, poll the relaunched
#: engine's build-info for this long before giving up.
#: How long the app has to come back once the engine has gone away underneath
#: the status poll. Post-claim engine death IS the relaunch (issue #2989), and
#: the restart takes longer than a round trip, so it gets its own window rather
#: than whatever is left of APPLY_TIMEOUT_S.
RELAUNCH_BUILD_INFO_TIMEOUT_S: float = 120.0

#: The error code a caller branches on when the channel could not answer.
CODE_UPDATE_CHECK_FAILED: str = "update_check_failed"

#: How long to wait on the manifest before calling the endpoint unreachable.
#: Short on purpose: this sits behind a button a human is watching.
REQUEST_TIMEOUT_S: float = 10.0

#: Tauri's platform key: ``<os>-<arch>``, where os is darwin/windows/linux and
#: arch is the Rust target arch. v1 ships arm64 macOS only, but the key is
#: derived rather than hardcoded so a Windows build reports the truth.
_OS_KEYS: dict[str, str] = {"Darwin": "darwin", "Windows": "windows", "Linux": "linux"}
_ARCH_KEYS: dict[str, str] = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}



UpdateStatus = Literal[
    "update-available",
    "up-to-date",
    "ahead-of-channel",
    "endpoint-unreachable",
    "endpoint-refused",
    "manifest-malformed",
    "platform-unsupported",
    "identity-unavailable",
]

#: The statuses that mean "the question was answered". Anything else is a
#: fault and must never be rendered as reassurance.
ANSWERED: frozenset[str] = frozenset(
    {"update-available", "up-to-date", "ahead-of-channel"}
)

#: The statuses an agent may ACT on, which is a strictly smaller set than
#: ANSWERED. `ahead-of-channel` is a real answer (this build is newer than the
#: channel) and not an actionable one, so a script that ran `update check &&
#: update apply` on a zero exit must never be told it holds an update it does
#: not. Exit 0 is reserved for these two.
ACTIONABLE_STATUSES: frozenset[str] = frozenset({"up-to-date", "update-available"})

#: Two fault codes that are NOT ``UpdateStatus`` values, and say so in their
#: names. The status vocabulary describes the CHANNEL; these describe the
#: ENGINE that was asked about it, and collapsing them into a channel fault
#: would hide which of the two components to go and look at.
STATUS_ENGINE_UNREACHABLE: str = "engine-unreachable"
STATUS_ENGINE_MALFORMED: str = "engine-response-malformed"

#: Exit codes shared by every surface that speaks to the updater, so a caller
#: branching on them gets the same answer from the CLI, the MCP tool and the
#: module entry point.
EXIT_APPLIED: int = 0
EXIT_NOT_APPLIED: int = 2
EXIT_APPLY_FAILED: int = 3

#: How long the shell has to report the order done, counted from the moment it
#: is posted. The status poll that waits for that word had no bound at all,
#: which let a shell that never answered hang the caller forever.
APPLY_TIMEOUT_S: float = 600.0



class UpdateCheckError(RuntimeError):
    """The channel could not answer. Always names why."""

    def __init__(self, status_: UpdateStatus, message: str) -> None:
        super().__init__(message)
        self.status: UpdateStatus = status_
        self.message: str = message


class UpdateApplyAccepted(BaseModel):
    accepted: bool = True
    available_version: str
    command_id: str


class UpdateApplyRefused(BaseModel):
    status: UpdateStatus
    detail: str


class UpdateApplyStatusOut(BaseModel):
    command_id: str
    state: Literal["pending", "claimed", "succeeded", "failed"]
    outcome: Literal["installed", "no-update", "refused"] | None = None
    error: str | None = None
    enqueued_at_utc: str
    claimed_at_utc: str | None = None
    completed_at_utc: str | None = None


class UpdateCheckOut(BaseModel):
    """What the UI and any agent read.

    ``applies_via`` is stated because the answer and the action come from
    different components: this route answers, and the Tauri updater inside
    the desktop shell is what can act. A browser tab can learn an update
    exists and cannot install it, and saying so beats a dead button.
    """

    status: UpdateStatus
    endpoint: str
    platform_key: str
    #: The semver the running app reports -- the number the updater compares.
    current_version: str | None = None
    #: The semver the channel is offering, when a manifest was read.
    available_version: str | None = None
    #: Identity of the RUNNING build, from the payload manifest. The field
    #: that actually distinguishes two builds carrying one semver.
    current_git_sha: str | None = None
    current_built_at_utc: str | None = None
    #: Release metadata, when the manifest carried it.
    published_at: str | None = None
    notes: str | None = None
    #: Set when the channel offers the same semver this build already is. The
    #: updater will not act; a human may still be on a different commit.
    same_version_different_build: bool = False
    #: Present on every non-answered status. Never empty when set.
    detail: str | None = None
    applies_via: str = (
        "the desktop shell's Tauri updater; this endpoint only reports"
    )


# ----- platform -----------------------------------------------------------
def platform_key(system: str | None = None, machine: str | None = None) -> str:
    """This machine's Tauri platform key, or a fault naming what it saw.

    Refuses rather than guessing: a wrong key would read a manifest entry
    built for a different architecture, and on macOS that is the difference
    between an arm64 and an x86_64 bundle.
    """
    system_name = platform.system() if system is None else system
    machine_name = platform.machine() if machine is None else machine
    os_key = _OS_KEYS.get(system_name)
    arch_key = _ARCH_KEYS.get(machine_name.lower())
    if os_key is None or arch_key is None:
        raise UpdateCheckError(
            "platform-unsupported",
            f"this machine reports system={system_name!r} machine="
            f"{machine_name!r}, which does not map to a Tauri platform key",
        )
    return f"{os_key}-{arch_key}"


# ----- semver -------------------------------------------------------------
def parse_version(raw: str, source: str) -> _Version:
    """Strict SemVer precedence, or the existing named malformed-version fault."""
    try:
        return _parse_semver(raw)
    except ValueError as exc:
        raise UpdateCheckError(
            "manifest-malformed",
            f"{source} is {raw!r}, which is not a semver version, so this "
            "build cannot be ordered against the channel",
        ) from exc


def compare_versions(current: str, available: str) -> UpdateStatus:
    """Which way round the running build and the channel sit.

    Mirrors what ``tauri-plugin-updater`` will do with the same two strings:
    strictly greater means an update. Equal is up to date. A running build
    NEWER than the channel is its own answer -- that is a developer build, and
    calling it "up to date" would hide the fact that the channel is behind.
    """
    here = parse_version(current, "the running app's version")
    there = parse_version(available, "the channel manifest's 'version'")
    if there > here:
        return "update-available"
    if there == here:
        return "up-to-date"
    return "ahead-of-channel"


# ----- manifest -----------------------------------------------------------
def _fetch_manifest(endpoint: str, client: httpx.Client) -> dict[str, object]:
    try:
        response = client.get(
            endpoint,
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=True,
            headers={"accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise UpdateCheckError(
            "endpoint-unreachable",
            f"{endpoint} could not be reached: {exc}",
        ) from exc
    if response.status_code != 200:
        hint = ""
        if response.status_code == 404:
            hint = " See docs/auto-update.md for the public release host."
        raise UpdateCheckError(
            "endpoint-refused",
            f"{endpoint} answered HTTP {response.status_code}.{hint}",
        )
    try:
        manifest = response.json()
    except ValueError as exc:
        raise UpdateCheckError(
            "manifest-malformed",
            f"{endpoint} answered 200 with a body that is not JSON: {exc}",
        ) from exc
    if not isinstance(manifest, dict):
        raise UpdateCheckError(
            "manifest-malformed",
            f"{endpoint} answered 200 with a JSON {type(manifest).__name__}, "
            "expected an object",
        )
    return manifest


def _platform_entry(manifest: dict[str, object], key: str) -> dict[str, object]:
    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict):
        raise UpdateCheckError(
            "manifest-malformed",
            "the channel manifest has no 'platforms' object, so it was not "
            "written by the Tauri bundler",
        )
    entry = platforms.get(key)
    if not isinstance(entry, dict):
        raise UpdateCheckError(
            "platform-unsupported",
            f"the channel manifest carries no '{key}' build. It offers: "
            f"{sorted(platforms)}",
        )
    if not isinstance(entry.get("url"), str) or entry["url"] == "":
        raise UpdateCheckError(
            "manifest-malformed",
            f"the '{key}' entry has no download url",
        )
    if not isinstance(entry.get("signature"), str) or entry["signature"] == "":
        raise UpdateCheckError(
            "manifest-malformed",
            f"the '{key}' entry has no signature. An unsigned entry would be "
            "refused by the updater at install time, so it is refused here.",
        )
    return entry


def _manifest_version(manifest: dict[str, object]) -> str:
    raw_version = manifest.get("version")
    if not isinstance(raw_version, str):
        raise UpdateCheckError(
            "manifest-malformed",
            "the channel manifest has no string 'version' field",
        )
    return raw_version


# ----- resolution ---------------------------------------------------------
def resolve_update_check(
    identity: BuildIdentity,
    *,
    client: httpx.Client,
    endpoint: str = UPDATE_ENDPOINT,
    key: str | None = None,
) -> UpdateCheckOut:
    """Answer the question, or say precisely why it could not be answered.

    Takes the resolved build identity rather than re-reading it: the running
    build's version must be the one ``/build-info`` already published, or the
    two readouts on the same screen could disagree.
    """
    try:
        resolved_key = platform_key() if key is None else key
    except UpdateCheckError as exc:
        return UpdateCheckOut(
            status=exc.status,
            endpoint=endpoint,
            platform_key="?",
            detail=exc.message,
        )

    if identity.info is None:
        return UpdateCheckOut(
            status="identity-unavailable",
            endpoint=endpoint,
            platform_key=resolved_key,
            detail=(
                "this build cannot state its own identity, so it cannot be "
                f"compared against the channel: {identity.failure}"
            ),
        )

    info = identity.info
    # app_version is the number the updater compares. A payload built before
    # that field existed has None, and guessing it would mean comparing a
    # made-up version against a real one.
    current_version = info.app_version
    base = UpdateCheckOut(
        status="up-to-date",
        endpoint=endpoint,
        platform_key=resolved_key,
        current_version=current_version,
        current_git_sha=info.git_sha,
        current_built_at_utc=info.built_at_utc,
    )
    if current_version is None or current_version.strip() == "":
        return base.model_copy(
            update={
                "status": "identity-unavailable",
                "detail": (
                    "this build's manifest carries no app_version, so there is "
                    "no number to compare against the channel"
                ),
            }
        )

    try:
        manifest = _fetch_manifest(endpoint, client)
        entry = _platform_entry(manifest, resolved_key)
        raw_version = _manifest_version(manifest)
        verdict = compare_versions(current_version, raw_version)
    except UpdateCheckError as exc:
        return base.model_copy(
            update={"status": exc.status, "detail": exc.message}
        )

    notes = manifest.get("notes")
    published = manifest.get("pub_date")
    # The case the semver rule cannot see: same number, different build. The
    # updater will not act on it, and the reader is told rather than reassured.
    same_build_drift = verdict == "up-to-date" and _mentions_other_sha(
        notes, info.git_sha_full, info.git_sha
    )
    return base.model_copy(
        update={
            "status": verdict,
            "available_version": raw_version,
            "notes": notes if isinstance(notes, str) else None,
            "published_at": published if isinstance(published, str) else None,
            "same_version_different_build": same_build_drift,
            "detail": (
                f"the channel offers {raw_version} for {resolved_key} at "
                f"{entry['url']}"
                if verdict == "update-available"
                else None
            ),
        }
    )


def _mentions_other_sha(
    notes: object, git_sha_full: str, git_sha: str
) -> bool:
    """Does the release name a build that is not the one running?

    Deliberately conservative. Release notes are free text, so the only
    confident reading is "notes exist, and they do NOT contain this build's
    sha". Absent notes prove nothing and report nothing.
    """
    if not isinstance(notes, str) or notes.strip() == "":
        return False
    return git_sha_full not in notes and git_sha not in notes


# ----- route --------------------------------------------------------------
def _update_check_http_response(
    identity: BuildIdentity, result: UpdateCheckOut
) -> UpdateCheckOut | JSONResponse:
    """Map a resolver verdict to the HTTP contract this build source carries.

    Installed payloads answer 502 on a channel fault so a caller that only
    reads the status code cannot treat an outage as reassurance. Developer
    checkouts answer 200 with the named fault in the body: the UI and agents
    branch on ``status``, and e2e surfaces treat any 502 from this route as a
    defect even when the channel is genuinely unpublished.
    """
    if result.status in ANSWERED:
        return result
    info = identity.info
    if info is not None and info.source == "repo":
        return result
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={
            "error": CODE_UPDATE_CHECK_FAILED,
            "message": result.detail,
            **result.model_dump(),
        },
    )


def add_update_check_route(
    app: FastAPI, *, endpoint: str = UPDATE_ENDPOINT
) -> None:
    """Mount the check. Resolved per request: the channel moves, the build does not."""

    @app.get(
        UPDATE_CHECK_PATH,
        response_model=UpdateCheckOut,
        tags=["health"],
        name="update_check",
        responses={
            status.HTTP_502_BAD_GATEWAY: {
                "description": "the update channel could not be read"
            }
        },
    )
    def update_check() -> UpdateCheckOut | JSONResponse:
        identity: BuildIdentity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
        with httpx.Client() as client:
            result = resolve_update_check(identity, client=client, endpoint=endpoint)
        return _update_check_http_response(identity, result)


def add_update_apply_route(
    app: FastAPI, *, endpoint: str = UPDATE_ENDPOINT
) -> None:
    """Mount apply: enqueue shell work when the channel reports update-available."""

    @app.post(
        UPDATE_APPLY_PATH,
        response_model=UpdateApplyAccepted,
        tags=["health"],
        name="update_apply",
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            status.HTTP_409_CONFLICT: {
                "description": "apply refused; no shell command enqueued",
                "model": UpdateApplyRefused,
            }
        },
    )
    def update_apply(request: Request) -> UpdateApplyAccepted | JSONResponse:
        identity: BuildIdentity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
        info = identity.info
        if info is None or info.source != "payload":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status="identity-unavailable",
                    detail="not a packaged build; only installed payloads may apply updates",
                ).model_dump(),
            )
        with httpx.Client() as client:
            result = resolve_update_check(identity, client=client, endpoint=endpoint)
        if result.status != "update-available":
            detail = result.detail or f"update check reported {result.status}"
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status=result.status,
                    detail=detail,
                ).model_dump(),
            )
        available_version = result.available_version
        if available_version is None or available_version.strip() == "":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status=result.status,
                    detail="update-available without available_version",
                ).model_dump(),
            )
        broker = shell_broker(request)
        try:
            command_id = broker.enqueue_apply(available_version)
        except ShellCommandConflictError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=UpdateApplyRefused(
                    status="update-available",
                    detail=f"apply-update already pending or claimed (command_id={error.command_id})",
                ).model_dump(),
            )
        return UpdateApplyAccepted(
            available_version=available_version,
            command_id=command_id,
        )

    @app.get(
        UPDATE_APPLY_STATUS_PATH,
        response_model=UpdateApplyStatusOut,
        tags=["health"],
        name="update_apply_status",
    )
    def update_apply_status(request: Request, command_id: str) -> UpdateApplyStatusOut:
        status_payload = shell_broker(request).get_status(command_id)
        if status_payload is None:
            raise HTTPException(status_code=404, detail="unknown apply command")
        return UpdateApplyStatusOut(**status_payload)


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
            f"{url} answered HTTP {response.status_code} with no 'status' field: "
            f"{json.dumps(document)[:500]}",
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
        relaunch_deadline = time.monotonic() + RELAUNCH_BUILD_INFO_TIMEOUT_S
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
            detail=(
                f"{url} answered HTTP {response.status_code}: {response.text[:500]}"
            ),
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
            detail=refusal.get("detail")
            or f"{apply_url} answered HTTP {response.status_code}: {response.text[:500]}",
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
    client: httpx.Client, base: str, command_id: str, deadline: float, timeout_s: float
) -> _InstallWait | ApplyOutcome:
    """Wait for the shell to report the order done, bounded by the deadline.

    Returns what the shell said, or the outcome that says why the wait ended.
    The shell is the only component that may install an update, so this is the
    only evidence that an install was even attempted.
    """
    status_url = f"{base}{UPDATE_APPLY_STATUS_PATH.format(command_id=command_id)}"
    seen_claimed = False
    while True:
        if time.monotonic() >= deadline:
            return replace(
                _not_applied(
                    "install-timeout",
                    status="update-available",
                    detail=(
                        f"the shell did not report {command_id} installed within "
                        f"{timeout_s:g}s"
                    ),
                ),
                code=EXIT_APPLY_FAILED,
            )
        try:
            response = client.get(status_url, timeout=REQUEST_TIMEOUT_S)
        except httpx.HTTPError as exc:
            if seen_claimed:
                return _InstallWait(relaunch_detected=True)
            return _not_applied(
                "install-status-unreachable", detail=f"{status_url}: {exc}"
            )
        if response.status_code == 404 and seen_claimed:
            return _InstallWait(relaunch_detected=True)
        status_body = _json_object(response)
        if status_body is None:
            return _not_applied(
                "install-status-malformed",
                detail=(
                    f"{status_url} answered HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                ),
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
        time.sleep(APPLY_STATUS_POLL_INTERVAL_S)


def _await_relaunch(
    client: httpx.Client,
    base: str,
    before: dict[str, str | None],
    lock_path: Path | None,
    deadline: float,
) -> dict[str, str | None]:
    """Poll build-info until the running identity moves, or the deadline passes.

    The origin is re-resolved every pass: the relaunched app writes its own
    lock, and it may well bind a different port than the one it replaced.
    """
    after = before
    backoff = 1.0
    while time.monotonic() < deadline:
        poll_base = base
        try:
            poll_base = resolve_origin(lock_path).base_url
        except EngineNotRunning:
            poll_base = base
        try:
            response = client.get(
                f"{poll_base}{BUILD_INFO_PATH}", timeout=REQUEST_TIMEOUT_S
            )
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code == 200:
            after = _build_identity_slice(response.json())
            if _version_higher(before, after):
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


# ----- CLI ----------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """``python -m apps.engine_core.update_channel check``.

    The agent-native half of the UI's check button: same resolver, same
    endpoint, JSON on stdout, and a NON-ZERO exit when the channel could not
    be read so a script cannot mistake an outage for "up to date".
    """
    parser = argparse.ArgumentParser(
        prog="python -m apps.engine_core.update_channel",
        description="Ask the Open DJ update channel what it is offering.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="compare this build against the channel")
    check.add_argument(
        "--endpoint",
        default=UPDATE_ENDPOINT,
        help=f"manifest URL to read (default: {UPDATE_ENDPOINT})",
    )
    check.add_argument(
        "--engine",
        default=None,
        help=(
            "origin of a RUNNING engine to ask instead of resolving locally, "
            "e.g. http://127.0.0.1:8685. Use this to check the build a "
            "packaged app is actually running."
        ),
    )
    apply = sub.add_parser(
        "apply",
        help="request install-and-restart on a running packaged engine",
    )
    apply.add_argument(
        "--engine",
        required=True,
        help="origin of the RUNNING engine, e.g. http://127.0.0.1:8685",
    )
    apply.add_argument(
        "--timeout-s",
        type=float,
        default=APPLY_TIMEOUT_S,
        help=(
            "seconds to wait for the shell to install and the app to relaunch "
            f"(default: {APPLY_TIMEOUT_S:g})"
        ),
    )
    args = parser.parse_args(argv)

    if args.command == "check":
        if args.engine is not None:
            return _check_via_engine(args.engine)
        return _check_locally(args.endpoint)
    if args.command == "apply":
        return _apply_via_engine(args.engine, args.timeout_s)
    raise AssertionError(f"unknown command: {args.command}")


def _check_locally(endpoint: str) -> int:
    try:
        info = resolve_build_info(dict(os.environ), platform_paths.PROJECT_ROOT)
        identity = BuildIdentity(info=info, failure=None)
    except BuildInfoUnavailable as exc:
        identity = BuildIdentity(info=None, failure=str(exc))
    with httpx.Client() as client:
        result = resolve_update_check(identity, client=client, endpoint=endpoint)
    return _emit(result)


def _check_via_engine(origin: str) -> int:
    """Print the running engine's answer, and exit 0 only for an actionable one."""
    outcome = check_via_engine(origin)
    print(json.dumps(outcome.document, indent=2))
    if not outcome.actionable:
        print(f"[ERROR] {outcome.status}: {outcome.detail}", file=sys.stderr)
        return EXIT_NOT_APPLIED
    return EXIT_APPLIED


def _emit(result: UpdateCheckOut) -> int:
    print(json.dumps(result.model_dump(), indent=2))
    if result.status not in ANSWERED:
        print(f"[ERROR] {result.status}: {result.detail}", file=sys.stderr)
        return 2
    if result.status == "update-available":
        print(
            f"[UPDATE] {result.current_version} -> {result.available_version}",
            file=sys.stderr,
        )
    return 0


def _build_identity_slice(body: dict[str, object]) -> dict[str, str | None]:
    app_version = body.get("app_version")
    git_sha_full = body.get("git_sha_full")
    return {
        "app_version": app_version if isinstance(app_version, str) else None,
        "git_sha_full": git_sha_full if isinstance(git_sha_full, str) else None,
    }


def _version_higher(
    before: dict[str, str | None], after: dict[str, str | None]
) -> bool:
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


def _apply_via_engine(origin: str, timeout_s: float = APPLY_TIMEOUT_S) -> int:
    """Print what one apply found, and return its exit code.

    A renderer over :func:`apply_via_engine`, not a second implementation:
    the packaged CLI prints the SAME outcome the same way, so the two cannot
    disagree about whether an install landed.
    """
    outcome = apply_via_engine(origin, timeout_s, progress=_stderr_progress)
    _emit_apply(outcome)
    return outcome.code


def _stderr_progress(message: str) -> None:
    print(f"[APPLY] {message}", file=sys.stderr)


def _emit_apply(outcome: ApplyOutcome) -> None:
    if outcome.check:
        print(json.dumps(outcome.check, indent=2))
    if outcome.shell_status is not None:
        print(json.dumps(outcome.shell_status, indent=2))
    if outcome.before or outcome.after:
        print(
            json.dumps(
                {
                    "before": outcome.before,
                    "after": outcome.after,
                    "changed": outcome.applied,
                    "reason": outcome.reason,
                },
                indent=2,
            )
        )
    if outcome.code != EXIT_APPLIED:
        print(f"[ERROR] {outcome.reason}: {outcome.detail}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
