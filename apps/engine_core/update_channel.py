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
import re
import sys
from dataclasses import dataclass
from typing import Literal

import httpx
from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.build_info import (
    BUILD_IDENTITY_STATE_ATTR,
    BuildIdentity,
    BuildInfoUnavailable,
    resolve_build_info,
)
from apps.shared import platform_paths

#: THE update endpoint. One string, read by this module and compiled into the
#: desktop shell via ``plugins.updater.endpoints`` in tauri.conf.json; a test
#: asserts the two are identical.
#:
#: GitHub Releases is the standard host for a Tauri updater and needs no
#: server to operate. NOTE THE CONSEQUENCE: this repository is PRIVATE, and
#: GitHub serves release assets of a private repo only to authenticated
#: callers, answering 404 to everyone else. Until the repo is public (or
#: releases move to a public host) this endpoint resolves to a 404, which this
#: module reports as ``endpoint-refused`` rather than concealing. See
#: docs/auto-update.md.
UPDATE_ENDPOINT: str = (
    "https://github.com/maintainer/music-dj-tools"
    "/releases/latest/download/latest.json"
)

UPDATE_CHECK_PATH: str = "/api/v1/update/check"

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

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)

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


class UpdateCheckError(RuntimeError):
    """The channel could not answer. Always names why."""

    def __init__(self, status_: UpdateStatus, message: str) -> None:
        super().__init__(message)
        self.status: UpdateStatus = status_
        self.message: str = message


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
@dataclass(frozen=True, order=True)
class _Version:
    major: int
    minor: int
    patch: int


def parse_version(raw: str, source: str) -> _Version:
    """Strict semver, or a fault naming the string and where it came from.

    A leading ``v`` is accepted because git tags carry one and release
    manifests are written by hand often enough that refusing it would be
    pedantry rather than safety. Anything else unparseable is refused: a
    version this code cannot order is a version it must not silently treat
    as older or newer.
    """
    candidate = raw.strip()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    matched = _SEMVER.match(candidate)
    if matched is None:
        raise UpdateCheckError(
            "manifest-malformed",
            f"{source} is {raw!r}, which is not a semver version, so this "
            "build cannot be ordered against the channel",
        )
    return _Version(
        int(matched["major"]), int(matched["minor"]), int(matched["patch"])
    )


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
    elif there == here:
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
            hint = (
                " A 404 here is what a PRIVATE GitHub repository returns for "
                "release assets to an unauthenticated caller, which is the "
                "expected state of this channel until the repo or its "
                "releases are published. See docs/auto-update.md."
            )
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
        raw_version = manifest.get("version")
        if not isinstance(raw_version, str):
            raise UpdateCheckError(
                "manifest-malformed",
                "the channel manifest has no string 'version' field",
            )
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
        if result.status in ANSWERED:
            return result
        # A fault is an HTTP fault. Returning 200 with a sad field is how a
        # broken channel gets rendered as a working one by the next caller
        # who only checks the status code.
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": CODE_UPDATE_CHECK_FAILED,
                "message": result.detail,
                **result.model_dump(),
            },
        )


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
    args = parser.parse_args(argv)

    if args.engine is not None:
        return _check_via_engine(args.engine)
    return _check_locally(args.endpoint)


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
    url = f"{origin.rstrip('/')}{UPDATE_CHECK_PATH}"
    try:
        with httpx.Client() as client:
            response = client.get(url, timeout=REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        print(
            json.dumps(
                {"status": "endpoint-unreachable", "detail": f"{url}: {exc}"},
                indent=2,
            )
        )
        return 2
    body = response.json()
    print(json.dumps(body, indent=2))
    return 0 if body.get("status") in ANSWERED else 2


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


if __name__ == "__main__":
    raise SystemExit(main())
