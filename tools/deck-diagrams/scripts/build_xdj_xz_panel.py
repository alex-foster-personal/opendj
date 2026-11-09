"""Build tools/deck-diagrams/devices/xdj-xz/panel.json and panel.reference.json (practice replica).

Panel layout format v1 ((private path)): one entry per
physical control on the XDJ-XZ top panel that the vendor MIDI list gives a wire, with its rectangle
in millimetres (origin top-left, panel = the published width x depth), a label, hover text and the
wire identity (channel, note|cc, number) the unit sends. The two physical decks carry deck 1 (left)
and deck 2 (right) wires; DECK SELECT to 3/4 reuses the same controls and is out of scope here.

Sources (all public, vendor-official, none redistributed here):
- Panel size 878 x 466.1 mm (W x D): Pioneer DJ XDJ-XZ specifications page (see
  docs/controller/research/XDJ-XZ-MIDI-Message-List.md for the citation and its caveat).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj-xz.ts, transcribed from the
  official "XDJ-XZ MIDI Message List" (E1). `fig` is that document's figure number.
- Positions: traced from the top-panel photo on page 1 of that PDF (embedded image 0, 1850 x 1419 px),
  read on a labelled pixel grid at 2x and re-read at 3x for the repeat pass. The right deck is the
  left deck translated by +1204 px (normalised cross-correlation of four regions, dy 0, score 0.93
  to 0.97); the jog ring is the ellipse fitted to the rim edge. Only numbers are committed.

The pixel table `_PX` below is the measurement of record. This script writes BOTH files from it:
`panel.reference.json` carries the raw boxes converted to mm (0.1 mm), `panel.json` the same trace
normalised for the renderer (0.5 mm centres, whole-mm sizes). They are scored against each other by
scripts/skins/control_position_error.py, which is why they are never identical.

Run: uv run --locked python tools/deck-diagrams/scripts/build_xdj_xz_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 878.0
PANEL_D_MM = 466.1
DEVICE = "xdj-xz"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE

# Vendor image calibration: the unit's footprint spans these inclusive pixel columns (left deck
# edge to right deck edge) and rows (rear face of the raised display housing, which is the unit's
# rearmost point, to the front edge). The deck plates start at row 79. Check: with these scales
# the jog's outer rim (450 x 643 px) reads 216 x 217 mm, round, around the 206 mm platter the
# specifications page lists; the pads read 26 x 27 mm.
IMAGE_PX = (1850, 1419)
PANEL_PX = {"x0": 8, "x1": 1834, "y0": 11, "y1": 1394}
MM_PER_PX_X = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5)
# The photo is not square in physical terms: a round cap is this much taller in px than wide.
Y_STRETCH = MM_PER_PX_X / MM_PER_PX_Y
RIGHT_DECK_DX = 1204

TRACE = {
    "source": "XDJ-XZ MIDI Message List E1, page 1, embedded image 0 (top-panel photo, 1850 x 1419 px)",
    "source_pdf_sha256": "c0e23435447a5c3f444f55e777e4d7e1b02a0abd6238e8dd0ff1a6a00df867b9",
    "source_image_sha256": "a8d6fe8f79020f374bd035c0c615f263c872af6e053812736710a5a590f08513",
    "provenance": "docs/controller/research/XDJ-XZ-MIDI-Message-List.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
    "method": (
        "Button: body bounding box. Knob and encoder: visible cap, read as a circle in mm (x radius "
        "from the image, y radius = x radius times the x/y scale ratio). Pad: pad square. Jog: outer "
        "platter ring (ellipse fitted to the rim edge). Vertical faders: travel slot with cap width. "
        "Crossfader: travel slot with cap height. Slide switches and the X-PAD strip: body. Right deck "
        "= left deck + 1204 px (cross-correlation)."
    ),
    "repeat_spread_mm": 1.2,
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
}


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


Box = tuple[int, int, int, int]  # inclusive x0, y0, x1, y1 on the vendor image


def _round(cx: int, cy: int, rx: int) -> Box:
    """A round cap read as a circle in mm: y radius follows the image's y stretch."""
    ry = round(rx * Y_STRETCH)
    return (cx - rx, cy - ry, cx + rx, cy + ry)


# ----------------------------------------------------------------- pixel trace (left deck + centre)

_DECK_PX: dict[str, Box] = {
    "in-cue": _round(59, 380, 15),
    "out": _round(112, 380, 15),
    "reloop-exit": _round(175, 384, 12),
    "call-prev": _round(376, 384, 9),
    "call-next": _round(421, 384, 9),
    "delete": _round(470, 384, 11),
    "memory": _round(512, 384, 9),
    "vinyl-speed-adjust": _round(582, 385, 16),
    "4-beat": _round(59, 470, 11),
    "jog-mode": (562, 455, 602, 490),
    "slip": _round(59, 560, 12),
    "jog": (94, 428, 544, 1072),
    "direction": (37, 670, 82, 707),
    "sync": _round(567, 680, 11),
    "master": _round(602, 680, 11),
    "tempo-range": _round(579, 857, 9),
    "master-tempo": _round(579, 920, 8),
    "track-search-rev": _round(69, 985, 10),
    "track-search-fwd": _round(102, 985, 10),
    "search-rev": _round(69, 1056, 10),
    "search-fwd": _round(102, 1056, 10),
    "cue": (57, 1133, 116, 1200),
    "play-pause": (50, 1264, 122, 1336),
    "mode-hot-cue": (192, 1112, 247, 1130),
    "mode-beat-loop": (258, 1112, 314, 1130),
    "mode-slip-loop": (325, 1112, 380, 1130),
    "mode-beat-jump": (391, 1112, 446, 1130),
    "pad-1": (193, 1168, 246, 1247),
    "pad-2": (258, 1168, 314, 1247),
    "pad-3": (325, 1168, 380, 1247),
    "pad-4": (391, 1168, 446, 1247),
    "pad-5": (193, 1262, 246, 1345),
    "pad-6": (258, 1262, 314, 1345),
    "pad-7": (325, 1262, 380, 1345),
    "pad-8": (391, 1262, 446, 1345),
    "tempo-reset": _round(507, 1156, 14),
    "tempo": (559, 980, 597, 1335),
}

# Mixer strips left to right on the panel: channel 3, 1, 2, 4.
_STRIP_X = {3: 786, 1: 876, 2: 966, 4: 1056}
_STRIP_CUE_X = {3: (765, 807), 1: (855, 897), 2: (945, 987), 4: (1035, 1077)}
_STRIP_FADER_X = {3: (770, 805), 1: (860, 895), 2: (950, 984), 4: (1040, 1074)}
_STRIP_ASSIGN_X = {3: (761, 812), 1: (851, 902), 2: (941, 992), 4: (1031, 1082)}

_CENTRE_PX: dict[str, Box] = {
    # browser
    "browse": _round(1155, 201, 28),
    "back": (1122, 118, 1156, 150),
    "tag-track": (1160, 118, 1194, 150),
    "track-filter": _round(1135, 295, 9),
    "time-mode": _round(687, 317, 8),
    "deck-quantize": _round(687, 370, 8),
    "load-1": (1112, 340, 1150, 370),
    "load-2": (1160, 340, 1197, 370),
    # mixer, non-strip
    "ch3-input": (672, 457, 700, 480),
    "ch4-input": (1140, 457, 1167, 480),
    "aux-select": (674, 580, 701, 600),
    "aux-trim": _round(686, 677, 15),
    "phones-mix": _round(686, 1080, 15),
    "phones-level": _round(686, 1182, 15),
    "crossfader": (865, 1285, 980, 1347),
    "master-level": _round(1282, 168, 15),
    "master-cue": (1260, 255, 1305, 280),
    "master-eq-low": _round(1344, 265, 15),
    "master-eq-mid": _round(1406, 265, 15),
    "master-eq-hi": _round(1468, 265, 15),
    "booth-level": _round(1540, 265, 14),
    "mic1-eq-low": _round(362, 162, 17),
    "mic1-eq-mid": _round(424, 162, 17),
    "mic1-eq-hi": _round(486, 162, 17),
    "mic2-eq-low": _round(362, 262, 17),
    "mic2-eq-mid": _round(424, 262, 17),
    "mic2-eq-hi": _round(486, 262, 17),
    "feedback-reducer-mode": (539, 185, 580, 215),
    "feedback-reducer-on": (540, 252, 582, 280),
    # effect
    "x-pad": (1112, 557, 1210, 635),
    "beat-prev": (1112, 672, 1140, 692),
    "beat-next": (1174, 672, 1202, 692),
    "auto-tap": _round(1119, 745, 7),
    "tap": _round(1156, 739, 18),
    "fx-quantize": _round(1191, 745, 6),
    "fx-freq-low": (1109, 815, 1134, 837),
    "fx-freq-mid": (1144, 815, 1169, 837),
    "fx-freq-hi": (1180, 815, 1205, 837),
    "fx-select": _round(1155, 892, 15),
    "ch-select": _round(1155, 975, 14),
    "time": _round(1155, 1077, 17),
    "level-depth": _round(1155, 1182, 18),
    "fx-on-off": _round(1155, 1305, 21),
    "space": (641, 782, 664, 804),
    "dub-echo": (676, 782, 699, 804),
    "sweep": (711, 782, 734, 804),
    "noise": (641, 844, 664, 865),
    "crush": (676, 844, 699, 865),
    "filter": (711, 844, 734, 865),
    "parameter": _round(686, 937, 15),
}
for _ch, _x in _STRIP_X.items():
    _CENTRE_PX[f"ch{_ch}-trim"] = _round(_x, 465, 14)
    _CENTRE_PX[f"ch{_ch}-hi"] = _round(_x, 555, 14)
    _CENTRE_PX[f"ch{_ch}-mid"] = _round(_x, 642, 13)
    _CENTRE_PX[f"ch{_ch}-low"] = _round(_x, 730, 13)
    _CENTRE_PX[f"ch{_ch}-color"] = _round(_x, 812, 20)
    _CENTRE_PX[f"ch{_ch}-ch-cue"] = (*_STRIP_CUE_X[_ch][:1], 890, _STRIP_CUE_X[_ch][1], 910)
    _CENTRE_PX[f"ch{_ch}-ch-fader"] = (_STRIP_FADER_X[_ch][0], 982, _STRIP_FADER_X[_ch][1], 1120)
    _CENTRE_PX[f"ch{_ch}-xf-assign"] = (_STRIP_ASSIGN_X[_ch][0], 1187, _STRIP_ASSIGN_X[_ch][1], 1230)


def _px_table() -> dict[str, Box]:
    out: dict[str, Box] = {}
    for suffix, (x0, y0, x1, y1) in _DECK_PX.items():
        out[f"d1-{suffix}"] = (x0, y0, x1, y1)
        out[f"d2-{suffix}"] = (x0 + RIGHT_DECK_DX, y0, x1 + RIGHT_DECK_DX, y1)
    out.update(_CENTRE_PX)
    return out


_PX = _px_table()


# ----------------------------------------------------------------- helpers


def _mm(box: Box) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    x = (x0 - PANEL_PX["x0"]) * MM_PER_PX_X
    y = (y0 - PANEL_PX["y0"]) * MM_PER_PX_Y
    w = (x1 - x0 + 1) * MM_PER_PX_X
    h = (y1 - y0 + 1) * MM_PER_PX_Y
    return round(x, 1), round(y, 1), round(w, 1), round(h, 1)


def _normalised(box: Box) -> tuple[float, float, float, float]:
    """Renderer form: centre on a 0.5 mm grid, whole-mm sizes (at least 1 mm)."""
    x, y, w, h = _mm(box)
    cx, cy = round((x + w / 2) * 2) / 2, round((y + h / 2) * 2) / 2
    w_n, h_n = max(1.0, float(round(w))), max(1.0, float(round(h)))
    return round(cx - w_n / 2, 1), round(cy - h_n / 2, 1), w_n, h_n


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _normalised(_PX[cid])
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _PX entry for {cid!r}") from e


# ----------------------------------------------------------------- deck strips

# suffix, fig, type, label, does, kind, number, shifted (kind, number) or None
DeckRow = tuple[str, str, str, str, str, str, int, tuple[str, int] | None]

_DECK_ROWS: list[DeckRow] = [
    ("in-cue", "1-17", "button", "IN/CUE", "Set the loop in point", "note", 6, None),
    ("out", "1-18", "button", "OUT", "Set the loop out point", "note", 7, None),
    ("reloop-exit", "1-19", "button", "RELOOP/EXIT", "Exit or re-enter the loop", "note", 8, None),
    ("call-prev", "1-20", "button", "CALL <", "Call the previous cue or loop", "note", 12, None),
    ("call-next", "1-20", "button", "CALL >", "Call the next cue or loop", "note", 11, None),
    ("delete", "1-21", "button", "DELETE", "Delete the called cue or loop", "note", 13, None),
    ("memory", "1-22", "button", "MEMORY", "Store the cue or loop", "note", 10, None),
    ("vinyl-speed-adjust", "1-10", "knob", "VINYL SPEED ADJUST", "Vinyl mode stop and start speed", "cc", 30, None),
    ("4-beat", "1-16", "button", "4/8 BEAT", "Four-beat loop cutter (long press: eight)", "note", 67, None),
    ("jog-mode", "1-9", "button", "JOG MODE", "Vinyl or CDJ jog mode", "note", 18, None),
    ("slip", "1-15", "button", "SLIP", "Slip mode on or off", "note", 44, None),
    ("jog", "1-1", "jog", "JOG", "Scratch, nudge or search the track", "cc", 34, ("cc", 41)),
    ("direction", "1-14", "button", "DIRECTION", "Reverse playback", "note", 33, None),
    ("sync", "1-6", "button", "SYNC", "Beat sync to the master deck", "note", 31, None),
    ("master", "1-7", "button", "MASTER", "Make this deck the sync master", "note", 30, None),
    ("tempo-range", "1-5", "button", "TEMPO RANGE", "Tempo fader range", "note", 16, None),
    ("master-tempo", "1-4", "button", "MASTER TEMPO", "Key lock on or off", "note", 17, None),
    ("track-search-rev", "1-11", "button", "TRACK SEARCH <", "Previous track", "note", 5, None),
    ("track-search-fwd", "1-11", "button", "TRACK SEARCH >", "Next track", "note", 4, None),
    ("search-rev", "1-12", "button", "SEARCH <", "Fast reverse", "note", 3, None),
    ("search-fwd", "1-12", "button", "SEARCH >", "Fast forward", "note", 2, None),
    ("cue", "1-24", "button", "CUE", "Set or return to the cue point", "note", 1, None),
    ("play-pause", "1-23", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0, None),
    ("mode-hot-cue", "5-1", "button", "HOT CUE", "Pad mode: hot cues", "note", 34, ("note", 38)),
    ("mode-beat-loop", "5-2", "button", "BEAT LOOP", "Pad mode: beat loops", "note", 35, ("note", 39)),
    ("mode-slip-loop", "5-3", "button", "SLIP LOOP", "Pad mode: slip loops", "note", 36, ("note", 40)),
    ("mode-beat-jump", "5-4", "button", "BEAT JUMP", "Pad mode: beat jump", "note", 37, ("note", 41)),
    ("tempo-reset", "1-3", "button", "TEMPO RESET", "Return the tempo to zero", "note", 19, None),
    ("tempo", "1-2", "fader", "TEMPO", "Tempo fader", "cc", 0, None),
]


def _deck(deck: int) -> list[Control]:
    pad_ch = 5 + deck
    out: list[Control] = []
    for suffix, fig, typ, label, does, kind, num, shifted in _DECK_ROWS:
        cid = f"d{deck}-{suffix}"
        x, y, w, h = _at(cid)
        shift = Wire(deck, shifted[0], shifted[1]) if shifted else None
        out.append(
            Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, "deck", Wire(deck, kind, num), shift)
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                f"5-{5 + pad}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch, "note", 8 + pad),
            )
        )
    return out


# ----------------------------------------------------------------- mixer and centre

MIXER_CH = 5
BROWSER_CH = 1
_TRIM_CC = {1: 1, 2: 6, 3: 12, 4: 80}
_EQ_CC = {"hi": {1: 2, 2: 7, 3: 14, 4: 81}, "mid": {1: 3, 2: 8, 3: 15, 4: 92}, "low": {1: 4, 2: 9, 3: 21, 4: 82}}
_COLOR_CC = {1: 5, 2: 10, 3: 22, 4: 83}
_FADER_CC = {1: 17, 2: 18, 3: 19, 4: 20}
_CH_CUE_CC = {1: 70, 2: 71, 3: 72, 4: 73}
_ASSIGN_CC = {1: 65, 2: 66, 3: 67, 4: 68}


def _strip(ch: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int]] = [
        ("trim", "3-5", "knob", "TRIM", "Channel gain", "cc", _TRIM_CC[ch]),
        ("hi", "3-6", "knob", "HI", "EQ high band", "cc", _EQ_CC["hi"][ch]),
        ("mid", "3-7", "knob", "MID", "EQ mid band", "cc", _EQ_CC["mid"][ch]),
        ("low", "3-8", "knob", "LOW", "EQ low band", "cc", _EQ_CC["low"][ch]),
        ("color", "2-14", "knob", "COLOR", "Sound color effect amount", "cc", _COLOR_CC[ch]),
        ("ch-cue", "3-11", "button", "CUE", "Headphone cue for this channel", "cc", _CH_CUE_CC[ch]),
        ("ch-fader", "3-2", "fader", "CH FADER", "Channel volume", "cc", _FADER_CC[ch]),
        ("xf-assign", "3-3", "button", "A THRU B", "Crossfader assign switch", "cc", _ASSIGN_CC[ch]),
    ]
    out: list[Control] = []
    for suffix, fig, typ, label, does, kind, num in rows:
        cid = f"ch{ch}-{suffix}"
        x, y, w, h = _at(cid)
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {ch})", x, y, w, h, "mixer", Wire(MIXER_CH, kind, num))
        )
    return out


# id, fig, type, label, does, section, channel, kind, number, shifted wire or None
GlobalRow = tuple[str, str, str, str, str, str, int, str, int, Wire | None]

_GLOBAL_ROWS: list[GlobalRow] = [
    (
        "browse",
        "4-1",
        "encoder",
        "ROTARY SELECTOR",
        "Scroll the library",
        "browse",
        BROWSER_CH,
        "cc",
        79,
        Wire(BROWSER_CH, "cc", 96),
    ),
    (
        "back",
        "4-2",
        "button",
        "BACK",
        "Back one browse level",
        "browse",
        BROWSER_CH,
        "note",
        50,
        Wire(BROWSER_CH, "note", 95),
    ),
    (
        "tag-track",
        "4-3",
        "button",
        "TAG TRACK/REMOVE",
        "Tag or untag the selected track",
        "browse",
        BROWSER_CH,
        "note",
        48,
        Wire(BROWSER_CH, "note", 98),
    ),
    (
        "track-filter",
        "4-4",
        "button",
        "TRACK FILTER/EDIT",
        "Track filter on or off",
        "browse",
        BROWSER_CH,
        "note",
        49,
        Wire(BROWSER_CH, "note", 93),
    ),
    ("time-mode", "4-6", "button", "TIME MODE", "Elapsed or remaining time", "browse", BROWSER_CH, "note", 14, None),
    (
        "deck-quantize",
        "4-7",
        "button",
        "DECK QUANTIZE",
        "Deck quantize on or off",
        "browse",
        BROWSER_CH,
        "note",
        9,
        None,
    ),
    ("load-1", "1-26", "button", "LOAD 1", "Load the selected track to deck 1", "browse", 1, "note", 81, None),
    ("load-2", "1-26", "button", "LOAD 2", "Load the selected track to deck 2", "browse", 2, "note", 81, None),
    (
        "ch3-input",
        "3-4",
        "button",
        "CH 3 INPUT",
        "Computer, line or phono input (channel 3)",
        "mixer",
        MIXER_CH,
        "note",
        59,
        None,
    ),
    (
        "ch4-input",
        "3-4",
        "button",
        "CH 4 INPUT",
        "Computer, line or phono input (channel 4)",
        "mixer",
        MIXER_CH,
        "note",
        63,
        None,
    ),
    ("aux-select", "3-31", "button", "AUX", "Aux input off, line or portable", "mixer", MIXER_CH, "cc", 120, None),
    ("aux-trim", "3-32", "knob", "AUX TRIM", "Aux input gain", "mixer", MIXER_CH, "cc", 122, None),
    ("phones-mix", "3-9", "knob", "MIX", "Headphone cue and master blend", "mixer", MIXER_CH, "cc", 27, None),
    ("phones-level", "3-10", "knob", "LEVEL", "Headphone level", "mixer", MIXER_CH, "cc", 26, None),
    ("crossfader", "3-1", "crossfader", "CROSSFADER", "Blend the A and B sides", "mixer", MIXER_CH, "cc", 11, None),
    ("master-level", "3-13", "knob", "MASTER LEVEL", "Master output level", "mixer", MIXER_CH, "cc", 24, None),
    ("master-cue", "3-12", "button", "MASTER CUE", "Hear the master in headphones", "mixer", MIXER_CH, "cc", 74, None),
    ("master-eq-low", "3-14", "knob", "MASTER EQ LOW", "Master EQ low band", "mixer", MIXER_CH, "cc", 49, None),
    ("master-eq-mid", "3-15", "knob", "MASTER EQ MID", "Master EQ mid band", "mixer", MIXER_CH, "cc", 48, None),
    ("master-eq-hi", "3-16", "knob", "MASTER EQ HI", "Master EQ high band", "mixer", MIXER_CH, "cc", 47, None),
    ("booth-level", "3-17", "knob", "BOOTH LEVEL", "Booth output level", "mixer", MIXER_CH, "cc", 25, None),
    ("mic1-eq-low", "3-25", "knob", "MIC 1 EQ LOW", "Microphone 1 EQ low", "mixer", MIXER_CH, "cc", 31, None),
    ("mic1-eq-mid", "3-26", "knob", "MIC 1 EQ MID", "Microphone 1 EQ mid", "mixer", MIXER_CH, "cc", 98, None),
    ("mic1-eq-hi", "3-27", "knob", "MIC 1 EQ HI", "Microphone 1 EQ high", "mixer", MIXER_CH, "cc", 30, None),
    ("mic2-eq-low", "3-25", "knob", "MIC 2 EQ LOW", "Microphone 2 EQ low", "mixer", MIXER_CH, "cc", 97, None),
    ("mic2-eq-mid", "3-26", "knob", "MIC 2 EQ MID", "Microphone 2 EQ mid", "mixer", MIXER_CH, "cc", 99, None),
    ("mic2-eq-hi", "3-27", "knob", "MIC 2 EQ HI", "Microphone 2 EQ high", "mixer", MIXER_CH, "cc", 96, None),
    (
        "feedback-reducer-mode",
        "3-28",
        "button",
        "LIGHT/HEAVY",
        "Mic feedback reducer strength",
        "mixer",
        MIXER_CH,
        "cc",
        88,
        None,
    ),
    (
        "feedback-reducer-on",
        "3-29",
        "button",
        "FEEDBACK REDUCER ON",
        "Mic feedback reducer on or off",
        "mixer",
        MIXER_CH,
        "cc",
        89,
        None,
    ),
    ("x-pad", "2-1", "crossfader", "X-PAD", "Beat effect touch strip", "effect", MIXER_CH, "cc", 116, None),
    ("beat-prev", "2-2", "button", "BEAT <", "Shorter effect beat", "effect", MIXER_CH, "cc", 76, None),
    ("beat-next", "2-2", "button", "BEAT >", "Longer effect beat", "effect", MIXER_CH, "cc", 77, None),
    ("auto-tap", "2-3", "button", "AUTO/TAP", "Auto BPM or tap tempo", "effect", MIXER_CH, "cc", 69, None),
    ("tap", "2-4", "button", "TAP", "Tap the effect tempo", "effect", MIXER_CH, "cc", 78, None),
    ("fx-quantize", "2-5", "button", "FX QUANTIZE", "Effect quantize on or off", "effect", MIXER_CH, "note", 118, None),
    (
        "fx-freq-low",
        "2-8",
        "button",
        "FX FREQUENCY LOW",
        "Apply the beat effect to the lows",
        "effect",
        MIXER_CH,
        "cc",
        104,
        None,
    ),
    (
        "fx-freq-mid",
        "2-7",
        "button",
        "FX FREQUENCY MID",
        "Apply the beat effect to the mids",
        "effect",
        MIXER_CH,
        "cc",
        103,
        None,
    ),
    (
        "fx-freq-hi",
        "2-6",
        "button",
        "FX FREQUENCY HI",
        "Apply the beat effect to the highs",
        "effect",
        MIXER_CH,
        "cc",
        102,
        None,
    ),
    (
        "fx-select",
        "2-9",
        "knob",
        "EFFECT SELECT",
        "Pick the beat effect (14 positions)",
        "effect",
        MIXER_CH,
        "cc",
        42,
        None,
    ),
    (
        "ch-select",
        "2-10",
        "knob",
        "CH SELECT",
        "Pick the channel the beat effect hits",
        "effect",
        MIXER_CH,
        "cc",
        39,
        None,
    ),
    ("time", "2-11", "encoder", "TIME", "Beat effect time", "effect", MIXER_CH, "cc", 13, None),
    ("level-depth", "2-12", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", MIXER_CH, "cc", 91, None),
    ("fx-on-off", "2-13", "button", "ON/OFF", "Beat effect on or off", "effect", MIXER_CH, "cc", 114, None),
    ("space", "2-16", "button", "SPACE", "Sound color FX: space", "effect", MIXER_CH, "cc", 105, None),
    ("dub-echo", "2-17", "button", "DUB ECHO", "Sound color FX: dub echo", "effect", MIXER_CH, "cc", 107, None),
    ("sweep", "2-18", "button", "SWEEP", "Sound color FX: sweep", "effect", MIXER_CH, "cc", 106, None),
    ("noise", "2-19", "button", "NOISE", "Sound color FX: noise", "effect", MIXER_CH, "cc", 85, None),
    ("crush", "2-20", "button", "CRUSH", "Sound color FX: crush", "effect", MIXER_CH, "cc", 86, None),
    ("filter", "2-21", "button", "FILTER", "Sound color FX: filter", "effect", MIXER_CH, "cc", 87, None),
    ("parameter", "2-15", "knob", "PARAMETER", "Sound color FX parameter", "effect", MIXER_CH, "cc", 108, None),
]


def _globals() -> list[Control]:
    out: list[Control] = []
    for cid, fig, typ, label, does, section, ch, kind, num, shift in _GLOBAL_ROWS:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), shift))
    return out


# ----------------------------------------------------------------- documents


def controls() -> list[Control]:
    out = [*_deck(1), *_deck(2), *_strip(3), *_strip(1), *_strip(2), *_strip(4), *_globals()]
    ids = [c.id for c in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    missing = sorted(set(_PX) - set(ids))
    if missing:
        raise SystemExit(f"[ERROR] traced boxes with no control row: {missing}")
    for c in out:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")
    return out


def build() -> dict[str, object]:
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel all-in-one system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "XDJ_XZ_MAP",
        "sources": {
            "panel_size": "Pioneer DJ XDJ-XZ specifications page, General: 878 x 466.1 mm (W x D); see docs/controller/research/XDJ-XZ-MIDI-Message-List.md",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj-xz.ts (XDJ-XZ MIDI Message List E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/xdj-xz/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "SHIFT: the vendor list gives it no wire, so it is not placed; shift_midi rows still carry the shifted numbers",
            "controls the list marks Hardware Control (JOG ADJUST, JOG RING, MIC LEVEL and switches, TALKOVER, meters, USB STOP, MASTER REC, TRACK MARK, SHORT CUT, display tabs)",
            "touch-screen controls (NEEDLE POSITION, DECK SELECT) and the on-jog display",
            "decks 3 and 4: the two physical decks carry deck 1 and deck 2 wires",
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity",
            "rotary switches carry their first printed position's wire (EFFECT SELECT: DELAY, CH SELECT: CROSSFADER A); the other positions are map hints",
            "MSB/LSB companion CCs: TEMPO and TIME carry their MSB",
        ],
        "controls": [asdict(c) for c in controls()],
    }


def build_reference() -> dict[str, object]:
    rows = []
    for c in controls():
        box = _PX[c.id]
        x, y, w, h = _mm(box)
        rows.append({"id": c.id, "px": list(box), "x": x, "y": y, "w": w, "h": h})
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": dict(TRACE),
        "controls": rows,
    }


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = build()
    ref = build_reference()
    (OUT_DIR / "panel.json").write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    (OUT_DIR / "panel.reference.json").write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_DIR / 'panel.json'} and panel.reference.json ({len(panel['controls'])} controls)")  # type: ignore[arg-type]
