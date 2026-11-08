"""Open DJ fixture profiles: what each DMX channel of a fixture does, per mode.

Open DJ's own clean-room format (no fixture-library data or code). A profile is
JSON shaped as ``profile.schema.json`` documents; ``load_profile`` validates it
and raises ``ProfileError`` listing every problem by its JSON path.

    {"id": "acme-par-7", "name": "Acme RGB Par", "kind": "par",
     "modes": [{"name": "7ch", "channels": [
        {"capability": "intensity"}, {"capability": "red"}, ...,
        {"capability": "strobe", "ranges": [{"from": 0, "to": 9, "meaning": "off"},
                                             {"from": 10, "to": 255, "meaning": "slow..fast"}]}]}]}

A channel's offset is its index in ``channels`` (0-based, as DMX address minus
the fixture's start address). ``fine_of`` makes a 16-bit pair: it names the
offset of the coarse channel this one refines. ``pan_fine`` and ``tilt_fine``
must name a ``pan`` / ``tilt`` channel.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CAPABILITIES: tuple[str, ...] = (
    "intensity",
    "red",
    "green",
    "blue",
    "white",
    "amber",
    "uv",
    "strobe",
    "pan",
    "pan_fine",
    "tilt",
    "tilt_fine",
    "pt_speed",
    "color_wheel",
    "gobo",
    "shutter",
    "macro",
    "generic",
)

#: A fine capability -> the coarse capability its ``fine_of`` must name.
FINE_PAIRS: Mapping[str, str] = {"pan_fine": "pan", "tilt_fine": "tilt"}

DMX_MIN = 0
DMX_MAX = 255
DMX_UNIVERSE_CHANNELS = 512

_PROFILE_KEYS = frozenset({"id", "name", "kind", "modes"})
_MODE_KEYS = frozenset({"name", "channels"})
_CHANNEL_KEYS = frozenset({"capability", "fine_of", "ranges"})
_RANGE_KEYS = frozenset({"from", "to", "meaning"})


@dataclass(frozen=True)
class ValueRange:
    """DMX values ``start..end`` (inclusive) on one channel mean ``meaning``."""

    start: int
    end: int
    meaning: str


@dataclass(frozen=True)
class Channel:
    capability: str
    fine_of: int | None = None
    ranges: tuple[ValueRange, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Mode:
    name: str
    channels: tuple[Channel, ...]

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Each channel's capability, by offset."""
        return tuple(channel.capability for channel in self.channels)


@dataclass(frozen=True)
class FixtureProfile:
    id: str
    name: str
    kind: str
    modes: tuple[Mode, ...]

    def mode(self, name: str) -> Mode:
        for mode in self.modes:
            if mode.name == name:
                return mode
        raise KeyError(f"profile {self.id!r} has no mode {name!r}; modes: {[m.name for m in self.modes]}")


class ProfileError(ValueError):
    """A profile failed validation; ``errors`` holds one ``<json path>: <reason>`` per problem."""

    def __init__(self, source: str, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"invalid fixture profile {source}:\n" + "\n".join(f"  {e}" for e in self.errors))


# ----- validation helpers ------------------------------------------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, reason: str) -> None:
        self.items.append(f"{path}: {reason}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_dmx_value(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and DMX_MIN <= value <= DMX_MAX


def _object(value: object, path: str, allowed: frozenset[str], errors: _Errors) -> Mapping[str, Any] | None:
    if not isinstance(value, dict):
        errors.add(path, f"must be an object, got {type(value).__name__}")
        return None
    for key in sorted(set(value) - allowed):
        errors.add(f"{path}.{key}", f"unknown key; allowed: {sorted(allowed)}")
    return value


def _text(obj: Mapping[str, Any], key: str, path: str, errors: _Errors) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.add(f"{path}.{key}", "must be a non-empty string")
        return ""
    return value


def _ranges(raw: object, path: str, errors: _Errors) -> tuple[ValueRange, ...]:
    if not isinstance(raw, list):
        errors.add(path, "must be a list")
        return ()
    ranges: list[tuple[int, ValueRange]] = []
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        obj = _object(item, item_path, _RANGE_KEYS, errors)
        if obj is None:
            continue
        start, end = obj.get("from"), obj.get("to")
        meaning = _text(obj, "meaning", item_path, errors)
        for key, value in (("from", start), ("to", end)):
            if not _is_dmx_value(value):
                errors.add(f"{item_path}.{key}", f"must be an integer {DMX_MIN}..{DMX_MAX}, got {value!r}")
        if not (isinstance(start, int) and isinstance(end, int) and _is_dmx_value(start) and _is_dmx_value(end)):
            continue
        if start > end:
            errors.add(item_path, f"from {start} is above to {end}")
            continue
        ranges.append((index, ValueRange(start, end, meaning)))
    for i, (index_a, a) in enumerate(ranges):
        for index_b, b in ranges[i + 1 :]:
            if a.start <= b.end and b.start <= a.end:
                errors.add(path, f"ranges[{index_a}] {a.start}..{a.end} overlaps ranges[{index_b}] {b.start}..{b.end}")
    return tuple(r for _, r in ranges)


def _channel(raw: object, path: str, errors: _Errors) -> Channel | None:
    obj = _object(raw, path, _CHANNEL_KEYS, errors)
    if obj is None:
        return None
    capability = obj.get("capability")
    if capability not in CAPABILITIES:
        errors.add(f"{path}.capability", f"unknown capability {capability!r}; allowed: {', '.join(CAPABILITIES)}")
        return None
    fine_of = obj.get("fine_of")
    if fine_of is not None and not _is_int(fine_of):
        errors.add(f"{path}.fine_of", f"must be a channel offset (integer), got {fine_of!r}")
        fine_of = None
    ranges = _ranges(obj["ranges"], f"{path}.ranges", errors) if "ranges" in obj else ()
    return Channel(str(capability), fine_of, ranges)


def _check_fine_pairs(channels: Sequence[Channel], path: str, errors: _Errors) -> None:
    for offset, channel in enumerate(channels):
        channel_path = f"{path}[{offset}]"
        coarse_capability = FINE_PAIRS.get(channel.capability)
        if channel.fine_of is None:
            if coarse_capability is not None:
                errors.add(
                    f"{channel_path}.fine_of",
                    f"channel {offset} is {channel.capability} but names no coarse {coarse_capability} channel",
                )
            continue
        target = channel.fine_of
        if not 0 <= target < len(channels) or target == offset:
            errors.add(
                f"{channel_path}.fine_of", f"channel {offset} fine_of {target} is not another channel of this mode"
            )
            continue
        coarse = channels[target]
        if coarse.fine_of is not None:
            errors.add(f"{channel_path}.fine_of", f"channel {offset} fine_of {target}, which is itself a fine channel")
        elif coarse_capability is not None and coarse.capability != coarse_capability:
            errors.add(
                f"{channel_path}.fine_of",
                f"channel {offset} is {channel.capability} but channel {target} is {coarse.capability}, not {coarse_capability}",
            )
    claimed: dict[int, int] = {}
    for offset, channel in enumerate(channels):
        if channel.fine_of is None:
            continue
        if channel.fine_of in claimed:
            errors.add(
                f"{path}[{offset}].fine_of",
                f"channels {claimed[channel.fine_of]} and {offset} both refine channel {channel.fine_of}",
            )
        claimed.setdefault(channel.fine_of, offset)


def _mode(raw: object, path: str, errors: _Errors) -> Mode | None:
    obj = _object(raw, path, _MODE_KEYS, errors)
    if obj is None:
        return None
    name = _text(obj, "name", path, errors)
    raw_channels = obj.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        errors.add(f"{path}.channels", "must be a non-empty list")
        return None
    if len(raw_channels) > DMX_UNIVERSE_CHANNELS:
        errors.add(
            f"{path}.channels", f"{len(raw_channels)} channels exceed one DMX universe ({DMX_UNIVERSE_CHANNELS})"
        )
    parsed = [_channel(item, f"{path}.channels[{i}]", errors) for i, item in enumerate(raw_channels)]
    channels = [c for c in parsed if c is not None]
    if len(channels) != len(parsed):
        return None
    _check_fine_pairs(channels, f"{path}.channels", errors)
    return Mode(name, tuple(channels))


# ----- entry points ------------------------------------------------------


def parse_profile(data: object, source: str = "<data>") -> FixtureProfile:
    """Validate decoded profile JSON; raises ``ProfileError`` listing every problem."""
    errors = _Errors()
    obj = _object(data, "$", _PROFILE_KEYS, errors)
    if obj is None:
        raise ProfileError(source, errors.items)
    profile_id = _text(obj, "id", "$", errors)
    name = _text(obj, "name", "$", errors)
    kind = _text(obj, "kind", "$", errors)
    raw_modes = obj.get("modes")
    modes: list[Mode] = []
    if not isinstance(raw_modes, list) or not raw_modes:
        errors.add("$.modes", "must be a non-empty list")
    else:
        for index, item in enumerate(raw_modes):
            mode = _mode(item, f"$.modes[{index}]", errors)
            if mode is not None:
                modes.append(mode)
        seen: set[str] = set()
        for index, mode in enumerate(modes):
            if mode.name in seen:
                errors.add(f"$.modes[{index}].name", f"duplicate mode name {mode.name!r}")
            seen.add(mode.name)
    if errors.items:
        raise ProfileError(source, errors.items)
    return FixtureProfile(profile_id, name, kind, tuple(modes))


def load_profile(path: Path | str) -> FixtureProfile:
    """Read and validate one profile JSON file."""
    profile_path = Path(path)
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(str(profile_path), [f"$: not valid JSON: {exc}"]) from exc
    return parse_profile(data, str(profile_path))
