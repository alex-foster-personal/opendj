"""Build tools/deck-diagrams/devices/ddj-flx4/panel.json, the practice-replica panel layout.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the device's top panel (origin
top-left, panel = the manual's published width x depth), a function label, a hover text and
the wire identity (channel, note|cc, number) the real unit sends.

Sources:
- Panel size 482.6 x 272.4 mm: DDJ-FLX4 operating instructions, Specifications.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts, itself transcribed
  from the official "DDJ-FLX4 List of MIDI messages" PDF v1.0 E1 (fig numbers kept as `fig`).
- Positions: a schematic trace of the manual's top-panel figure (control order and grouping),
  NOT measured; scripts/skins/control_position_error.py reports UNKNOWN until a measured
  reference table exists.

Run: uv run --locked python tools/deck-diagrams/scripts/build_flx4_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 482.6
PANEL_D_MM = 272.4
OUT = Path(__file__).resolve().parents[1] / "devices" / "ddj-flx4" / "panel.json"


@dataclass(frozen=True)
class Wire:
    ch: int
    kind: str  # note | cc
    id: int


@dataclass(frozen=True)
class Control:
    id: str
    fig: str
    type: str  # jog | fader | knob | pad | button | crossfader | encoder
    label: str
    does: str
    x: float
    y: float
    w: float
    h: float
    section: str
    midi: Wire
    shift_midi: Wire | None = None


Row = tuple[str, str, str, str, float, float, float, float, str, int]

# ----------------------------------------------------------------- helpers


def _mirror_x(x: float, w: float) -> float:
    return round(PANEL_W_MM - x - w, 1)


def _slug(label: str) -> str:
    named = label.lower().replace("<", "prev").replace(">", "next")
    raw = "".join(c if c.isalnum() else "-" for c in named).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw


# ----------------------------------------------------------------- deck strips

_DECK_ROWS: list[Row] = [
    # fig, type, label, does, x, y, w, h, kind, id
    ("1-5", "button", "IN", "Set the loop in point", 20, 22, 18, 9, "note", 0x10),
    ("1-6", "button", "OUT", "Set the loop out point", 42, 22, 18, 9, "note", 0x11),
    ("1-7", "button", "4 BEAT/EXIT", "Exit the active loop", 64, 22, 18, 9, "note", 0x4D),
    ("1-8", "button", "CALL <", "Call the previous cue or loop", 86, 22, 14, 9, "note", 0x51),
    ("1-9", "button", "CALL >", "Call the next cue or loop", 104, 22, 14, 9, "note", 0x53),
    ("1-10", "button", "BEAT SYNC", "Match tempo to the other deck", 140, 22, 22, 9, "note", 0x58),
    ("1-4", "jog", "JOG", "Scratch or nudge the track", 25, 42, 120, 120, "cc", 0x22),
    ("5-1", "button", "HOT CUE", "Pad mode: hot cues", 60, 170, 18, 8, "note", 0x1B),
    ("5-2", "button", "PAD FX", "Pad mode: pad effects", 82, 170, 18, 8, "note", 0x1E),
    ("5-3", "button", "BEAT JUMP", "Pad mode: beat jump", 104, 170, 18, 8, "note", 0x20),
    ("5-4", "button", "SAMPLER", "Pad mode: sampler", 126, 170, 18, 8, "note", 0x22),
    ("1-3", "button", "SHIFT", "Hold for each control's second function", 15, 184, 30, 12, "note", 0x3F),
    ("1-2", "button", "CUE", "Set or return to the cue point", 15, 206, 30, 18, "note", 0x0C),
    ("1-1", "button", "PLAY/PAUSE", "Start or pause the deck", 15, 232, 30, 18, "note", 0x0B),
]


def _deck(deck: int) -> list[Control]:
    pad_ch = 8 if deck == 1 else 10
    out = [
        Control(
            f"d{deck}-{_slug(label)}",
            fig,
            typ,
            label,
            f"{does} (deck {deck})",
            x if deck == 1 else _mirror_x(x, w),
            y,
            w,
            h,
            "deck",
            Wire(deck, kind, num),
        )
        for fig, typ, label, does, x, y, w, h, kind, num in _DECK_ROWS
    ]
    # TEMPO sits to the right of the jog on both decks, so it is translated, not mirrored.
    tempo_x = 160.0 if deck == 1 else 160.0 + 241.3
    out.append(
        Control(
            f"d{deck}-tempo",
            "1-11",
            "fader",
            "TEMPO",
            f"Tempo fader (deck {deck})",
            round(tempo_x, 1),
            48,
            12,
            120,
            "deck",
            Wire(deck, "cc", 0x00),
        )
    )
    for pad in range(8):
        x = 60 + (pad % 4) * 22
        out.append(
            Control(
                f"d{deck}-pad-{pad + 1}",
                "5-5",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x if deck == 1 else _mirror_x(x, 19),
                184 + (pad // 4) * 24,
                19,
                19,
                "performance",
                Wire(pad_ch, "note", pad),
            )
        )
    return out


def _channel(deck: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, float, float, float, float, str, int, int]] = [
        ("3-3", "knob", "TRIM", "Channel gain", 205, 44, 14, 14, "cc", deck, 0x04),
        ("3-4", "knob", "HI", "EQ high band", 205, 64, 14, 14, "cc", deck, 0x07),
        ("3-4", "knob", "MID", "EQ mid band", 205, 84, 14, 14, "cc", deck, 0x0B),
        ("3-4", "knob", "LOW", "EQ low band", 205, 104, 14, 14, "cc", deck, 0x0F),
        ("3-5", "knob", "CFX", "Color filter", 205, 124, 14, 14, "cc", 7, 0x17 if deck == 1 else 0x18),
        ("3-6", "button", "CH CUE", "Headphone cue for this channel", 203, 144, 18, 8, "note", deck, 0x54),
        ("3-7", "fader", "CH FADER", "Channel volume", 207, 158, 10, 62, "cc", deck, 0x13),
    ]
    return [
        Control(
            f"ch{deck}-{_slug(label)}",
            fig,
            typ,
            label,
            f"{does} (channel {deck})",
            x if deck == 1 else _mirror_x(x, w),
            y,
            w,
            h,
            "mixer",
            Wire(ch, kind, num),
        )
        for fig, typ, label, does, x, y, w, h, kind, ch, num in rows
    ]


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, float, float, float, float, str, int, str, int]] = [
        ("4-1", "encoder", "BROWSE", "Scroll the library", 231.3, 14, 20, 20, "browse", 7, "cc", 0x40),
        ("4-2", "button", "LOAD 1", "Load the selected track to deck 1", 200, 18, 20, 10, "browse", 7, "note", 0x46),
        ("4-3", "button", "LOAD 2", "Load the selected track to deck 2", 262.6, 18, 20, 10, "browse", 7, "note", 0x47),
        ("3-1", "knob", "MASTER", "Master output level", 234.3, 44, 14, 14, "mixer", 7, "cc", 0x08),
        ("3-2", "button", "MASTER CUE", "Hear the master in headphones", 232.3, 62, 18, 8, "mixer", 7, "note", 0x63),
        ("3-11", "knob", "PHONES MIX", "Headphone cue and master blend", 234.3, 76, 14, 14, "mixer", 7, "cc", 0x0C),
        ("3-12", "knob", "PHONES LEVEL", "Headphone level", 234.3, 94, 14, 14, "mixer", 7, "cc", 0x0D),
        ("3-9", "knob", "MIC LEVEL", "Microphone level", 234.3, 112, 14, 14, "mixer", 7, "cc", 0x05),
        ("3-10", "button", "SMART CFX", "Smart color effect", 232.3, 132, 18, 8, "mixer", 7, "note", 0x00),
        ("3-13", "button", "SMART FADER", "Smart fader", 232.3, 144, 18, 8, "mixer", 7, "note", 0x01),
        ("3-8", "crossfader", "CROSSFADER", "Blend deck 1 and deck 2", 211.3, 236, 60, 10, "mixer", 7, "cc", 0x1F),
        ("2-1", "button", "FX CH", "Pick the channel the beat effect hits", 300, 6, 16, 8, "effect", 5, "note", 0x10),
        ("2-2", "button", "FX SELECT", "Pick the beat effect", 320, 6, 16, 8, "effect", 5, "note", 0x4A),
        ("2-3", "button", "BEAT <", "Shorter effect beat", 340, 6, 14, 8, "effect", 5, "note", 0x4B),
        ("2-4", "button", "BEAT >", "Longer effect beat", 358, 6, 14, 8, "effect", 5, "note", 0x4C),
        ("2-5", "knob", "LEVEL/DEPTH", "Beat effect amount", 380, 3, 13, 13, "effect", 6, "cc", 0x02),
        ("2-6", "button", "FX ON", "Beat effect on or off", 400, 6, 18, 8, "effect", 5, "note", 0x47),
    ]
    return [
        Control(_slug(label), fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num))
        for fig, typ, label, does, x, y, w, h, section, ch, kind, num in rows
    ]


# ----------------------------------------------------------------- SHIFT layer

# The FLX4 encodes SHIFT in hardware: a shifted control sends a different number. Only the
# shifted rows FLX4_MAP knows (ddj-flx4.ts, as a binding or a hint) are listed; a control with
# no entry has no documented shifted row in the map yet (PLAY/PAUSE, CUE, pads, TEMPO, knobs).
_DECK_SHIFT: dict[str, tuple[str, int]] = {
    "in": ("note", 0x4C),
    "out": ("note", 0x4E),
    "4-beat-exit": ("note", 0x50),
    "call-prev": ("note", 0x3E),
    "call-next": ("note", 0x3D),
    "beat-sync": ("note", 0x60),
    "jog": ("cc", 0x29),
    "hot-cue": ("note", 0x69),
    "pad-fx": ("note", 0x6B),
    "beat-jump": ("note", 0x6D),
    "sampler": ("note", 0x6F),
}
_CHANNEL_SHIFT: dict[str, tuple[str, int]] = {"ch-cue": ("note", 0x68)}
_GLOBAL_SHIFT: dict[str, Wire] = {
    "browse": Wire(7, "cc", 0x64),
    "load-1": Wire(7, "note", 0x68),
    "load-2": Wire(7, "note", 0x7A),
    "master-cue": Wire(7, "note", 0x78),
    "smart-cfx": Wire(7, "note", 0x08),
    "smart-fader": Wire(7, "note", 0x09),
    "fx-select": Wire(5, "note", 0x64),
    "beat-prev": Wire(5, "note", 0x66),
    "beat-next": Wire(5, "note", 0x6B),
    "fx-on": Wire(5, "note", 0x43),
}


def _shift_wire(control: Control) -> Wire | None:
    head, _, rest = control.id.partition("-")
    if head in ("d1", "d2") and rest in _DECK_SHIFT:
        kind, num = _DECK_SHIFT[rest]
        return Wire(control.midi.ch, kind, num)
    if head in ("ch1", "ch2") and rest in _CHANNEL_SHIFT:
        kind, num = _CHANNEL_SHIFT[rest]
        return Wire(control.midi.ch, kind, num)
    return _GLOBAL_SHIFT.get(control.id)


def build() -> dict[str, object]:
    base = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    controls = [replace(c, shift_midi=_shift_wire(c)) for c in base]
    known = {c.id for c in controls}
    missing = sorted(set(_GLOBAL_SHIFT) - known)
    if missing:
        raise SystemExit(f"[ERROR] shift rows name unknown controls: {missing}")
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    return {
        "format": "opendj-panel-layout/1",
        "device": "ddj-flx4",
        "display_name": "2-deck entry controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "FLX4_MAP",
        "sources": {
            "panel_size": "DDJ-FLX4 operating instructions, Specifications (482.6 x 272.4 mm)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts (DDJ-FLX4 List of MIDI messages v1.0 E1)",
            "positions": "schematic trace of the manual's top-panel figure, not measured (ADR-0137)",
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity",
            "MSB/LSB companion CCs: each control carries its MSB",
            "jog touch and wheel-side rows",
            "Android MONO/STEREO switch and level meters: no performance input",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
