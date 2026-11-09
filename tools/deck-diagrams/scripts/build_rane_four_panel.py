"""Build the RANE FOUR measured panel reference (ADR-0137, epic #9366).

Writes tools/deck-diagrams/devices/rane-four/panel.reference.json: the measurement of record, one
pixel box per top-panel control, read off the official top-down product render on the RANE FOUR
product page (1680 x 884 px, WebP with alpha) and converted to millimetres with the calibration
below. The guide's section crops are not one figure, so the product render is the trace source.

It does NOT write panel.json. RANE publishes no MIDI message list for the FOUR (see
docs/controller/research/RANE-FOUR-MIDI-Message-List.md), the layout format requires a wire per
control, and no wire here may be invented. The layout geometry is still derived from the
reference (never the other way round) by `build_layout_candidate()`, normalised for the renderer:
centres snapped to 0.5 mm, sizes to whole mm, row and column mates within 1.5 mm aligned. The
candidate is what `scripts/skins/control_position_error.py` scores against the reference; the
`--candidate PATH` flag writes it to a scratch path for that check, and the test does the same in
tmp_path. When the wires exist (a hardware capture, or a RANE publication), the rows below take
`midi` and `shift_midi` and panel.json is written from the same table.

Sources:
- Panel size 783.34 x 402.34 mm: RANE FOUR product page, Specifications (Dimensions, width x depth
  x height, 30.84" x 15.84" x 4.08", 783.34 x 402.34 x 103.63 mm); the user guide v1.2 Technical
  Specifications prints the same inches.
- Control names and `fig` numbers: FOUR User Guide v1.2, Features: Top Panel (Deck Controls D1..D17,
  Performance Pads P1..P13, FX Section F1..F9, Mixer Controls M1..M22).
- Positions: `_PX` below, the first trace. A second independent read of eight controls by a
  different method (1-D luminance and saturation profiles instead of 2-D component boxes, or a 4x
  crop read for a control first traced from profiles) gave `REPEAT_SPREAD_MM`.

Calibration: the plate (alpha channel) spans columns 68..1611 (1544 px = 783.34 mm, 0.50735 mm/px)
and rows 74..816. The render is isotropic (SLIP, CENSOR and SHIFT 23 to 25 px square, PLAY/PAUSE
69 x 69 px), so y uses the x scale; the 743 visible rows cover 377.0 mm of the published 402.34 mm
depth, the front-panel controls and rear connectors lying outside a top view. The image never
enters the repo.

Run: uv run --locked python tools/deck-diagrams/scripts/build_rane_four_panel.py [--candidate PATH]
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path

PANEL_W_MM = 783.34
PANEL_D_MM = 402.34
DEVICE = "rane-four"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "bf80bf957e3897aa46e57a46db094623dc07a9a2b94e47ec02e1c6fd6ab433bf"
SOURCE_IMAGE_SHA256 = "deed73a93db2593cb452ee0f7fbf5f7ec7d8ea50cddf066bb62395021a614b11"
SOURCE_IMAGE_PNG_SHA256 = "13352031fba662fcf07ba0fb7b1c0f361580784117620a7996963a9f3dd27d7e"
IMAGE_PX = (1680, 884)
PLATE_PX = {"x0": 68, "x1": 1611, "y0": 74, "y1": 816}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
VISIBLE_DEPTH_MM = round((PLATE_PX["y1"] - PLATE_PX["y0"] + 1) * MM_PER_PX_Y, 1)
REPEAT_SPREAD_MM = 1.6
TRACE_DATE = "Sun 8 Nov 2026"
TRACE_ISSUE = "#9366"

# Deck 2 is deck 1 translated right by this many pixels (checked on 14 lit deck-2 controls, within 1 px).
DECK_2_DX_PX = 994


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


# Traced pixel boxes [x0, y0, x1, y1], inclusive, on the 1680 x 884 render. Lit button and pad:
# bounding box of the lit body. Knob: ring or cap bounding box. Dark knob, encoder, switch: body
# edges on the row and column through it. Jog: outer bezel lip. Vertical fader: travel slot with cap
# width. Crossfader: travel slot with cap height. FX paddle: lever housing. Joystick: stick and ring.
_PX: dict[str, tuple[int, int, int, int]] = {
    # deck 1
    "d1-beat-jump-back": (114, 103, 136, 126),
    "d1-beat-jump-fwd": (162, 103, 184, 126),
    "d1-beat-jump-length": (208, 101, 234, 126),
    "d1-slip": (390, 103, 413, 126),
    "d1-censor": (438, 102, 461, 126),
    "d1-back": (495, 113, 518, 126),
    "d1-scroll-load": (540, 93, 575, 127),
    "d1-stem-split": (541, 158, 575, 190),
    "d1-deck-a": (546, 210, 570, 233),
    "d1-deck-b": (546, 255, 570, 279),
    "d1-range": (546, 313, 569, 326),
    "d1-jog": (94, 146, 530, 581),
    "d1-pitch": (540, 361, 575, 575),
    "d1-shift": (114, 551, 136, 574),
    "d1-sync": (91, 612, 159, 639),
    "d1-cue": (92, 660, 159, 687),
    "d1-play-pause": (91, 713, 159, 781),
    "d1-mode-hot-cue": (184, 617, 236, 634),
    "d1-mode-roll": (249, 617, 301, 634),
    "d1-mode-sampler": (314, 618, 365, 634),
    "d1-mode-stems": (378, 618, 430, 634),
    "d1-param-prev": (449, 620, 472, 633),
    "d1-param-next": (486, 620, 508, 633),
    "d1-bend-minus": (528, 620, 551, 633),
    "d1-bend-plus": (565, 620, 588, 633),
    "d1-key-down": (528, 670, 551, 683),
    "d1-key-up": (564, 670, 588, 683),
    "d1-pad-1": (184, 691, 236, 729),
    "d1-pad-2": (248, 690, 302, 730),
    "d1-pad-3": (313, 690, 366, 729),
    "d1-pad-4": (378, 690, 431, 730),
    "d1-pad-5": (183, 741, 238, 781),
    "d1-pad-6": (248, 742, 301, 781),
    "d1-pad-7": (314, 742, 366, 781),
    "d1-pad-8": (378, 742, 430, 781),
    "d1-loop": (447, 707, 510, 725),
    "d1-half-x": (448, 754, 472, 768),
    "d1-double-x": (485, 754, 509, 768),
    "d1-acapella": (527, 707, 589, 725),
    "d1-instrumental": (527, 752, 589, 770),
    # deck 2 (lit controls read directly; dark controls are deck 1 + DECK_2_DX_PX)
    "d2-beat-jump-back": (1107, 103, 1130, 125),
    "d2-beat-jump-fwd": (1156, 103, 1178, 125),
    "d2-beat-jump-length": (1204, 101, 1230, 126),
    "d2-slip": (1384, 102, 1407, 126),
    "d2-censor": (1431, 102, 1455, 126),
    "d2-back": (1489, 113, 1512, 126),
    "d2-scroll-load": (1535, 93, 1571, 127),
    "d2-stem-split": (1535, 158, 1569, 191),
    "d2-deck-a": (1540, 210, 1563, 233),
    "d2-deck-b": (1540, 255, 1563, 279),
    "d2-range": (1540, 313, 1563, 326),
    "d2-jog": (1088, 145, 1523, 580),
    "d2-pitch": (1535, 360, 1570, 575),
    "d2-shift": (1107, 551, 1131, 573),
    "d2-sync": (1085, 612, 1153, 639),
    "d2-cue": (1085, 660, 1153, 686),
    "d2-play-pause": (1085, 713, 1153, 781),
    "d2-mode-hot-cue": (1179, 618, 1231, 634),
    "d2-mode-roll": (1243, 618, 1295, 634),
    "d2-mode-sampler": (1307, 618, 1359, 634),
    "d2-mode-stems": (1372, 618, 1424, 634),
    "d2-param-prev": (1443, 620, 1465, 633),
    "d2-param-next": (1479, 620, 1502, 633),
    "d2-bend-minus": (1522, 620, 1545, 633),
    "d2-bend-plus": (1558, 620, 1581, 633),
    "d2-key-down": (1522, 670, 1545, 683),
    "d2-key-up": (1558, 670, 1582, 683),
    "d2-pad-1": (1178, 690, 1231, 730),
    "d2-pad-2": (1242, 690, 1296, 730),
    "d2-pad-3": (1306, 690, 1360, 730),
    "d2-pad-4": (1371, 690, 1425, 730),
    "d2-pad-5": (1178, 741, 1231, 781),
    "d2-pad-6": (1242, 742, 1295, 781),
    "d2-pad-7": (1306, 741, 1360, 781),
    "d2-pad-8": (1371, 742, 1425, 781),
    "d2-loop": (1441, 707, 1503, 725),
    "d2-half-x": (1442, 754, 1466, 768),
    "d2-double-x": (1479, 754, 1503, 768),
    "d2-acapella": (1520, 707, 1583, 725),
    "d2-instrumental": (1520, 752, 1583, 770),
    # mixer strips, panel order 3, 1, 2, 4
    "ch3-level": (716, 137, 752, 171),
    "ch3-high": (718, 191, 750, 223),
    "ch3-mid": (718, 242, 750, 274),
    "ch3-low": (718, 293, 750, 325),
    "ch3-fx": (713, 350, 757, 394),
    "ch3-cue": (692, 544, 716, 558),
    "ch3-fader": (686, 589, 721, 693),
    "ch1-level": (787, 137, 823, 171),
    "ch1-high": (789, 191, 820, 223),
    "ch1-mid": (789, 242, 820, 274),
    "ch1-low": (789, 293, 820, 325),
    "ch1-fx": (783, 350, 827, 394),
    "ch1-cue": (782, 544, 807, 558),
    "ch1-fader": (776, 589, 811, 693),
    "ch2-level": (857, 137, 893, 171),
    "ch2-high": (859, 191, 890, 223),
    "ch2-mid": (859, 242, 890, 273),
    "ch2-low": (859, 293, 890, 325),
    "ch2-fx": (852, 350, 896, 394),
    "ch2-cue": (873, 544, 897, 558),
    "ch2-fader": (867, 589, 902, 693),
    "ch4-level": (927, 137, 963, 171),
    "ch4-high": (929, 191, 961, 223),
    "ch4-mid": (929, 242, 960, 274),
    "ch4-low": (929, 293, 960, 325),
    "ch4-fx": (922, 350, 966, 394),
    "ch4-cue": (963, 544, 987, 558),
    "ch4-fader": (959, 589, 994, 693),
    # mixer globals
    "source-left": (762, 98, 797, 132),
    "source-right": (889, 98, 925, 132),
    "ch3-usb-line": (701, 107, 714, 125),
    "ch4-usb-line": (966, 107, 979, 125),
    "quantize": (1003, 103, 1025, 116),
    "chfx-filter": (635, 352, 659, 366),
    "chfx-filter-roll": (672, 352, 695, 366),
    "chfx-noise": (635, 381, 658, 395),
    "chfx-flanger": (672, 381, 695, 395),
    "mic1-on": (653, 103, 677, 117),
    "mic1-level": (650, 140, 676, 171),
    "mic1-high": (652, 192, 677, 216),
    "mic1-low": (652, 243, 677, 267),
    "mic1-echo": (652, 293, 675, 316),
    "mic1-echo-switch": (629, 289, 641, 316),
    "main-level": (1000, 140, 1030, 171),
    "booth-zone": (1000, 284, 1032, 316),
    "sampler-level": (1000, 330, 1032, 362),
    "sampler-assign": (1003, 385, 1025, 398),
    "crossfader": (790, 750, 891, 792),
    # effects
    "fx-echo": (693, 418, 717, 431),
    "fx-recycler": (729, 418, 753, 431),
    "fx-scale-dn": (766, 418, 789, 431),
    "fx-riser": (890, 418, 913, 431),
    "fx-matrix": (926, 418, 950, 431),
    "fx-echo-out": (963, 418, 987, 431),
    "fx-param-encoder": (651, 422, 677, 448),
    "fx-param-joystick": (1003, 424, 1026, 447),
    "fx-assign-ch1": (653, 469, 677, 482),
    "fx-assign-ch3": (653, 498, 677, 511),
    "fx-assign-ch2": (1002, 469, 1026, 482),
    "fx-assign-ch4": (1002, 498, 1026, 511),
    "fx-paddle-left": (715, 454, 781, 521),
    "fx-paddle-right": (897, 454, 966, 521),
    "fx-hw-sw": (803, 462, 826, 475),
    "fx-tap": (854, 462, 876, 475),
    "fx-depth": (821, 489, 855, 528),
}

# Second independent read, used only for REPEAT_SPREAD_MM. Controls first traced as 2-D component
# or ring boxes were re-read on the 1-D luminance (or saturation, fx-depth) profile through them;
# d1-play-pause was re-read on a fresh 4x crop with a 10 px grid.
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-play-pause": (92, 713, 159, 781),
    "d2-pad-3": (1307, 690, 1360, 730),
    "ch1-high": (788, 187, 822, 224),
    "fx-depth": (821, 487, 857, 524),
    "d1-sync": (90, 612, 161, 640),
    "d1-shift": (113, 550, 138, 575),
    "fx-hw-sw": (802, 462, 827, 476),
    "ch4-cue": (963, 544, 988, 560),
}

# ----------------------------------------------------------------- helpers


def _mm_box(px: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round((x0 - PLATE_PX["x0"]) * MM_PER_PX_X, 1),
        round((y0 - PLATE_PX["y0"]) * MM_PER_PX_Y, 1),
        round((x1 - x0 + 1) * MM_PER_PX_X, 1),
        round((y1 - y0 + 1) * MM_PER_PX_Y, 1),
    )


def _centre(px: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = px
    return ((x0 + x1 + 1) / 2 * MM_PER_PX_X, (y0 + y1 + 1) / 2 * MM_PER_PX_Y)


def repeat_spread_mm() -> float:
    """Largest centre distance between the two traces, in mm."""
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        (ax, ay), (bx, by) = _centre(_PX[cid]), _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _mm_box(_PX[cid])
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _PX entry for {cid!r}") from e


# ----------------------------------------------------------------- deck sides

# fig, id tail, type, label, does, section
DeckRow = tuple[str, str, str, str, str, str]

_DECK_ROWS: list[DeckRow] = [
    ("D9", "beat-jump-back", "button", "BEAT JUMP <", "Jump back by the beat jump length", "deck"),
    ("D9", "beat-jump-fwd", "button", "BEAT JUMP >", "Jump forward by the beat jump length", "deck"),
    (
        "D10",
        "beat-jump-length",
        "encoder",
        "BEAT JUMP LENGTH",
        "Turn to change the beat jump length; push to reset it to 8 beats",
        "deck",
    ),
    ("D11", "slip", "button", "SLIP", "Slip mode on or off; with SHIFT, vinyl mode for the jog", "deck"),
    ("D12", "censor", "button", "CENSOR", "Hold to censor (or silent cue); with SHIFT, reverse", "deck"),
    ("D3", "back", "button", "BACK", "Back one level in the library; with SHIFT, load to the Prepare crate", "browse"),
    (
        "D2",
        "scroll-load",
        "encoder",
        "SCROLL / LOAD",
        "Turn to browse; push to load; push twice to load both layers",
        "browse",
    ),
    (
        "D17",
        "stem-split",
        "button",
        "STEM-SPLIT",
        "Instant-double the track onto both layers as acapella and instrumental",
        "deck",
    ),
    ("D1", "deck-a", "button", "DECK", "Select the first deck layer on this side", "deck"),
    ("D1", "deck-b", "button", "DECK", "Select the second deck layer on this side", "deck"),
    ("D6", "range", "button", "RANGE", "Pitch range 8, 16 or 50 percent; with SHIFT, keylock", "deck"),
    ("D7", "jog", "jog", "JOG", "Scratch, nudge or pitch bend; with SHIFT, seek", "deck"),
    ("D4", "pitch", "fader", "PITCH", "Tempo slider, vertical", "deck"),
    ("D13", "shift", "button", "SHIFT", "Hold for each control's second function", "deck"),
    ("D16", "sync", "button", "SYNC", "Match tempo to the other deck; with SHIFT, sync off", "deck"),
    (
        "D15",
        "cue",
        "button",
        "CUE",
        "Set or return to the temporary cue point; with SHIFT, track start or previous track",
        "deck",
    ),
    ("D14", "play-pause", "button", "PLAY/PAUSE", "Start or pause the deck; with SHIFT, stutter play", "deck"),
    (
        "P1",
        "mode-hot-cue",
        "button",
        "HOT CUE",
        "Pad mode: hot cue; twice for pitch play; with SHIFT, custom user mode",
        "performance",
    ),
    ("P2", "mode-roll", "button", "ROLL", "Pad mode: roll; twice for saved loops", "performance"),
    ("P3", "mode-sampler", "button", "SAMPLER", "Pad mode: sampler; twice for scratch bank", "performance"),
    ("P4", "mode-stems", "button", "STEMS", "Pad mode: stems; twice for slicer", "performance"),
    ("P5", "param-prev", "button", "PARAMETER <", "Previous parameter for the pad mode", "performance"),
    ("P5", "param-next", "button", "PARAMETER >", "Next parameter for the pad mode", "performance"),
    ("D5", "bend-minus", "button", "PITCH BEND -", "Hold to slow the track momentarily", "deck"),
    ("D5", "bend-plus", "button", "PITCH BEND +", "Hold to speed the track up momentarily", "deck"),
    ("P10", "key-down", "button", "KEY <", "Key down one step; with SHIFT, key sync", "performance"),
    ("P10", "key-up", "button", "KEY >", "Key up one step; with SHIFT, key reset", "performance"),
    ("P11", "loop", "button", "LOOP ON/OFF", "Auto loop at the software length; with SHIFT, reloop", "performance"),
    ("P12", "half-x", "button", "x1/2", "Halve the loop; with SHIFT, loop in", "performance"),
    ("P13", "double-x", "button", "x2", "Double the loop; with SHIFT, loop out", "performance"),
    (
        "P8",
        "acapella",
        "button",
        "ACAPELLA",
        "Play only the vocal stem; with SHIFT, echo out the removed stems",
        "performance",
    ),
    (
        "P9",
        "instrumental",
        "button",
        "INSTRUMENTAL",
        "Remove the vocal stem; with SHIFT, echo out the removed stems",
        "performance",
    ),
]


def _deck_select_text(deck: int, tail: str, label: str, does: str) -> tuple[str, str]:
    """The two DECK layer buttons name their deck: 1 and 3 on the left side, 2 and 4 on the right."""
    if tail == "deck-a":
        return f"DECK {deck}", f"Select deck {deck} as this side's layer"
    if tail == "deck-b":
        return f"DECK {deck + 2}", f"Select deck {deck + 2} as this side's layer"
    return label, does


def _deck(deck: int) -> list[Control]:
    out: list[Control] = []
    for fig, tail, typ, row_label, row_does, section in _DECK_ROWS:
        cid = f"d{deck}-{tail}"
        x, y, w, h = _at(cid)
        label, does = _deck_select_text(deck, tail, row_label, row_does)
        out.append(Control(cid, fig, typ, label, f"{does} (side {deck})", x, y, w, h, section))
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                "P7",
                "pad",
                f"PAD {pad + 1}",
                f"Performance pad {pad + 1} (side {deck})",
                x,
                y,
                w,
                h,
                "performance",
            )
        )
    return out


# ----------------------------------------------------------------- mixer channels


def _channel(ch: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str]] = [
        ("M3", "level", "knob", "LEVEL", "Pre-fader, pre-EQ channel level"),
        ("M4", "high", "knob", "HIGH", "EQ high; vocal stem level in Stem Level mode"),
        ("M5", "mid", "knob", "MID", "EQ mid; melody and bass stem level in Stem Level mode"),
        ("M6", "low", "knob", "LOW", "EQ low; drum stem level in Stem Level mode"),
        ("M7", "fx", "knob", "CHANNEL FX", "Low-pass left, high-pass right, off at centre"),
        ("M10", "cue", "button", "CUE", "Pre-fader signal to the headphone cue"),
        ("M11", "fader", "fader", "FADER", "Channel volume"),
    ]
    out: list[Control] = []
    for fig, tail, typ, label, does in rows:
        cid = f"ch{ch}-{tail}"
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, f"{does} (channel {ch})", x, y, w, h, "mixer"))
    return out


# ----------------------------------------------------------------- globals and effects

# fig, id, type, label, does, section
GlobalRow = tuple[str, str, str, str, str, str]

_GLOBAL_ROWS: list[GlobalRow] = [
    ("M1", "source-left", "knob", "SOURCE", "Deck source for the left side: USB A or B", "mixer"),
    ("M1", "source-right", "knob", "SOURCE", "Deck source for the right side: USB A or B", "mixer"),
    ("M2", "ch3-usb-line", "button", "USB / LINE", "Channel 3 source: computer audio or line input", "mixer"),
    ("M2", "ch4-usb-line", "button", "USB / LINE", "Channel 4 source: computer audio or line input", "mixer"),
    ("M9", "quantize", "button", "QUANTIZE", "Quantize mode in the software", "mixer"),
    ("M8", "chfx-filter", "button", "FILTER", "Channel FX: filter", "mixer"),
    ("M8", "chfx-filter-roll", "button", "FILTER ROLL", "Channel FX: filter roll", "mixer"),
    ("M8", "chfx-noise", "button", "NOISE", "Channel FX: noise", "mixer"),
    ("M8", "chfx-flanger", "button", "FLANGER", "Channel FX: flanger", "mixer"),
    ("M17", "mic1-on", "button", "MIC 1 ON", "Mic 1 on or off; with SHIFT, talkover", "mixer"),
    ("M18", "mic1-level", "knob", "LEVEL", "Mic 1 level", "mixer"),
    ("M19", "mic1-high", "knob", "HIGH", "Mic 1 high", "mixer"),
    ("M20", "mic1-low", "knob", "LOW", "Mic 1 low", "mixer"),
    ("M21", "mic1-echo", "knob", "ECHO", "Mic 1 echo level", "mixer"),
    ("M22", "mic1-echo-switch", "button", "ECHO ON/OFF", "Mic 1 echo on or off (slide switch)", "mixer"),
    ("M13", "main-level", "knob", "MAIN", "Main output level", "mixer"),
    ("M14", "booth-zone", "knob", "BOOTH / ZONE", "Booth and zone output level", "mixer"),
    ("M15", "sampler-level", "knob", "SAMPLER LEVEL", "Software sampler level", "mixer"),
    ("M16", "sampler-assign", "button", "SAMPLER ASSIGN", "Route the sampler through channel 4", "mixer"),
    ("M12", "crossfader", "crossfader", "CROSSFADER", "Blend between the assigned channels", "mixer"),
    ("F2", "fx-echo", "button", "ECHO", "Main FX slot: echo", "effect"),
    ("F2", "fx-recycler", "button", "RECYCLER", "Main FX slot: recycler", "effect"),
    ("F2", "fx-scale-dn", "button", "SCALE DN", "Main FX slot: scale down", "effect"),
    ("F2", "fx-riser", "button", "RISER", "Main FX slot: riser", "effect"),
    ("F2", "fx-matrix", "button", "MATRIX", "Main FX slot: matrix", "effect"),
    ("F2", "fx-echo-out", "button", "ECHO OUT", "Main FX slot: echo out", "effect"),
    (
        "F6",
        "fx-param-encoder",
        "encoder",
        "PARAMETER",
        "Turn for the FX parameter; push to cycle parameters; with SHIFT, manual BPM",
        "effect",
    ),
    (
        "F7",
        "fx-param-joystick",
        "encoder",
        "BPM / MENU",
        "Joystick: parameter up and down, BPM side left and right; push to reset",
        "effect",
    ),
    ("F8", "fx-assign-ch1", "button", "CH 1", "Left FX toggle feeds channel 1", "effect"),
    ("F8", "fx-assign-ch3", "button", "CH 3", "Left FX toggle feeds channel 3", "effect"),
    ("F8", "fx-assign-ch2", "button", "CH 2", "Right FX toggle feeds channel 2", "effect"),
    ("F8", "fx-assign-ch4", "button", "CH 4", "Right FX toggle feeds channel 4", "effect"),
    (
        "F3",
        "fx-paddle-left",
        "button",
        "FX ON",
        "Left FX paddle: push away to latch, press down for momentary",
        "effect",
    ),
    (
        "F3",
        "fx-paddle-right",
        "button",
        "FX ON",
        "Right FX paddle: push away to latch, press down for momentary",
        "effect",
    ),
    (
        "F5",
        "fx-hw-sw",
        "button",
        "HW / SW FX",
        "Internal hardware FX or software FX; with SHIFT, system menu",
        "effect",
    ),
    ("F4", "fx-tap", "button", "TAP", "Tap the FX tempo; hold to return to the deck BPM", "effect"),
    ("F9", "fx-depth", "knob", "DEPTH", "FX wet and dry mix", "effect"),
]


def _globals() -> list[Control]:
    out: list[Control] = []
    for fig, cid, typ, label, does, section in _GLOBAL_ROWS:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section))
    return out


# ----------------------------------------------------------------- normalisation for the renderer


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def _aligned_groups(values: dict[str, float], tol_mm: float = 1.5) -> dict[str, float]:
    """Groups of centres whose whole span is within tol_mm; each member takes the group mean.

    The span rule (not single linkage) keeps a long run of nearly aligned controls from chaining
    into one group and dragging centres more than tol_mm.
    """
    order = sorted(values, key=values.__getitem__)
    out: dict[str, float] = {}
    group: list[str] = []
    for cid in order:
        if group and values[cid] - values[group[0]] > tol_mm:
            mean = sum(values[g] for g in group) / len(group)
            out.update({g: mean for g in group})
            group = []
        group.append(cid)
    if group:
        mean = sum(values[g] for g in group) / len(group)
        out.update({g: mean for g in group})
    return out


def normalise(controls: list[Control]) -> list[Control]:
    cx = _aligned_groups({c.id: c.x + c.w / 2 for c in controls})
    cy = _aligned_groups({c.id: c.y + c.h / 2 for c in controls})
    out: list[Control] = []
    for c in controls:
        w, h = float(max(1, round(c.w))), float(max(1, round(c.h)))
        x = round(_snap_half(cx[c.id]) - w / 2, 1)
        y = round(_snap_half(cy[c.id]) - h / 2, 1)
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


# ----------------------------------------------------------------- documents


def _controls() -> list[Control]:
    controls = [*_deck(1), *_deck(2), *_channel(3), *_channel(1), *_channel(2), *_channel(4), *_globals()]
    known = {c.id for c in controls}
    missing_pos = sorted(known - set(_PX))
    extra_pos = sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    unknown_repeat = sorted(set(_PX_REPEAT) - known)
    if unknown_repeat:
        raise SystemExit(f"[ERROR] repeat trace names unknown controls: {unknown_repeat}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {(c.x, c.y, c.w, c.h)}")
    return controls


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but the two traces give {spread}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "RANE FOUR product page hero image: official top-down product render, 1680 x 884 px "
                "WebP with alpha (https://www.rane.com/four/); control names and fig numbers from the "
                "FOUR User Guide v1.2, Features: Top Panel"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_pdf": "FOUR - User Guide - v1.2.pdf (cdn.inmusicbrands.com/rane/four/)",
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "source_image_png_sha256": SOURCE_IMAGE_PNG_SHA256,
            "source_image_url": (
                "https://cdn.builder.io/api/v1/image/assets%2F47c5d023e7b3451a86ac03602c65dce8"
                "%2F18ce606d55974629aeaf6c8e8e322b62"
            ),
            "provenance": "docs/controller/research/RANE-FOUR-MIDI-Message-List.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "visible_depth_mm": VISIBLE_DEPTH_MM,
            "calibration": (
                "x from the plate width (783.34 mm over the 1544 opaque columns of the alpha channel). "
                "y set equal to x because the render is isotropic (SLIP, CENSOR and SHIFT 23 to 25 px "
                "square, PLAY/PAUSE 69 x 69 px); the 743 visible rows cover 377.0 mm of the published "
                "402.34 mm depth, the front-panel controls and rear connectors lying outside a top view."
            ),
            "method": (
                "Tool-assisted trace on the render. Lit button and pad: bounding box of the lit body "
                "(luminance above 150 or saturation above 90). Knob: ring or cap bounding box inside a "
                "search disk (luminance above 140, or saturation above 80 for blue rings). Dark knob, "
                "encoder, slide switch: body edges on the luminance profile of the row and column through "
                "it. Jog: outer bezel lip. Vertical fader: travel slot with cap width. Crossfader: travel "
                "slot with cap height. FX paddle: lever housing. Joystick: stick and ring. Deck 2 dark "
                "controls: deck 1 translated by 994 px, checked against 14 lit deck-2 controls."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "repeat_note": (
                "the 1.6 mm worst case is fx-depth, whose blue ring reads 4 px taller in the 2-D "
                "saturation box (pointer glow) than on the saturation profile"
            ),
            "midi": "no published MIDI message list; wires are not part of this measurement",
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [{"id": c.id, "px": list(_PX[c.id]), "x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in _controls()],
    }


def build_layout_candidate() -> dict[str, object]:
    """The renderer-normalised geometry, as a candidate for control_position_error.

    Not a panel.json: it carries no wires and no device_map (see the module docstring), only what
    the position check reads (panel.w and the control rectangles) plus the fig, type, label, does
    and section a later wired layout will reuse.
    """
    controls = normalise(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel after normalisation: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel stems controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "note": (
            "normalised candidate for control_position_error only; no midi wires exist for this unit, "
            "so this is not a panel.json and is never served"
        ),
        "controls": [
            {
                "id": c.id,
                "fig": c.fig,
                "type": c.type,
                "label": c.label,
                "does": c.does,
                "x": c.x,
                "y": c.y,
                "w": c.w,
                "h": c.h,
                "section": c.section,
            }
            for c in controls
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidate", type=Path, default=None, help="also write the normalised candidate here")
    args = parser.parse_args(argv)
    reference = build_reference()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    n = len(reference["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {REFERENCE_OUT} ({n} controls, repeat spread {REPEAT_SPREAD_MM} mm)")
    if args.candidate is not None:
        args.candidate.write_text(json.dumps(build_layout_candidate(), indent=1) + "\n", encoding="utf-8")
        print(f"[OK] wrote candidate {args.candidate} (not a panel.json: no wires)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
