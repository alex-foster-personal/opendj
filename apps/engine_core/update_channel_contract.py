"""Constants, status vocabulary and wire models of the update channel.

Part of the update channel split out of one oversized module (#9688). Import
from ``apps.engine_core.update_channel``, the public facade, not from here.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Literal

from pydantic import BaseModel

#: THE update feed directory. One string, read by this module and compiled
#: into the Electron shell as ``UPDATE_FEED_URL`` in
#: ``apps/desktop/electron/src/updater.ts``; a test asserts the two are
#: identical. One release publishes both feeds into it: ``latest-mac.yml``
#: (electron-updater) and ``latest.json`` (the Tauri updater, which carries the
#: Electron app as the bridge for alpha installs).
#:
#: GitHub Releases is the standard host for both updaters and needs no server
#: to operate. The product repository is private, so its releases are
#: deliberately not used here: unauthenticated clients receive a 404.
#: ``issue-assets`` is public and holds only signed release artifacts and their
#: manifests. See docs/auto-update.md.
UPDATE_FEED_URL: str = "https://github.com/alex-foster-personal/issue-assets/releases/latest/download"

#: The two feed files one release writes into that directory
#: (``scripts/electron_release.py``), naming one version and one sha. Both
#: formats are parsed; which one is read follows the endpoint's suffix.
ELECTRON_FEED_FILE: str = "latest-mac.yml"
TAURI_FEED_FILE: str = "latest.json"

#: The file this check reads: ``latest-mac.yml``, the feed that survives
#: deleting the Tauri shell. It was ``latest.json`` until the first Electron
#: release (1.0.0-alpha.2, Thu 5 Nov 2026) published ``latest-mac.yml``; it was
#: a 404 before that, and the e2e gate fetches whatever this names.
#: (private path) section (d).
CHECKED_FEED_FILE: str = ELECTRON_FEED_FILE
UPDATE_ENDPOINT: str = f"{UPDATE_FEED_URL}/{CHECKED_FEED_FILE}"

#: Replaces ``UPDATE_FEED_URL`` for this engine and the Electron shell alike,
#: so a private update-install proof reads one local feed. The shell's
#: updater still only installs a bundle signed by this team, never an older one.
FEED_URL_ENV: str = "OPENDJ_UPDATE_FEED_URL"

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
ANSWERED: frozenset[str] = frozenset({"update-available", "up-to-date", "ahead-of-channel"})

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
    #: ``cancelled`` is an install withdrawn during its countdown (INSTALL-43);
    #: only the engine writes it, never the shell.
    outcome: Literal["installed", "no-update", "refused", "cancelled"] | None = None
    error: str | None = None
    enqueued_at_utc: str
    claimed_at_utc: str | None = None
    completed_at_utc: str | None = None
    #: INSTALL-43: when the held-back install becomes claimable, and how long
    #: is left (null once claimed, or for an order with no countdown).
    countdown_ends_at_utc: str | None = None
    countdown_remaining_s: float | None = None


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
    applies_via: str = "the desktop shell's Tauri updater; this endpoint only reports"


# ----- facade -------------------------------------------------------------
#: The module every caller imports and every test patches. The update channel
#: is split across update_channel_* siblings; the few names tests replace
#: (platform_key, resolve_origin, APPLY_STATUS_POLL_INTERVAL_S,
#: RELAUNCH_BUILD_INFO_TIMEOUT_S) are read through this module per call, so a
#: patch on it reaches the code that moved out, exactly as it did before.
FACADE_MODULE: str = "apps.engine_core.update_channel"


def facade() -> ModuleType:
    """The public update-channel module, resolved at call time.

    ``import_module`` rather than ``sys.modules``: under
    ``python -m apps.engine_core.update_channel`` the running copy is
    ``__main__``, and this loads the importable copy on first use.
    """
    return importlib.import_module(FACADE_MODULE)
