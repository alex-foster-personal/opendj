"""Restart to update: a local build an agent flags as ready (INSTALL-39, INSTALL-40).

WHY THIS EXISTS BESIDE THE RELEASE CHANNEL

:mod:`apps.engine_core.update_channel` asks a PUBLISHED manifest whether a
newer release exists. A personal build made on this Mac (``ship_dmg.sh
--local-fast``) is never published: until now it was installed by quitting the
app, swapping ``/Applications/Open DJ.app`` and reopening it, which tears down
a live session and skips the updater's signature check entirely.

Every build already produces the updater's own artifact, ``<App>.app.tar.gz``
plus its minisign ``.sig``. So the standard flow (Sparkle, Tauri, VS Code:
"an update is ready, restart to apply it") needs only a way to SAY a build is
ready, and a manifest the shell's updater can read:

1. ``POST /api/v1/app-update/ready`` -- an agent names the artifact. This
   module checks the archive and its signature file exist, reads the build
   stamp out of the archive itself, and records one pending update in the
   engine's data dir. Nothing is installed.
2. ``GET /api/v1/app-update/status`` -- the UI's "Restart to update" nudge and
   the agent read the same document.
3. ``POST /api/v1/app-update/install`` -- re-checks the archive is the one
   that was flagged (sha256), then orders the desktop shell to install it over
   the shell command bus (ADR-0050), exactly as release apply does.
4. The shell's ``install_local_update`` command points ``tauri-plugin-updater``
   at ``GET /api/v1/app-update/manifest.json``, which hands it the archive
   URL and the ``.sig`` text. The PLUGIN downloads, verifies the signature
   against the pubkey compiled into the shell, and swaps the bundle. Nothing
   here verifies or installs anything: verification stays inseparable from
   installation, the rule docs/auto-update.md sets for the release path.

The record is a file, not memory, so the nudge survives an engine restart and
the relaunched build can see that the update it was installed from is the one
now running (``installed``).

Requirements (mini-PRD):

- ✔︎ ✅ 🎯 ``ready`` refuses an artifact that is missing, not a gzip
  ``.app.tar.gz``, has no non-empty ``.sig`` beside it, or has no payload
  build stamp, naming which. -> :func:`inspect_artifact`
- ✔︎ ✅ 🎯 ``ready`` refuses the build that is already running. -> route
- ✔︎ ✅ 🎯 ``status`` is ``none``, ``pending``, ``installed`` or
  ``artifact-changed``; only ``pending`` shows the nudge. -> :func:`resolve_status`
- ✔︎ ✅ 🎯 ``install`` refuses unless pending, refuses an archive whose sha256
  moved since it was flagged, and otherwise enqueues exactly one shell command.
- ✔︎ ✅ 🎯 INSTALL-43: that command is held back from the shell for
  :data:`INSTALL_COUNTDOWN_S`; ``status.install`` carries the countdown the
  app shows, and ``POST cancel`` (Esc Space x4, ``opendj update cancel``, MCP
  ``app_update_cancel``) withdraws it until the shell claims it.

Acceptance tests (tests/engine_core/test_local_update.py):

- [if] the ``.sig`` beside the archive is missing [then] ready is refused with
  ``signature-missing`` and nothing is recorded, else ⛔️
- [if] the flagged build's git sha is the running one [then] status reports
  ``installed`` and no nudge, else ⛔️
- [if] the archive is rebuilt after it was flagged [then] install is refused
  with ``artifact-changed`` and no shell command is enqueued, else ⛔️
- [if] the flagged build was stamped against the running build and does not
  descend from it [then] status and install carry "NOT DESCENDED FROM YOUR
  LAST BUILD: missing <n> commits from <line>" (INSTALL-44), else ⛔️
- [if] install was accepted [then] the shell's claim returns nothing until the
  countdown ends, else ⛔️
- [if] cancel lands during the countdown [then] the order reads ``failed`` +
  ``cancelled`` and the shell never receives it, else ⛔️
- [if] cancel arrives after the shell claimed the order [then] it is refused
  with ``already-installing``, else ⛔️
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import tarfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from apps.engine_core.build_info import BUILD_IDENTITY_STATE_ATTR, BuildIdentity, LineageOut
from apps.engine_core.build_lineage import not_descended_warning
from apps.engine_core.update_channel import UpdateApplyStatusOut, platform_key
from apps.webui.server.shell_commands import (
    INSTALL_LOCAL_UPDATE_TYPE,
    ShellCommandConflictError,
    shell_broker,
)

APP_UPDATE_PREFIX: str = "/api/v1/app-update"
READY_PATH: str = f"{APP_UPDATE_PREFIX}/ready"
STATUS_PATH: str = f"{APP_UPDATE_PREFIX}/status"
INSTALL_PATH: str = f"{APP_UPDATE_PREFIX}/install"
INSTALL_STATUS_PATH: str = f"{APP_UPDATE_PREFIX}/install/{{command_id}}"
CANCEL_PATH: str = f"{APP_UPDATE_PREFIX}/cancel"
MANIFEST_PATH: str = f"{APP_UPDATE_PREFIX}/manifest.json"
ARTIFACT_PATH: str = f"{APP_UPDATE_PREFIX}/artifact"

#: INSTALL-43 (the maintainer, Tue 3 Nov 2026): an install starts after this countdown,
#: even mid-play; the app shows it and the only in-app cancel is Esc Space x4.
INSTALL_COUNTDOWN_S: float = 10.0
CANCEL_COMBO_LABEL: str = "Esc Space x4"

#: The one record, in the engine's data dir.
PENDING_FILE_NAME: str = "app-update-pending.json"
ARCHIVE_SUFFIX: str = ".app.tar.gz"
SIGNATURE_SUFFIX: str = ".sig"
#: Where the build stamp lives inside the bundle (scripts/build_engine_payload.py).
PAYLOAD_MANIFEST_IN_BUNDLE: str = "Contents/Resources/payload/manifest.json"
_GZIP_MAGIC: bytes = b"\x1f\x8b"
_SHA_FULL = re.compile(r"^[0-9a-f]{40}$")
_HASH_CHUNK: int = 1 << 20
#: The loopback hosts the shell will accept a manifest from (local_update.rs).
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

AppUpdateState = Literal["none", "pending", "installed", "artifact-changed"]


class ArtifactRefused(ValueError):
    """The artifact cannot be flagged. ``code`` is what a caller branches on."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PendingUpdate:
    """One flagged build: where it is, what it is, and what proves it unchanged."""

    artifact: str
    signature: str
    sha256: str
    size_bytes: int
    mtime_ns: int
    app_version: str
    git_sha_full: str
    git_sha: str
    built_at_utc: str
    release_channel: str | None
    notes: str | None
    flagged_at_utc: str
    #: INSTALL-44: the archive's ``identity.lineage`` stamp, or None for a
    #: build from before it (the default keeps an older record readable).
    lineage: dict[str, Any] | None = None


DescentVerdict = Literal["descended", "not-descended", "unverified", "unknown"]


class DescentOut(BaseModel):
    """Does the flagged build contain the one running now? (INSTALL-44)

    ``descended`` / ``not-descended`` only when the build was stamped against
    the very build now running; ``unverified`` when it was stamped against
    another (or none); ``unknown`` when it carries no lineage stamp at all.
    """

    verdict: DescentVerdict
    running_git_sha_full: str | None
    previous_git_sha_full: str | None
    missing_commits: int | None
    missing_from: str | None
    warning: str | None


class AppUpdateReadyIn(BaseModel):
    artifact: str
    notes: str | None = None


class PendingBuildOut(BaseModel):
    artifact: str
    app_version: str
    git_sha_full: str
    git_sha: str
    built_at_utc: str
    release_channel: str | None
    notes: str | None
    flagged_at_utc: str
    size_bytes: int
    sha256: str
    lineage: LineageOut | None = None
    descent: DescentOut | None = None


class AppUpdateStatusOut(BaseModel):
    state: AppUpdateState
    pending: PendingBuildOut | None
    running_git_sha_full: str | None
    running_app_version: str | None
    detail: str | None
    #: The newest local install order (INSTALL-43): ``pending`` with
    #: ``countdown_remaining_s`` while the in-app countdown runs, ``failed`` +
    #: ``cancelled`` once withdrawn. Null before any install this engine life.
    install: UpdateApplyStatusOut | None = None


class AppUpdateCancelIn(BaseModel):
    #: Who cancelled, kept in the order's error text: "Esc Space x4", "opendj update cancel".
    reason: str | None = None


class AppUpdateCancelOut(BaseModel):
    cancelled: bool
    command_id: str
    git_sha_full: str | None
    install: UpdateApplyStatusOut


class AppUpdateRefusedOut(BaseModel):
    error: str
    detail: str


class AppUpdateInstallAccepted(BaseModel):
    command_id: str
    git_sha_full: str
    app_version: str
    manifest_url: str
    #: INSTALL-43: the shell takes the order only after this many seconds.
    countdown_s: float
    countdown_ends_at_utc: str | None
    #: INSTALL-44: "NOT DESCENDED FROM YOUR LAST BUILD: ..." or another
    #: descent warning; the install still proceeds (the maintainer's rule), loudly.
    warning: str | None


# ----- the artifact -------------------------------------------------------
def inspect_artifact(path: Path, notes: str | None, *, now_utc: str | None = None) -> PendingUpdate:
    """Check one archive can be offered, and read its build stamp from inside it.

    The stamp comes from the ARCHIVE, not from a sibling ``.app`` or a marker
    file, because the archive is what the shell will install: a stamp read from
    anywhere else could describe a different build than the one installed.
    """
    if not path.is_absolute():
        raise ArtifactRefused("artifact-not-absolute", f"give an absolute path, got {str(path)!r}")
    if not path.name.endswith(ARCHIVE_SUFFIX):
        raise ArtifactRefused(
            "artifact-not-updater-archive",
            f"{path} is not an updater archive; the name must end in {ARCHIVE_SUFFIX}",
        )
    if not path.is_file():
        raise ArtifactRefused("artifact-missing", f"{path} does not exist or is not a file")
    signature_path = path.with_name(path.name + SIGNATURE_SUFFIX)
    if not signature_path.is_file():
        raise ArtifactRefused(
            "signature-missing",
            f"{signature_path} does not exist; the shell refuses an unsigned archive, so it is "
            "not offered (sign it with `cargo tauri signer sign`)",
        )
    signature = signature_path.read_text(encoding="utf-8").strip()
    if signature == "":
        raise ArtifactRefused("signature-missing", f"{signature_path} is empty")
    with path.open("rb") as handle:
        if handle.read(2) != _GZIP_MAGIC:
            raise ArtifactRefused(
                "artifact-not-gzip",
                f"{path} is not gzip; the updater gunzips the download and would refuse it",
            )
    identity = _bundle_identity(path)
    stat = path.stat()
    return PendingUpdate(
        artifact=str(path),
        signature=signature,
        sha256=sha256_of(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        app_version=identity["app_version"],
        git_sha_full=identity["git_sha_full"],
        git_sha=identity["git_sha_full"][:8],
        built_at_utc=identity["built_at_utc"],
        release_channel=identity.get("release_channel") or None,
        notes=notes.strip() if notes and notes.strip() else None,
        flagged_at_utc=now_utc or _utc_now(),
        lineage=identity.get("lineage"),
    )


def _bundle_identity(path: Path) -> dict[str, Any]:
    """The payload manifest's identity block, streamed out of the archive."""
    try:
        with tarfile.open(path, "r|gz") as archive:
            bundle: str | None = None
            for member in archive:
                top = member.name.split("/", 1)[0]
                if bundle is None:
                    if not top.endswith(".app"):
                        raise ArtifactRefused(
                            "artifact-not-app-bundle",
                            f"the archive's top-level entry is {top!r}, not an .app bundle",
                        )
                    bundle = top
                if member.name == f"{bundle}/{PAYLOAD_MANIFEST_IN_BUNDLE}":
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        break
                    manifest = json.loads(extracted.read())
                    return _identity_fields(path, manifest)
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, OSError) as error:
        raise ArtifactRefused("artifact-unreadable", f"{path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ArtifactRefused("build-stamp-unreadable", f"{path}: payload manifest is not JSON: {error}") from error
    raise ArtifactRefused(
        "build-stamp-missing",
        f"{path} has no {PAYLOAD_MANIFEST_IN_BUNDLE}; it is not an Open DJ build with a payload",
    )


def _identity_fields(path: Path, manifest: object) -> dict[str, Any]:
    identity = manifest.get("identity") if isinstance(manifest, dict) else None
    if not isinstance(identity, dict):
        raise ArtifactRefused("build-stamp-unreadable", f"{path}: payload manifest has no identity block")
    fields: dict[str, Any] = {}
    for key in ("app_version", "git_sha_full", "built_at_utc"):
        value = identity.get(key)
        if not isinstance(value, str) or value == "":
            raise ArtifactRefused("build-stamp-unreadable", f"{path}: build stamp has no {key}")
        fields[key] = value
    if not _SHA_FULL.match(fields["git_sha_full"]):
        raise ArtifactRefused(
            "build-stamp-unreadable",
            f"{path}: git_sha_full {fields['git_sha_full']!r} is not a 40-hex commit",
        )
    channel = identity.get("release_channel")
    if isinstance(channel, str):
        fields["release_channel"] = channel
    lineage = identity.get("lineage")
    if isinstance(lineage, dict):
        try:
            fields["lineage"] = LineageOut(**lineage).model_dump()
        except ValueError as error:
            raise ArtifactRefused("build-stamp-unreadable", f"{path}: lineage stamp is malformed: {error}") from error
    return fields


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----- the record ---------------------------------------------------------
class PendingStore:
    """The one pending update, as a JSON file in the engine's data dir."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / PENDING_FILE_NAME

    def read(self) -> PendingUpdate | None:
        if not self.path.exists():
            return None
        try:
            return PendingUpdate(**json.loads(self.path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(
                f"{self.path} is not a pending-update record ({error}); delete it and flag the build again"
            ) from error

    def write(self, pending: PendingUpdate) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_suffix(".tmp")
        staging.write_text(json.dumps(asdict(pending), indent=2, sort_keys=True), encoding="utf-8")
        staging.replace(self.path)


def artifact_unchanged_cheaply(pending: PendingUpdate) -> bool:
    """Size and mtime still match: the per-poll check. Install re-hashes."""
    path = Path(pending.artifact)
    if not path.is_file():
        return False
    stat = path.stat()
    return stat.st_size == pending.size_bytes and stat.st_mtime_ns == pending.mtime_ns


def resolve_status(
    pending: PendingUpdate | None,
    running_sha: str | None,
    running_version: str | None,
    install: UpdateApplyStatusOut | None = None,
) -> AppUpdateStatusOut:
    """One document for the nudge and the agent. Only ``pending`` shows the nudge."""
    if pending is None:
        state: AppUpdateState = "none"
        detail = None
    elif running_sha is not None and pending.git_sha_full == running_sha:
        state = "installed"
        detail = f"the flagged build {pending.git_sha} is the one running"
    elif not artifact_unchanged_cheaply(pending):
        state = "artifact-changed"
        detail = (
            f"{pending.artifact} is gone or was rebuilt after it was flagged; flag the new "
            "build with `opendj update ready <artifact>`"
        )
    else:
        state = "pending"
        detail = f"build {pending.git_sha} is ready; restart to update"
    return AppUpdateStatusOut(
        state=state,
        pending=None if pending is None else PendingBuildOut(**_public_fields(pending, running_sha)),
        running_git_sha_full=running_sha,
        running_app_version=running_version,
        detail=detail,
        install=install,
    )


def descent_from_running(pending: PendingUpdate, running_sha: str | None) -> DescentOut:
    """Would installing ``pending`` keep every commit of the build running now?"""
    lineage = pending.lineage
    if lineage is None:
        return DescentOut(
            verdict="unknown", running_git_sha_full=running_sha, previous_git_sha_full=None,
            missing_commits=None, missing_from=None,
            warning=f"build {pending.git_sha} has no lineage stamp: cannot tell whether it descends from your last build",
        )
    previous = lineage.get("previous_git_sha_full")
    if running_sha is None or previous != running_sha or lineage.get("descends_from_previous") is None:
        built_over = previous[:8] if isinstance(previous, str) else "no running app"
        return DescentOut(
            verdict="unverified", running_git_sha_full=running_sha, previous_git_sha_full=previous,
            missing_commits=None, missing_from=None,
            warning=(
                f"build {pending.git_sha} was made over {built_over}, not the running "
                f"{(running_sha or '?')[:8]}: descent from your last build is unverified"
            ),
        )
    missing = lineage.get("missing_commits")
    missing_from = lineage.get("missing_from")
    if lineage["descends_from_previous"] is True:
        return DescentOut(
            verdict="descended", running_git_sha_full=running_sha, previous_git_sha_full=previous,
            missing_commits=0, missing_from=None, warning=None,
        )
    return DescentOut(
        verdict="not-descended", running_git_sha_full=running_sha, previous_git_sha_full=previous,
        missing_commits=missing, missing_from=missing_from,
        warning=not_descended_warning(int(missing or 0), missing_from),
    )


def _public_fields(pending: PendingUpdate, running_sha: str | None) -> dict[str, Any]:
    fields = asdict(pending)
    fields.pop("signature")
    fields.pop("mtime_ns")
    fields["descent"] = descent_from_running(pending, running_sha)
    return fields


def updater_manifest(pending: PendingUpdate, origin: str) -> dict[str, Any]:
    """The Tauri updater's static manifest for the flagged build.

    The shell's comparator for this path accepts any version, because a local
    build usually keeps the installed semver; WHICH build is installed is
    pinned by the archive and its signature, which the plugin verifies.
    """
    return {
        "version": pending.app_version,
        "notes": f"built from {pending.git_sha_full}" + (f": {pending.notes}" if pending.notes else ""),
        "pub_date": pending.built_at_utc,
        "platforms": {
            platform_key(): {
                "signature": pending.signature,
                "url": f"{origin.rstrip('/')}{ARTIFACT_PATH}?sha256={pending.sha256}",
            }
        },
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ----- routes ---------------------------------------------------------------
def _refused(http_status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content=AppUpdateRefusedOut(error=code, detail=detail).model_dump(),
    )


def _running(app: FastAPI) -> tuple[str | None, str | None, str | None]:
    """(git_sha_full, app_version, source) of the build this engine runs."""
    identity: BuildIdentity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
    info = identity.info
    if info is None:
        return None, None, None
    return info.git_sha_full, info.app_version, info.source


def _loopback_origin(request: Request) -> str | None:
    """This engine's origin as the caller reached it, when that is loopback."""
    host = request.url.hostname or ""
    if host not in LOOPBACK_HOSTS:
        return None
    return f"{request.url.scheme}://{request.url.netloc}"


def _latest_install(request: Request) -> UpdateApplyStatusOut | None:
    broker = shell_broker(request)
    command_id = broker.latest_of_type(INSTALL_LOCAL_UPDATE_TYPE)
    if command_id is None:
        return None
    payload = broker.get_status(command_id)
    return None if payload is None else UpdateApplyStatusOut(**payload)


def add_app_update_routes(
    app: FastAPI, *, data_dir: Path, countdown_s: float = INSTALL_COUNTDOWN_S
) -> None:
    """Mount ready, status, install, cancel, install status, manifest and artifact."""
    if not countdown_s >= 0:
        raise ValueError(f"countdown_s must be zero or more, got {countdown_s!r}")
    store = PendingStore(data_dir)
    _add_record_routes(app, store)
    _add_install_routes(app, store, countdown_s)
    _add_cancel_route(app)
    _add_updater_feed_routes(app, store)


def _add_cancel_route(app: FastAPI) -> None:
    @app.post(
        CANCEL_PATH,
        response_model=AppUpdateCancelOut,
        tags=["health"],
        name="app_update_cancel",
        responses={409: {"model": AppUpdateRefusedOut}},
    )
    def app_update_cancel(request: Request, body: AppUpdateCancelIn | None = None) -> AppUpdateCancelOut | JSONResponse:
        """Withdraw the local install still counting down (INSTALL-43).

        The same action as pressing Esc Space x4 in the app. Refused when no
        install is counting down, or once the shell has started installing.
        """
        broker = shell_broker(request)
        command_id = broker.latest_of_type(INSTALL_LOCAL_UPDATE_TYPE)
        current = None if command_id is None else broker.get_status(command_id)
        if command_id is None or current is None or current["state"] != "pending":
            state = "none" if current is None else f"{current['state']}:{current.get('outcome')}"
            return _refused(409, "not-counting-down", f"no install is counting down (latest: {state})")
        reason = body.reason.strip() if body is not None and body.reason and body.reason.strip() else "no reason given"
        try:
            command = broker.cancel(command_id, f"cancelled: {reason}")
        except RuntimeError as error:
            return _refused(409, "already-installing", str(error))
        sha = command.get("git_sha_full")
        return AppUpdateCancelOut(
            cancelled=True,
            command_id=command_id,
            git_sha_full=sha if isinstance(sha, str) else None,
            install=UpdateApplyStatusOut(**broker.require_status(command_id)),
        )


def _add_record_routes(app: FastAPI, store: PendingStore) -> None:

    @app.post(
        READY_PATH,
        response_model=AppUpdateStatusOut,
        tags=["health"],
        name="app_update_ready",
        responses={422: {"model": AppUpdateRefusedOut}, 409: {"model": AppUpdateRefusedOut}},
    )
    def app_update_ready(body: AppUpdateReadyIn) -> AppUpdateStatusOut | JSONResponse:
        try:
            pending = inspect_artifact(Path(body.artifact), body.notes)
        except ArtifactRefused as refusal:
            return _refused(422, refusal.code, refusal.detail)
        running_sha, running_version, _ = _running(app)
        if running_sha == pending.git_sha_full:
            return _refused(
                409,
                "already-running",
                f"build {pending.git_sha} is the one this app is running; nothing to update",
            )
        store.write(pending)
        return resolve_status(pending, running_sha, running_version)

    @app.get(STATUS_PATH, response_model=AppUpdateStatusOut, tags=["health"], name="app_update_status")
    def app_update_status(request: Request) -> AppUpdateStatusOut:
        running_sha, running_version, _ = _running(app)
        return resolve_status(store.read(), running_sha, running_version, _latest_install(request))



def _add_install_routes(app: FastAPI, store: PendingStore, countdown_s: float) -> None:
    @app.post(
        INSTALL_PATH,
        response_model=AppUpdateInstallAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["health"],
        name="app_update_install",
        responses={409: {"model": AppUpdateRefusedOut}},
    )
    def app_update_install(request: Request) -> AppUpdateInstallAccepted | JSONResponse:
        running_sha, running_version, source = _running(app)
        if source != "payload":
            return _refused(
                409,
                "identity-unavailable",
                "not a packaged build; only an installed app can replace its own bundle",
            )
        pending = store.read()
        current = resolve_status(pending, running_sha, running_version)
        if current.state != "pending" or pending is None:
            return _refused(409, f"not-pending:{current.state}", current.detail or "no update is pending")
        if sha256_of(Path(pending.artifact)) != pending.sha256:
            return _refused(
                409,
                "artifact-changed",
                f"{pending.artifact} no longer matches the sha256 it was flagged with; flag it again",
            )
        origin = _loopback_origin(request)
        if origin is None:
            return _refused(
                409,
                "not-loopback",
                f"install must be requested over loopback, not {request.url.hostname!r}: the shell "
                "only fetches an update manifest from this machine",
            )
        manifest_url = f"{origin}{MANIFEST_PATH}"
        try:
            command_id = shell_broker(request).enqueue_install_local(
                manifest_url, pending.git_sha_full, countdown_s=countdown_s
            )
        except ShellCommandConflictError as error:
            return _refused(
                409,
                "install-in-flight",
                f"an install is already pending or claimed (command_id={error.command_id})",
            )
        return AppUpdateInstallAccepted(
            command_id=command_id,
            git_sha_full=pending.git_sha_full,
            app_version=pending.app_version,
            manifest_url=manifest_url,
            countdown_s=countdown_s,
            countdown_ends_at_utc=shell_broker(request).require_status(command_id)["countdown_ends_at_utc"],
            warning=descent_from_running(pending, running_sha).warning,
        )

    @app.get(INSTALL_STATUS_PATH, response_model=UpdateApplyStatusOut, tags=["health"], name="app_update_install_status")
    def app_update_install_status(request: Request, command_id: str) -> UpdateApplyStatusOut:
        status_payload = shell_broker(request).get_status(command_id)
        if status_payload is None:
            raise HTTPException(status_code=404, detail="unknown install command")
        return UpdateApplyStatusOut(**status_payload)



def _add_updater_feed_routes(app: FastAPI, store: PendingStore) -> None:
    @app.get(MANIFEST_PATH, tags=["health"], name="app_update_manifest")
    def app_update_manifest(request: Request) -> dict[str, Any]:
        pending = _servable(store)
        origin = _loopback_origin(request)
        if origin is None:
            raise HTTPException(status_code=403, detail="the update manifest is served over loopback only")
        return updater_manifest(pending, origin)

    @app.get(ARTIFACT_PATH, tags=["health"], name="app_update_artifact")
    def app_update_artifact(request: Request) -> FileResponse:
        pending = _servable(store)
        if _loopback_origin(request) is None:
            raise HTTPException(status_code=403, detail="the update archive is served over loopback only")
        return FileResponse(pending.artifact, media_type="application/octet-stream")


def _servable(store: PendingStore) -> PendingUpdate:
    pending = store.read()
    if pending is None:
        raise HTTPException(status_code=404, detail="no update is pending")
    if not artifact_unchanged_cheaply(pending):
        raise HTTPException(status_code=409, detail=f"{pending.artifact} changed since it was flagged")
    return pending


__all__ = [
    "ARTIFACT_PATH",
    "CANCEL_PATH",
    "INSTALL_COUNTDOWN_S",
    "INSTALL_PATH",
    "INSTALL_STATUS_PATH",
    "MANIFEST_PATH",
    "READY_PATH",
    "STATUS_PATH",
    "ArtifactRefused",
    "PendingStore",
    "PendingUpdate",
    "add_app_update_routes",
    "inspect_artifact",
    "resolve_status",
    "updater_manifest",
]
