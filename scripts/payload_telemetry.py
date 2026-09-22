"""Bake the ship Sentry DSN into the engine payload.

A packaged install has no Doppler and no shell env, and until Mon 21 Sep 2026
a dmg carried no DSN at all: every installed engine defaulted itself off,
found nothing to report to, and the "errors from testers" the telemetry
module exists for never arrived. This mirrors ``payload_google_oauth``: the
ship DSN is copied from the build environment, or from Doppler project
``general`` config ``dev_personal``, into ``telemetry.json`` at the payload
root. The launcher exports its path as ``OPENDJ_BUNDLED_TELEMETRY`` and
``apps.shared.telemetry.bundled`` loads it before the telemetry decision.

ONLY the ship key. The build host's own ``SENTRY_DSN`` is the preview-dev
key (agents and preview Macs) and is deliberately NOT a fallback here: a
dmg reporting under the preview key would mislabel every tester as a
preview host and land in the wrong quota bucket. The ship DSN lives under
its own name, ``OPENDJ_SENTRY_DSN_BACKEND_SHIP`` (docs/telemetry.md), and a
build that cannot find it fails rather than shipping a silent engine.

The DSN is a public client key, not a secret, but the value is still never
logged: the file is the only place it is written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

#: Payload-relative path of the baked file. Next to manifest.json, not under
#: app/, because it is a build product rather than app source.
BUNDLED_RELATIVE = Path("telemetry.json")

#: The env var (and Doppler secret) the bake reads for the engine. See the
#: module docstring for why SENTRY_DSN is not consulted.
SHIP_DSN_ENV: str = "OPENDJ_SENTRY_DSN_BACKEND_SHIP"

#: The open-dj-fe DSN (Doppler, ADR-0017). The engine derives the Session
#: Replay loader URL from it (OBS-06); the browser loads that script only
#: after the tester accepts the terms (OBS-05).
FRONTEND_DSN_ENV: str = "OPENDJ_SENTRY_DSN_FRONTEND"

_MISSING_DSN = (
    f"payload is missing the ship Sentry DSN. Set {SHIP_DSN_ENV} in the build "
    "environment, or provision it in Doppler project general config "
    "dev_personal (it is the `Default` client key of the open-dj-be project). "
    "A packaged install reports nothing without it, which is the exact gap "
    "this bake closes; the build host's own SENTRY_DSN is the preview key "
    "and is deliberately not used."
)


_MISSING_FRONTEND_DSN = (
    f"payload is missing the frontend Sentry DSN. Set {FRONTEND_DSN_ENV} in the "
    "build environment, or provision it in Doppler project general config "
    "dev_personal (the open-dj-fe project's client key). Session replay for "
    "test users (OBS-06) is derived from it, so a build without it would ask "
    "for consent and then record nothing."
)


class PayloadTelemetryError(RuntimeError):
    """The payload cannot bake or prove a ship DSN."""


def bundled_telemetry_path(payload_dir: Path) -> Path:
    return payload_dir / BUNDLED_RELATIVE


def _doppler_get(name: str) -> str:
    """Read one Doppler secret. Never logs stdout (the value)."""
    doppler = shutil.which("doppler")
    if doppler is None:
        return ""
    try:
        result = subprocess.run(
            [
                doppler,
                "secrets",
                "get",
                name,
                "--project",
                "general",
                "--config",
                "dev_personal",
                "--plain",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _looks_like_dsn(value: str) -> bool:
    """A Sentry DSN is an https URL with a key before an @ host. Shape only."""
    return value.startswith("https://") and "@" in value and "/" in value.split("@", 1)[1]


def _resolve_one(env: Mapping[str, str], name: str, missing: str) -> str:
    dsn = (env.get(name) or "").strip()
    if not dsn:
        dsn = _doppler_get(name)
    if not dsn:
        raise PayloadTelemetryError(missing)
    if not _looks_like_dsn(dsn):
        raise PayloadTelemetryError(
            f"{name} is set but does not look like a Sentry DSN "
            "(expected https://<key>@<host>/<project>); value not printed."
        )
    return dsn


def _resolve_dsn(env: Mapping[str, str]) -> str:
    return _resolve_one(env, SHIP_DSN_ENV, _MISSING_DSN)


def _resolve_frontend_dsn(env: Mapping[str, str]) -> str:
    return _resolve_one(env, FRONTEND_DSN_ENV, _MISSING_FRONTEND_DSN)


def bake_telemetry(payload_dir: Path, env: Mapping[str, str] | None = None) -> Path:
    """Write ``telemetry.json`` into the staged payload.

    ``env`` defaults to ``os.environ``. Tests pass an isolated mapping so a
    live Doppler token cannot green a missing-DSN case. The file is created
    only under ``payload_dir``; it is never written to the repo.
    """
    import os

    source = os.environ if env is None else env
    dsn = _resolve_dsn(source)
    frontend_dsn = _resolve_frontend_dsn(source)
    dest = bundled_telemetry_path(payload_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"dsn": dsn, "frontend_dsn": frontend_dsn}) + "\n", encoding="utf-8"
    )
    return dest


def verify_bundled_telemetry(payload_dir: Path) -> None:
    """Fail loud when the baked DSN is absent or malformed, naming the env var."""
    path = bundled_telemetry_path(payload_dir)
    if not path.is_file():
        raise PayloadTelemetryError(_MISSING_DSN)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadTelemetryError(_MISSING_DSN) from exc
    if not isinstance(data, dict):
        raise PayloadTelemetryError(_MISSING_DSN)
    dsn = str(data.get("dsn") or "").strip()
    if not dsn or not _looks_like_dsn(dsn):
        raise PayloadTelemetryError(_MISSING_DSN)
    frontend_dsn = str(data.get("frontend_dsn") or "").strip()
    if not frontend_dsn or not _looks_like_dsn(frontend_dsn):
        raise PayloadTelemetryError(_MISSING_FRONTEND_DSN)


__all__ = [
    "BUNDLED_RELATIVE",
    "FRONTEND_DSN_ENV",
    "SHIP_DSN_ENV",
    "PayloadTelemetryError",
    "bake_telemetry",
    "bundled_telemetry_path",
    "verify_bundled_telemetry",
]
