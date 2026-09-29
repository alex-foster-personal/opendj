"""``GET /api/v1/build-info`` -- what this engine actually is.

WHY THIS EXISTS

An installed app that cannot say which commit it came from costs a human
their attention: someone opens a build, sees old behaviour, and spends the
next twenty minutes proving the artifact is stale rather than the code
broken. So the engine states its own identity over HTTP, the main UI surface
renders it, and a build made from a dirty tree says DIRTY rather than looking
byte-identical to a reproducible one.

TWO SOURCES, NEVER A DEFAULT

- ``source="payload"``: the launcher exported ``OPENDJ_PAYLOAD_MANIFEST`` and
  the manifest the payload builder stamped is read from disk.
- ``source="repo"``: no manifest, so the engine is running out of a checkout
  and asks git directly, once, at construction.

Anything else is a fault: an unreadable manifest, a manifest missing fields,
a checkout git cannot describe. The route answers 503 with the reason
attached, because a build-identity readout that silently renders blank is
indistinguishable from one that renders a lie.

Requirements:

- ✔︎ ✅ 🎯 A payload manifest is served verbatim, marked ``payload``.
  -> :func:`resolve_build_info`
- ✔︎ ✅ 🎯 A repo checkout is described from live git, marked ``repo``.
  -> :func:`resolve_build_info`
- ✔︎ ✅ 🎯 A manifest that is missing, unparseable or incomplete produces a
  503 naming the path and the reason, never a blank or a guess.
  -> :class:`BuildInfoUnavailable`, :func:`add_build_info_route`

Acceptance tests:

- [if] OPENDJ_PAYLOAD_MANIFEST points at a valid manifest [then] the response
  is 200 with source=payload and that manifest's sha, [else ⛔️].
- [if] OPENDJ_PAYLOAD_MANIFEST points at a missing file [then] the response
  is 503 whose message contains the path, [else ⛔️].
- [if] no manifest env is set inside a git checkout [then] the response is
  200 with source=repo and the checkout's real dirty flag, [else ⛔️].
- [if] no manifest env is set inside a git checkout [then] the response also
  carries ``app_version`` from ``tauri.conf.json``, the semver the updater
  compares, [else ⛔️].
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.config import ENGINE_VERSION

MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"
BUILD_INFO_PATH: str = "/api/v1/build-info"
TAURI_CONF_REL: Path = Path("apps/desktop/src-tauri/tauri.conf.json")

#: The error code a caller branches on when this engine cannot state what it
#: is. Spelled once, so the /build-info body and every route that DERIVES
#: behaviour from the identity refuse under the same name.
CODE_BUILD_IDENTITY_UNAVAILABLE: str = "build_identity_unavailable"

#: Where the resolved identity is mounted for other routers to read. It is
#: resolved once, at construction, so a route that switches on the build
#: source pays no git subprocess per request.
BUILD_IDENTITY_STATE_ATTR: str = "build_identity"

# Manifest keys the route refuses to serve without. A partial manifest is a
# packaging bug, and rendering half an identity is worse than rendering none.
REQUIRED_IDENTITY_KEYS: tuple[str, ...] = (
    "built_at_utc",
    "git_branch",
    "git_dirty",
    "git_sha",
    "git_sha_full",
    "lane_label",
)

# git log's ISO-8601 strict format. It carries the COMMITTER's offset, not
# UTC ("2026-08-19T14:17:23+01:00"), so it is converted before it goes out
# under a field called built_at_utc. Serving a local-offset timestamp there
# would be exactly the kind of nearly-right readout this endpoint exists to
# replace: it looks like an answer and quietly disagrees with the payload
# path, which stamps real UTC.
HEAD_TIME_FORMAT: str = "%cI"


class BuildInfoUnavailable(RuntimeError):
    """The engine cannot state its identity. Always names why."""


class BuildInfoOut(BaseModel):
    """The identity contract the UI and any agent read.

    ``built_at_kind`` exists because the sources measure different moments: a
    payload knows when it was packaged; a repo checkout stamps the running
    engine's start instant at identity resolution (``engine-start``). The
    legacy ``head-commit`` literal remains on the wire for older readers only.
    Labelling which moment is on screen removes "why does this say yesterday"
    confusion.
    """

    source: Literal["payload", "repo"]
    engine_version: str
    git_sha: str
    git_sha_full: str
    git_branch: str
    git_dirty: bool
    built_at_utc: str
    built_at_kind: Literal["payload-build", "head-commit", "engine-start"]
    lane_label: str | None = None
    product_name: str | None = None
    bundle_identifier: str | None = None
    app_version: str | None = None
    manifest_path: str | None = None


@dataclass(frozen=True)
class BuildIdentity:
    """The one resolution attempt, kept so other routes can read its verdict.

    A resolved identity is not just a readout: whether this engine is a
    developer checkout or an installed build changes what other endpoints
    should do (the setup wizard does not auto-trigger in a checkout). Those
    endpoints must not re-resolve -- three git subprocesses per request, and
    an answer that could disagree with the one /build-info already served --
    so the attempt is mounted on ``app.state`` and read from there.

    EXACTLY ONE of ``info`` and ``failure`` is set. A holder with neither
    would be a third state nobody wrote a branch for.
    """

    info: BuildInfoOut | None
    failure: str | None

    def __post_init__(self) -> None:
        if (self.info is None) == (self.failure is None):
            raise ValueError(
                "BuildIdentity carries either a resolved BuildInfoOut or the "
                f"reason resolution failed, never both and never neither "
                f"(info={self.info!r}, failure={self.failure!r})"
            )

    def require(self) -> BuildInfoOut:
        """The identity, or the original failure. Never a guess."""
        if self.info is None:
            raise BuildInfoUnavailable(self.failure)
        return self.info


# ----- sources -----------------------------------------------------------
def _from_manifest(manifest_path: Path) -> BuildInfoOut:
    if not manifest_path.is_file():
        raise BuildInfoUnavailable(
            f"{MANIFEST_ENV}={manifest_path} but no file is there. This build "
            "cannot state what it is; treat it as unidentified rather than "
            "current."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildInfoUnavailable(
            f"{manifest_path} could not be read as JSON: {exc}"
        ) from exc
    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        raise BuildInfoUnavailable(
            f"{manifest_path} has no 'identity' object; it was not written by "
            "scripts/build_engine_payload.py"
        )
    missing = [key for key in REQUIRED_IDENTITY_KEYS if key not in identity]
    if missing:
        raise BuildInfoUnavailable(
            f"{manifest_path} identity is missing {missing}"
        )
    return BuildInfoOut(
        source="payload",
        engine_version=identity.get("engine_version", ENGINE_VERSION),
        git_sha=identity["git_sha"],
        git_sha_full=identity["git_sha_full"],
        git_branch=identity["git_branch"],
        git_dirty=bool(identity["git_dirty"]),
        built_at_utc=identity["built_at_utc"],
        built_at_kind="payload-build",
        lane_label=identity["lane_label"],
        product_name=identity.get("product_name"),
        bundle_identifier=identity.get("bundle_identifier"),
        app_version=identity.get("app_version"),
        manifest_path=str(manifest_path),
    )


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BuildInfoUnavailable(
            f"git {' '.join(args)} failed in {repo_root}: "
            f"{result.stderr.strip() or result.returncode}"
        )
    return result.stdout.strip()


def head_time_as_utc(raw: str) -> str:
    """Normalise git's committer timestamp to the Z form the field promises."""
    try:
        committed = datetime.fromisoformat(raw)
    except ValueError as err:
        raise BuildInfoUnavailable(
            f"git reported HEAD's commit time as {raw!r}, which is not "
            "ISO-8601; the engine will not guess at its own build time"
        ) from err
    if committed.tzinfo is None:
        raise BuildInfoUnavailable(
            f"git reported HEAD's commit time as {raw!r} with no timezone, "
            "so it cannot be converted to UTC"
        )
    return committed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tauri_app_version(repo_root: Path) -> str:
    """The semver the updater compares, read from the desktop shell config.

    A repo checkout has no payload manifest, but the update channel still needs
    the same version string ``tauri-plugin-updater`` will compare. Reading it
    from ``tauri.conf.json`` keeps the two halves aligned without inventing a
    second source.
    """
    conf_path = repo_root / TAURI_CONF_REL
    if not conf_path.is_file():
        raise BuildInfoUnavailable(
            f"no payload manifest and {conf_path} is missing, so this checkout "
            "cannot name the app version the updater compares"
        )
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildInfoUnavailable(
            f"{conf_path} could not be read as JSON: {exc}"
        ) from exc
    raw_version = conf.get("version")
    if not isinstance(raw_version, str) or raw_version.strip() == "":
        raise BuildInfoUnavailable(
            f"{conf_path} has no non-empty string 'version' field, so this "
            "checkout cannot name the app version the updater compares"
        )
    return raw_version.strip()


def _from_repo(repo_root: Path) -> BuildInfoOut:
    if shutil.which("git") is None:
        raise BuildInfoUnavailable(
            "no payload manifest and git is not on PATH, so this engine cannot "
            "describe the source it is running from"
        )
    sha_full = _git(repo_root, "rev-parse", "HEAD")
    return BuildInfoOut(
        source="repo",
        engine_version=ENGINE_VERSION,
        git_sha=sha_full[:8],
        git_sha_full=sha_full,
        git_branch=_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        git_dirty=_git(repo_root, "status", "--porcelain") != "",
        built_at_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        built_at_kind="engine-start",
        app_version=_tauri_app_version(repo_root),
    )


def resolve_build_info(
    environ: dict[str, str], repo_root: Path
) -> BuildInfoOut:
    """Payload manifest when the launcher named one, otherwise live git."""
    raw = environ.get(MANIFEST_ENV, "").strip()
    if raw != "":
        return _from_manifest(Path(raw))
    return _from_repo(repo_root)


# ----- route -------------------------------------------------------------
def add_build_info_route(app: FastAPI, *, environ: dict[str, str], repo_root: Path) -> None:
    """Resolve once at construction; serve the same answer for the process.

    Resolution is cached because it shells out to git, and an identity that
    changed mid-process would mean the running code changed underneath the
    reader, which it cannot. The FAILURE is cached too: a bundle with no
    readable manifest is broken for its whole life, and retrying the same
    stat on every request would only add latency to the fault.

    The attempt is also MOUNTED on ``app.state`` under
    ``BUILD_IDENTITY_STATE_ATTR``, because the build source is behaviour and
    not only a readout: see ``apps.engine_core.setup.api``, whose wizard gate
    is off in a developer checkout.
    """
    try:
        resolved: BuildInfoOut | None = resolve_build_info(environ, repo_root)
        failure: str | None = None
    except BuildInfoUnavailable as exc:
        resolved, failure = None, str(exc)

    setattr(
        app.state,
        BUILD_IDENTITY_STATE_ATTR,
        BuildIdentity(info=resolved, failure=failure),
    )

    @app.get(
        BUILD_INFO_PATH,
        response_model=BuildInfoOut,
        tags=["health"],
        name="build_info",
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "this build cannot state its own identity"
            }
        },
    )
    def build_info() -> BuildInfoOut | JSONResponse:
        if resolved is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "error": CODE_BUILD_IDENTITY_UNAVAILABLE,
                    "message": failure,
                    "details": None,
                },
            )
        return resolved


__all__ = [
    "BUILD_IDENTITY_STATE_ATTR",
    "BUILD_INFO_PATH",
    "CODE_BUILD_IDENTITY_UNAVAILABLE",
    "MANIFEST_ENV",
    "REQUIRED_IDENTITY_KEYS",
    "BuildIdentity",
    "BuildInfoOut",
    "BuildInfoUnavailable",
    "add_build_info_route",
    "resolve_build_info",
]
