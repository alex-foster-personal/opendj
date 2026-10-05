"""Persisted per-machine CloudSync configuration: ``<data-dir>/cloudsync-config.json``.

Why a file: the packaged desktop shell launches the engine with a stripped
environment (launchd-launched GUI apps inherit no shell env, and the Tauri
shell injects none), so an env-only setting can never reach ``Open DJ.app``.
The file is what the HTTP ``PUT /api/v1/cloudsync/config`` and the CLI twin
``python -m apps.sync_hub config set`` write, and what the scheduler and the
status surface read.

File shape (``extra`` keys refused, types strict)::

    {"enabled": bool, "hub_url": str | null, "machine_name": str | null}

Precedence, per field -- ENV WINS, and the winner is reported:

* ``enabled``  <- ``MDT_CLOUDSYNC_SCHEDULER`` when set to ``1`` or ``0``
  (any other non-empty value raises), else the file, else ``False`` with
  source ``default`` (no file = never configured).
* ``hub_url``  <- ``MDT_CLOUDSYNC_HUB_URL`` when non-blank, else the file.
* ``machine_name`` <- the file only.

:class:`EffectiveConfig` carries ``enabled_source`` and ``hub_url_source``
(``env`` / ``file`` / ``default``) so the status payload can say that an env
override is masking what the UI saved. A malformed file raises
:class:`CloudSyncConfigError`; it never degrades into "off".
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from apps.sync_hub.atomic_json import write_json_atomic

CONFIG_FILENAME: str = "cloudsync-config.json"
SCHEDULER_ENV: str = "MDT_CLOUDSYNC_SCHEDULER"
ENDPOINT_ENV: str = "MDT_CLOUDSYNC_HUB_URL"

ConfigSource = Literal["env", "file", "default"]


class CloudSyncConfigError(RuntimeError):
    """The CloudSync config file or an override env var is malformed."""


class CloudSyncConfig(BaseModel):
    """The persisted file, validated. Also the HTTP PUT body."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool
    hub_url: str | None
    machine_name: str | None

    @field_validator("hub_url")
    @classmethod
    def _hub_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or value != value.strip():
            raise ValueError(f"hub_url must be an http(s) URL with a host, got {value!r}")
        return value

    @field_validator("machine_name")
    @classmethod
    def _machine_name_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("machine_name must be null or non-blank")
        return value

    @model_validator(mode="after")
    def _enabled_needs_a_hub(self) -> CloudSyncConfig:
        if self.enabled and self.hub_url is None:
            raise ValueError("enabled=true requires hub_url")
        return self


@dataclass(frozen=True)
class EffectiveConfig:
    """What this machine will actually do, after env overrides, with sources."""

    enabled: bool
    hub_url: str | None
    machine_name: str | None
    enabled_source: ConfigSource
    hub_url_source: ConfigSource

    @property
    def configured(self) -> bool:
        """Enabled AND a hub to talk to. Intent only -- not evidence of a loop."""
        return self.enabled and self.hub_url is not None

    def to_wire(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "hub_url": self.hub_url,
            "machine_name": self.machine_name,
            "configured": self.configured,
            "enabled_source": self.enabled_source,
            "hub_url_source": self.hub_url_source,
        }


def config_path(data_dir: Path) -> Path:
    return Path(data_dir) / CONFIG_FILENAME


def read_config(data_dir: Path) -> CloudSyncConfig | None:
    """The persisted file, or ``None`` when it does not exist. Malformed raises."""
    path = config_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CloudSyncConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return CloudSyncConfig.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise CloudSyncConfigError(f"{path} is malformed: {exc}") from exc


def write_config(data_dir: Path, config: CloudSyncConfig) -> Path:
    """Atomically persist ``config``. The data dir must already exist."""
    path = config_path(data_dir)
    try:
        write_json_atomic(path, config.model_dump())
    except OSError as exc:
        raise CloudSyncConfigError(f"cannot write {path}: {exc}") from exc
    return path


def configured_machine_name(data_dir: Path) -> str | None:
    """The machine name saved in the config file, or ``None`` when none is saved.

    THE one answer to "what is this data dir called on the fleet". Every path
    that registers this machine (the scheduler, Sync now, the CLI, enroll, the
    feedback pin sync, ``GET /cloudsync/machines``) asks here before falling
    back to a hostname: ``machines.name`` is UNIQUE, and a second data dir on a
    host that has pulled the fleet's ``machines`` table finds the hostname
    already taken by another machine id. A malformed file raises.
    """
    stored = read_config(data_dir)
    return None if stored is None else stored.machine_name


def _env_enabled(env: Mapping[str, str]) -> bool | None:
    raw = env.get(SCHEDULER_ENV, "")
    if raw == "":
        return None
    if raw == "1":
        return True
    if raw == "0":
        return False
    raise CloudSyncConfigError(f"{SCHEDULER_ENV}={raw!r} is not understood; use 1 or 0.")


def resolve_config(data_dir: Path, *, env: Mapping[str, str] | None = None) -> EffectiveConfig:
    """Merge the file with the env overrides. Env wins per field; see module doc."""
    source = os.environ if env is None else env
    stored = read_config(data_dir)
    env_enabled = _env_enabled(source)
    env_hub = source.get(ENDPOINT_ENV, "").strip() or None

    enabled_source: ConfigSource
    if env_enabled is not None:
        enabled, enabled_source = env_enabled, "env"
    elif stored is not None:
        enabled, enabled_source = stored.enabled, "file"
    else:
        enabled, enabled_source = False, "default"

    hub_url_source: ConfigSource
    if env_hub is not None:
        hub_url, hub_url_source = env_hub, "env"
    elif stored is not None and stored.hub_url is not None:
        hub_url, hub_url_source = stored.hub_url, "file"
    else:
        hub_url, hub_url_source = None, "default"

    return EffectiveConfig(
        enabled=enabled,
        hub_url=hub_url,
        machine_name=None if stored is None else stored.machine_name,
        enabled_source=enabled_source,
        hub_url_source=hub_url_source,
    )


def config_payload(data_dir: Path, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The one wire object ``GET /cloudsync/config`` and ``config show`` print."""
    stored = read_config(data_dir)
    return {
        "path": str(config_path(data_dir)),
        "file": None if stored is None else stored.model_dump(),
        "effective": resolve_config(data_dir, env=env).to_wire(),
    }


__all__ = [
    "CONFIG_FILENAME",
    "ENDPOINT_ENV",
    "SCHEDULER_ENV",
    "CloudSyncConfig",
    "CloudSyncConfigError",
    "ConfigSource",
    "EffectiveConfig",
    "config_path",
    "config_payload",
    "configured_machine_name",
    "read_config",
    "resolve_config",
    "write_config",
]
