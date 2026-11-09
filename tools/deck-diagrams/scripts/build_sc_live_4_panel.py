"""Build the Denon DJ SC LIVE 4 practice-replica layout pair (ADR-0137, epic #9366).

Writes tools/deck-diagrams/devices/sc-live-4/panel.reference.json (the measurement of record,
raw trace) and panel.json (the same trace normalised for the renderer). The pixel tables `_PX_*`
below are the single source of position truth.

Sources (public and official, nothing else):
- Panel size 719 x 404 mm (W x D): "User Guide - PRIME 4, PRIME 4+, PRIME 2, PRIME GO, PRIME GO+,
  SC LIVE 4, SC LIVE 2" v4.2.0 RevB, page 104, SC LIVE 4 Technical Specifications, Dimensions
  (width x depth x height) 71.9 x 40.4 x 9.9 cm. sha256 of the PDF in
  docs/controller/research/SC-LIVE-4-User-Guide.md.
- Control names and callout numbers (`fig`): the same guide, pages 23 to 26, "SC LIVE 4 > Top Panel".
- Positions: page 23 of that guide is a vector drawing. It was rendered once at 300 dpi
  (2481 x 3509 px, PyMuPDF 2.1 / MuPDF 1.28.6, `page.get_pixmap(dpi=300, alpha=False)`); every
  `_PX_*` row is the inclusive pixel box of a control on that render. Boxes were read from the
  drawing's own vector primitives wherever the control is a closed path (knobs and round buttons:
  the largest polygon circle of the knob body; buttons and pads: the closed rounded rectangle, or
  the box between its nearest hairline sides where the outline is drawn as separate strokes;
  faders: the travel-slot hairlines for the y extent and the cap grips for the x extent;
  crossfader: slot x extent with cap height). Thirteen controls drawn as arcs or switch glyphs
  with no closed path (BACK, FWD, LOAD x2, the four crossfader-assign switches, SPEAKER, SPLIT CUE
  and three PITCH BEND halves) were read off a 25 px/pt ruled zoom of the same render
  (`_MANUAL_IDS`); ch1's assign switch is half hidden by callout 14, so its x is the ch1 knob
  column centre with the width of the three visible switches. The jog is its outer platter ring.
- Calibration: the footprint hairlines of the drawing span 44.28..554.94 pt in x and
  106.50..393.54 pt in y (the raised screen housing included). Against the published 719 x 404 mm
  the two axes agree to 0.04 percent (1.4080 and 1.4075 mm/pt), recorded as separate x and y
  scales over the same box in render pixels. The guide, the render and every crop stay out of
  the repo (manufacturer copyright): only numbers are committed.
- Wires: Denon DJ publishes no MIDI message list for the SC LIVE 4 (the Engine DJ community
  thread "Midi specification sc live 4" confirms none exists), so every `midi` wire here is a
  PLACEHOLDER on channel 16 that exists only to join a control to its hint in
  apps/webui/frontend/src/lib/rb/midi/maps/sc-live-4.ts. SC_LIVE_4_MAP binds nothing and is not
  in DEVICE_MAP_REGISTRY; the replica is inert end to end. No wire is a claim about the hardware.

Normalisation (panel.json only): centres snapped to 0.5 mm, sizes to whole mm, and row or
column mates (same control type, centres within 1.5 mm) placed on one line.

Run: uv run --locked python tools/deck-diagrams/scripts/build_sc_live_4_panel.py
     uv run --locked python tools/deck-diagrams/scripts/build_sc_live_4_panel.py --print-ts-hints
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 719.0
PANEL_D_MM = 404.0
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / "sc-live-4"
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (2481, 3509)
PANEL_PX = {"x0": 184, "x1": 2312, "y0": 444, "y1": 1640}
MM_PER_PX = {
    "x": round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 6),
    "y": round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 6),
}
SOURCE_PDF_SHA256 = "fc22e49892305f9c8ef476008aeaa590369839a919b03547d76ca28a8bcb85ac"
SOURCE_IMAGE_SHA256 = "bfe571866ffc221d60155d0b86c8bbc670106b45bf7cbbb548c29fdef96b114d"
ALIGN_TOL_MM = 1.5
PLACEHOLDER_CH = 16
_MANUAL_IDS: list[str] = [
    "back",
    "ch1-xf-assign",
    "ch2-xf-assign",
    "ch3-xf-assign",
    "ch4-xf-assign",
    "d1-pitch-bend-plus",
    "d2-pitch-bend-minus",
    "d2-pitch-bend-plus",
    "fwd",
    "load-1",
    "load-2",
    "speaker",
    "split-cue",
]


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


# ----------------------------------------------------------------- the trace (render pixels)

# Inclusive [x0, y0, x1, y1] on the 2481 x 3509 px render of page 23. Deck 2 was read on its own
# primitives, not derived from deck 1.
_PX_DECK_1: dict[str, list[int]] = {
    "d1-auto-loop": [218, 860, 290, 932],
    "d1-loop-in": [208, 971, 247, 998],
    "d1-loop-out": [261, 971, 299, 998],
    "d1-censor": [219, 1035, 288, 1075],
    "d1-deck": [229, 1150, 279, 1200],
    "d1-track-skip-back": [210, 1270, 242, 1303],
    "d1-track-skip-fwd": [266, 1270, 298, 1303],
    "d1-beat-jump-back": [210, 1345, 242, 1377],
    "d1-beat-jump-fwd": [266, 1345, 298, 1377],
    "d1-cue": [210, 1426, 298, 1500],
    "d1-play-pause": [210, 1534, 298, 1608],
    "d1-vinyl": [808, 901, 903, 958],
    "d1-sync": [810, 1014, 848, 1040],
    "d1-key-sync": [863, 1014, 901, 1040],
    "d1-jog": [310, 914, 800, 1405],
    "d1-tempo": [826, 1181, 885, 1462],
    "d1-slip": [744, 1353, 783, 1391],
    "d1-shift": [384, 1419, 428, 1451],
    "d1-hot-cue": [438, 1424, 472, 1446],
    "d1-loop": [488, 1424, 523, 1447],
    "d1-roll": [538, 1424, 572, 1447],
    "d1-slicer": [587, 1424, 622, 1447],
    "d1-param-prev": [637, 1424, 671, 1446],
    "d1-param-next": [685, 1422, 723, 1448],
    "d1-pad-1": [390, 1472, 457, 1528],
    "d1-pad-2": [476, 1471, 546, 1530],
    "d1-pad-3": [564, 1471, 634, 1530],
    "d1-pad-4": [652, 1471, 722, 1530],
    "d1-pad-5": [390, 1548, 457, 1605],
    "d1-pad-6": [476, 1548, 546, 1606],
    "d1-pad-7": [564, 1548, 634, 1606],
    "d1-pad-8": [652, 1548, 722, 1606],
    "d1-pitch-bend-minus": [810, 1558, 844, 1584],
    "d1-pitch-bend-plus": [865, 1558, 899, 1585],
}

_PX_DECK_2: dict[str, list[int]] = {
    "d2-auto-loop": [1605, 860, 1677, 932],
    "d2-loop-in": [1596, 971, 1634, 998],
    "d2-loop-out": [1648, 971, 1686, 998],
    "d2-censor": [1606, 1035, 1676, 1075],
    "d2-deck": [1616, 1150, 1666, 1200],
    "d2-track-skip-back": [1597, 1270, 1629, 1303],
    "d2-track-skip-fwd": [1653, 1270, 1685, 1303],
    "d2-beat-jump-back": [1597, 1345, 1629, 1377],
    "d2-beat-jump-fwd": [1653, 1344, 1685, 1377],
    "d2-cue": [1597, 1426, 1685, 1500],
    "d2-play-pause": [1597, 1534, 1685, 1608],
    "d2-vinyl": [2195, 901, 2290, 958],
    "d2-sync": [2198, 1014, 2236, 1040],
    "d2-key-sync": [2250, 1014, 2288, 1040],
    "d2-jog": [1697, 914, 2187, 1405],
    "d2-tempo": [2214, 1181, 2272, 1462],
    "d2-slip": [2131, 1353, 2170, 1391],
    "d2-shift": [1771, 1419, 1815, 1451],
    "d2-hot-cue": [1825, 1424, 1860, 1446],
    "d2-loop": [1875, 1423, 1910, 1446],
    "d2-roll": [1924, 1424, 1959, 1447],
    "d2-slicer": [1975, 1424, 2009, 1446],
    "d2-param-prev": [2025, 1424, 2059, 1446],
    "d2-param-next": [2074, 1423, 2109, 1447],
    "d2-pad-1": [1776, 1472, 1842, 1528],
    "d2-pad-2": [1863, 1471, 1933, 1530],
    "d2-pad-3": [1951, 1471, 2021, 1530],
    "d2-pad-4": [2039, 1471, 2109, 1530],
    "d2-pad-5": [1776, 1548, 1842, 1605],
    "d2-pad-6": [1863, 1548, 1933, 1606],
    "d2-pad-7": [1951, 1548, 2021, 1606],
    "d2-pad-8": [2039, 1548, 2109, 1606],
    "d2-pitch-bend-minus": [2197, 1558, 2231, 1585],
    "d2-pitch-bend-plus": [2251, 1558, 2285, 1585],
}

# Mixer strips in physical order 3 1 2 4 (the guide prints the channel numbers under the CUE buttons).
_PX_MIXER: dict[str, list[int]] = {
    "ch3-level": [1075, 847, 1120, 892],
    "ch3-eq-high": [1075, 929, 1120, 974],
    "ch3-eq-mid": [1075, 1010, 1120, 1055],
    "ch3-eq-low": [1075, 1091, 1120, 1136],
    "ch3-sweep-fx": [1075, 1172, 1120, 1217],
    "ch3-cue": [1070, 1247, 1125, 1273],
    "ch3-fader": [1058, 1318, 1138, 1451],
    "ch3-xf-assign": [1079, 1513, 1117, 1537],
    "ch1-level": [1176, 847, 1220, 892],
    "ch1-eq-high": [1176, 929, 1220, 974],
    "ch1-eq-mid": [1176, 1010, 1220, 1055],
    "ch1-eq-low": [1176, 1091, 1220, 1136],
    "ch1-sweep-fx": [1176, 1172, 1220, 1217],
    "ch1-cue": [1171, 1247, 1225, 1273],
    "ch1-fader": [1158, 1318, 1238, 1451],
    "ch1-xf-assign": [1179, 1513, 1217, 1537],
    "ch2-level": [1276, 847, 1321, 892],
    "ch2-eq-high": [1276, 929, 1321, 974],
    "ch2-eq-mid": [1276, 1010, 1321, 1055],
    "ch2-eq-low": [1276, 1091, 1321, 1136],
    "ch2-sweep-fx": [1276, 1172, 1321, 1217],
    "ch2-cue": [1271, 1247, 1326, 1273],
    "ch2-fader": [1259, 1318, 1338, 1451],
    "ch2-xf-assign": [1280, 1513, 1317, 1537],
    "ch4-level": [1377, 847, 1422, 892],
    "ch4-eq-high": [1377, 929, 1422, 974],
    "ch4-eq-mid": [1377, 1010, 1422, 1055],
    "ch4-eq-low": [1377, 1091, 1422, 1136],
    "ch4-sweep-fx": [1377, 1172, 1422, 1217],
    "ch4-cue": [1372, 1247, 1426, 1273],
    "ch4-fader": [1359, 1318, 1439, 1451],
    "ch4-xf-assign": [1380, 1513, 1417, 1537],
}

_PX_GLOBAL: dict[str, list[int]] = {
    "crossfader": [1171, 1591, 1301, 1617],
    "main-vol": [1489, 847, 1534, 892],
    "speaker": [1004, 863, 1035, 915],
    "speaker-booth-level": [940, 867, 985, 912],
    "mic1-level": [940, 966, 985, 1011],
    "talkover-1": [1003, 972, 1035, 1004],
    "mic2-level": [940, 1054, 985, 1099],
    "talkover-2": [1003, 1061, 1035, 1093],
    "cue-mix": [963, 1282, 1008, 1327],
    "headphones-level": [963, 1359, 1008, 1404],
    "split-cue": [960, 1441, 996, 1472],
    "sweep-filter": [941, 1150, 974, 1183],
    "sweep-noise": [998, 1150, 1030, 1183],
    "sweep-echo": [941, 1213, 974, 1245],
    "sweep-wash": [998, 1213, 1030, 1245],
    "fx-select": [1489, 1103, 1534, 1148],
    "fx-channel-assign": [1490, 1193, 1532, 1235],
    "fx-time": [1489, 1280, 1534, 1325],
    "fx-amount": [1489, 1369, 1534, 1414],
    "fx-on-off": [1474, 1453, 1548, 1528],
    "view": [903, 553, 953, 600],
    "menu": [893, 630, 963, 668],
    "lighting": [893, 710, 963, 747],
    "back": [1533, 558, 1568, 616],
    "fwd": [1569, 558, 1604, 616],
    "browse": [1530, 613, 1608, 680],
    "load-1": [1533, 686, 1568, 741],
    "load-2": [1569, 686, 1604, 741],
}

_PX: dict[str, list[int]] = {**_PX_DECK_1, **_PX_DECK_2, **_PX_MIXER, **_PX_GLOBAL}

# Independent second read of five controls off a 25 px/pt ruled zoom (repeat pass).
_PX_REPEAT: dict[str, list[int]] = {
    "d1-cue": [210, 1425, 299, 1499],
    "ch1-level": [1177, 847, 1221, 891],
    "d1-pad-8": [651, 1548, 723, 1606],
    "fx-on-off": [1475, 1453, 1549, 1528],
    "d2-slip": [2132, 1357, 2170, 1391],
}

REPEAT_SPREAD_MM = 0.7

TRACE: dict[str, object] = {
    "source": (
        "User Guide - PRIME 4, PRIME 4+, PRIME 2, PRIME GO, PRIME GO+, SC LIVE 4, SC LIVE 2, v4.2.0 RevB, "
        "page 23 (SC LIVE 4 Top Panel, vector figure) rendered at 300 dpi, 2481 x 3509 px"
    ),
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "render": "PyMuPDF 2.1 (MuPDF 1.28.6), page.get_pixmap(dpi=300, alpha=False), PNG",
    "provenance": "docs/controller/research/SC-LIVE-4-User-Guide.md",
    "image_px": list(IMAGE_PX),
    "panel_px": PANEL_PX,
    "mm_per_px": MM_PER_PX,
    "method": (
        "Boxes read from the figure's vector primitives on the render: knob and round button = largest "
        "polygon circle of the body; button and pad = closed rounded rectangle, or the box between the nearest "
        "hairline sides; vertical fader = travel-slot hairlines with cap-grip width; crossfader = slot hairlines "
        "with cap-grip height; jog = outer platter ring. Controls in manual_ids were read off a 25 px/pt ruled "
        "zoom of the same render (arcs and switch glyphs with no closed path). Calibration: footprint "
        "hairlines, screen housing included, against the published 719 x 404 mm."
    ),
    "manual_ids": _MANUAL_IDS,
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": REPEAT_SPREAD_MM,
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
}


# ----------------------------------------------------------------- geometry helpers


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    sx, sy = MM_PER_PX["x"], MM_PER_PX["y"]
    return (
        round((x0 - PANEL_PX["x0"]) * sx, 1),
        round((y0 - PANEL_PX["y0"]) * sy, 1),
        round((x1 - x0 + 1) * sx, 1),
        round((y1 - y0 + 1) * sy, 1),
    )


# ----------------------------------------------------------------- controls

_CONTINUOUS = {"knob", "fader", "crossfader", "encoder", "jog"}


class _Wires:
    """Placeholder wires: channel 16, notes for buttons and pads, CCs for continuous controls,
    numbered in build order. They exist only so a panel control can be joined to its hint."""

    def __init__(self) -> None:
        self.note = 0
        self.cc = 0

    def next(self, typ: str) -> Wire:
        if typ in _CONTINUOUS:
            self.cc += 1
            return Wire(PLACEHOLDER_CH, "cc", self.cc - 1)
        self.note += 1
        return Wire(PLACEHOLDER_CH, "note", self.note - 1)


Row = tuple[str, str, str, str, str]  # suffix, fig, type, label, does


def _deck_rows(deck: int) -> list[Row]:
    alt = deck + 2
    return [
        (
            "auto-loop",
            "54",
            "encoder",
            "AUTO LOOP",
            f"Turn for the auto loop size, press to toggle the loop; with SHIFT, move it (deck {deck})",
        ),
        ("loop-in", "55", "button", "IN", f"Set the loop in point; with SHIFT, adjust it (deck {deck})"),
        ("loop-out", "55", "button", "OUT", f"Set the loop out point; with SHIFT, adjust it (deck {deck})"),
        ("censor", "46", "button", "CENSOR", f"Censor while held; with SHIFT, reverse playback (deck {deck})"),
        ("deck", "34", "button", "DECK", f"Switch this hardware deck between deck {deck} and deck {alt}"),
        (
            "track-skip-back",
            "35",
            "button",
            "TRACK SKIP <",
            f"Previous track or back to the start; with SHIFT, search backward (deck {deck})",
        ),
        ("track-skip-fwd", "36", "button", "TRACK SKIP >", f"Next track; with SHIFT, search forward (deck {deck})"),
        (
            "beat-jump-back",
            "37",
            "button",
            "BEAT JUMP <",
            f"Jump back by the beat jump size; with SHIFT, smaller size (deck {deck})",
        ),
        (
            "beat-jump-fwd",
            "37",
            "button",
            "BEAT JUMP >",
            f"Jump forward by the beat jump size; with SHIFT, larger size (deck {deck})",
        ),
        (
            "cue",
            "38",
            "button",
            "CUE",
            f"Return to the cue point and stop; with SHIFT, set the cue point (deck {deck})",
        ),
        (
            "play-pause",
            "39",
            "button",
            "PLAY/PAUSE",
            f"Play or pause; with SHIFT, stutter-play from the cue point (deck {deck})",
        ),
        ("vinyl", "40", "button", "VINYL", f"Toggle vinyl mode on the platter (deck {deck})"),
        ("sync", "41", "button", "SYNC", f"Toggle sync (deck {deck})"),
        (
            "key-sync",
            "42",
            "button",
            "KEY SYNC",
            f"Hold to key sync to the playing track; with SHIFT, reset the key (deck {deck})",
        ),
        ("jog", "32", "jog", "PLATTER", f"Touch-sensitive platter: scratch in vinyl mode, else nudge (deck {deck})"),
        ("tempo", "43", "fader", "PITCH", f"Pitch fader (deck {deck})"),
        ("slip", "45", "button", "SLIP", f"Toggle slip mode; with SHIFT, beat grid edit (deck {deck})"),
        ("shift", "47", "button", "SHIFT", f"Hold for the secondary function of other controls (deck {deck})"),
        ("hot-cue", "49", "button", "HOT CUE", f"Pad mode: hot cue, press again for stems (deck {deck})"),
        ("loop", "50", "button", "LOOP", f"Pad mode: manual loop, press again for auto loop (deck {deck})"),
        ("roll", "51", "button", "ROLL", f"Pad mode: roll; with SHIFT, sampler (deck {deck})"),
        ("slicer", "52", "button", "SLICER", f"Pad mode: slicer, press again for slicer loop (deck {deck})"),
        ("param-prev", "53", "button", "PARAMETER <", f"Pad mode parameter down (deck {deck})"),
        ("param-next", "53", "button", "PARAMETER >", f"Pad mode parameter up (deck {deck})"),
        *[
            (f"pad-{i}", "48", "pad", f"PAD {i}", f"Performance pad {i} in the active pad mode (deck {deck})")
            for i in range(1, 9)
        ],
        (
            "pitch-bend-minus",
            "44",
            "button",
            "PITCH BEND -",
            f"Hold to slow the track momentarily; with SHIFT, pitch range (deck {deck})",
        ),
        (
            "pitch-bend-plus",
            "44",
            "button",
            "PITCH BEND +",
            f"Hold to speed the track momentarily; with SHIFT, pitch range (deck {deck})",
        ),
    ]


def _channel_rows(ch: int) -> list[Row]:
    return [
        ("level", "9", "knob", "LEVEL", f"Channel gain (channel {ch})"),
        ("eq-high", "11", "knob", "HIGH", f"EQ high band (channel {ch})"),
        ("eq-mid", "11", "knob", "MID", f"EQ mid band (channel {ch})"),
        ("eq-low", "11", "knob", "LOW", f"EQ low band (channel {ch})"),
        ("sweep-fx", "26", "knob", "SWEEP FX", f"Amount of the selected sweep effect (channel {ch})"),
        ("cue", "12", "button", "CUE", f"Headphone cue for this channel (channel {ch})"),
        ("fader", "13", "fader", "CH FADER", f"Channel volume (channel {ch})"),
        (
            "xf-assign",
            "14",
            "button",
            "XF ASSIGN",
            f"Slide switch: route the channel to crossfader side A, thru, or B (channel {ch})",
        ),
    ]


_GLOBAL_ROWS: list[tuple[str, str, str, str, str, str]] = [  # id, fig, type, label, does, section
    ("crossfader", "15", "crossfader", "CROSSFADER", "Mix between crossfader sides A and B", "mixer"),
    ("main-vol", "16", "knob", "MAIN VOL", "Main output level", "mixer"),
    ("speaker", "18", "button", "SPEAKER", "Slide switch: mute or unmute the internal speakers", "mixer"),
    ("speaker-booth-level", "19", "knob", "SPEAKER/BOOTH LEVEL", "Internal speaker and booth output level", "mixer"),
    ("mic1-level", "21", "knob", "MIC 1 LEVEL", "Microphone 1 input level", "mixer"),
    ("talkover-1", "20", "button", "TALKOVER 1", "Talkover on or off for microphone 1", "mixer"),
    ("mic2-level", "21", "knob", "AUX/MIC 2 LEVEL", "Aux and microphone 2 input level", "mixer"),
    ("talkover-2", "20", "button", "TALKOVER 2", "Talkover on or off for microphone 2", "mixer"),
    ("cue-mix", "22", "knob", "CUE MIX", "Headphone blend between cue and main mix", "mixer"),
    ("headphones-level", "23", "knob", "HEADPHONES LEVEL", "Headphone level", "mixer"),
    ("split-cue", "24", "button", "SPLIT CUE", "Slide switch: split cue on or off", "mixer"),
    ("sweep-filter", "25", "button", "FILTER", "Select the filter sweep effect", "effect"),
    ("sweep-noise", "25", "button", "NOISE", "Select the noise sweep effect", "effect"),
    ("sweep-echo", "25", "button", "ECHO", "Select the echo sweep effect", "effect"),
    ("sweep-wash", "25", "button", "WASH OUT", "Select the wash out sweep effect", "effect"),
    ("fx-select", "27", "knob", "FX SELECT", "Select the active BPM effect", "effect"),
    (
        "fx-channel-assign",
        "28",
        "knob",
        "FX CHANNEL ASSIGN",
        "Rotary switch: route the BPM effect to deck 1 to 4 or the main output",
        "effect",
    ),
    ("fx-time", "29", "knob", "TIME/PARAMETER", "BPM effect time; press to switch to the parameter", "effect"),
    ("fx-amount", "30", "knob", "AMOUNT", "BPM effect wet/dry amount", "effect"),
    ("fx-on-off", "31", "button", "FX ON/OFF", "BPM effect on or off", "effect"),
    ("view", "6", "button", "VIEW", "Cycle library and performance views; with SHIFT, cycle layouts", "browse"),
    ("menu", "7", "button", "MENU", "Hold to show the control center", "browse"),
    ("lighting", "8", "button", "LIGHTING", "Enter the lighting view", "browse"),
    ("back", "3", "button", "BACK", "Previous window in the library view", "browse"),
    ("fwd", "4", "button", "FWD", "Next window in the library view; with SHIFT, quantize on or off", "browse"),
    (
        "browse",
        "2",
        "encoder",
        "BROWSE",
        "Turn to scroll lists, press to select or load; with SHIFT, add to the prepare list",
        "browse",
    ),
    ("load-1", "5", "button", "LOAD <", "Load the selected track to deck 1 or 3; with SHIFT, unload", "browse"),
    ("load-2", "5", "button", "LOAD >", "Load the selected track to deck 2 or 4; with SHIFT, unload", "browse"),
]


def _controls() -> list[Control]:
    wires = _Wires()
    out: list[Control] = []

    def add(cid: str, fig: str, typ: str, label: str, does: str, section: str) -> None:
        out.append(Control(cid, fig, typ, label, does, 0, 0, 0, 0, section, wires.next(typ), None))

    for deck in (1, 2):
        for suffix, fig, typ, label, does in _deck_rows(deck):
            add(f"d{deck}-{suffix}", fig, typ, label, does, "performance" if typ == "pad" else "deck")
    for ch in (3, 1, 2, 4):
        for suffix, fig, typ, label, does in _channel_rows(ch):
            add(f"ch{ch}-{suffix}", fig, typ, label, does, "mixer")
    for cid, fig, typ, label, does, section in _GLOBAL_ROWS:
        add(cid, fig, typ, label, does, section)

    ids = [c.id for c in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    known = set(ids)
    missing_pos, extra_pos = sorted(known - set(_PX)), sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    for cid in [*_PX_REPEAT, *_MANUAL_IDS]:
        if cid not in known:
            raise SystemExit(f"[ERROR] trace names an unknown control: {cid}")
    wires_seen = {(c.midi.ch, c.midi.kind, c.midi.id) for c in out}
    if len(wires_seen) != len(out):
        raise SystemExit("[ERROR] placeholder wires collide")
    return out


# ----------------------------------------------------------------- normalisation


def _aligned_centres(controls: list[Control], axis: int) -> dict[str, float]:
    """Row/column mates (same type, centres within ALIGN_TOL_MM) share one snapped centre."""
    centres: dict[str, float] = {}
    for c in controls:
        x, y, w, h = _mm_box(_PX[c.id])
        centres[c.id] = (x + w / 2) if axis == 0 else (y + h / 2)
    by_type: dict[str, list[Control]] = {}
    for c in controls:
        by_type.setdefault(c.type, []).append(c)
    out: dict[str, float] = {}
    for mates in by_type.values():
        ordered = sorted(mates, key=lambda c: centres[c.id])
        group: list[Control] = []
        for c in ordered:
            if group and centres[c.id] - centres[group[-1].id] > ALIGN_TOL_MM:
                _snap_group(group, centres, out)
                group = []
            group.append(c)
        if group:
            _snap_group(group, centres, out)
    return out


def _snap_group(group: list[Control], centres: dict[str, float], out: dict[str, float]) -> None:
    mean = sum(centres[c.id] for c in group) / len(group)
    snapped = round(mean * 2) / 2
    for c in group:
        out[c.id] = snapped


def _normalised(controls: list[Control]) -> list[Control]:
    cx = _aligned_centres(controls, 0)
    cy = _aligned_centres(controls, 1)
    out: list[Control] = []
    for c in controls:
        _, _, w_raw, h_raw = _mm_box(_PX[c.id])
        w, h = float(round(w_raw)), float(round(h_raw))
        out.append(replace(c, x=round(cx[c.id] - w / 2, 1), y=round(cy[c.id] - h / 2, 1), w=w, h=h))
    return out


# ----------------------------------------------------------------- build


def build_reference() -> dict[str, object]:
    controls = _controls()
    rows: list[dict[str, object]] = []
    for c in controls:
        x, y, w, h = _mm_box(_PX[c.id])
        rows.append({"id": c.id, "px": list(_PX[c.id]), "x": x, "y": y, "w": w, "h": h})
    return {
        "format": "opendj-panel-layout/1",
        "device": "sc-live-4",
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": TRACE,
        "controls": rows,
    }


def build() -> dict[str, object]:
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {c.x},{c.y},{c.w},{c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": "sc-live-4",
        "display_name": "4-channel standalone system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "SC_LIVE_4_MAP",
        "sources": {
            "panel_size": "SC LIVE 4 user guide v4.2.0 RevB, page 104, Technical Specifications (71.9 x 40.4 cm)",
            "wire": (
                "PLACEHOLDER wires on channel 16 (no MIDI message list is published for the SC LIVE 4); "
                "apps/webui/frontend/src/lib/rb/midi/maps/sc-live-4.ts hints every one, binds none"
            ),
            "positions": (
                "measured trace of the official top-panel figure (user guide page 23), normalised for the "
                "renderer; reference: devices/sc-live-4/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "every control is inert: no official MIDI message list exists, so nothing is bound",
            "touchscreen, jog displays, channel and main level meters, peak LEDs: no performance input",
            "decks 3 and 4: each hardware deck carries its deck 1 or deck 2 id; DECK is a hinted button",
            "front-panel headphone sockets and rear-panel connections: not on the top panel",
            "SHIFT layers: the guide names them but publishes no wires, so shift_midi is null throughout",
        ],
        "controls": [asdict(c) for c in controls],
    }


def ts_hints() -> str:
    """The hint table for sc-live-4.ts, one row per control, in build order."""
    return "\n".join(f"\t['{c.id}', '{c.midi.kind}', {c.midi.id}, '{c.label}']," for c in _controls())


if __name__ == "__main__":
    if "--print-ts-hints" in sys.argv[1:]:
        print(ts_hints())
        raise SystemExit(0)
    ref = build_reference()
    doc = build()
    DEVICE_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
