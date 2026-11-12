"""Generate controller MIDI bindings from a user-supplied vendor MIDI CSV export.

Offline only: this reads a CSV file the user exported from a vendor MIDI message list
and returns bindings in the installed device-map shape (``MidiMapDoc.bindings`` in
``apps/webui/server/routes/midi_maps.py``). It never reads a connected device and
never writes a shipped or installed map.

CSV columns (header row required, case-insensitive): ``control``, ``deck``,
``shift``, ``message``, ``channel``, ``number``. ``deck`` is 1 to 4, or empty for a
global control; ``shift`` is ``0`` or ``1``; ``message`` is ``note`` or ``cc``;
``channel`` is 1 to 16; ``number`` is 0 to 127 in decimal or ``0x`` hex. Controls
outside ``VENDOR_CONTROLS`` are returned as unsupported rows, never dropped silently.
Malformed or duplicated rows raise ``CsvMapError`` naming the row.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_COLUMNS: tuple[str, ...] = ("control", "deck", "shift", "message", "channel", "number")
MESSAGE_KINDS: dict[str, str] = {"note": "note", "cc": "cc"}
HOT_CUE_PREFIX: str = "HOT CUE "
HOT_CUE_SLOTS: frozenset[str] = frozenset("ABCDEFGH")

#: Vendor control name -> (action fields, whether the control is per deck).
VENDOR_CONTROLS: dict[str, tuple[dict[str, Any], bool]] = {
    "PLAY/PAUSE": ({"type": "deck_play_toggle"}, True),
    "CUE": ({"type": "deck_cue"}, True),
    "CHANNEL FADER": ({"type": "mixer_channel", "target": "fader"}, True),
    "TRIM": ({"type": "mixer_channel", "target": "trim"}, True),
    "EQ HI": ({"type": "mixer_channel", "target": "eq", "band": "high"}, True),
    "EQ MID": ({"type": "mixer_channel", "target": "eq", "band": "mid"}, True),
    "EQ LOW": ({"type": "mixer_channel", "target": "eq", "band": "low"}, True),
    "FILTER": ({"type": "mixer_channel", "target": "filter"}, True),
    "HEADPHONES CUE": ({"type": "channel_cue"}, True),
    "CROSSFADER": ({"type": "mixer_global", "target": "crossfader"}, False),
    "MASTER LEVEL": ({"type": "mixer_global", "target": "master"}, False),
}


class CsvMapError(ValueError):
    """A vendor CSV row is malformed or duplicates another row."""


@dataclass(frozen=True)
class UnsupportedRow:
    """A well-formed CSV row whose control has no action in ``VENDOR_CONTROLS``."""

    row: int
    control: str


@dataclass(frozen=True)
class GeneratedMap:
    """Bindings generated from one CSV, plus the rows no action covers."""

    bindings: tuple[dict[str, Any], ...]
    unsupported: tuple[UnsupportedRow, ...]


# -----------------------------------------------------------------------------
# Row parsing


def _int_field(raw: str, field: str, row: int, low: int, high: int) -> int:
    text = raw.strip()
    try:
        value = int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError as error:
        raise CsvMapError(f"row {row}: {field} {raw!r} is not a number") from error
    if not low <= value <= high:
        raise CsvMapError(f"row {row}: {field} {value} is outside {low}..{high}")
    return value


def _action_for(control: str, deck_raw: str, row: int) -> dict[str, Any] | None:
    if control.startswith(HOT_CUE_PREFIX):
        slot = control[len(HOT_CUE_PREFIX) :]
        if slot not in HOT_CUE_SLOTS:
            raise CsvMapError(f"row {row}: hot cue slot {slot!r} is not A..H")
        fields: dict[str, Any] = {"type": "deck_hot_cue", "slot": slot}
        per_deck = True
    elif control in VENDOR_CONTROLS:
        template, per_deck = VENDOR_CONTROLS[control]
        fields = dict(template)
    else:
        return None
    if per_deck:
        fields["deck"] = _int_field(deck_raw, "deck", row, 1, 4)
    elif deck_raw.strip():
        raise CsvMapError(f"row {row}: {control} is a global control and takes no deck")
    return fields


def _shift_field(raw: str, row: int) -> bool:
    if raw.strip() == "0":
        return False
    elif raw.strip() == "1":
        return True
    raise CsvMapError(f"row {row}: shift {raw!r} must be 0 or 1")


# -----------------------------------------------------------------------------


def generate_map(csv_path: Path) -> GeneratedMap:
    """Return the bindings the vendor CSV at ``csv_path`` describes, in file order."""
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise CsvMapError(f"{csv_path}: empty CSV, no header row")
        header = [name.strip().lower() for name in reader.fieldnames]
        missing = [column for column in REQUIRED_COLUMNS if column not in header]
        if missing:
            raise CsvMapError(f"{csv_path}: missing columns {missing}")
        rows = [{key.strip().lower(): value for key, value in raw.items() if key is not None} for raw in reader]
    bindings: list[dict[str, Any]] = []
    unsupported: list[UnsupportedRow] = []
    seen_sources: dict[tuple[int, str, int, bool], int] = {}
    seen_actions: dict[tuple[str, bool], int] = {}
    for row_number, row in enumerate(rows, start=2):
        if any(row.get(column) is None for column in REQUIRED_COLUMNS) or None in row.values():
            raise CsvMapError(f"row {row_number}: wrong number of fields")
        control = row["control"].strip().upper()
        if not control:
            raise CsvMapError(f"row {row_number}: control is empty")
        kind = MESSAGE_KINDS.get(row["message"].strip().lower())
        if kind is None:
            raise CsvMapError(f"row {row_number}: message {row['message']!r} must be note or cc")
        channel = _int_field(row["channel"], "channel", row_number, 1, 16)
        number = _int_field(row["number"], "number", row_number, 0, 127)
        shift = _shift_field(row["shift"], row_number)
        source_key = (channel, kind, number, shift)
        if source_key in seen_sources:
            raise CsvMapError(f"row {row_number}: same MIDI source as row {seen_sources[source_key]}")
        seen_sources[source_key] = row_number
        action = _action_for(control, row["deck"], row_number)
        if action is None:
            unsupported.append(UnsupportedRow(row=row_number, control=control))
            continue
        action_key = (repr(sorted(action.items())), shift)
        if action_key in seen_actions:
            raise CsvMapError(f"row {row_number}: same control as row {seen_actions[action_key]}")
        seen_actions[action_key] = row_number
        bindings.append(
            {
                "source": {"ch": channel, "kind": kind, "id": number},
                "action": action,
                "shift": shift,
                "provenance": {"tier": "vendor-pdf", "cite": f"{csv_path.name} row {row_number}"},
            }
        )
    return GeneratedMap(bindings=tuple(bindings), unsupported=tuple(unsupported))
