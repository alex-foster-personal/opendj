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
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.config import ENGINE_VERSION

MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"
BUILD_INFO_PATH: str = "/api/v1/build-info"

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

# git log's ISO-8601 strict format, so the timestamp needs no parsing here.
HEAD_TIME_FORMAT: str = "%cI"


class BuildInfoUnavailable(RuntimeError):
    """The engine cannot state its identity. Always names why."""


class BuildInfoOut(BaseModel):
    """The identity contract the UI and any agent read.

    ``built_at_kind`` exists because the two sources measure different
    moments: a payload knows when it was packaged, a checkout only knows when
    HEAD was committed. Labelling which one is on screen costs one field and
    removes a whole class of "why does this say yesterday" confusion.
    """

    source: Literal["payload", "repo"]
    engine_version: str
    git_sha: str
    git_sha_full: str
    git_branch: str
    git_dirty: bool
    built_at_utc: str
    built_at_kind: Literal["payload-build", "head-commit"]
    lane_label: str | None = None
    product_name: str | None = None
    bundle_identifier: str | None = None
    app_version: str | None = None
    manifest_path: str | None = None


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
        built_at_utc=_git(repo_root, "log", "-1", f"--format={HEAD_TIME_FORMAT}"),
        built_at_kind="head-commit",
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
    """
    try:
        resolved: BuildInfoOut | None = resolve_build_info(environ, repo_root)
        failure: str | None = None
    except BuildInfoUnavailable as exc:
        resolved, failure = None, str(exc)

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
                    "error": "build_identity_unavailable",
                    "message": failure,
                    "details": None,
                },
            )
        return resolved


__all__ = [
    "BUILD_INFO_PATH",
    "MANIFEST_ENV",
    "REQUIRED_IDENTITY_KEYS",
    "BuildInfoOut",
    "BuildInfoUnavailable",
    "add_build_info_route",
    "resolve_build_info",
]
