"""``opendj midi monitor``: print each incoming message with its mapped action.

    opendj midi monitor --device <name-match> [--seconds N]

The resolver reads the exported built-in maps at
``apps/audio-engine/maps/device-maps.json`` (the same JSON the engine embeds).
It does not re-implement device maps. The live port path is a thin wrapper
around the pure decoder, lookup and fader-verdict functions, which tests drive
with recorded bytes and no MIDI device.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.catalog import (
    MIDI_MONITOR_DEVICE_HELP,
    MIDI_MONITOR_SECONDS_HELP,
    MIDI_MONITOR_USAGE,
)
from apps.opendj_cli.refusal import fail
from apps.shared.platform_paths import PROJECT_ROOT

DEFAULT_MAPS_PATH = PROJECT_ROOT / "apps" / "audio-engine" / "maps" / "device-maps.json"

_Source = tuple[int, str, int]  # ch, kind, id


def load_builtin_maps(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the exported DeviceMap registry. Never invents bindings."""
    import json

    doc = json.loads((path or DEFAULT_MAPS_PATH).read_text(encoding="utf-8"))
    maps = doc.get("maps")
    if not isinstance(maps, list) or not maps:
        raise RuntimeError(f"{path or DEFAULT_MAPS_PATH} has no maps[]")
    return maps


def choose_map(maps: Sequence[Mapping[str, Any]], port_name: str) -> dict[str, Any] | None:
    """The first built-in map whose nameMatch matches ``port_name``."""
    for device_map in maps:
        pattern = str(device_map.get("nameMatch") or "")
        if pattern and re.search(pattern, port_name, flags=re.IGNORECASE):
            return dict(device_map)
    return None


def decode_source(data: Sequence[int]) -> tuple[_Source, int] | None:
    """Decode a raw MIDI triple the same way the page decoder does.

    Realtime bytes (status >= 0xF8) and families outside note/cc/pitchbend
    are dropped. Channel is 1..16.
    """
    if not data:
        return None
    status = data[0]
    if status >= 0xF8:
        return None
    if status < 0x80 or len(data) < 2:
        return None
    family = status & 0xF0
    channel = (status & 0x0F) + 1
    data1 = data[1]
    value = data[2] if len(data) > 2 else 0
    if family in (0x80, 0x90):
        return (channel, "note", data1), value
    if family == 0xB0:
        return (channel, "cc", data1), value
    if family == 0xE0:
        return (channel, "pitchbend", 0), value
    return None


def index_bindings(device_map: Mapping[str, Any]) -> dict[_Source, dict[str, Any]]:
    """Unshifted bindings keyed by (ch, kind, id)."""
    index: dict[_Source, dict[str, Any]] = {}
    for binding in device_map.get("bindings") or []:
        if binding.get("shift") is True:
            continue
        source = binding.get("source") or {}
        kind = source.get("kind")
        if kind not in {"note", "cc", "pitchbend"}:
            continue
        key = (int(source["ch"]), str(kind), int(source["id"]))
        index[key] = binding
    return index


def format_action(action: Mapping[str, Any]) -> str:
    """Action type and deck when the action carries one."""
    parts = [str(action.get("type") or "unknown")]
    if "deck" in action:
        parts.append(f"deck={action['deck']}")
    return " ".join(parts)


def format_source(source: _Source) -> str:
    channel, kind, ident = source
    return f"{kind} ch{channel} {ident}"


def format_line(source: _Source, value: int, binding: Mapping[str, Any] | None) -> str:
    """One printed row: the wire identity plus the action, or unmapped."""
    wire = format_source(source)
    if source[1] == "cc":
        wire = f"{wire} {value}"
    if binding is None:
        return f"{wire} unmapped"
    return f"{wire} {format_action(binding.get('action') or {})}"


def is_channel_fader(action: Mapping[str, Any]) -> bool:
    return action.get("type") == "mixer_channel" and action.get("target") == "fader"


def fader_label(action: Mapping[str, Any]) -> str:
    return f"ch{action.get('deck')} fader"


def fader_verdict(first: int, last: int) -> str:
    """First-to-last trend: rising is normal, falling is inverted."""
    if last > first:
        return "rising (normal)"
    if last < first:
        return "falling (inverted)"
    return "not moved"


@dataclass
class MonitorSession:
    """Lookup table plus the samples needed for the exit summary."""

    index: dict[_Source, dict[str, Any]]
    seen: set[_Source] = field(default_factory=set)
    fader_samples: dict[str, list[int]] = field(default_factory=dict)

    def handle(self, data: Sequence[int]) -> str | None:
        decoded = decode_source(data)
        if decoded is None:
            return None
        source, value = decoded
        binding = self.index.get(source)
        self.seen.add(source)
        action = (binding or {}).get("action") or {}
        if binding is not None and is_channel_fader(action):
            self.fader_samples.setdefault(fader_label(action), []).append(value)
        return format_line(source, value, binding)

    def fader_verdicts(self, device_map: Mapping[str, Any]) -> list[str]:
        labels: list[str] = []
        seen_labels: set[str] = set()
        for binding in device_map.get("bindings") or []:
            action = binding.get("action") or {}
            if not is_channel_fader(action):
                continue
            label = fader_label(action)
            if label in seen_labels:
                continue
            seen_labels.add(label)
            samples = self.fader_samples.get(label, [])
            if len(samples) < 2:
                labels.append(f"{label}: not moved")
            else:
                labels.append(f"{label}: {fader_verdict(samples[0], samples[-1])}")
        return labels

    def unseen(self, device_map: Mapping[str, Any]) -> list[str]:
        rows: list[str] = []
        for binding in device_map.get("bindings") or []:
            if binding.get("shift") is True:
                continue
            source = binding.get("source") or {}
            key = (int(source["ch"]), str(source["kind"]), int(source["id"]))
            if key in self.seen:
                continue
            rows.append(f"{format_source(key)} {format_action(binding.get('action') or {})}")
        return rows


def match_ports(names: Sequence[str], needle: str) -> list[tuple[int, str]]:
    """Case-insensitive substring match against port names."""
    lowered = needle.lower()
    return [(i, name) for i, name in enumerate(names) if lowered in name.lower()]


def refuse_no_matching_device(names: Sequence[str], needle: str, as_json: bool) -> int:
    listing = "\n".join(f"  [{i}] {name}" for i, name in enumerate(names)) or "  (none)"
    message = f"no input port matching {needle!r}. Ports:\n{listing}"
    return fail(as_json, "no_matching_device", message, EXIT_FAILED)


def refuse_ambiguous_device(hits: Sequence[tuple[int, str]], needle: str, as_json: bool) -> int:
    listing = "\n".join(f"  [{i}] {name}" for i, name in hits)
    message = f"ambiguous device {needle!r}, matches {len(hits)} ports:\n{listing}"
    return fail(as_json, "ambiguous_device", message, EXIT_FAILED)


def list_input_ports() -> list[str]:
    """Live MIDI input port names. Imported lazily so tests need no rtmidi.

    A host with no sequencer (no /dev/snd/seq, no CoreMIDI) is treated as
    having zero ports so `--device` can still refuse with the empty list.
    """
    import rtmidi

    try:
        midi_in = rtmidi.MidiIn()
    except (RuntimeError, OSError, rtmidi.SystemError):
        return []
    try:
        return list(midi_in.get_ports())
    finally:
        midi_in.delete()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opendj midi",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    verbs = parser.add_subparsers(dest="verb", required=True)
    monitor = verbs.add_parser("monitor", help=MIDI_MONITOR_USAGE)
    monitor.add_argument("--device", required=True, help=MIDI_MONITOR_DEVICE_HELP)
    monitor.add_argument("--seconds", type=float, default=None, help=MIDI_MONITOR_SECONDS_HELP)
    return parser


def _listen(
    port_name: str,
    ports: Sequence[str],
    seconds: float | None,
    on_bytes: Callable[[Sequence[int]], None],
) -> None:
    import rtmidi

    midi_in = rtmidi.MidiIn()
    try:
        midi_in.open_port(ports.index(port_name))
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            message = midi_in.get_message()
            if message is None:
                time.sleep(0.002)
                continue
            data, _delta = message
            on_bytes(list(data))
    finally:
        midi_in.close_port()
        midi_in.delete()


def run(
    argv: Sequence[str],
    *,
    as_json: bool = False,
    list_ports: Callable[[], Sequence[str]] | None = None,
    maps_path: Path | None = None,
    feed: Iterable[Sequence[int]] | None = None,
) -> int:
    """Entry point from ``opendj midi ...``. ``feed`` is the host-free test path."""
    args = _parser().parse_args(list(argv))

    ports = list(list_ports() if list_ports is not None else list_input_ports())
    if feed is None:
        hits = match_ports(ports, args.device)
        if not hits:
            return refuse_no_matching_device(ports, args.device, as_json)
        if len(hits) > 1:
            return refuse_ambiguous_device(hits, args.device, as_json)
        port_name = hits[0][1]
    else:
        port_name = args.device

    maps = load_builtin_maps(maps_path)
    device_map = choose_map(maps, port_name) or choose_map(maps, args.device) or {}
    session = MonitorSession(index=index_bindings(device_map))

    def _emit(data: Sequence[int]) -> None:
        line = session.handle(data)
        if line is not None:
            print(line, flush=True)

    try:
        if feed is not None:
            for data in feed:
                _emit(data)
        else:
            _listen(port_name, ports, args.seconds, _emit)
    except KeyboardInterrupt:
        pass

    if device_map:
        for line in session.fader_verdicts(device_map):
            print(line, flush=True)
        unseen = session.unseen(device_map)
        if unseen:
            print("unseen:", flush=True)
            for line in unseen:
                print(line, flush=True)
    return EXIT_CONFIRMED


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
