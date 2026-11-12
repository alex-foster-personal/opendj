"""Opt-in integration settings read from a JSON settings file.

The file is a JSON object; each integration reads its own key. An absent file or key
means the integration was never turned on, so it reads as disabled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LISTENBRAINZ_KEY: str = "listenbrainz"


class SettingsError(ValueError):
    """The settings file exists but does not have the documented shape."""


@dataclass(frozen=True)
class ListenBrainzSettings:
    """ListenBrainz submit settings. Disabled unless the user turns it on."""

    enabled: bool = False
    token: str | None = None


def _read_settings_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: settings must be a JSON object")
    return data


def load_listenbrainz_settings(path: Path) -> ListenBrainzSettings:
    """Return the ListenBrainz settings in ``path``; disabled when never configured."""
    section = _read_settings_object(path).get(LISTENBRAINZ_KEY, {})
    if not isinstance(section, dict):
        raise SettingsError(f"{path}: '{LISTENBRAINZ_KEY}' must be a JSON object")
    enabled = section.get("enabled", False)
    token = section.get("token")
    if not isinstance(enabled, bool):
        raise SettingsError(f"{path}: '{LISTENBRAINZ_KEY}.enabled' must be a boolean")
    if token is not None and not isinstance(token, str):
        raise SettingsError(f"{path}: '{LISTENBRAINZ_KEY}.token' must be a string")
    return ListenBrainzSettings(enabled=enabled, token=token.strip() if token and token.strip() else None)
