"""Bake the Google Desktop-app OAuth client into the engine payload.

A packaged install has no Doppler and no shell env. The Desktop-app client
id (and its non-confidential installed-app secret) is copied from the
build environment, or from Doppler project ``general`` config
``dev_personal``, into a JSON file next to
``apps.shared.google_oauth_client`` inside the staged payload. The engine
loads that file before ``_oauth_config()``. Values are never logged.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from apps.shared.google_oauth_client import (
    CLIENT_ID_ENV_NAMES,
    CLIENT_SECRET_ENV_NAMES,
    first_present,
)

BUNDLED_RELATIVE = Path("app/apps/shared/bundled_google_oauth.json")

_MISSING_CLIENT_ID = (
    "payload is missing a Google OAuth client id. Set "
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID (or GOOGLE_OAUTH_CLIENT_ID) in the "
    "build environment, or provision it in Doppler project general "
    "config dev_personal. A packaged install cannot sign in without it."
)
_MISSING_CLIENT_SECRET = (
    "payload is missing a Google OAuth client secret. Set "
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET (or GOOGLE_OAUTH_CLIENT_SECRET) "
    "in the build environment, or provision it in Doppler project "
    "general config dev_personal."
)


class PayloadOAuthError(RuntimeError):
    """The payload cannot bake or prove a Google OAuth client id."""


def bundled_oauth_path(payload_dir: Path) -> Path:
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


def _resolve_credentials(env: Mapping[str, str]) -> tuple[str, str]:
    client_id = first_present(env, CLIENT_ID_ENV_NAMES) or ""
    client_secret = first_present(env, CLIENT_SECRET_ENV_NAMES) or ""
    if not client_id:
        for name in CLIENT_ID_ENV_NAMES:
            client_id = _doppler_get(name)
            if client_id:
                break
    if not client_secret:
        for name in CLIENT_SECRET_ENV_NAMES:
            client_secret = _doppler_get(name)
            if client_secret:
                break
    if not client_id:
        raise PayloadOAuthError(_MISSING_CLIENT_ID)
    if not client_secret:
        raise PayloadOAuthError(_MISSING_CLIENT_SECRET)
    return client_id, client_secret


def bake_google_oauth(
    payload_dir: Path, env: Mapping[str, str] | None = None
) -> Path:
    """Write the bundled client JSON into the staged payload.

    ``env`` defaults to ``os.environ``. Tests pass an isolated mapping so a
    live Doppler token cannot green a missing-secret case. The file is
    created only under ``payload_dir``; it is never written to the repo.
    """
    import os

    client_id, client_secret = _resolve_credentials(
        os.environ if env is None else env
    )
    dest = bundled_oauth_path(payload_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"client_id": client_id, "client_secret": client_secret}),
        encoding="utf-8",
    )
    dest.chmod(0o600)
    return dest


def verify_bundled_oauth(payload_dir: Path) -> None:
    """Fail loud when the baked client id is absent, naming the env var."""
    path = bundled_oauth_path(payload_dir)
    if not path.is_file():
        raise PayloadOAuthError(_MISSING_CLIENT_ID)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PayloadOAuthError(_MISSING_CLIENT_ID) from exc
    if not isinstance(data, dict):
        raise PayloadOAuthError(_MISSING_CLIENT_ID)
    client_id = str(data.get("client_id") or "").strip()
    if not client_id:
        raise PayloadOAuthError(_MISSING_CLIENT_ID)
    client_secret = str(data.get("client_secret") or "").strip()
    if not client_secret:
        raise PayloadOAuthError(_MISSING_CLIENT_SECRET)
