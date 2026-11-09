"""Build tools/deck-diagrams/devices/ddj-rev7/panel.json, the DDJ-REV7 practice-replica panel layout.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the device's top panel (origin
top-left, panel = the maker's published width x depth), a function label, a hover text and
the wire identity (channel, note|cc, number) the real unit sends.

Sources:
- Panel size 732.0 x 382.2 mm: pioneerdj.com DDJ-REV7 product page, Specifications tab
  (docs/controller/research/DDJ-REV7-MIDI-Message-List.md).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev7.ts, itself transcribed
  from the official "DDJ-REV7 List of MIDI message" PDF E1 (fig ids kept as `fig`).
- Positions: measured from the top-panel photo on page 1, embedded image 1, of that PDF
  (sha256 22b9306a15704ef0938edd6f4278be4d1823ca492e5aa10d7f6ab0e82ab0b50a), calibrated with
  separate x and y mm-per-px scales, normalised for the renderer (0.5 mm centres, whole-mm
  sizes, row or column mates aligned to within 1.5 mm). Both decks are traced from the photo:
  the D-row block of deck 2 is a translation of deck 1, while the pad block sits against the
  mixer on each side, so neither a mirror nor a translation reproduces the whole deck. Scored
  against `devices/ddj-rev7/panel.reference.json` by `scripts/skins/control_position_error.py`.

Run: uv run --locked python tools/deck-diagrams/scripts/build_rev7_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 732.0
PANEL_D_MM = 382.2
OUT = Path(__file__).resolve().parents[1] / "devices" / "ddj-rev7" / "panel.json"

GLOBAL_CH = 7
FX_CH = {1: 5, 2: 6}


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


# Measured layout (mm), generated from devices/ddj-rev7/panel.reference.json by the trace
# pipeline and normalised for the renderer. One entry per control id.
_POS: dict[str, tuple[float, float, float, float]] = {
    "d1-tempo": (18.5, 15.5, 128.0, 26.0),
    "d1-tempo-range": (18.0, 51.0, 12.0, 12.0),
    "d1-tempo-reset": (76.5, 51.5, 10.0, 10.0),
    "d1-key-lock": (104.0, 51.5, 11.0, 11.0),
    "d1-sync": (134.0, 51.5, 11.0, 11.0),
    "d1-auto-loop": (20.0, 79.0, 34.0, 11.0),
    "d1-loop-half": (71.5, 79.0, 18.0, 11.0),
    "d1-loop-double": (90.0, 79.0, 18.0, 11.0),
    "d1-slip": (136.0, 79.0, 16.0, 11.0),
    "d1-censor": (155.5, 79.0, 16.0, 11.0),
    "d1-key-minus": (187.0, 79.0, 16.0, 11.0),
    "d1-key-plus": (206.0, 79.0, 16.0, 11.0),
    "d1-instant-scratch": (20.0, 104.0, 34.0, 9.0),
    "d1-slot-1": (72.0, 105.5, 20.0, 8.0),
    "d1-slot-2": (98.0, 104.0, 20.0, 9.0),
    "d1-slot-3": (124.0, 104.0, 20.0, 9.0),
    "d1-slot-4": (150.0, 104.0, 20.0, 9.0),
    "d1-hot-cue": (189.0, 104.0, 14.0, 9.0),
    "d1-scratch-bank": (206.0, 104.0, 16.0, 9.0),
    "d1-back": (188.5, 51.5, 13.0, 11.0),
    "d1-load": (204.0, 47.5, 20.0, 16.0),
    "d1-browse": (192.0, 11.5, 27.0, 27.0),
    "d1-stop-time": (199.5, 135.5, 13.0, 13.0),
    "d1-jog": (18.5, 133.5, 206.0, 209.0),
    "d1-start-stop": (194.5, 326.0, 31.0, 32.0),
    "d1-pitch-bend-minus": (20.0, 349.5, 14.0, 9.0),
    "d1-pitch-bend-plus": (37.5, 349.5, 15.0, 9.0),
    "d1-33": (153.0, 349.5, 15.0, 9.0),
    "d1-45": (170.0, 349.5, 15.0, 9.0),
    "d1-hot-cue-mode": (243.5, 161.5, 26.0, 6.0),
    "d1-roll-mode": (274.0, 161.5, 25.0, 6.0),
    "d1-saved-loop-mode": (304.0, 161.5, 25.0, 6.0),
    "d1-sampler-mode": (334.0, 161.5, 25.0, 6.0),
    "d1-pad-1": (244.0, 179.0, 25.0, 21.0),
    "d1-pad-2": (274.0, 179.0, 25.0, 21.0),
    "d1-pad-3": (304.0, 179.0, 25.0, 21.0),
    "d1-pad-4": (334.0, 179.0, 25.0, 21.0),
    "d1-pad-5": (244.0, 205.0, 25.0, 21.0),
    "d1-pad-6": (274.0, 205.0, 25.0, 21.0),
    "d1-pad-7": (304.0, 205.0, 25.0, 21.0),
    "d1-pad-8": (334.0, 205.0, 25.0, 21.0),
    "d1-param-prev": (243.5, 139.0, 13.0, 10.0),
    "d1-param-next": (256.5, 139.0, 13.0, 10.0),
    "d1-silent-cue": (275.5, 138.5, 9.0, 9.0),
    "d2-tempo": (509.5, 15.5, 125.0, 26.0),
    "d2-tempo-range": (507.5, 51.0, 12.0, 12.0),
    "d2-tempo-reset": (567.0, 51.5, 10.0, 10.0),
    "d2-key-lock": (594.0, 51.5, 11.0, 11.0),
    "d2-sync": (624.0, 51.5, 11.0, 11.0),
    "d2-auto-loop": (508.5, 79.0, 36.0, 11.0),
    "d2-loop-half": (562.0, 79.0, 18.0, 11.0),
    "d2-loop-double": (581.0, 79.0, 16.0, 11.0),
    "d2-slip": (626.0, 79.0, 16.0, 10.0),
    "d2-censor": (645.0, 78.5, 16.0, 12.0),
    "d2-key-minus": (678.5, 79.0, 16.0, 11.0),
    "d2-key-plus": (695.5, 79.0, 18.0, 11.0),
    "d2-instant-scratch": (509.5, 104.0, 34.0, 9.0),
    "d2-slot-1": (562.0, 104.0, 20.0, 9.0),
    "d2-slot-2": (588.0, 104.0, 20.0, 9.0),
    "d2-slot-3": (614.0, 104.0, 20.0, 9.0),
    "d2-slot-4": (640.0, 104.0, 20.0, 9.0),
    "d2-hot-cue": (679.0, 104.0, 15.0, 9.0),
    "d2-scratch-bank": (695.5, 104.0, 16.0, 9.0),
    "d2-back": (678.5, 51.5, 13.0, 11.0),
    "d2-load": (693.5, 47.5, 21.0, 16.0),
    "d2-browse": (683.0, 12.5, 25.0, 25.0),
    "d2-stop-time": (689.5, 135.5, 13.0, 13.0),
    "d2-jog": (509.0, 134.5, 204.0, 208.0),
    "d2-start-stop": (684.0, 326.0, 32.0, 32.0),
    "d2-pitch-bend-minus": (509.5, 349.5, 15.0, 9.0),
    "d2-pitch-bend-plus": (528.0, 349.5, 15.0, 9.0),
    "d2-33": (643.5, 349.5, 14.0, 9.0),
    "d2-45": (659.5, 349.5, 15.0, 9.0),
    "d2-hot-cue-mode": (373.0, 161.5, 25.0, 6.0),
    "d2-roll-mode": (402.5, 161.5, 26.0, 6.0),
    "d2-saved-loop-mode": (433.0, 161.5, 25.0, 6.0),
    "d2-sampler-mode": (463.0, 161.5, 25.0, 6.0),
    "d2-pad-1": (373.0, 179.0, 25.0, 21.0),
    "d2-pad-2": (403.0, 179.0, 25.0, 21.0),
    "d2-pad-3": (433.0, 179.0, 25.0, 21.0),
    "d2-pad-4": (463.0, 179.0, 25.0, 21.0),
    "d2-pad-5": (373.0, 205.0, 25.0, 21.0),
    "d2-pad-6": (403.0, 205.0, 25.0, 21.0),
    "d2-pad-7": (433.0, 205.0, 25.0, 21.0),
    "d2-pad-8": (463.0, 205.0, 25.0, 21.0),
    "d2-silent-cue": (447.5, 138.5, 9.0, 9.0),
    "d2-param-prev": (464.0, 139.0, 12.0, 10.0),
    "d2-param-next": (476.0, 139.0, 14.0, 10.0),
    "ch1-input-select": (257.0, 21.5, 10.0, 8.0),
    "ch1-trim": (256.5, 44.5, 13.0, 13.0),
    "ch1-filter": (253.0, 66.5, 20.0, 20.0),
    "ch1-hi": (296.5, 19.5, 12.0, 12.0),
    "ch1-mid": (296.5, 45.0, 12.0, 12.0),
    "ch1-low": (296.5, 69.5, 12.0, 12.0),
    "ch1-ch-fader": (313.5, 253.5, 22.0, 46.0),
    "ch2-input-select": (465.0, 21.5, 10.0, 8.0),
    "ch2-trim": (462.5, 44.5, 13.0, 13.0),
    "ch2-filter": (459.0, 66.5, 20.0, 20.0),
    "ch2-hi": (422.5, 19.5, 12.0, 12.0),
    "ch2-mid": (422.5, 45.0, 12.0, 12.0),
    "ch2-low": (421.5, 68.5, 13.0, 14.0),
    "ch2-ch-fader": (395.0, 253.5, 24.0, 46.0),
    "booth": (338.0, 18.0, 15.0, 16.0),
    "master": (379.5, 19.5, 12.0, 12.0),
    "shift": (359.0, 141.0, 15.0, 8.0),
    "crossfader": (343.0, 332.5, 49.0, 22.0),
    "sampler-volume": (248.0, 240.5, 9.0, 9.0),
    "phones-level": (447.5, 240.5, 9.0, 9.0),
    "phones-mix": (471.5, 240.5, 9.0, 9.0),
    "phones-cue-fader": (467.5, 262.5, 24.0, 13.0),
    "fx-echo": (243.5, 105.5, 10.0, 8.0),
    "fx-spin": (257.0, 105.5, 10.0, 8.0),
    "fx-flanger": (272.5, 105.5, 10.0, 8.0),
    "fx-reverb": (449.5, 105.5, 10.0, 8.0),
    "fx-brake": (465.0, 105.5, 10.0, 8.0),
    "fx-duck-down": (478.5, 105.5, 10.0, 8.0),
    "fx-tap": (358.5, 82.0, 16.0, 11.0),
    "beat-prev": (338.5, 82.0, 16.0, 11.0),
    "beat-next": (377.0, 82.0, 17.0, 11.0),
    "level-depth": (358.5, 111.0, 18.0, 18.0),
    "fx-lever-1": (301.5, 108.0, 30.0, 23.0),
    "fx-lever-2": (401.5, 108.0, 31.0, 23.0),
    "smooth-echo": (248.0, 268.0, 12.0, 8.0),
}

# ----------------------------------------------------------------- helpers


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = _POS[cid]
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _POS entry for {cid!r}") from e
    return round(x, 1), round(y, 1), w, h


def _wire(ch: int, kind: str, num: int | None) -> Wire | None:
    return None if num is None else Wire(ch, kind, num)


def _ctl(
    cid: str,
    fig: str,
    typ: str,
    label: str,
    does: str,
    section: str,
    midi: Wire,
    shift: Wire | None = None,
) -> Control:
    x, y, w, h = _at(cid)
    return Control(cid, fig, typ, label, does, x, y, w, h, section, midi, shift)


# ----------------------------------------------------------------- deck strips

# slug, fig, type, label, does, kind, id, shift id (same kind, same channel) or None
DeckRow = tuple[str, str, str, str, str, str, int, int | None]

_DECK_ROWS: list[DeckRow] = [
    ("start-stop", "D24", "button", "START/STOP", "Start or stop the deck", "note", 0x0B, 0x47),
    ("tempo", "D11", "fader", "TEMPO", "Tempo fader", "cc", 0x00, 0x05),
    ("jog", "D3", "jog", "JOG", "Scratch or nudge the track", "cc", 0x22, 0x29),
    ("auto-loop", "D4", "button", "AUTO LOOP", "Engage or release an auto loop", "note", 0x14, 0x50),
    ("loop-half", "D5", "button", "LOOP 1/2X", "Halve the loop", "note", 0x12, 0x10),
    ("loop-double", "D6", "button", "LOOP 2X", "Double the loop", "note", 0x13, 0x11),
    ("tempo-range", "D7", "button", "TEMPO RANGE", "Cycle the tempo fader range", "note", 0x60, 0x34),
    ("tempo-reset", "D8", "button", "TEMPO RESET", "Return to the track's own tempo", "note", 0x45, 0x37),
    ("key-lock", "D9", "button", "KEY LOCK", "Keep the key while the tempo changes", "note", 0x1A, 0x19),
    ("sync", "D10", "button", "SYNC", "Match tempo to the other deck", "note", 0x58, 0x5C),
    ("slip", "D12", "button", "SLIP", "Slip mode", "note", 0x40, 0x17),
    ("censor", "D13", "button", "CENSOR", "Play backwards while held", "note", 0x15, 0x38),
    ("key-minus", "D14", "button", "KEY -", "Key down a semitone", "note", 0x1F, 0x65),
    ("key-plus", "D15", "button", "KEY +", "Key up a semitone", "note", 0x1C, 0x64),
    ("instant-scratch", "D16", "button", "INSTANT SCRATCH", "Instant scratch mode", "note", 0x43, 0x44),
    ("hot-cue", "D17", "button", "HOT CUE", "Instant scratch bank: hot cues", "note", 0x49, 0x4A),
    ("scratch-bank", "D18", "button", "SCRATCH BANK", "Instant scratch bank: scratch sounds", "note", 0x5A, 0x5B),
    ("stop-time", "D23", "knob", "STOP TIME", "Platter stop time", "cc", 0x08, None),
    ("pitch-bend-minus", "D1", "button", "PITCH BEND -", "Bend the tempo down while held", "note", 0x39, 0x59),
    ("pitch-bend-plus", "D2", "button", "PITCH BEND +", "Bend the tempo up while held", "note", 0x3A, 0x5D),
    ("33", "D25", "button", "33", "Platter at 33 rpm", "note", 0x61, 0x4B),
    ("45", "D26", "button", "45", "Platter at 45 rpm", "note", 0x62, 0x4C),
    ("silent-cue", "M9", "button", "SILENT CUE", "Silent cue", "note", 0x73, 0x77),
    ("hot-cue-mode", "P1", "button", "HOT CUE MODE", "Pad mode: hot cues", "note", 0x1B, 0x69),
    ("roll-mode", "P2", "button", "ROLL MODE", "Pad mode: loop roll", "note", 0x1E, 0x6B),
    ("saved-loop-mode", "P3", "button", "SAVED LOOP MODE", "Pad mode: saved loops", "note", 0x20, 0x6D),
    ("sampler-mode", "P4", "button", "SAMPLER MODE", "Pad mode: sampler", "note", 0x22, 0x6F),
    ("param-prev", "P13", "button", "PARAMETER <", "Pad parameter back", "note", 0x24, 0x01),
    ("param-next", "P14", "button", "PARAMETER >", "Pad parameter forward", "note", 0x2C, 0x09),
]

_DECK_SECTION = {
    "M9": "mixer",
    "P1": "performance",
    "P2": "performance",
    "P3": "performance",
    "P4": "performance",
    "P13": "performance",
    "P14": "performance",
}

_SLOT_FIGS = ["D19", "D20", "D21", "D22"]
_LOAD_NOTE = {1: 0x46, 2: 0x47}
_LOAD_SHIFT_NOTE = {1: 0x5D, 2: 0x6D}


def _deck(deck: int) -> list[Control]:
    ch = deck
    out: list[Control] = []
    for slug, fig, typ, label, does, kind, num, shift in _DECK_ROWS:
        out.append(
            _ctl(
                f"d{deck}-{slug}",
                fig,
                typ,
                label,
                f"{does} (deck {deck})",
                _DECK_SECTION.get(fig, "deck"),
                Wire(ch, kind, num),
                _wire(ch, kind, shift),
            )
        )
    slot_ch = 2 + deck
    for slot, fig in enumerate(_SLOT_FIGS):
        out.append(
            _ctl(
                f"d{deck}-slot-{slot + 1}",
                fig,
                "button",
                f"SLOT {slot + 1}",
                f"Instant scratch slot {slot + 1}, pad {slot + 1} (deck {deck})",
                "deck",
                Wire(slot_ch, "note", slot),
                Wire(slot_ch, "note", 4 + slot),
            )
        )
    pad_ch = 6 + 2 * deck
    out.extend(
        _ctl(
            f"d{deck}-pad-{pad + 1}",
            f"P{5 + pad}",
            "pad",
            f"PAD {pad + 1}",
            f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
            "performance",
            Wire(pad_ch, "note", pad),
            Wire(pad_ch, "note", 8 + pad),
        )
        for pad in range(8)
    )
    out.append(
        _ctl(
            f"d{deck}-browse",
            "B1",
            "encoder",
            "BROWSE",
            f"Scroll the library (deck {deck} side)",
            "browse",
            Wire(GLOBAL_CH, "cc", 0x40),
            Wire(GLOBAL_CH, "cc", 0x64),
        )
    )
    out.append(
        _ctl(
            f"d{deck}-back",
            "B2",
            "button",
            "BACK",
            f"Back in the browser (deck {deck} side)",
            "browse",
            Wire(GLOBAL_CH, "note", 0x65),
            Wire(GLOBAL_CH, "note", 0x66),
        )
    )
    out.append(
        _ctl(
            f"d{deck}-load",
            "B3",
            "button",
            "LOAD",
            f"Load the selected track to deck {deck}",
            "browse",
            Wire(GLOBAL_CH, "note", _LOAD_NOTE[deck]),
            Wire(GLOBAL_CH, "note", _LOAD_SHIFT_NOTE[deck]),
        )
    )
    return out


# ----------------------------------------------------------------- mixer strips

_FILTER_CC = {1: 0x17, 2: 0x18}


def _channel(deck: int) -> list[Control]:
    ch = deck
    rows: list[tuple[str, str, str, str, str, Wire]] = [
        (
            "input-select",
            "M1",
            "button",
            "INPUT SELECT",
            "Channel input: USB A, PHONO/LINE or USB B",
            Wire(ch, "note", 0x55),
        ),
        ("trim", "M4", "knob", "TRIM", "Channel gain", Wire(ch, "cc", 0x04)),
        ("filter", "M5", "knob", "FILTER", "Channel filter, LPF to HPF", Wire(GLOBAL_CH, "cc", _FILTER_CC[deck])),
        ("hi", "M6", "knob", "ISO HI", "Isolator high band", Wire(ch, "cc", 0x07)),
        ("mid", "M7", "knob", "ISO MID", "Isolator mid band", Wire(ch, "cc", 0x0B)),
        ("low", "M8", "knob", "ISO LOW", "Isolator low band", Wire(ch, "cc", 0x0F)),
        ("ch-fader", "M11", "fader", "CH FADER", "Channel volume", Wire(ch, "cc", 0x13)),
    ]
    return [
        _ctl(f"ch{deck}-{slug}", fig, typ, label, f"{does} (channel {deck})", "mixer", midi)
        for slug, fig, typ, label, does, midi in rows
    ]


# ----------------------------------------------------------------- globals


def _globals() -> list[Control]:
    g = GLOBAL_CH
    rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        ("booth", "M2", "knob", "BOOTH", "Booth output level", "mixer", Wire(g, "cc", 0x09), None),
        ("master", "M3", "knob", "MASTER", "Master output level", "mixer", Wire(g, "cc", 0x08), None),
        (
            "shift",
            "M10",
            "button",
            "SHIFT",
            "Hold for each control's second function",
            "mixer",
            Wire(g, "note", 0x3F),
            None,
        ),
        (
            "crossfader",
            "M12",
            "crossfader",
            "CROSSFADER",
            "Blend deck 1 and deck 2",
            "mixer",
            Wire(g, "cc", 0x1F),
            None,
        ),
        ("sampler-volume", "M13", "knob", "SAMPLER VOLUME", "Sampler level", "mixer", Wire(g, "cc", 0x03), None),
        ("phones-level", "M14", "knob", "PHONES LEVEL", "Headphone level", "mixer", Wire(g, "cc", 0x0D), None),
        (
            "phones-mix",
            "M15",
            "knob",
            "PHONES MIX",
            "Headphone cue and master blend",
            "mixer",
            Wire(g, "cc", 0x0C),
            None,
        ),
        (
            "phones-cue-fader",
            "M16",
            "crossfader",
            "PHONES CUE",
            "Headphone cue blend, channel 1 to channel 2",
            "mixer",
            Wire(g, "cc", 0x0E),
            None,
        ),
        (
            "fx-echo",
            "E1",
            "button",
            "ECHO",
            "Select the ECHO effect",
            "effect",
            Wire(g, "note", 0x00),
            Wire(g, "note", 0x0C),
        ),
        (
            "fx-spin",
            "E2",
            "button",
            "SPIN",
            "Select the SPIN effect",
            "effect",
            Wire(g, "note", 0x01),
            Wire(g, "note", 0x0D),
        ),
        (
            "fx-flanger",
            "E3",
            "button",
            "FLANGER",
            "Select the FLANGER effect",
            "effect",
            Wire(g, "note", 0x02),
            Wire(g, "note", 0x0E),
        ),
        (
            "fx-reverb",
            "E4",
            "button",
            "REVERB",
            "Select the REVERB effect",
            "effect",
            Wire(g, "note", 0x03),
            Wire(g, "note", 0x0F),
        ),
        (
            "fx-brake",
            "E5",
            "button",
            "BRAKE",
            "Select the BRAKE effect",
            "effect",
            Wire(g, "note", 0x04),
            Wire(g, "note", 0x10),
        ),
        (
            "fx-duck-down",
            "E6",
            "button",
            "DUCK DOWN",
            "Select the DUCK DOWN effect",
            "effect",
            Wire(g, "note", 0x05),
            Wire(g, "note", 0x11),
        ),
        (
            "fx-tap",
            "E7",
            "button",
            "TAP",
            "Tap the effect tempo",
            "effect",
            Wire(g, "note", 0x0A),
            Wire(g, "note", 0x16),
        ),
        (
            "beat-prev",
            "E8",
            "button",
            "BEAT <",
            "Shorter effect beat",
            "effect",
            Wire(g, "note", 0x06),
            Wire(g, "note", 0x12),
        ),
        (
            "beat-next",
            "E9",
            "button",
            "BEAT >",
            "Longer effect beat",
            "effect",
            Wire(g, "note", 0x07),
            Wire(g, "note", 0x13),
        ),
        ("level-depth", "E10", "knob", "LEVEL/DEPTH", "Effect amount", "effect", Wire(g, "cc", 0x0A), None),
        (
            "fx-lever-1",
            "E11",
            "button",
            "FX LEVER 1",
            "Channel 1 effect lever, held or locked on",
            "effect",
            Wire(FX_CH[1], "note", 0x50),
            None,
        ),
        (
            "fx-lever-2",
            "E12",
            "button",
            "FX LEVER 2",
            "Channel 2 effect lever, held or locked on",
            "effect",
            Wire(FX_CH[2], "note", 0x50),
            None,
        ),
        (
            "smooth-echo",
            "E13",
            "button",
            "SMOOTH ECHO",
            "Smooth echo on or off",
            "effect",
            Wire(g, "note", 0x09),
            Wire(g, "note", 0x15),
        ),
    ]
    return [
        _ctl(cid, fig, typ, label, does, section, midi, shift)
        for cid, fig, typ, label, does, section, midi, shift in rows
    ]


def build() -> dict[str, object]:
    controls = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    controls = [replace(c) for c in controls]
    known = {c.id for c in controls}
    missing_pos = sorted(known - set(_POS))
    extra_pos = sorted(set(_POS) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _POS mismatch: missing={missing_pos} extra={extra_pos}")
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": "ddj-rev7",
        "display_name": "2-deck motorised battle controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "REV7_MAP",
        "sources": {
            "panel_size": "pioneerdj.com DDJ-REV7 product page, Specifications tab (732.0 x 382.2 mm)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev7.ts (DDJ-REV7 List of MIDI message E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/ddj-rev7/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE MODE wire identity",
            "SLOT 1-4 and PARAMETER buttons carry their HOT CUE MODE wire identity",
            "MSB/LSB companion CCs: each knob and fader carries its MSB",
            "software-FX rows of the FX section (channels 5 and 6 beyond the lever base note)",
            "INPUT SELECT carries its 'switch to USB A' note; the other two positions are map hints",
            "rear and front panel controls (M17-M33): not on the top panel",
            "jog touch, platter motor state, jog display: no MIDI row",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
