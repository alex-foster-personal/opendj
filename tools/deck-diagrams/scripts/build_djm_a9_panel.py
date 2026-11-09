"""Build the DJM-A9 practice-replica panel pair (ADR-0137, epic #9366).

Writes two files under tools/deck-diagrams/devices/djm-a9/:
- panel.reference.json: the measurement of record. One pixel box per control, traced on the
  top-panel figure embedded in the official "DJM-A9 MIDI Message List" PDF (page 1, embedded
  image 1, 1383 x 1344 px), converted to millimetres with the calibration below.
- panel.json: the layout the practice route renders, derived FROM the reference (never the other
  way round): centres snapped to 0.5 mm, sizes to whole mm, row and column mates aligned.

Sources:
- Panel size 407.4 x 458.3 mm: DJM-A9 product page, Specifications (Dimensions W x D x H
  407.4 x 458.3 x 107.9 mm).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/djm-a9.ts, itself transcribed from
  the MIDI message list PDF (the sheet's Part No. ids L1..L22, C1..C10, R1..R29 kept as `fig`).
- Positions: `_PX` below, the first trace. A second independent trace of ten controls on fresh
  4x crops with a 5 px grid (no first-pass boxes drawn) gave `REPEAT_SPREAD_MM`.

Calibration: the plate spans image columns 22..1353 (1332 px = 407.4 mm, 0.30586 mm/px). The
plate-derived row scale would be 458.3 / 1304 = 0.35146 mm/px, a 14.9% disagreement, but the
figure is isotropic: the round BEAT FX ON/OFF button measures 59 x 58 px, the LEVEL/DEPTH cap
50 x 50 px, the TIME knob ring 52 x 52 px and the MASTER LEVEL cap 48 x 50 px. So the y scale is
set equal to x, and the plate's 1304 visible rows cover 398.8 of the 458.3 mm depth; the sloped
rear connector panel lies behind the top plate and shows only as the strip above row 22. The
image never enters the repo.

Run: uv run --locked python tools/deck-diagrams/scripts/build_djm_a9_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 407.4
PANEL_D_MM = 458.3
DEVICE = "djm-a9"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "4bca4d93965d669e8a22bd58a9582ce2fdd23fc3cc29a576203f8e2df819c76f"
SOURCE_IMAGE_SHA256 = "205f2c2c4afe3d03814022b11e404cdf23af03804554d18de4ea606eaf5ec273"
IMAGE_PX = (1383, 1344)
PLATE_PX = {"x0": 22, "x1": 1353, "y0": 22, "y1": 1325}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
PLATE_ROW_SCALE_MM_PER_PX = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
REPEAT_SPREAD_MM = 0.3
TRACE_DATE = "Sun 8 Nov 2026"
TRACE_ISSUE = "#9366"

# The mixer sends every message on its UTILITY "MIDI CH" setting; the map binds the factory
# default, channel 1 (apps/webui/frontend/src/lib/rb/midi/maps/djm-a9.ts, A9_MIDI_CH).
MIDI_CH = 1
CHANNELS = (1, 2, 3, 4)


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


# Traced pixel boxes [x0, y0, x1, y1], inclusive, on the 1383 x 1344 figure. Button: body
# bounding box (outlined buttons: the outline; round buttons: the round body). Knob and rotary
# selector: the visible cap. CH FADER: travel slot (scale 10 to 0) with cap width. CROSSFADER:
# travel slot with cap height. X-PAD: the touch strip. Slide switches: the switch body.
_PX: dict[str, tuple[int, int, int, int]] = {
    # MIC column (L1..L10) and BLUETOOTH (L11)
    "mic-eq-hi": (88, 380, 128, 420),
    "mic-eq-low": (88, 455, 128, 497),
    "talkover": (95, 545, 124, 574),
    "mic-fx-echo": (88, 618, 130, 643),
    "mic-fx-pitch": (88, 681, 130, 706),
    "mic-fx-megaphone": (88, 744, 130, 769),
    "mic-fx-parameter": (90, 822, 130, 865),
    "mic-reverb": (88, 936, 130, 961),
    "mic-reverb-parameter": (90, 1003, 130, 1047),
    "mic-on": (91, 1185, 128, 1221),
    "bluetooth": (243, 303, 290, 332),
    # SOUND COLOR FX (L12..L18)
    "cfx-space": (210, 618, 250, 642),
    "cfx-dub-echo": (279, 618, 320, 641),
    "cfx-crush": (210, 681, 250, 705),
    "cfx-sweep": (279, 682, 319, 705),
    "cfx-noise": (210, 744, 251, 768),
    "cfx-filter": (279, 744, 319, 767),
    "cfx-parameter": (244, 821, 286, 864),
    # HEADPHONES A (L19..L22)
    "phones-a-link-cue": (210, 931, 257, 965),
    "phones-a-mono-split": (279, 931, 326, 965),
    "phones-a-mix": (243, 1002, 288, 1048),
    "phones-a-level": (245, 1090, 289, 1135),
    # channel strips (C1..C9): channel n sits 141 px to the right of channel n-1
    "ch1-input": (405, 115, 444, 155),
    "ch1-trim": (403, 208, 446, 252),
    "ch1-hi": (403, 302, 448, 348),
    "ch1-mid": (403, 402, 448, 448),
    "ch1-low": (403, 502, 448, 548),
    "ch1-color": (397, 632, 452, 688),
    "ch1-cue-a": (371, 763, 425, 785),
    "ch1-cue-b": (444, 764, 478, 785),
    "ch1-fader": (388, 905, 456, 1058),
    "ch1-xf-assign": (404, 1128, 449, 1154),
    "ch2-input": (546, 115, 585, 155),
    "ch2-trim": (544, 208, 587, 252),
    "ch2-hi": (544, 302, 589, 348),
    "ch2-mid": (544, 402, 589, 448),
    "ch2-low": (544, 502, 589, 548),
    "ch2-color": (538, 632, 593, 688),
    "ch2-cue-a": (511, 763, 565, 786),
    "ch2-cue-b": (585, 764, 619, 785),
    "ch2-fader": (530, 905, 597, 1058),
    "ch2-xf-assign": (545, 1128, 590, 1154),
    "ch3-input": (687, 115, 726, 155),
    "ch3-trim": (685, 208, 728, 252),
    "ch3-hi": (685, 302, 730, 348),
    "ch3-mid": (685, 402, 730, 448),
    "ch3-low": (685, 502, 730, 548),
    "ch3-color": (679, 632, 734, 688),
    "ch3-cue-a": (652, 763, 706, 785),
    "ch3-cue-b": (726, 764, 760, 785),
    "ch3-fader": (670, 905, 739, 1058),
    "ch3-xf-assign": (686, 1128, 731, 1154),
    "ch4-input": (828, 115, 867, 155),
    "ch4-trim": (826, 208, 869, 252),
    "ch4-hi": (826, 302, 871, 348),
    "ch4-mid": (826, 402, 871, 448),
    "ch4-low": (826, 502, 871, 548),
    "ch4-color": (820, 632, 875, 688),
    "ch4-cue-a": (793, 762, 846, 785),
    "ch4-cue-b": (867, 764, 901, 785),
    "ch4-fader": (812, 905, 881, 1058),
    "ch4-xf-assign": (827, 1128, 872, 1154),
    # CROSSFADER (C10)
    "crossfader": (562, 1222, 716, 1290),
    # MASTER, BOOTH, MASTER CUE, HEADPHONES B, curve switches (R1..R13)
    "master-level": (975, 92, 1022, 141),
    "booth-eq-hi": (981, 543, 1017, 581),
    "booth-eq-low": (981, 607, 1017, 645),
    "booth-level": (975, 673, 1022, 722),
    "master-cue-a": (947, 762, 1001, 785),
    "master-cue-b": (1021, 764, 1055, 785),
    "phones-b-link-cue": (947, 873, 984, 903),
    "phones-b-mono-split": (1018, 873, 1053, 903),
    "phones-b-mix": (981, 932, 1018, 970),
    "phones-b-level": (981, 1005, 1018, 1045),
    "eq-curve": (992, 1086, 1024, 1112),
    "ch-fader-curve": (992, 1148, 1024, 1175),
    "crossfader-curve": (992, 1210, 1024, 1237),
    # MULTI I/O (R14..R17)
    "send-ch": (1102, 101, 1138, 139),
    "insert-source": (1199, 108, 1217, 140),
    "send-level": (1097, 196, 1142, 244),
    "insert": (1189, 203, 1224, 238),
    # BEAT FX (R18..R29)
    "x-pad": (1098, 490, 1316, 525),
    "beat-left": (1122, 562, 1178, 586),
    "beat-right": (1236, 562, 1292, 586),
    "auto-tap": (1133, 621, 1160, 648),
    "tap": (1183, 603, 1228, 648),
    "quantize": (1258, 619, 1285, 646),
    "fx-freq-low": (1121, 690, 1163, 715),
    "fx-freq-mid": (1185, 690, 1227, 715),
    "fx-freq-hi": (1249, 690, 1291, 715),
    "fx-select": (1184, 752, 1227, 797),
    "ch-select-1": (1111, 862, 1145, 881),
    "ch-select-2": (1164, 862, 1197, 881),
    "ch-select-3": (1216, 862, 1249, 881),
    "ch-select-4": (1268, 862, 1301, 881),
    "ch-select-mic": (1111, 919, 1144, 938),
    "ch-select-cf-a": (1164, 919, 1197, 938),
    "ch-select-cf-b": (1216, 919, 1249, 938),
    "ch-select-mst": (1268, 919, 1301, 938),
    "fx-time": (1180, 991, 1231, 1042),
    "fx-level-depth": (1181, 1097, 1230, 1146),
    "fx-on-off": (1178, 1205, 1236, 1262),
}

# Second independent trace (fresh 4x crops, 5 px grid), used only for REPEAT_SPREAD_MM.
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "ch1-trim": (404, 209, 446, 252),
    "ch3-cue-a": (652, 763, 706, 786),
    "ch4-fader": (813, 904, 880, 1058),
    "master-level": (976, 93, 1022, 141),
    "crossfader": (562, 1221, 716, 1291),
    "fx-on-off": (1178, 1206, 1236, 1263),
    "cfx-space": (210, 618, 251, 643),
    "send-level": (1097, 197, 1142, 244),
    "phones-a-mix": (243, 1003, 288, 1049),
    "x-pad": (1098, 489, 1316, 526),
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


def _control(cid: str, fig: str, typ: str, label: str, does: str, section: str, kind: str, num: int) -> Control:
    x, y, w, h = _at(cid)
    return Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(MIDI_CH, kind, num))


# ----------------------------------------------------------------- left column

# fig, id, type, label, does, section, kind, number
Row = tuple[str, str, str, str, str, str, str, int]

_LEFT_ROWS: list[Row] = [
    ("L1", "mic-eq-hi", "knob", "MIC EQ HI", "Microphone EQ high band", "mixer", "cc", 0x1E),
    ("L2", "mic-eq-low", "knob", "MIC EQ LOW", "Microphone EQ low band", "mixer", "cc", 0x1F),
    ("L3", "talkover", "button", "TALKOVER", "Duck the music while the microphone is used", "mixer", "note", 0x61),
    ("L4", "mic-fx-echo", "button", "ECHO", "Microphone effect: echo", "mixer", "cc", 0x61),
    ("L5", "mic-fx-pitch", "button", "PITCH", "Microphone effect: pitch", "mixer", "cc", 0x62),
    ("L6", "mic-fx-megaphone", "button", "MEGAPHONE", "Microphone effect: megaphone", "mixer", "cc", 0x63),
    ("L7", "mic-fx-parameter", "knob", "PARAMETER", "Microphone effect amount", "mixer", "cc", 0x64),
    ("L8", "mic-reverb", "button", "MIC REVERB", "Microphone reverb on or off", "mixer", "note", 0x60),
    ("L9", "mic-reverb-parameter", "knob", "PARAMETER", "Microphone reverb amount", "mixer", "cc", 0x65),
    ("L10", "mic-on", "button", "MIC ON", "Microphone on, or push to talk", "mixer", "note", 0x62),
    ("L11", "bluetooth", "button", "BLUETOOTH", "Bluetooth pairing", "mixer", "note", 0x70),
    ("L12", "cfx-space", "button", "SPACE", "Sound Color FX: space", "effect", "note", 0x55),
    ("L13", "cfx-dub-echo", "button", "DUB ECHO", "Sound Color FX: dub echo", "effect", "note", 0x69),
    ("L14", "cfx-crush", "button", "CRUSH", "Sound Color FX: crush", "effect", "note", 0x6A),
    ("L15", "cfx-sweep", "button", "SWEEP", "Sound Color FX: sweep", "effect", "note", 0x6B),
    ("L16", "cfx-noise", "button", "NOISE", "Sound Color FX: noise", "effect", "note", 0x56),
    ("L17", "cfx-filter", "button", "FILTER", "Sound Color FX: filter", "effect", "note", 0x57),
    ("L18", "cfx-parameter", "knob", "PARAMETER", "Sound Color FX parameter", "effect", "cc", 0x6C),
    ("L19", "phones-a-link-cue", "button", "LINK CUE", "Headphones A: link cue", "mixer", "note", 0x0F),
    ("L20", "phones-a-mono-split", "button", "MONO SPLIT", "Headphones A: mono split or stereo", "mixer", "note", 0x21),
    ("L21", "phones-a-mix", "knob", "MIX", "Headphones A: cue and master blend", "mixer", "cc", 0x1B),
    ("L22", "phones-a-level", "knob", "LEVEL", "Headphones A level", "mixer", "cc", 0x1A),
]

# ----------------------------------------------------------------- channel strips

# [PDF] C1..C9 per channel. One wire per channel in sheet order ch1, ch2, ch3, ch4.
_INPUT_USB_NOTE = (0x4A, 0x4B, 0x4C, 0x4D)
_TRIM_CC = (0x01, 0x06, 0x0C, 0x50)
_HI_CC = (0x02, 0x07, 0x0E, 0x51)
_MID_CC = (0x03, 0x08, 0x0F, 0x5C)
_LOW_CC = (0x04, 0x09, 0x15, 0x52)
_COLOR_CC = (0x05, 0x0A, 0x16, 0x53)
_CUE_A_NOTE = (0x0A, 0x0B, 0x0C, 0x0D)
_CUE_B_NOTE = (0x10, 0x11, 0x12, 0x13)
_FADER_CC = (0x11, 0x12, 0x13, 0x14)
_XF_ASSIGN_CC = (0x41, 0x42, 0x43, 0x44)


def _channel(n: int) -> list[Control]:
    i = n - 1
    rows: list[tuple[str, str, str, str, str, str, int]] = [
        # fig, id tail, type, label, does, kind, number
        (
            "C1",
            "input",
            "knob",
            "INPUT",
            "Input selector; the USB position's note is carried",
            "note",
            _INPUT_USB_NOTE[i],
        ),
        ("C2", "trim", "knob", "TRIM", "Channel gain", "cc", _TRIM_CC[i]),
        ("C3", "hi", "knob", "HI", "EQ or isolator high band", "cc", _HI_CC[i]),
        ("C4", "mid", "knob", "MID", "EQ or isolator mid band", "cc", _MID_CC[i]),
        ("C5", "low", "knob", "LOW", "EQ or isolator low band", "cc", _LOW_CC[i]),
        ("C6", "color", "knob", "COLOR", "Sound Color FX amount for this channel", "cc", _COLOR_CC[i]),
        ("C7", "cue-a", "button", "CUE A", "Headphones A cue for this channel", "note", _CUE_A_NOTE[i]),
        ("C7", "cue-b", "button", "CUE B", "Headphones B cue for this channel", "note", _CUE_B_NOTE[i]),
        ("C8", "fader", "fader", "CH FADER", "Channel volume", "cc", _FADER_CC[i]),
        ("C9", "xf-assign", "button", "A THRU B", "Crossfader assign switch", "cc", _XF_ASSIGN_CC[i]),
    ]
    return [
        _control(f"ch{n}-{tail}", f"{fig}-{n}", typ, label, f"{does} (channel {n})", "mixer", kind, num)
        for fig, tail, typ, label, does, kind, num in rows
    ]


# ----------------------------------------------------------------- right column

_RIGHT_ROWS: list[Row] = [
    ("C10", "crossfader", "crossfader", "CROSSFADER", "Blend the A and B assigned channels", "mixer", "cc", 0x0B),
    ("R1", "master-level", "knob", "MASTER LEVEL", "Master output level", "mixer", "cc", 0x18),
    ("R2", "booth-eq-hi", "knob", "BOOTH EQ HI", "Booth EQ high band", "mixer", "cc", 0x6D),
    ("R3", "booth-eq-low", "knob", "BOOTH EQ LOW", "Booth EQ low band", "mixer", "cc", 0x6E),
    ("R4", "booth-level", "knob", "BOOTH LEVEL", "Booth output level", "mixer", "cc", 0x19),
    ("R5", "master-cue-a", "button", "CUE A", "Hear the master in headphones A", "mixer", "note", 0x0E),
    ("R6", "master-cue-b", "button", "CUE B", "Hear the master in headphones B", "mixer", "note", 0x14),
    ("R7", "phones-b-link-cue", "button", "LINK CUE", "Headphones B: link cue", "mixer", "note", 0x15),
    ("R8", "phones-b-mono-split", "button", "MONO SPLIT", "Headphones B: mono split or stereo", "mixer", "cc", 0x22),
    ("R9", "phones-b-mix", "knob", "MIX", "Headphones B: cue and master blend", "mixer", "cc", 0x55),
    ("R10", "phones-b-level", "knob", "LEVEL", "Headphones B level", "mixer", "cc", 0x56),
    ("R11", "eq-curve", "button", "EQ CURVE", "Isolator or EQ curve switch", "mixer", "cc", 0x21),
    ("R12", "ch-fader-curve", "button", "CH FADER CURVE", "Channel fader curve switch", "mixer", "cc", 0x5E),
    ("R13", "crossfader-curve", "button", "CROSSFADER CURVE", "Crossfader curve switch", "mixer", "cc", 0x5F),
    (
        "R14",
        "send-ch",
        "knob",
        "SEND CH",
        "Multi I/O send source selector; position 1's note is carried",
        "effect",
        "note",
        0x22,
    ),
    (
        "R15",
        "insert-source",
        "button",
        "INSERT SOURCE",
        "Multi I/O insert source: RETURN or USB",
        "effect",
        "note",
        0x6F,
    ),
    ("R16", "send-level", "knob", "SEND LEVEL", "Multi I/O send level", "effect", "cc", 0x71),
    ("R17", "insert", "button", "INSERT", "Multi I/O insert on or off", "effect", "cc", 0x40),
    (
        "R18",
        "x-pad",
        "fader",
        "X-PAD",
        "Beat FX touch strip: slide to set the beat; the slide CC is carried",
        "effect",
        "cc",
        0x74,
    ),
    ("R19", "beat-left", "button", "BEAT <", "Shorter beat division", "effect", "cc", 0x4C),
    ("R20", "beat-right", "button", "BEAT >", "Longer beat division", "effect", "cc", 0x4D),
    ("R21", "auto-tap", "button", "AUTO/TAP", "BPM source: auto or tap", "effect", "cc", 0x45),
    ("R22", "tap", "button", "TAP", "Tap the tempo", "effect", "cc", 0x4E),
    ("R23", "quantize", "button", "QUANTIZE", "Quantize the beat effect", "effect", "note", 0x76),
    ("R24", "fx-freq-low", "button", "LOW", "Beat FX frequency band: low", "effect", "cc", 0x68),
    ("R24", "fx-freq-mid", "button", "MID", "Beat FX frequency band: mid", "effect", "cc", 0x67),
    ("R24", "fx-freq-hi", "button", "HI", "Beat FX frequency band: high", "effect", "cc", 0x66),
    (
        "R25",
        "fx-select",
        "knob",
        "EFFECT SELECT",
        "Beat FX selector; the DELAY position's CC is carried",
        "effect",
        "cc",
        0x2A,
    ),
    ("R26", "ch-select-1", "button", "1", "Beat FX on channel 1", "effect", "note", 0x01),
    ("R26", "ch-select-2", "button", "2", "Beat FX on channel 2", "effect", "note", 0x02),
    ("R26", "ch-select-3", "button", "3", "Beat FX on channel 3", "effect", "note", 0x03),
    ("R26", "ch-select-4", "button", "4", "Beat FX on channel 4", "effect", "note", 0x04),
    ("R26", "ch-select-mic", "button", "MIC", "Beat FX on the microphone", "effect", "note", 0x05),
    ("R26", "ch-select-cf-a", "button", "CF.A", "Beat FX on crossfader side A", "effect", "note", 0x06),
    ("R26", "ch-select-cf-b", "button", "CF.B", "Beat FX on crossfader side B", "effect", "note", 0x07),
    ("R26", "ch-select-mst", "button", "MST", "Beat FX on the master", "effect", "note", 0x08),
    ("R27", "fx-time", "knob", "TIME", "Beat FX time; the MSB CC is carried", "effect", "cc", 0x0D),
    ("R28", "fx-level-depth", "knob", "LEVEL/DEPTH", "Beat FX amount", "effect", "cc", 0x5B),
    ("R29", "fx-on-off", "button", "ON/OFF", "Beat FX on or off", "effect", "cc", 0x72),
]


def _rows(rows: list[Row]) -> list[Control]:
    return [
        _control(cid, fig, typ, label, does, section, kind, num)
        for fig, cid, typ, label, does, section, kind, num in rows
    ]


# ----------------------------------------------------------------- normalisation for the renderer


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def _aligned_groups(values: dict[str, float], tol_mm: float = 1.5) -> dict[str, float]:
    """Single-linkage clusters of centres within tol_mm; each member takes the cluster mean."""
    order = sorted(values, key=values.__getitem__)
    out: dict[str, float] = {}
    group: list[str] = []
    for cid in order:
        if group and values[cid] - values[group[-1]] > tol_mm:
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
    controls = [*_rows(_LEFT_ROWS), *[c for n in CHANNELS for c in _channel(n)], *_rows(_RIGHT_ROWS)]
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
            "source": "DJM-A9 MIDI Message List (E, 1.0), page 1, embedded image 1 (top-panel figure, 1383 x 1344 px)",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/DJM-A9-MIDI-Message-List.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": PLATE_ROW_SCALE_MM_PER_PX,
            "calibration": (
                "x from the plate width (407.4 mm over 1332 px). y set equal to x because the figure is "
                "isotropic (round ON/OFF button 59 x 58 px, LEVEL/DEPTH cap 50 x 50 px, TIME ring 52 x 52 px); "
                "the plate-derived row scale 0.35146 disagrees by 14.9% because the figure shows 1304 rows "
                "(398.8 mm) of the 458.3 mm depth, the sloped rear connector panel lying behind the top plate."
            ),
            "method": (
                "Trace on 2x tiles with a 50 px grid and 3x or 4x crops with a 10 px grid; outlined buttons "
                "(CUE, Sound Color FX, FX FREQUENCY, CH SELECT, MIC FX) confirmed by a colour-threshold bounding "
                "box. Button: body bounding box (round buttons: the round body). Knob and rotary selector: visible "
                "cap. CH FADER: travel slot (scale 10 to 0) with cap width. CROSSFADER: travel slot with cap height. "
                "X-PAD: touch strip. Slide switches: switch body."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [{"id": c.id, "px": list(_PX[c.id]), "x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in _controls()],
    }


def build() -> dict[str, object]:
    controls = normalise(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel club mixer",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "DJM_A9_MAP",
        "sources": {
            "panel_size": "DJM-A9 product page, Specifications: Dimensions (W x D x H) 407.4 x 458.3 x 107.9 mm",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/djm-a9.ts (DJM-A9 MIDI Message List E 1.0)",
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                "reference: devices/djm-a9/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "the mixer has no SHIFT layer: every shift_midi is null",
            "rotary selectors (INPUT, SEND CH, EFFECT SELECT) carry one position's wire; the other positions are map hints",
            "X-PAD is a horizontal touch strip drawn as a fader from its rectangle; its touch CC is a map hint",
            "TIME carries its MSB CC; the LSB CC is a map hint",
            "controls the MIDI list does not document: MIC LEVEL 1 and 2, +48V, MIDI ON/OFF and START/STOP, CENTER LOCK, level meters, display",
            "the mixer accepts no MIDI input, so there are no LED rules",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    n = len(doc["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({n} controls, repeat spread {REPEAT_SPREAD_MM} mm)")
