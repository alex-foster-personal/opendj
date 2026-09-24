"""First run: a packaged build's default hub turns CloudSync on, once.

Issue #3870. Test users install the packaged app and must never be asked
for a hub URL: the build (or the launcher's environment) names a default
hub, and the engine's first boot writes ``cloudsync-config.json`` with
``enabled: true`` and that URL. From then on the file is the one source
(:mod:`apps.sync_hub.config`), exactly as if the user had saved it.

Where the default comes from, in order:

1. ``MDT_CLOUDSYNC_DEFAULT_HUB_URL`` in the environment (runtime).
2. ``cloudsync.default_hub_url`` in the payload manifest named by
   ``OPENDJ_PAYLOAD_MANIFEST`` (build time; ``scripts/build_engine_payload.py``
   stamps it through :func:`manifest_block`). A manifest with no ``cloudsync``
   block is an older build and carries no default.

Neither is ever a literal in source: the OSS audit forbids real tailnet
names in the tracked tree, so the URL is injected.

Rules the tests pin:

- An existing ``cloudsync-config.json`` ALWAYS wins, byte for byte. Seeding
  is for the first run only; it never repairs, migrates or overwrites.
- No default means nothing is written and status reads as before.
- A malformed default (not an http(s) URL with a host) is refused loudly,
  at build time through :func:`manifest_block` and again at runtime, never
  quietly dropped: a build that ships a broken default is a packaging bug.
- The hub itself (``MDT_IS_HUB=1``) never seeds a spoke config.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from apps.shared.state import machine_identity
from apps.sync_hub import config as sync_config

log = logging.getLogger(__name__)

DEFAULT_HUB_ENV: str = "MDT_CLOUDSYNC_DEFAULT_HUB_URL"
#: Same variable :mod:`apps.engine_core.build_info` reads; spelled here so
#: this module does not import the engine.
MANIFEST_ENV: str = "OPENDJ_PAYLOAD_MANIFEST"
MANIFEST_KEY: str = "cloudsync"
MANIFEST_URL_KEY: str = "default_hub_url"

SeedOutcome = Literal["seeded", "existing", "no-default", "hub"]


@dataclass(frozen=True)
class FirstRunSeed:
    """What first-run seeding did. ``hub_url`` is the effective file value."""

    outcome: SeedOutcome
    hub_url: str | None
    path: str


# ----- resolution ------------------------------------------------------------


def _validated_hub_url(raw: str, *, source: str) -> str:
    try:
        checked = sync_config.CloudSyncConfig(enabled=True, hub_url=raw, machine_name=None)
    except ValidationError as exc:
        raise sync_config.CloudSyncConfigError(f"{source} is not a usable hub URL: {exc}") from exc
    assert checked.hub_url is not None
    return checked.hub_url


def manifest_block(raw: str | None) -> dict[str, Any]:
    """The ``cloudsync`` block the payload build stamps; validated at build time."""
    url = None if raw is None else _validated_hub_url(raw, source="default hub URL for the build")
    return {MANIFEST_KEY: {MANIFEST_URL_KEY: url}}


def _default_from_manifest(path: Path) -> str | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise sync_config.CloudSyncConfigError(
            f"{MANIFEST_ENV}={path} is unreadable: {exc}"
        ) from exc
    block = manifest.get(MANIFEST_KEY) if isinstance(manifest, dict) else None
    if block is None:
        return None
    if not isinstance(block, dict) or set(block) != {MANIFEST_URL_KEY}:
        raise sync_config.CloudSyncConfigError(
            f"{path} {MANIFEST_KEY!r} must be an object with only {MANIFEST_URL_KEY!r}"
        )
    raw = block[MANIFEST_URL_KEY]
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise sync_config.CloudSyncConfigError(
            f"{path} {MANIFEST_URL_KEY} must be a string or null"
        )
    return _validated_hub_url(raw, source=f"{path} {MANIFEST_URL_KEY}")


def resolve_default_hub_url(env: Mapping[str, str]) -> str | None:
    """The build's default hub, or ``None`` when this build has none."""
    from_env = env.get(DEFAULT_HUB_ENV, "")
    if from_env.strip():
        return _validated_hub_url(from_env, source=DEFAULT_HUB_ENV)
    manifest = env.get(MANIFEST_ENV, "").strip()
    if manifest:
        return _default_from_manifest(Path(manifest))
    return None


# ----- seeding ---------------------------------------------------------------


def seed_default_config(data_dir: Path, *, env: Mapping[str, str]) -> FirstRunSeed:
    """Write ``enabled: true`` + the default hub on first run. Existing file wins.

    Detects an existing file by presence, not by parsing it: a malformed file
    must not abort engine boot here, the same as it does not abort the
    scheduler or status paths (:func:`apps.sync_hub.scheduler.CloudSyncScheduler._effective`,
    :func:`apps.sync_hub.status.read_status`), both of which already catch
    :class:`~apps.sync_hub.config.CloudSyncConfigError` and idle/report instead
    of raising. Seeding never touches an existing file regardless of its
    contents, so validating it here has no seeding purpose - only the boot
    path to lose.
    """
    path_obj = sync_config.config_path(data_dir)
    path = str(path_obj)
    if machine_identity.is_hub_from_env(dict(env)):
        return FirstRunSeed("hub", None, path)
    if path_obj.exists():
        try:
            existing = sync_config.read_config(data_dir)
        except sync_config.CloudSyncConfigError as exc:
            log.warning(
                "cloudsync first run: existing %s is unreadable, leaving it alone: %s", path, exc
            )
            return FirstRunSeed("existing", None, path)
        return FirstRunSeed("existing", existing.hub_url if existing is not None else None, path)
    default = resolve_default_hub_url(env)
    if default is None:
        return FirstRunSeed("no-default", None, path)
    sync_config.write_config(
        data_dir, sync_config.CloudSyncConfig(enabled=True, hub_url=default, machine_name=None)
    )
    log.info("cloudsync first run: enabled against the build's default hub %s", default)
    return FirstRunSeed("seeded", default, path)


__all__ = [
    "DEFAULT_HUB_ENV",
    "MANIFEST_ENV",
    "MANIFEST_KEY",
    "MANIFEST_URL_KEY",
    "FirstRunSeed",
    "SeedOutcome",
    "manifest_block",
    "resolve_default_hub_url",
    "seed_default_config",
]
