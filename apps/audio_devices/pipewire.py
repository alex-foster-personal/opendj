"""Parse ``pw-dump`` JSON into the output-device list (Linux PipeWire).

Pure and cross-platform: this module never runs ``pw-dump``; the caller passes the
JSON text it printed. Reimplemented from PipeWire's public object model: ``pw-dump``
prints a JSON array of objects, and a node (``type`` ``PipeWire:Interface:Node``)
carries ``info.props`` with ``media.class``, ``node.name`` (the stable identity
across restarts), ``node.description``, ``audio.channels`` and ``audio.position``.

Only ``Audio/Sink`` nodes become output devices. Every other node (sources, streams,
video, MIDI) and every non-node object is left out. A sink with a missing or malformed
required field raises ``PipeWireParseError`` rather than producing a guessed device.
Several nodes with the same ``node.name`` are one device, listed once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

NODE_TYPE: str = "PipeWire:Interface:Node"
AUDIO_SINK_CLASS: str = "Audio/Sink"


class PipeWireParseError(ValueError):
    """``pw-dump`` output is malformed where an output device depends on it."""


@dataclass(frozen=True)
class OutputDevice:
    """One selectable audio output: ``id`` and ``label`` as the output-device list carries them."""

    id: str
    label: str
    channels: int
    channel_positions: tuple[str, ...]


# -----------------------------------------------------------------------------


def _required_text(props: dict[str, Any], key: str, node_id: object) -> str:
    value = props.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PipeWireParseError(f"sink node {node_id}: {key} must be a non-empty string")
    return value.strip()


def _channels(props: dict[str, Any], node_id: object) -> tuple[int, tuple[str, ...]]:
    count = props.get("audio.channels")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise PipeWireParseError(f"sink node {node_id}: audio.channels must be a positive integer")
    raw_positions = props.get("audio.position")
    if raw_positions is None:
        return count, ()
    if not isinstance(raw_positions, str):
        raise PipeWireParseError(f"sink node {node_id}: audio.position must be a string")
    positions = tuple(part.strip() for part in raw_positions.split(",") if part.strip())
    if len(positions) != count:
        raise PipeWireParseError(f"sink node {node_id}: audio.position names {len(positions)} of {count} channels")
    return count, positions


def _sink_props(obj: object) -> dict[str, Any] | None:
    if not isinstance(obj, dict) or obj.get("type") != NODE_TYPE:
        return None
    info = obj.get("info")
    if not isinstance(info, dict) or not isinstance(info.get("props"), dict):
        raise PipeWireParseError(f"node {obj.get('id')}: info.props must be an object")
    props: dict[str, Any] = info["props"]
    return props if props.get("media.class") == AUDIO_SINK_CLASS else None


def parse_pw_dump(text: str) -> list[OutputDevice]:
    """Return the audio sinks in ``pw-dump`` output, one per stable ``node.name``, in dump order."""
    try:
        objects = json.loads(text)
    except json.JSONDecodeError as error:
        raise PipeWireParseError("pw-dump output is not JSON") from error
    if not isinstance(objects, list):
        raise PipeWireParseError("pw-dump output must be a JSON array")
    devices: dict[str, OutputDevice] = {}
    for obj in objects:
        props = _sink_props(obj)
        if props is None:
            continue
        node_id = obj.get("id")
        name = _required_text(props, "node.name", node_id)
        label = props.get("node.description") or props.get("node.nick") or name
        if not isinstance(label, str):
            raise PipeWireParseError(f"sink node {node_id}: node.description must be a string")
        count, positions = _channels(props, node_id)
        device = OutputDevice(id=name, label=label.strip(), channels=count, channel_positions=positions)
        existing = devices.get(name)
        if existing is not None and (existing.channels, existing.channel_positions) != (count, positions):
            raise PipeWireParseError(f"sink {name!r} appears twice with different channel layouts")
        devices.setdefault(name, device)
    return list(devices.values())
