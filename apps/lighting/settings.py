"""Typed home for ``lighting.*`` variant settings and their pick log (VARIANT, issue #10103).

Each variant setting is one registry row (key, allowed values, default). ``LightingSettings``
holds the current values for one daemon lifetime, validates every change, and appends a
``{key, from, to, at, decks_playing}`` line to ``state/lighting/variant_picks.ndjson`` so the
default can be picked from play. Later variants (energy curve, handover) add rows here;
``NetOutSettings`` holds the free-form network output values (L24, issue #9878).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from apps.shared.paths import STATE_DIR


@dataclass(frozen=True)
class VariantSetting:
    key: str
    allowed: tuple[str, ...]
    default: str


PALETTE_KEY: Final = "lighting.palette"
ENERGY_CURVE_KEY: Final = "lighting.energy_curve"
MOVEMENT_KEY: Final = "lighting.movement"
HANDOVER_KEY: Final = "lighting.handover"

SETTINGS: Final[Mapping[str, VariantSetting]] = {
    s.key: s
    for s in (
        VariantSetting(PALETTE_KEY, ("cool", "warm", "vivid", "contrast"), "cool"),
        VariantSetting(ENERGY_CURVE_KEY, ("gentle", "matched", "punchy"), "matched"),
        VariantSetting(MOVEMENT_KEY, ("small", "wide"), "small"),
        VariantSetting(HANDOVER_KEY, ("blend", "upfader_only", "master_only", "phrase_switch"), "blend"),
    )
}

NET_OUT_PROTOCOLS: Final = ("sacn", "artnet")
SACN_MULTICAST: Final = "multicast"


class NetOutSettingError(ValueError):
    """Raised when the network output settings miss or misname a value; names the setting."""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"{key}: {message}")


@dataclass(frozen=True)
class NetOutSettings:
    """Free-form network output settings (``lighting.net_out.*``); there is no default destination.

    ``sacn_destination`` is ``"multicast"`` or a unicast IP. ``artnet_poll_address`` is where ArtPoll
    goes (a directed broadcast on a show LAN); ArtDmx only ever goes unicast to subscribers.
    """

    protocol: str | None = None
    universes: tuple[int, ...] = ()
    sacn_destination: str | None = None
    artnet_poll_address: str | None = None
    source_name: str = "Open DJ"
    sacn_port: int = 5568
    artnet_port: int = 6454

    def validate(self) -> None:
        """Raise ``NetOutSettingError`` naming the first missing or invalid setting."""
        if self.protocol not in NET_OUT_PROTOCOLS:
            raise NetOutSettingError(
                "lighting.net_out.protocol", f"{self.protocol!r} is not one of {', '.join(NET_OUT_PROTOCOLS)}"
            )
        if not self.universes:
            raise NetOutSettingError("lighting.net_out.universes", "no universe is named")
        if self.protocol == "sacn" and not self.sacn_destination:
            raise NetOutSettingError("lighting.net_out.sacn_destination", "no destination is set")
        if self.protocol == "artnet" and not self.artnet_poll_address:
            raise NetOutSettingError("lighting.net_out.artnet_poll_address", "no poll address is set")


def default_pick_log_path() -> Path:
    """Default ``variant_picks.ndjson`` path under the state dir."""
    return STATE_DIR / "lighting" / "variant_picks.ndjson"


class SettingError(ValueError):
    """Raised for an unknown key or a value outside the allowed set."""

    def __init__(self, key: str, value: str, allowed: tuple[str, ...]) -> None:
        self.key = key
        self.value = value
        self.allowed = allowed
        super().__init__(f"{key}: {value!r} is not allowed; allowed: {', '.join(allowed)}")


def setting(key: str) -> VariantSetting:
    """Registry row for ``key`` (accepts ``palette`` or ``lighting.palette``)."""
    full = key if key.startswith("lighting.") else f"lighting.{key}"
    row = SETTINGS.get(full)
    if row is None:
        raise SettingError(full, key, tuple(SETTINGS))
    return row


class LightingSettings:
    def __init__(self, pick_log: Path | None = None) -> None:
        self._values = {key: row.default for key, row in SETTINGS.items()}
        self._pick_log = pick_log

    def get(self, key: str) -> str:
        """Current value of a variant setting."""
        return self._values[setting(key).key]

    def as_dict(self) -> dict[str, str]:
        """All current values keyed by full setting key."""
        return dict(self._values)

    def set(self, key: str, value: str, *, decks_playing: int = 0) -> str:
        """Validate and apply ``value``; logs the pick when it changes; returns the previous value."""
        row = setting(key)
        if value not in row.allowed:
            raise SettingError(row.key, value, row.allowed)
        previous = self._values[row.key]
        if value != previous:
            self._values[row.key] = value
            self._log(row.key, previous, value, decks_playing)
        return previous

    def _log(self, key: str, previous: str, value: str, decks_playing: int) -> None:
        path = self._pick_log or default_pick_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "key": key,
            "from": previous,
            "to": value,
            "at": datetime.now(UTC).isoformat(),
            "decks_playing": decks_playing,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")


__all__ = [
    "ENERGY_CURVE_KEY",
    "HANDOVER_KEY",
    "MOVEMENT_KEY",
    "NET_OUT_PROTOCOLS",
    "PALETTE_KEY",
    "SACN_MULTICAST",
    "SETTINGS",
    "LightingSettings",
    "NetOutSettingError",
    "NetOutSettings",
    "SettingError",
    "VariantSetting",
    "default_pick_log_path",
    "setting",
]
