"""Which environment variables name openDJ's Google OAuth client.

One home for the names, below both of their readers:

* :mod:`apps.webui.server.auth` reads the id to run sign-in;
* :mod:`apps.sync_hub.google_id_token` reads it to pin ``aud`` when a spoke
  presents a Google id_token at enrollment (ADR 12 section B).

It lives in ``apps.shared`` because the hub cannot import the webui:
``apps.webui`` already imports ``apps.sync_hub``, so that edge would be a
package cycle. Two copies of the list is how sign-in and verification start
disagreeing about which OAuth client this install is, and a hub that pinned
``aud`` to a different client than the one its installs sign in with would
refuse every real user while looking correctly configured.

A packaged install has no Doppler. The payload builder bakes a Desktop-app
client into ``bundled_google_oauth.json`` next to this module (and the
launcher exports ``OPENDJ_GOOGLE_OAUTH_CONFIG`` to that path). Process env
wins; the file fills only blank names. Values are never logged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path

# Precedence is explicit, not a hidden default: an openDJ-specific client
# wins if one is ever provisioned, otherwise the shared personal client in
# Doppler (project ``general``, config ``dev_personal``) is used.
CLIENT_ID_ENV_NAMES: tuple[str, ...] = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
)
CLIENT_SECRET_ENV_NAMES: tuple[str, ...] = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)

BUNDLED_FILENAME = "bundled_google_oauth.json"
CONFIG_PATH_ENV = "OPENDJ_GOOGLE_OAUTH_CONFIG"
MANIFEST_ENV = "OPENDJ_PAYLOAD_MANIFEST"


def first_present(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """The first non-blank value among ``names`` in ``env``, or ``None``."""
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


def _candidate_paths(env: Mapping[str, str]) -> list[Path]:
    paths: list[Path] = []
    explicit = str(env.get(CONFIG_PATH_ENV) or "").strip()
    if explicit:
        paths.append(Path(explicit))
    paths.append(Path(__file__).with_name(BUNDLED_FILENAME))
    manifest = str(env.get(MANIFEST_ENV) or "").strip()
    if manifest:
        paths.append(Path(manifest).expanduser().resolve().parent / "config" / "google_oauth.json")
    return paths


def load_bundled_oauth(env: Mapping[str, str]) -> dict[str, str]:
    """Return client_id/client_secret from the first readable bundled file.

    Missing or unreadable files yield ``{}``. Never logs the values.
    """
    seen: set[Path] = set()
    for path in _candidate_paths(env):
        try:
            resolved = path.expanduser()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_file():
                continue
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, RuntimeError):
            continue
        if not isinstance(data, dict):
            continue
        client_id = str(data.get("client_id") or "").strip()
        client_secret = str(data.get("client_secret") or "").strip()
        if not client_id and not client_secret:
            continue
        out: dict[str, str] = {}
        if client_id:
            out["client_id"] = client_id
        if client_secret:
            out["client_secret"] = client_secret
        return out
    return {}


def env_with_bundled(env: Mapping[str, str]) -> dict[str, str]:
    """Copy of ``env`` with bundled Desktop-app client filled in for blanks.

    Process env wins. The bundled file is consulted only for names that are
    absent or blank. Never logs the values.
    """
    merged = dict(env)
    if first_present(merged, CLIENT_ID_ENV_NAMES) and first_present(
        merged, CLIENT_SECRET_ENV_NAMES
    ):
        return merged
    bundled = load_bundled_oauth(merged)
    if not bundled:
        return merged
    if not first_present(merged, CLIENT_ID_ENV_NAMES) and bundled.get("client_id"):
        merged[CLIENT_ID_ENV_NAMES[0]] = bundled["client_id"]
    if not first_present(merged, CLIENT_SECRET_ENV_NAMES) and bundled.get(
        "client_secret"
    ):
        merged[CLIENT_SECRET_ENV_NAMES[0]] = bundled["client_secret"]
    return merged


def apply_bundled_oauth(environ: MutableMapping[str, str]) -> None:
    """Publish baked client id/secret into ``environ`` for blank names only."""
    filled = env_with_bundled(environ)
    for key in (CLIENT_ID_ENV_NAMES[0], CLIENT_SECRET_ENV_NAMES[0]):
        value = filled.get(key, "").strip()
        if value and not str(environ.get(key, "")).strip():
            environ[key] = value


__all__ = [
    "BUNDLED_FILENAME",
    "CLIENT_ID_ENV_NAMES",
    "CLIENT_SECRET_ENV_NAMES",
    "CONFIG_PATH_ENV",
    "apply_bundled_oauth",
    "env_with_bundled",
    "first_present",
    "load_bundled_oauth",
]
