"""Build tools/deck-diagrams/devices/ddj-sx3/panel.json, the DDJ-SX3 practice-replica panel layout.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the device's top panel (origin
top-left, panel = the published width x depth), a function label, a hover text and the wire
identity (channel, note|cc, number) the real unit sends.

Sources:
- Panel size 664.0 x 354.4 mm: DDJ-SX3 specifications, "Maximum Dimensions (WxDxH)
  664.0 x 354.4 x 70.4 mm" (pioneerdj.com DDJ-SX3 specifications; Quick Start Guide,
  Specifications).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-sx3.ts, itself transcribed
  from the official "DDJ-SX3 List of MIDI messages" version 1.00 E1 (fig numbers kept as `fig`).
- Positions: the measured trace in devices/ddj-sx3/panel.reference.json (top-panel photo on
  page 1 of that PDF, separate x and y mm-per-px scales), normalised here for the renderer:
  centres to 0.5 mm, sizes to whole mm, row and column mates of one control type snapped onto
  one line when they lie within 1.5 mm. Scored against the reference by
  scripts/skins/control_position_error.py (ADR-0137).

Run: uv run --locked python tools/deck-diagrams/scripts/build_sx3_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 664.0
PANEL_D_MM = 354.4
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / "ddj-sx3"
OUT = DEVICE_DIR / "panel.json"
REFERENCE = DEVICE_DIR / "panel.reference.json"
ALIGN_MM = 1.5

GLOBAL_CH = 7
FX_CH = {1: 5, 2: 6}
PAD_CH = {1: 8, 2: 9, 3: 10, 4: 11}
COLOR_CC = {1: 0x17, 2: 0x18, 3: 0x19, 4: 0x1A}


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


# ----------------------------------------------------------------- positions


def _load_reference() -> dict[str, tuple[float, float, float, float]]:
    doc = json.loads(REFERENCE.read_text(encoding="utf-8"))
    if doc.get("measured") is not True:
        raise SystemExit("[ERROR] panel.reference.json is not a measurement (measured != true)")
    return {c["id"]: (c["x"], c["y"], c["w"], c["h"]) for c in doc["controls"]}


def _normalise(
    raw: dict[str, tuple[float, float, float, float]], types: dict[str, str]
) -> dict[str, tuple[float, float, float, float]]:
    """Centres to 0.5 mm, sizes to whole mm, mates of one type within ALIGN_MM snapped to one line."""
    cx = {k: v[0] + v[2] / 2 for k, v in raw.items()}
    cy = {k: v[1] + v[3] / 2 for k, v in raw.items()}
    size = {k: (max(1.0, round(v[2])), max(1.0, round(v[3]))) for k, v in raw.items()}
    for axis in (cx, cy):
        done: set[str] = set()
        for k in sorted(axis, key=axis.get):  # type: ignore[arg-type]
            if k in done:
                continue
            group = [j for j in axis if j not in done and types[j] == types[k] and abs(axis[j] - axis[k]) <= ALIGN_MM]
            mean = sum(axis[j] for j in group) / len(group)
            for j in group:
                axis[j] = mean
                done.add(j)
    out: dict[str, tuple[float, float, float, float]] = {}
    for k in raw:
        w, h = size[k]
        x = round(round(cx[k] * 2) / 2 - w / 2, 1)
        y = round(round(cy[k] * 2) / 2 - h / 2, 1)
        out[k] = (max(0.0, x), max(0.0, y), w, h)
    return out


# ----------------------------------------------------------------- deck strips

# fig, type, label, does, section, kind, number, shifted number (same channel) or None
DeckRow = tuple[str, str, str, str, str, str, int, int | None]
_DECK_ROWS: list[DeckRow] = [
    ("1-8", "button", "CENSOR", "Play backwards while held, then rejoin", "deck", "note", 0x15, 0x38),
    ("1-9", "button", "KEY UP", "Raise the key a semitone; with SHIFT, key sync", "deck", "note", 0x79, 0x64),
    ("1-10", "button", "KEY DOWN", "Lower the key a semitone; with SHIFT, key reset", "deck", "note", 0x0A, 0x65),
    ("1-4", "button", "SHIFT", "Hold for each control's second function", "deck", "note", 0x3F, None),
    ("1-3", "button", "SYNC", "Match tempo and beats to the master deck", "deck", "note", 0x5D, 0x5C),
    ("1-2", "button", "CUE", "Set or return to the cue point", "deck", "note", 0x0C, 0x48),
    ("1-1", "button", "PLAY/PAUSE", "Start or pause the deck", "deck", "note", 0x0B, 0x47),
    ("1-5", "button", "QUANTIZE", "Snap cues and loops to the beat grid", "deck", "note", 0x35, 0x39),
    ("1-6", "jog", "JOG", "Scratch or nudge the track", "deck", "cc", 0x22, 0x1F),
    ("1-7", "crossfader", "NEEDLE SEARCH", "Touch to jump to that point in the track", "deck", "cc", 0x03, 0x28),
    (
        "1-16",
        "button",
        "KEY LOCK",
        "Keep the key while the tempo changes; with SHIFT, tempo range",
        "deck",
        "note",
        0x1A,
        0x60,
    ),
    ("1-15", "button", "SLIP", "Keep the track running underneath loops and scratches", "deck", "note", 0x40, 0x17),
    ("1-17", "fader", "TEMPO", "Tempo fader", "deck", "cc", 0x00, 0x05),
    ("1-18", "button", "SLOT", "Pick the Flip slot; with SHIFT, save", "deck", "note", 0x49, 0x59),
    ("1-19", "button", "REC", "Record a Flip; with SHIFT, loop it", "deck", "note", 0x4A, 0x5A),
    ("1-20", "button", "START", "Play the Flip; with SHIFT, Flip on or off", "deck", "note", 0x4B, 0x5B),
    ("1-21", "button", "AUTO LOOP", "Loop the selected length, or exit the loop", "deck", "note", 0x14, 0x50),
    ("1-22", "button", "LOOP 1/2X", "Halve the loop; with SHIFT, shift the loop back", "deck", "note", 0x12, 0x61),
    ("1-23", "button", "LOOP 2X", "Double the loop; with SHIFT, shift the loop forward", "deck", "note", 0x13, 0x62),
    ("1-24", "button", "LOOP IN", "Set the loop in point; with SHIFT, slot select", "deck", "note", 0x10, 0x4C),
    ("1-25", "button", "LOOP OUT", "Set the loop out point; with SHIFT, reloop or exit", "deck", "note", 0x11, 0x4D),
    ("5-1", "button", "HOT CUE", "Pad mode: hot cues; with SHIFT, cue loop", "performance", "note", 0x1B, 0x69),
    ("5-2", "button", "ROLL", "Pad mode: loop roll; with SHIFT, saved loop", "performance", "note", 0x1E, 0x6B),
    ("5-3", "button", "SLICER", "Pad mode: slicer; with SHIFT, slicer loop", "performance", "note", 0x20, 0x6D),
    ("5-4", "button", "SAMPLER", "Pad mode: sampler; with SHIFT, pitch play", "performance", "note", 0x22, 0x6E),
    ("5-13", "button", "PARAM <", "Previous pad parameter", "performance", "note", 0x24, 0x01),
    ("5-14", "button", "PARAM >", "Next pad parameter", "performance", "note", 0x2C, 0x09),
]
_DECK_SLUG = {
    "CENSOR": "censor",
    "KEY UP": "key-up",
    "KEY DOWN": "key-down",
    "SHIFT": "shift",
    "SYNC": "sync",
    "CUE": "cue",
    "PLAY/PAUSE": "play-pause",
    "QUANTIZE": "quantize",
    "JOG": "jog",
    "NEEDLE SEARCH": "needle-search",
    "KEY LOCK": "key-lock",
    "SLIP": "slip",
    "TEMPO": "tempo",
    "SLOT": "slot",
    "REC": "rec",
    "START": "start",
    "AUTO LOOP": "auto-loop",
    "LOOP 1/2X": "loop-half",
    "LOOP 2X": "loop-double",
    "LOOP IN": "loop-in",
    "LOOP OUT": "loop-out",
    "HOT CUE": "hot-cue",
    "ROLL": "roll",
    "SLICER": "slicer",
    "SAMPLER": "sampler",
    "PARAM <": "param-prev",
    "PARAM >": "param-next",
}
# The two deck-select buttons on each physical deck: (fig, layer deck it selects).
_DECK_SELECT = {1: (("1-11", 1), ("1-12", 3)), 2: (("1-13", 2), ("1-14", 4))}


def _deck(deck: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    out: list[Control] = []
    for fig, typ, label, does, section, kind, num, shifted in _DECK_ROWS:
        cid = f"d{deck}-{_DECK_SLUG[label]}"
        x, y, w, h = pos[cid]
        shift = Wire(deck, kind, shifted) if shifted is not None else None
        out.append(
            Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, section, Wire(deck, kind, num), shift)
        )
    for fig, layer in _DECK_SELECT[deck]:
        cid = f"d{deck}-deck-{layer}"
        x, y, w, h = pos[cid]
        out.append(
            Control(
                cid,
                fig,
                "button",
                f"DECK {layer}",
                f"Switch this side to deck {layer}",
                x,
                y,
                w,
                h,
                "deck",
                Wire(layer, "note", 0x72),
                Wire(layer, "note", 0x73),
            )
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = pos[cid]
        out.append(
            Control(
                cid,
                f"5-{pad + 5}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode); with SHIFT, saved flip",
                x,
                y,
                w,
                h,
                "performance",
                Wire(PAD_CH[deck], "note", pad),
                Wire(PAD_CH[deck], "note", 0x08 + pad),
            )
        )
    return out


# ----------------------------------------------------------------- FX units


def _fx(unit: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    ch = FX_CH[unit]
    base = 0x50 if unit == 1 else 0x54
    rows: list[tuple[str, str, str, str, Wire, Wire | None]] = [
        ("2-1", "knob", "FX 1", "Effect 1 amount", Wire(ch, "cc", 0x02), Wire(ch, "cc", 0x12)),
        ("2-2", "knob", "FX 2", "Effect 2 amount", Wire(ch, "cc", 0x04), Wire(ch, "cc", 0x14)),
        ("2-3", "knob", "FX 3", "Effect 3 amount", Wire(ch, "cc", 0x06), Wire(ch, "cc", 0x16)),
        ("2-4", "encoder", "BEATS", "Effect beat length; press to tap", Wire(ch, "cc", 0x00), Wire(ch, "cc", 0x10)),
        (
            "2-5",
            "button",
            "ON 1",
            "Effect 1 on or off; with SHIFT, select",
            Wire(ch, "note", 0x47),
            Wire(ch, "note", 0x63),
        ),
        (
            "2-6",
            "button",
            "ON 2",
            "Effect 2 on or off; with SHIFT, select",
            Wire(ch, "note", 0x48),
            Wire(ch, "note", 0x64),
        ),
        (
            "2-7",
            "button",
            "ON 3",
            "Effect 3 on or off; with SHIFT, select",
            Wire(ch, "note", 0x49),
            Wire(ch, "note", 0x65),
        ),
        (
            "2-8",
            "button",
            f"CH{1 if unit == 1 else 2}",
            "Send this channel to the effect unit",
            Wire(GLOBAL_CH, "note", base),
            Wire(GLOBAL_CH, "note", base + 2),
        ),
        (
            "2-9",
            "button",
            f"CH{2 if unit == 1 else 4}",
            "Send this channel to the effect unit",
            Wire(GLOBAL_CH, "note", base + 1),
            Wire(GLOBAL_CH, "note", base + 3),
        ),
    ]
    slugs = ["knob-1", "knob-2", "knob-3", "beats", "on-1", "on-2", "on-3", "assign-1", "assign-2"]
    out: list[Control] = []
    for slug, (fig, typ, label, does, wire, shift) in zip(slugs, rows, strict=True):
        cid = f"fx{unit}-{slug}"
        x, y, w, h = pos[cid]
        out.append(Control(cid, fig, typ, label, f"{does} (FX{unit})", x, y, w, h, "effect", wire, shift))
    return out


# ----------------------------------------------------------------- mixer


def _channel(ch: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, Wire, Wire | None]] = [
        ("3-3", "knob", "TRIM", "Channel gain", "trim", Wire(ch, "cc", 0x04), None),
        ("3-4", "knob", "HI", "EQ high band", "hi", Wire(ch, "cc", 0x07), None),
        ("3-4", "knob", "MID", "EQ mid band", "mid", Wire(ch, "cc", 0x0B), None),
        ("3-4", "knob", "LOW", "EQ low band", "low", Wire(ch, "cc", 0x0F), None),
        ("3-6", "knob", "COLOR", "Sound color effect amount", "color", Wire(GLOBAL_CH, "cc", COLOR_CC[ch]), None),
        (
            "3-7",
            "button",
            "CUE",
            "Headphone cue for this channel; with SHIFT, mic reverb",
            "ch-cue",
            Wire(ch, "note", 0x54),
            Wire(ch, "note", 0x68),
        ),
        ("3-9", "fader", "CH FADER", "Channel volume", "ch-fader", Wire(ch, "cc", 0x13), None),
    ]
    out: list[Control] = []
    for fig, typ, label, does, slug, wire, shift in rows:
        cid = f"ch{ch}-{slug}"
        x, y, w, h = pos[cid]
        out.append(Control(cid, fig, typ, label, f"{does} (channel {ch})", x, y, w, h, "mixer", wire, shift))
    return out


def _globals(pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    g = GLOBAL_CH
    rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        (
            "browse",
            "4-1",
            "encoder",
            "BROWSE",
            "Scroll the library",
            "browse",
            Wire(g, "cc", 0x40),
            Wire(g, "cc", 0x64),
        ),
        (
            "load-1",
            "4-2",
            "button",
            "LOAD 1",
            "Load the selected track to deck 1",
            "browse",
            Wire(g, "note", 0x46),
            Wire(g, "note", 0x58),
        ),
        (
            "load-2",
            "4-2",
            "button",
            "LOAD 2",
            "Load the selected track to deck 2",
            "browse",
            Wire(g, "note", 0x47),
            Wire(g, "note", 0x59),
        ),
        (
            "load-3",
            "4-2",
            "button",
            "LOAD 3",
            "Load the selected track to deck 3",
            "browse",
            Wire(g, "note", 0x48),
            Wire(g, "note", 0x60),
        ),
        (
            "load-4",
            "4-2",
            "button",
            "LOAD 4",
            "Load the selected track to deck 4",
            "browse",
            Wire(g, "note", 0x49),
            Wire(g, "note", 0x61),
        ),
        (
            "back",
            "4-3",
            "button",
            "BACK",
            "Go back in the library",
            "browse",
            Wire(g, "note", 0x65),
            Wire(g, "note", 0x66),
        ),
        (
            "load-prepare",
            "4-4",
            "button",
            "LOAD PREPARE",
            "Add the track to the prepare crate",
            "browse",
            Wire(g, "note", 0x67),
            Wire(g, "note", 0x68),
        ),
        (
            "usb-a",
            "4-5",
            "button",
            "USB A",
            "Control the computer on USB A",
            "browse",
            Wire(g, "note", 0x69),
            Wire(g, "note", 0x6A),
        ),
        (
            "usb-b",
            "4-6",
            "button",
            "USB B",
            "Control the computer on USB B",
            "browse",
            Wire(g, "note", 0x6B),
            Wire(g, "note", 0x6C),
        ),
        (
            "echo",
            "3-14",
            "button",
            "ECHO",
            "Sound color effect: echo",
            "mixer",
            Wire(g, "note", 0x00),
            Wire(g, "note", 0x08),
        ),
        (
            "jet",
            "3-16",
            "button",
            "JET",
            "Sound color effect: jet",
            "mixer",
            Wire(g, "note", 0x03),
            Wire(g, "note", 0x0B),
        ),
        (
            "noise",
            "3-15",
            "button",
            "NOISE",
            "Sound color effect: noise",
            "mixer",
            Wire(g, "note", 0x02),
            Wire(g, "note", 0x0A),
        ),
        (
            "filter",
            "3-17",
            "button",
            "FILTER",
            "Sound color effect: filter",
            "mixer",
            Wire(g, "note", 0x01),
            Wire(g, "note", 0x09),
        ),
        (
            "master-cue",
            "3-18",
            "button",
            "MASTER CUE",
            "Hear the master in headphones",
            "mixer",
            Wire(g, "note", 0x63),
            Wire(g, "note", 0x62),
        ),
        (
            "sampler-volume",
            "3-20",
            "knob",
            "SAMPLER VOLUME",
            "Sampler output level",
            "mixer",
            Wire(g, "cc", 0x03),
            Wire(g, "cc", 0x69),
        ),
        (
            "crossfader",
            "3-11",
            "crossfader",
            "CROSSFADER",
            "Blend the left and right sides",
            "mixer",
            Wire(g, "cc", 0x1F),
            None,
        ),
    ]
    out: list[Control] = []
    for cid, fig, typ, label, does, section, wire, shift in rows:
        x, y, w, h = pos[cid]
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire, shift))
    return out


# ----------------------------------------------------------------- build


def build() -> dict[str, object]:
    raw = _load_reference()
    # Types are needed to normalise; build once on raw positions to learn them, then rebuild on the normalised set.
    draft = [
        *_deck(1, raw),
        *_deck(2, raw),
        *_fx(1, raw),
        *_fx(2, raw),
        *_channel(3, raw),
        *_channel(1, raw),
        *_channel(2, raw),
        *_channel(4, raw),
        *_globals(raw),
    ]
    types = {c.id: c.type for c in draft}
    missing_pos = sorted(set(types) - set(raw))
    extra_pos = sorted(set(raw) - set(types))
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] reference mismatch: missing={missing_pos} extra={extra_pos}")
    pos = _normalise(raw, types)
    controls = [replace(c, x=pos[c.id][0], y=pos[c.id][1], w=pos[c.id][2], h=pos[c.id][3]) for c in draft]
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    for c in controls:
        if c.x + c.w > PANEL_W_MM + 1e-6 or c.y + c.h > PANEL_D_MM + 1e-6:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")
    return {
        "format": "opendj-panel-layout/1",
        "device": "ddj-sx3",
        "display_name": "4-channel performance controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "SX3_MAP",
        "sources": {
            "panel_size": (
                "DDJ-SX3 Quick Start Guide (DRH1502A), Specifications, page 15: "
                "Max. external dimensions (W x D x H) 664.0 mm x 354.4 mm x 70.4 mm"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-sx3.ts (DDJ-SX3 List of MIDI messages version 1.00 E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/ddj-sx3/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity (SHIFT row: SAVED FLIP)",
            "MSB/LSB companion CCs: each control carries its MSB",
            "jog touch and wheel-side rows",
            "MASTER LEVEL, BOOTH MONITOR LEVEL, HEADPHONES LEVEL and MIX, MIC3 LEVEL: no MIDI message",
            "input selectors, CROSSFADER ASSIGN, crossfader curve, MIC3 and LINE/PHONO switches, level meters: slide switches and indicators",
            "NEEDLE SEARCH is drawn with the horizontal slider shape (crossfader type): the format has no touch-strip type yet",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
