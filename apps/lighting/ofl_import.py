"""Convert Open Fixture Library (OFL) fixture JSON into an Open DJ profile.

Reads the OFL layout (``availableChannels`` with ``capability`` or
``capabilities``, ``fineChannelAliases``, ``modes[].channels`` naming channels)
and builds profile JSON that ``parse_profile`` validates. Nothing is dropped:
a capability type with no Open DJ mapping raises ``ProfileError`` naming its
OFL JSON path.

- Fine channels (aliases from ``fineChannelAliases``) become ``fine_of`` pairs
  pointing at their coarse channel's offset in the mode; pan and tilt fines get
  ``pan_fine`` / ``tilt_fine``, others keep the coarse capability.
- A channel's ``capabilities`` list maps onto ``ranges`` (``dmxRange`` ->
  ``from``/``to``, the OFL type plus details -> ``meaning``). Its capability is
  the first non-``NoFunction`` entry's mapping.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apps.lighting.fixtures import FixtureProfile, ProfileError, parse_profile

_COLORS: Mapping[str, str] = {
    "Red": "red",
    "Green": "green",
    "Blue": "blue",
    "White": "white",
    "Warm White": "white",
    "Cold White": "white",
    "Amber": "amber",
    "UV": "uv",
}

_TYPES: Mapping[str, str] = {
    "Intensity": "intensity",
    "ShutterStrobe": "strobe",
    "Pan": "pan",
    "PanContinuous": "pan",
    "Tilt": "tilt",
    "TiltContinuous": "tilt",
    "PanTiltSpeed": "pt_speed",
    "WheelSlot": "color_wheel",
    "WheelRotation": "color_wheel",
    "ColorPreset": "color_wheel",
    "Effect": "macro",
    "EffectSpeed": "macro",
    "Maintenance": "generic",
    "Generic": "generic",
    "NoFunction": "generic",
}

_FINE: Mapping[str, str] = {"pan": "pan_fine", "tilt": "tilt_fine"}

_KINDS: Mapping[str, str] = {
    "Color Changer": "par",
    "Dimmer": "dimmer",
    "Strobe": "strobe",
    "Moving Head": "moving_head",
    "Pixel Bar": "bar",
}


def _capability(cap: object, path: str, errors: list[str]) -> str | None:
    if not isinstance(cap, dict):
        errors.append(f"{path}: must be an object")
        return None
    kind = cap.get("type")
    if kind == "ColorIntensity":
        color = _COLORS.get(str(cap.get("color")))
        if color is None:
            errors.append(f"{path}.color: unsupported OFL color {cap.get('color')!r}")
        return color
    mapped = _TYPES.get(str(kind))
    if mapped is None:
        errors.append(
            f"{path}.type: unsupported OFL capability type {kind!r}; supported: ColorIntensity, {', '.join(_TYPES)}"
        )
    return mapped


def _meaning(cap: Mapping[str, Any]) -> str:
    details = [str(cap[key]) for key in ("color", "shutterEffect", "effectName", "comment") if key in cap]
    return " ".join([str(cap.get("type")), *details])


def _channel(name: str, raw: object, path: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be an object")
        return None
    if "capabilities" in raw:
        caps = raw["capabilities"]
        if not isinstance(caps, list) or not caps:
            errors.append(f"{path}.capabilities: must be a non-empty list")
            return None
        mapped = [_capability(cap, f"{path}.capabilities[{i}]", errors) for i, cap in enumerate(caps)]
        if None in mapped:
            return None
        main = next((m for m, c in zip(mapped, caps, strict=True) if c.get("type") != "NoFunction"), mapped[0])
        ranges = []
        for i, cap in enumerate(caps):
            span = cap.get("dmxRange")
            if not (isinstance(span, list) and len(span) == 2):
                errors.append(f"{path}.capabilities[{i}].dmxRange: must be [from, to]")
                continue
            ranges.append({"from": span[0], "to": span[1], "meaning": _meaning(cap)})
        return {"capability": main, "ranges": ranges}
    if "capability" in raw:
        main = _capability(raw["capability"], f"{path}.capability", errors)
        return None if main is None else {"capability": main}
    errors.append(f"{path}: channel {name!r} has neither capability nor capabilities")
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "fixture"


def _modes(
    raw_modes: object,
    available: Mapping[str, Any],
    coarse: Mapping[str, dict[str, Any]],
    fine_to_coarse: Mapping[str, str],
    errors: list[str],
) -> list[dict[str, Any]]:
    modes_out: list[dict[str, Any]] = []
    if not isinstance(raw_modes, list) or not raw_modes:
        errors.append("$.modes: must be a non-empty list")
        raw_modes = []
    for m_index, mode in enumerate(raw_modes):
        m_path = f"$.modes[{m_index}]"
        names = mode.get("channels") if isinstance(mode, dict) else None
        if not isinstance(names, list):
            errors.append(f"{m_path}.channels: must be a list")
            continue
        offsets = {n: i for i, n in enumerate(names)}
        channels: list[dict[str, Any]] = []
        for c_index, name in enumerate(names):
            c_path = f"{m_path}.channels[{c_index}]"
            if name in fine_to_coarse:
                parent = fine_to_coarse[name]
                if parent not in offsets:
                    errors.append(f"{c_path}: fine channel {name!r} without its coarse {parent!r} in this mode")
                    continue
                cap = coarse.get(parent, {}).get("capability", "generic")
                channels.append({"capability": _FINE.get(cap, cap), "fine_of": offsets[parent]})
            elif name in coarse:
                channels.append(dict(coarse[name]))
            elif name not in available:
                errors.append(f"{c_path}: unknown channel {name!r}")
        modes_out.append({"name": str(mode.get("shortName") or mode.get("name")), "channels": channels})
    return modes_out


def convert_ofl(data: object, source: str = "<ofl>") -> dict[str, Any]:
    """Convert decoded OFL fixture JSON to Open DJ profile JSON; raises ``ProfileError``."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ProfileError(source, ["$: must be an object"])
    available = data.get("availableChannels")
    if not isinstance(available, dict) or not available:
        raise ProfileError(source, ["$.availableChannels: must be a non-empty object"])
    coarse: dict[str, dict[str, Any]] = {}
    fine_to_coarse: dict[str, str] = {}
    for name, raw in available.items():
        channel = _channel(name, raw, f"$.availableChannels.{name}", errors)
        if channel is not None:
            coarse[name] = channel
        aliases = raw.get("fineChannelAliases", []) if isinstance(raw, dict) else []
        for alias in aliases:
            fine_to_coarse[alias] = name
        if aliases and channel is not None:
            channel.pop("ranges", None)  # OFL ranges of a 16-bit channel are 16-bit values
    modes_out = _modes(data.get("modes"), available, coarse, fine_to_coarse, errors)
    if errors:
        raise ProfileError(source, errors)
    name = str(data.get("name") or "")
    categories = data.get("categories") or []
    kind = next((_KINDS[c] for c in categories if c in _KINDS), "generic")
    return {"id": _slug(str(data.get("shortName") or name)), "name": name, "kind": kind, "modes": modes_out}


def import_ofl(path: Path | str) -> FixtureProfile:
    """Read an OFL fixture JSON file and return the validated Open DJ profile."""
    ofl_path = Path(path)
    try:
        data = json.loads(ofl_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(str(ofl_path), [f"$: not valid JSON: {exc}"]) from exc
    return parse_profile(convert_ofl(data, str(ofl_path)), str(ofl_path))
