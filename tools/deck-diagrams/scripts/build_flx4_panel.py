"""Build tools/deck-diagrams/devices/ddj-flx4/panel.json, the practice-replica panel layout.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the device's top panel (origin
top-left, panel = the manual's published width x depth), a function label, a hover text and
the wire identity (channel, note|cc, number) the real unit sends.

Sources:
- Panel size 482.6 x 272.4 mm: DDJ-FLX4 operating instructions, Specifications.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx4.ts, itself transcribed
  from the official "DDJ-FLX4 List of MIDI messages" PDF v1.0 E1 (fig numbers kept as `fig`).
- Positions: measured from the top-panel photo on page 1, embedded image 0, of that PDF
  (sha256 e03ac376ebaa2d461c1a2c832c3c182e3579c893a1531944ebac1b5ce620388f), calibrated with
  separate x and y mm-per-px scales, normalised for the renderer (0.5 mm centres, whole-mm
  sizes, row or column mates aligned to within 1.5 mm). Deck 2 and channel 2 are translations
  of deck 1 and channel 1, not mirrors. Scored against `devices/ddj-flx4/panel.reference.json`
  by `scripts/skins/control_position_error.py` (ADR-0137).

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


Row = tuple[str, str, str, str, str, int]

# Measured layout (mm). One entry per control id; order in build() is unchanged from v1.
_POS: dict[str, tuple[float, float, float, float]] = {
    "d1-in": (14.0, 22.5, 18.0, 9.0),
    "d1-out": (39.0, 22.5, 18.0, 9.0),
    "d1-4-beat-exit": (64.0, 22.5, 18.0, 9.0),
    "d1-call-prev": (91.5, 22.5, 14.0, 9.0),
    "d1-call-next": (113.5, 22.5, 14.0, 9.0),
    "d1-beat-sync": (133.0, 22.5, 22.0, 9.0),
    "d1-jog": (23.0, 57.0, 120.0, 120.0),
    "d1-hot-cue": (41.0, 197.0, 18.0, 8.0),
    "d1-pad-fx": (63.5, 197.0, 18.0, 8.0),
    "d1-beat-jump": (85.5, 197.0, 18.0, 8.0),
    "d1-sampler": (108.0, 197.0, 18.0, 8.0),
    "d1-shift": (7.5, 178.5, 30.0, 12.0),
    "d1-cue": (7.5, 204.0, 30.0, 18.0),
    "d1-play-pause": (7.5, 232.5, 30.0, 18.0),
    "d1-tempo": (138.5, 156.0, 12.0, 120.0),
    "d1-pad-1": (40.5, 214.0, 19.0, 19.0),
    "d1-pad-2": (62.5, 214.0, 19.0, 19.0),
    "d1-pad-3": (85.0, 214.0, 19.0, 19.0),
    "d1-pad-4": (107.5, 214.0, 19.0, 19.0),
    "d1-pad-5": (40.5, 236.5, 19.0, 19.0),
    "d1-pad-6": (62.5, 236.5, 19.0, 19.0),
    "d1-pad-7": (85.0, 236.5, 19.0, 19.0),
    "d1-pad-8": (107.5, 236.5, 19.0, 19.0),
    "d2-in": (329.0, 22.5, 18.0, 9.0),
    "d2-out": (354.0, 22.5, 18.0, 9.0),
    "d2-4-beat-exit": (379.0, 22.5, 18.0, 9.0),
    "d2-call-prev": (406.5, 22.5, 14.0, 9.0),
    "d2-call-next": (428.5, 22.5, 14.0, 9.0),
    "d2-beat-sync": (448.0, 22.5, 22.0, 9.0),
    "d2-jog": (338.0, 57.0, 120.0, 120.0),
    "d2-hot-cue": (356.0, 197.0, 18.0, 8.0),
    "d2-pad-fx": (378.5, 197.0, 18.0, 8.0),
    "d2-beat-jump": (400.5, 197.0, 18.0, 8.0),
    "d2-sampler": (423.0, 197.0, 18.0, 8.0),
    "d2-shift": (322.5, 178.5, 30.0, 12.0),
    "d2-cue": (322.5, 204.0, 30.0, 18.0),
    "d2-play-pause": (322.5, 232.5, 30.0, 18.0),
    "d2-tempo": (453.5, 156.0, 12.0, 120.0),
    "d2-pad-1": (355.5, 214.0, 19.0, 19.0),
    "d2-pad-2": (377.5, 214.0, 19.0, 19.0),
    "d2-pad-3": (400.0, 214.0, 19.0, 19.0),
    "d2-pad-4": (422.5, 214.0, 19.0, 19.0),
    "d2-pad-5": (355.5, 236.5, 19.0, 19.0),
    "d2-pad-6": (377.5, 236.5, 19.0, 19.0),
    "d2-pad-7": (400.0, 236.5, 19.0, 19.0),
    "d2-pad-8": (422.5, 236.5, 19.0, 19.0),
    "ch1-trim": (213.5, 32.0, 14.0, 14.0),
    "ch1-hi": (213.5, 56.0, 14.0, 14.0),
    "ch1-mid": (213.5, 81.0, 14.0, 14.0),
    "ch1-low": (213.5, 105.5, 14.0, 14.0),
    "ch1-cfx": (213.5, 131.0, 14.0, 14.0),
    "ch1-ch-cue": (212.0, 160.5, 18.0, 8.0),
    "ch1-ch-fader": (216.0, 174.5, 10.0, 62.0),
    "ch2-trim": (254.5, 32.0, 14.0, 14.0),
    "ch2-hi": (254.5, 56.0, 14.0, 14.0),
    "ch2-mid": (254.5, 81.0, 14.0, 14.0),
    "ch2-low": (254.5, 105.5, 14.0, 14.0),
    "ch2-cfx": (254.5, 131.0, 14.0, 14.0),
    "ch2-ch-cue": (253.0, 160.5, 18.0, 8.0),
    "ch2-ch-fader": (257.0, 174.5, 10.0, 62.0),
    "browse": (231.0, 4.5, 20.0, 20.0),
    "load-1": (198.5, 12.5, 20.0, 10.0),
    "load-2": (263.5, 12.5, 20.0, 10.0),
    "master": (291.0, 30.0, 14.0, 14.0),
    "master-cue": (289.0, 60.0, 18.0, 8.0),
    "phones-mix": (177.0, 161.5, 14.0, 14.0),
    "phones-level": (177.0, 188.0, 14.0, 14.0),
    "mic-level": (177.0, 82.0, 14.0, 14.0),
    "smart-cfx": (175.0, 135.0, 18.0, 8.0),
    "smart-fader": (175.0, 220.5, 18.0, 8.0),
    "crossfader": (211.0, 250.0, 60.0, 10.0),
    "fx-ch": (290.0, 108.5, 16.0, 8.0),
    "fx-select": (290.0, 134.5, 16.0, 8.0),
    "beat-prev": (283.5, 160.0, 14.0, 8.0),
    "beat-next": (299.0, 160.0, 14.0, 8.0),
    "level-depth": (291.5, 187.5, 13.0, 13.0),
    "fx-on": (289.0, 220.5, 18.0, 8.0),
}

# ----------------------------------------------------------------- helpers


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = _POS[cid]
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _POS entry for {cid!r}") from e
    return round(x, 1), round(y, 1), w, h


def _slug(label: str) -> str:
    named = label.lower().replace("<", "prev").replace(">", "next")
    raw = "".join(c if c.isalnum() else "-" for c in named).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw


# ----------------------------------------------------------------- deck strips

_DECK_ROWS: list[Row] = [
    # fig, type, label, does, kind, hex id
    ("1-5", "button", "IN", "Set the loop in point", "note", 0x10),
    ("1-6", "button", "OUT", "Set the loop out point", "note", 0x11),
    ("1-7", "button", "4 BEAT/EXIT", "Exit the active loop", "note", 0x4D),
    ("1-8", "button", "CALL <", "Call the previous cue or loop", "note", 0x51),
    ("1-9", "button", "CALL >", "Call the next cue or loop", "note", 0x53),
    ("1-10", "button", "BEAT SYNC", "Match tempo to the other deck", "note", 0x58),
    ("1-4", "jog", "JOG", "Scratch or nudge the track", "cc", 0x22),
    ("5-1", "button", "HOT CUE", "Pad mode: hot cues", "note", 0x1B),
    ("5-2", "button", "PAD FX", "Pad mode: pad effects", "note", 0x1E),
    ("5-3", "button", "BEAT JUMP", "Pad mode: beat jump", "note", 0x20),
    ("5-4", "button", "SAMPLER", "Pad mode: sampler", "note", 0x22),
    ("1-3", "button", "SHIFT", "Hold for each control's second function", "note", 0x3F),
    ("1-2", "button", "CUE", "Set or return to the cue point", "note", 0x0C),
    ("1-1", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B),
]


def _deck(deck: int) -> list[Control]:
    pad_ch = 8 if deck == 1 else 10
    out: list[Control] = []
    for fig, typ, label, does, kind, num in _DECK_ROWS:
        cid = f"d{deck}-{_slug(label)}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (deck {deck})",
                x,
                y,
                w,
                h,
                "deck",
                Wire(deck, kind, num),
            )
        )
    tx, ty, tw, th = _at(f"d{deck}-tempo")
    out.append(
        Control(
            f"d{deck}-tempo",
            "1-11",
            "fader",
            "TEMPO",
            f"Tempo fader (deck {deck})",
            tx,
            ty,
            tw,
            th,
            "deck",
            Wire(deck, "cc", 0x00),
        )
    )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        px, py, pw, ph = _at(cid)
        out.append(
            Control(
                cid,
                "5-5",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                px,
                py,
                pw,
                ph,
                "performance",
                Wire(pad_ch, "note", pad),
            )
        )
    return out


def _channel(deck: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int, int]] = [
        ("3-3", "knob", "TRIM", "Channel gain", "cc", deck, 0x04),
        ("3-4", "knob", "HI", "EQ high band", "cc", deck, 0x07),
        ("3-4", "knob", "MID", "EQ mid band", "cc", deck, 0x0B),
        ("3-4", "knob", "LOW", "EQ low band", "cc", deck, 0x0F),
        ("3-5", "knob", "CFX", "Color filter", "cc", 7, 0x17 if deck == 1 else 0x18),
        ("3-6", "button", "CH CUE", "Headphone cue for this channel", "note", deck, 0x54),
        ("3-7", "fader", "CH FADER", "Channel volume", "cc", deck, 0x13),
    ]
    out: list[Control] = []
    for fig, typ, label, does, kind, ch, num in rows:
        cid = f"ch{deck}-{_slug(label)}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (channel {deck})",
                x,
                y,
                w,
                h,
                "mixer",
                Wire(ch, kind, num),
            )
        )
    return out


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int, str, int]] = [
        ("4-1", "encoder", "BROWSE", "Scroll the library", "browse", 7, "cc", 0x40),
        ("4-2", "button", "LOAD 1", "Load the selected track to deck 1", "browse", 7, "note", 0x46),
        ("4-3", "button", "LOAD 2", "Load the selected track to deck 2", "browse", 7, "note", 0x47),
        ("3-1", "knob", "MASTER", "Master output level", "mixer", 7, "cc", 0x08),
        ("3-2", "button", "MASTER CUE", "Hear the master in headphones", "mixer", 7, "note", 0x63),
        ("3-11", "knob", "PHONES MIX", "Headphone cue and master blend", "mixer", 7, "cc", 0x0C),
        ("3-12", "knob", "PHONES LEVEL", "Headphone level", "mixer", 7, "cc", 0x0D),
        ("3-9", "knob", "MIC LEVEL", "Microphone level", "mixer", 7, "cc", 0x05),
        ("3-10", "button", "SMART CFX", "Smart color effect", "mixer", 7, "note", 0x00),
        ("3-13", "button", "SMART FADER", "Smart fader", "mixer", 7, "note", 0x01),
        ("3-8", "crossfader", "CROSSFADER", "Blend deck 1 and deck 2", "mixer", 7, "cc", 0x1F),
        ("2-1", "button", "FX CH", "Pick the channel the beat effect hits", "effect", 5, "note", 0x10),
        ("2-2", "button", "FX SELECT", "Pick the beat effect", "effect", 5, "note", 0x4A),
        ("2-3", "button", "BEAT <", "Shorter effect beat", "effect", 5, "note", 0x4B),
        ("2-4", "button", "BEAT >", "Longer effect beat", "effect", 5, "note", 0x4C),
        ("2-5", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", 6, "cc", 0x02),
        ("2-6", "button", "FX ON", "Beat effect on or off", "effect", 5, "note", 0x47),
    ]
    out: list[Control] = []
    for fig, typ, label, does, section, ch, kind, num in rows:
        cid = _slug(label)
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num)))
    return out


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
    missing_pos = sorted(known - set(_POS))
    extra_pos = sorted(set(_POS) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _POS mismatch: missing={missing_pos} extra={extra_pos}")
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
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/ddj-flx4/panel.reference.json (ADR-0137)"
            ),
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
