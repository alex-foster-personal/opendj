"""Resolve the running engine's origin from its lock file.

The port is never guessed and never defaulted: it is read from the JSON the
incumbent engine wrote into ``<data_dir>/.engine.lock`` (see
``apps.engine_core.lock``, which writes ``pid``, ``role``, ``host`` and
``port``). READING that file used to live beside the writer, in
``apps.engine_core.origin`` (issue #2942, which moved it there so
``opendj_cli`` could stop importing ``engine_core`` for it). It moved again,
here, to ``apps.shared`` (PR #3831), because ``apps.sync_hub`` also needs to
resolve a live engine's origin to probe it directly, and ``apps.engine_core``
itself already imports from ``apps.sync_hub`` (its own app-lifespan wiring
pulls in the sync scheduler) -- so ``sync_hub`` importing back from
``engine_core`` would close a package cycle the quality gate's
``python.package_cycles`` check catches. This module has no ``apps.*``
imports of its own, so it can sit below every consumer without cycling back to
any of them. ``apps.engine_core.origin`` and ``apps.opendj_cli.origin`` both
re-export it so neither import path moves for their existing callers.

A missing or unusable lock file is an explicit refusal that names the path
that was checked, which is the AGENT-05 acceptance criterion at
``.planning/REQUIREMENTS.md:4196``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

EXPECTED_ENGINE_ROLE = "opendj-engine"
HEALTH_PATH = "/api/v1/health"
IDENTITY_PROBE_TIMEOUT_S = 3.0

# The same override the live-debug skill uses, so an agent that already points
# OPENDJ_LIVE_LOCK_PATH at a sandboxed engine drives that engine from the CLI.
LOCK_PATH_ENV = "OPENDJ_LIVE_LOCK_PATH"
DEFAULT_LOCK_PATH = (
    Path.home() / "Library/Application Support/com.opendj.desktop/.engine.lock"
)


class EngineNotRunning(RuntimeError):
    """The engine cannot be reached. The message always names the lock file."""

    def __init__(self, message: str, lock_path: Path) -> None:
        super().__init__(message)
        self.lock_path = lock_path


class EngineIdentityMismatch(EngineNotRunning):
    """The process on the lock's port is not the engine the lock describes."""

    def __init__(
        self,
        message: str,
        lock_path: Path,
        *,
        lock_boot_id: str | None,
        health_boot_id: str | None,
        port: int,
    ) -> None:
        super().__init__(message, lock_path)
        self.lock_boot_id = lock_boot_id
        self.health_boot_id = health_boot_id
        self.port = port


@dataclass(frozen=True)
class EngineOrigin:
    """Where the engine the lock file describes is listening."""

    host: str
    port: int
    lock_path: Path
    pid: int | None
    role: str | None
    boot_id: str | None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def describe(self) -> str:
        return f"{self.base_url} (lock {self.lock_path}, pid {self.pid})"


def lock_path(environ: Mapping[str, str] | None = None) -> Path:
    """The lock file this invocation checks: env override first, else the app's."""
    env = os.environ if environ is None else environ
    override = env.get(LOCK_PATH_ENV, "").strip()
    if override == "":
        return DEFAULT_LOCK_PATH
    return Path(override).expanduser()


def _lock_document(resolved: Path) -> dict[str, Any]:
    """The lock file's JSON object, or a refusal naming that exact file."""
    if not resolved.exists():
        raise EngineNotRunning(
            f"no engine lock file at {resolved}; the engine is not running",
            resolved,
        )
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as error:
        raise EngineNotRunning(
            f"engine lock file {resolved} could not be read: {error}", resolved
        ) from error
    except ValueError as error:
        raise EngineNotRunning(
            f"engine lock file {resolved} is not valid JSON, so its port cannot be "
            f"trusted: {error}",
            resolved,
        ) from error
    if not isinstance(payload, dict):
        raise EngineNotRunning(
            f"engine lock file {resolved} does not hold a JSON object", resolved
        )
    return payload


def _lock_port(payload: Mapping[str, Any], resolved: Path) -> int:
    """The port the lock file records. A lock without one is not usable."""
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise EngineNotRunning(
            f"engine lock file {resolved} carries no usable port (port={port!r}); "
            "the engine is still starting, or the file is a leftover",
            resolved,
        )
    return port


def resolve_origin(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> EngineOrigin:
    """Read the lock file into an origin, or refuse naming that file."""
    resolved = lock_path(environ) if path is None else path
    payload = _lock_document(resolved)
    host = payload.get("host")
    pid = payload.get("pid")
    role = payload.get("role")
    boot_id = payload.get("boot_id")
    return EngineOrigin(
        host=host if isinstance(host, str) and host != "" else "127.0.0.1",
        port=_lock_port(payload, resolved),
        lock_path=resolved,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        role=role if isinstance(role, str) else None,
        boot_id=boot_id if isinstance(boot_id, str) and boot_id != "" else None,
    )


def verify_engine_identity(origin: EngineOrigin) -> None:
    """Refuse unless the lock's role and boot_id match the live engine health."""
    if origin.role != EXPECTED_ENGINE_ROLE:
        observed = origin.role if origin.role is not None else "(missing)"
        raise EngineIdentityMismatch(
            f"engine lock file {origin.lock_path} names port {origin.port} with "
            f"role={observed}, but expected role={EXPECTED_ENGINE_ROLE}",
            origin.lock_path,
            lock_boot_id=origin.boot_id,
            health_boot_id=None,
            port=origin.port,
        )

    url = f"{origin.base_url}{HEALTH_PATH}"
    try:
        with httpx.Client(timeout=IDENTITY_PROBE_TIMEOUT_S) as client:
            response = client.get(url)
    except httpx.TransportError as error:
        raise unreachable(origin, error) from error

    if response.status_code != 200:
        raise EngineNotRunning(
            f"engine lock file {origin.lock_path} names {origin.base_url}, but "
            f"{HEALTH_PATH} returned status {response.status_code}",
            origin.lock_path,
        )

    try:
        payload = response.json()
    except ValueError:
        raise EngineNotRunning(
            f"engine lock file {origin.lock_path} names {origin.base_url}, but "
            f"{HEALTH_PATH} is not JSON (not an Open DJ engine)",
            origin.lock_path,
        ) from None

    if not isinstance(payload, dict):
        raise EngineNotRunning(
            f"engine lock file {origin.lock_path} names {origin.base_url}, but "
            f"{HEALTH_PATH} is not an Open DJ engine health document",
            origin.lock_path,
        )

    health_boot_id = payload.get("boot_id")
    if not isinstance(health_boot_id, str) or health_boot_id == "":
        raise EngineNotRunning(
            f"engine lock file {origin.lock_path} names {origin.base_url}, but "
            f"{HEALTH_PATH} is missing boot_id (not an Open DJ engine health document)",
            origin.lock_path,
        )

    lock_boot_id = origin.boot_id
    if lock_boot_id is None:
        raise EngineIdentityMismatch(
            f"engine lock file {origin.lock_path} names port {origin.port} with "
            f"boot_id=(missing), but {HEALTH_PATH} reported boot_id={health_boot_id}",
            origin.lock_path,
            lock_boot_id=None,
            health_boot_id=health_boot_id,
            port=origin.port,
        )

    if lock_boot_id != health_boot_id:
        raise EngineIdentityMismatch(
            f"engine lock file {origin.lock_path} names port {origin.port} with "
            f"boot_id={lock_boot_id}, but {HEALTH_PATH} reported boot_id={health_boot_id}",
            origin.lock_path,
            lock_boot_id=lock_boot_id,
            health_boot_id=health_boot_id,
            port=origin.port,
        )


def resolve_verified_origin(
    path: Path | None = None, environ: Mapping[str, str] | None = None
) -> EngineOrigin:
    """Read the lock file and verify the named engine is the one listening."""
    origin = resolve_origin(path, environ)
    verify_engine_identity(origin)
    return origin


def unreachable(origin: EngineOrigin, error: BaseException) -> EngineNotRunning:
    """Turn a transport failure into the exit-2 refusal, naming lock and URL."""
    return EngineNotRunning(
        f"engine lock file {origin.lock_path} names {origin.base_url}, but nothing "
        f"answered there ({type(error).__name__}: {error})",
        origin.lock_path,
    )


def lock_document(path: Path) -> dict[str, Any]:
    """The raw lock document, for diagnostics that must show what was read."""
    return json.loads(path.read_text(encoding="utf-8"))
