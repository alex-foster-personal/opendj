"""Build the Traktor Kontrol S4 MK3 practice-replica panel pair (ADR-0137, epic #9366).

Writes two files under tools/deck-diagrams/devices/traktor-s4-mk3/:
- panel.reference.json: the measurement of record. One pixel box per control, read off the
  top-panel rendering embedded in the official "TRAKTOR KONTROL S4 MK3 Manual English" PDF
  (section 5 "TRAKTOR KONTROL S4 Overview", page 17, embedded image 2235 x 1502 px), converted to
  millimetres with the calibration below.
- panel.json: the layout the practice route renders, derived FROM the reference (never the other
  way round): centres snapped to 0.5 mm, sizes to whole mm, row and column mates aligned.

Sources (all public and official; the PDF and its images never enter the repository):
- Panel size 542 x 339 mm: TRAKTOR KONTROL S4 MK3 Manual (3.0 edition, 85 pages), section 11
  "Specification", Dimensions: Width 542 mm, Height 65 mm, Depth 339 mm.
- Figure: TRAKTOR KONTROL S4 MK3 Manual (3.1 edition, 87 pages, 0219), section 5 overview image.
  The callout numbers of sections 5.1 (Decks, D1..D19), 5.1.1 (Browse controls, B1..B5), 5.1.2
  (Pad section, P1..P6), 5.2 (Mixer, M1..M9), 5.2.1 (Mixer channel, C1..C13) and 5.3 (FX Unit,
  F1..F8) are kept as `fig`.
- Wire identities: NONE exist. Native Instruments publishes no MIDI message list for this unit and
  the manual has no MIDI mode chapter: the S4 MK3 drives Traktor over HID. Every control's `midi`
  and `shift_midi` is null (wire explicitly unavailable, no number is invented), so the replica
  renders every control unwired and inert with the PARITY-TODO title.
- Positions: `_PX` below, the first trace, read from the rendering by shape detection (Canny edge
  boxes for buttons, knob rings and encoders; bright-face boxes for pads and the deck-select keys;
  intensity profiles for the jog rim and the fader travels) and checked on gridded 2x crops. A
  second independent read of ten controls with different detectors (Hough circle centres,
  bright-face boxes) gave `REPEAT_SPREAD_MM`.

Calibration: the plate spans image columns 4..2229 (2226 px = 542 mm, 0.24349 mm/px). The rendering
is isotropic (knob rings 68 x 68 px, CUE and PLAY 112 x 112 px, deck-select keys 48 x 48 px, jog
platter 484 x 484 px), so y uses the x scale; the plate's 1358 visible rows cover 330.7 of the 339 mm
depth, the sloped front face with the crossfader assign switches lying outside the top view.

Run: uv run --locked python tools/deck-diagrams/scripts/build_traktor_s4_mk3_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 542.0
PANEL_D_MM = 339.0
DEVICE = "traktor-s4-mk3"
DEVICE_MAP = "TRAKTOR_S4_MK3_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "a4efebd14a5b8c9f2446799c2d9d1509312bbb52396d1fd888102656549ccaa7"
SOURCE_IMAGE_SHA256 = "226b516e1c0a40f5a0bc4fdc58cba8caefd6fd20430800b7c33e26085c4fa903"
SPEC_PDF_SHA256 = "71634a1d733d5f442ac96e62372c1f5e41ee85372630c61c0592aa3b0aaa25a3"
IMAGE_PX = (2235, 1502)
PLATE_PX = {"x0": 4, "x1": 2229, "y0": 139, "y1": 1496}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
PLATE_ROW_SCALE_MM_PER_PX = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
REPEAT_SPREAD_MM = 0.7
TRACE_DATE = "Sun 8 Nov 2026"
TRACE_ISSUE = "#9366"

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
    midi: Wire | None
    shift_midi: Wire | None = None


# Traced pixel boxes [x0, y0, x1, y1], inclusive, on the 2235 x 1502 rendering. Button: outer edge
# of the drawn body. Knob and encoder: outer edge of the ring. Round button (FLX, REV, GRID, TT, JOG):
# outer ring, 62 px. Pad and deck-select key: light face. Jog: outer rim (radius 317 px around the
# platter centre). TEMPO and CH FADER: travel slot height with cap width. CROSSFADER: travel slot
# width with cap height.
_PX: dict[str, tuple[int, int, int, int]] = {
    # left deck
    "d1-jog": (57, 349, 691, 983),
    "d1-master": (622, 909, 708, 945),
    "d1-sync": (622, 960, 708, 997),
    "d1-tempo": (631, 1061, 699, 1420),
    "d1-play-pause": (46, 1344, 157, 1455),
    "d1-cue": (46, 1212, 157, 1323),
    "d1-shift": (49, 1108, 154, 1155),
    "d1-deck-a": (49, 1038, 96, 1085),
    "d1-deck-c": (107, 1038, 154, 1085),
    "d1-mode-jog": (121, 920, 182, 981),
    "d1-mode-tt": (64, 865, 125, 926),
    "d1-mode-grid": (21, 799, 82, 860),
    "d1-move": (200, 1052, 288, 1140),
    "d1-loop": (493, 1053, 580, 1140),
    "d1-rev": (43, 433, 104, 494),
    "d1-flx": (95, 372, 156, 433),
    "d1-browse": (621, 187, 709, 274),
    "d1-view": (621, 295, 709, 333),
    "d1-prep": (561, 295, 600, 333),
    "d1-star": (561, 241, 599, 277),
    "d1-preview": (561, 183, 600, 221),
    "d1-mode-hotcues": (198, 1215, 286, 1254),
    "d1-mode-record": (297, 1215, 335, 1254),
    "d1-mode-samples": (346, 1215, 434, 1254),
    "d1-mode-mute": (445, 1215, 484, 1254),
    "d1-mode-stems": (495, 1215, 583, 1254),
    "d1-pad-1": (202, 1285, 281, 1357),
    "d1-pad-2": (301, 1285, 380, 1357),
    "d1-pad-3": (399, 1285, 479, 1357),
    "d1-pad-4": (499, 1285, 578, 1357),
    "d1-pad-5": (202, 1376, 281, 1447),
    "d1-pad-6": (301, 1376, 380, 1447),
    "d1-pad-7": (399, 1376, 479, 1447),
    "d1-pad-8": (499, 1376, 578, 1447),
    "fx1-knob-1": (64, 206, 131, 273),
    "fx1-knob-2": (187, 206, 255, 273),
    "fx1-knob-3": (312, 206, 378, 273),
    "fx1-knob-4": (435, 206, 502, 273),
    "fx1-on-1": (50, 296, 144, 332),
    "fx1-on-2": (174, 296, 268, 332),
    "fx1-on-3": (298, 296, 392, 332),
    "fx1-on-4": (422, 296, 516, 332),
    # right deck
    "d2-jog": (1542, 349, 2176, 983),
    "d2-master": (2108, 909, 2193, 945),
    "d2-sync": (2108, 960, 2193, 997),
    "d2-tempo": (2117, 1061, 2185, 1420),
    "d2-play-pause": (1531, 1344, 1643, 1455),
    "d2-cue": (1531, 1212, 1643, 1323),
    "d2-shift": (1535, 1108, 1640, 1155),
    "d2-deck-b": (1535, 1038, 1582, 1085),
    "d2-deck-d": (1593, 1038, 1640, 1085),
    "d2-mode-jog": (1605, 920, 1666, 981),
    "d2-mode-tt": (1550, 865, 1611, 926),
    "d2-mode-grid": (1507, 799, 1568, 860),
    "d2-move": (1686, 1053, 1773, 1140),
    "d2-loop": (1979, 1053, 2066, 1140),
    "d2-rev": (1529, 433, 1590, 494),
    "d2-flx": (1581, 373, 1642, 434),
    "d2-browse": (2107, 187, 2195, 274),
    "d2-view": (2106, 295, 2194, 333),
    "d2-prep": (2047, 295, 2085, 333),
    "d2-star": (2047, 239, 2085, 277),
    "d2-preview": (2047, 185, 2085, 221),
    "d2-mode-hotcues": (1685, 1215, 1772, 1254),
    "d2-mode-record": (1783, 1215, 1821, 1254),
    "d2-mode-samples": (1832, 1215, 1920, 1254),
    "d2-mode-mute": (1931, 1215, 1969, 1254),
    "d2-mode-stems": (1981, 1215, 2069, 1254),
    "d2-pad-1": (1687, 1285, 1767, 1357),
    "d2-pad-2": (1787, 1285, 1866, 1357),
    "d2-pad-3": (1886, 1285, 1965, 1357),
    "d2-pad-4": (1984, 1285, 2064, 1357),
    "d2-pad-5": (1687, 1376, 1767, 1447),
    "d2-pad-6": (1787, 1376, 1866, 1447),
    "d2-pad-7": (1886, 1376, 1965, 1447),
    "d2-pad-8": (1984, 1376, 2064, 1447),
    "fx2-knob-1": (1549, 206, 1616, 273),
    "fx2-knob-2": (1673, 206, 1741, 273),
    "fx2-knob-3": (1798, 206, 1864, 273),
    "fx2-knob-4": (1921, 206, 1988, 273),
    "fx2-on-1": (1536, 296, 1630, 332),
    "fx2-on-2": (1660, 296, 1754, 332),
    "fx2-on-3": (1784, 296, 1877, 332),
    "fx2-on-4": (1908, 296, 2001, 332),
    # mixer channels, left to right: C, A, B, D
    "ch-c-gain": (830, 179, 893, 242),
    "ch-c-ext": (780, 207, 809, 237),
    "ch-c-fx-left": (777, 279, 813, 315),
    "ch-c-fx-right": (843, 279, 880, 315),
    "ch-c-hi": (795, 362, 862, 429),
    "ch-c-mid": (795, 486, 862, 553),
    "ch-c-low": (795, 610, 862, 677),
    "ch-c-mixer-fx": (785, 761, 872, 848),
    "ch-c-mixer-fx-on": (777, 881, 880, 917),
    "ch-c-cue": (776, 959, 881, 998),
    "ch-c-fader": (787, 1058, 868, 1276),
    "ch-a-gain": (970, 179, 1033, 242),
    "ch-a-ext": (920, 207, 951, 237),
    "ch-a-fx-left": (918, 279, 954, 315),
    "ch-a-fx-right": (983, 279, 1020, 315),
    "ch-a-hi": (935, 362, 1003, 429),
    "ch-a-mid": (935, 486, 1003, 553),
    "ch-a-low": (935, 610, 1003, 677),
    "ch-a-mixer-fx": (925, 761, 1012, 848),
    "ch-a-mixer-fx-on": (918, 881, 1020, 917),
    "ch-a-cue": (916, 959, 1021, 996),
    "ch-a-fader": (928, 1058, 1009, 1276),
    "ch-b-gain": (1111, 179, 1174, 242),
    "ch-b-ext": (1063, 207, 1091, 237),
    "ch-b-fx-left": (1058, 279, 1094, 315),
    "ch-b-fx-right": (1124, 279, 1160, 315),
    "ch-b-hi": (1076, 362, 1143, 429),
    "ch-b-mid": (1076, 486, 1143, 553),
    "ch-b-low": (1076, 610, 1143, 677),
    "ch-b-mixer-fx": (1065, 761, 1153, 848),
    "ch-b-mixer-fx-on": (1058, 881, 1160, 917),
    "ch-b-cue": (1056, 959, 1161, 998),
    "ch-b-fader": (1068, 1058, 1149, 1276),
    "ch-d-gain": (1251, 179, 1314, 242),
    "ch-d-ext": (1201, 210, 1228, 234),
    "ch-d-fx-left": (1198, 279, 1235, 315),
    "ch-d-fx-right": (1264, 279, 1300, 315),
    "ch-d-hi": (1216, 362, 1283, 429),
    "ch-d-mid": (1216, 486, 1283, 553),
    "ch-d-low": (1216, 610, 1283, 677),
    "ch-d-mixer-fx": (1206, 761, 1293, 848),
    "ch-d-mixer-fx-on": (1198, 881, 1300, 917),
    "ch-d-cue": (1197, 959, 1301, 996),
    "ch-d-fader": (1209, 1058, 1289, 1276),
    # mixer globals
    "master-level": (1375, 177, 1442, 244),
    "quant": (1360, 278, 1455, 316),
    "booth-level": (1375, 610, 1442, 677),
    "mixer-fx-select-1": (1362, 757, 1398, 794),
    "mixer-fx-select-2": (1419, 757, 1456, 794),
    "mixer-fx-select-3": (1362, 815, 1398, 852),
    "mixer-fx-select-4": (1419, 815, 1456, 852),
    "mixer-fx-select-filter": (1362, 881, 1456, 917),
    "phones-mix": (1375, 994, 1442, 1061),
    "phones-vol": (1375, 1132, 1442, 1199),
    "crossfader": (940, 1364, 1137, 1446),
}

# Second independent read (Hough circle centres with their radius, bright-face boxes), used only
# for REPEAT_SPREAD_MM.
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "ch-c-gain": (830, 180, 890, 240),
    "d1-pad-1": (198, 1284, 286, 1361),
    "d1-deck-a": (53, 1041, 91, 1080),
    "master-level": (1375, 177, 1441, 243),
    "d1-flx": (103, 381, 145, 423),
    "d2-browse": (2106, 188, 2190, 272),
    "d1-cue": (47, 1213, 155, 1321),
    "d1-move": (202, 1056, 286, 1140),
    "ch-b-hi": (1078, 366, 1138, 426),
    "d2-jog": (1617, 423, 2101, 908),
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
    """Largest centre distance between the two reads, in mm."""
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


# ----------------------------------------------------------------- inventory

# fig, id tail, type, section, label, does. The wire is assigned afterwards from panel order.
Row = tuple[str, str, str, str, str, str]

_DECK_LETTERS = {1: ("A", "C"), 2: ("B", "D")}


def _deck_rows(deck: int) -> list[Row]:
    main, alt = _DECK_LETTERS[deck]
    return [
        ("D2", "jog", "jog", "deck", "JOG WHEEL", "Nudge, scratch or seek by turning the platter or its rim"),
        ("D3", "master", "button", "deck", "MASTER", "Make this deck the tempo master; with SHIFT, reset the tempo"),
        ("D4", "sync", "button", "deck", "SYNC", "Sync this deck to the tempo master; with SHIFT, lock the tempo"),
        ("D5", "tempo", "fader", "deck", "TEMPO", "Tempo slider, vertical: minus at the top, plus at the bottom"),
        ("D7", "play-pause", "button", "deck", "PLAY/PAUSE", "Start or pause the deck"),
        ("D8", "cue", "button", "deck", "CUE", "Set or return to the cue point; play from it while held"),
        ("D9", "shift", "button", "deck", "SHIFT", "Hold for each control's second function"),
        ("D10", f"deck-{main.lower()}", "button", "deck", f"DECK {main}", f"Focus this side on deck {main}"),
        ("D10", f"deck-{alt.lower()}", "button", "deck", f"DECK {alt}", f"Focus this side on deck {alt}"),
        ("D11", "mode-jog", "button", "deck", "JOG", "Jog wheel mode: pitch bend on the rim, scratch on the plate"),
        ("D12", "mode-tt", "button", "deck", "TT", "Turntable mode: the platter spins with playback"),
        ("D13", "mode-grid", "button", "deck", "GRID", "Beatgrid mode: correct the grid with the platter"),
        ("D14", "move", "encoder", "deck", "MOVE", "Jump through the track, or move an active loop"),
        ("D16", "loop", "encoder", "deck", "LOOP", "Turn to pick the loop size, press to loop on or off"),
        ("D17", "rev", "button", "deck", "REV", "Play backwards in flux mode while held"),
        ("D18", "flx", "button", "deck", "FLX", "Flux mode on or off"),
        ("B1", "browse", "encoder", "browse", "BROWSE", "Scroll the track list; press to load the selected track"),
        ("B2", "view", "button", "browse", "VIEW", "Grow or shrink the browser view"),
        ("B3", "prep", "button", "browse", "PREP LIST", "Add the selected track to the preparation list"),
        ("B4", "star", "button", "browse", "STAR", "Hold to browse favourites with the encoder"),
        ("B5", "preview", "button", "browse", "PREVIEW", "Load the selected track into the preview player"),
        ("P1", "mode-hotcues", "button", "performance", "HOTCUES", "Pad mode: hot cues and stored loops"),
        ("P2", "mode-record", "button", "performance", "REC", "Pattern recorder on a remix deck"),
        ("P3", "mode-samples", "button", "performance", "SAMPLES", "Pad mode: sample slots of a remix deck"),
        ("P4", "mode-mute", "button", "performance", "MUTE", "Mute samples or stem parts"),
        ("P5", "mode-stems", "button", "performance", "STEMS", "Pad mode: stem part levels and mutes"),
        *[("P6", f"pad-{n}", "pad", "performance", f"PAD {n}", f"Hot cue {n} in HOTCUES mode") for n in range(1, 9)],
        *[(f"F{n}", f"fx-knob-{n}", "knob", "effect", f"FX {n}", f"FX unit {deck} parameter {n}") for n in range(1, 5)],
        *[
            (f"F{4 + n}", f"fx-on-{n}", "button", "effect", f"ON {n}", f"FX unit {deck} effect {n} on or off")
            for n in range(1, 5)
        ],
    ]


def _deck(deck: int) -> list[Control]:
    out: list[Control] = []
    for fig, tail, typ, section, label, does in _deck_rows(deck):
        cid = f"fx{deck}-{tail[3:]}" if tail.startswith("fx-") else f"d{deck}-{tail}"
        x, y, w, h = _at(cid)
        suffix = "" if tail.startswith("fx-") else f" (deck {deck})"
        out.append(Control(cid, fig, typ, label, f"{does}{suffix}", x, y, w, h, section, None))
    return out


def _channel(letter: str) -> list[Control]:
    rows: list[tuple[str, str, str, str, str]] = [
        ("C1", "gain", "knob", "GAIN", "Channel input gain"),
        ("C12", "ext", "button", "EXT", "Take the channel's audio from its external input"),
        ("C2", "fx-left", "button", "FX <", "Assign FX unit 1 to this channel"),
        ("C2", "fx-right", "button", "FX >", "Assign FX unit 2 to this channel"),
        ("C3", "hi", "knob", "HI", "EQ high band"),
        ("C4", "mid", "knob", "MID", "EQ mid band"),
        ("C5", "low", "knob", "LOW", "EQ low band"),
        ("C6", "mixer-fx", "knob", "MIXER FX", "Mixer FX amount: off at the centre"),
        ("C7", "mixer-fx-on", "button", "ON", "Mixer FX on or off for this channel"),
        ("C8", "cue", "button", "CUE", "Headphone cue for this channel"),
        ("C9", "fader", "fader", "CH FADER", "Channel volume"),
    ]
    out: list[Control] = []
    for fig, tail, typ, label, does in rows:
        cid = f"ch-{letter}-{tail}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (channel {letter.upper()})",
                x,
                y,
                w,
                h,
                "mixer",
                None,
            )
        )
    return out


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str]] = [
        ("M2", "master-level", "knob", "MASTER", "Master output level"),
        ("M3", "quant", "button", "QUANT", "Quantize and snap on or off"),
        ("M5", "booth-level", "knob", "BOOTH", "Booth output level"),
        ("M6", "mixer-fx-select-1", "button", "FX SELECT 1", "Pick mixer FX 1 for the channel FX knobs"),
        ("M6", "mixer-fx-select-2", "button", "FX SELECT 2", "Pick mixer FX 2 for the channel FX knobs"),
        ("M6", "mixer-fx-select-3", "button", "FX SELECT 3", "Pick mixer FX 3 for the channel FX knobs"),
        ("M6", "mixer-fx-select-4", "button", "FX SELECT 4", "Pick mixer FX 4 for the channel FX knobs"),
        ("M6", "mixer-fx-select-filter", "button", "FILTER", "Pick the filter for the channel FX knobs"),
        ("M7", "phones-mix", "knob", "MIX", "Headphone blend between cue and master"),
        ("M8", "phones-vol", "knob", "VOL", "Headphone level"),
        ("M9", "crossfader", "crossfader", "CROSSFADER", "Blend the channels assigned to each side"),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, "mixer", None))
    return out


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
    controls = [*_deck(1), *_deck(2), *[c for letter in "cabd" for c in _channel(letter)], *_globals()]
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
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but the two reads give {spread}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "TRAKTOR KONTROL S4 MK3 Manual English (0219 edition), section 5 overview, page 17, "
                "embedded top-panel rendering (2235 x 1502 px)"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "panel_size_pdf_sha256": SPEC_PDF_SHA256,
            "provenance": "docs/controller/research/TRAKTOR-KONTROL-S4-MK3-Sources.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": PLATE_ROW_SCALE_MM_PER_PX,
            "calibration": (
                "x from the plate width (542 mm over 2226 px). y set equal to x because the rendering is "
                "isotropic (knob rings 68 x 68 px, CUE and PLAY 112 x 112 px, deck-select keys 48 x 48 px, "
                "jog platter 484 x 484 px); the plate-derived row scale 0.24963 disagrees by 2.5% because the "
                "image shows 1358 rows (330.7 mm) of the 339 mm depth, the sloped front face lying outside "
                "the top view."
            ),
            "method": (
                "Shape detection on the rendering, checked on gridded 2x crops. Button: outer edge of the drawn "
                "body (Canny edge box). Knob and encoder: outer edge of the ring. Round button: outer ring, 62 px. "
                "Pad and deck-select key: light face. Jog: outer rim at radius 317 px from the platter centre "
                "(intensity profile). TEMPO and CH FADER: travel slot height with cap width (column profile). "
                "CROSSFADER: travel slot width with cap height (row profile)."
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
        "display_name": "4-channel motorised-jog controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": DEVICE_MAP,
        "sources": {
            "panel_size": (
                "TRAKTOR KONTROL S4 MK3 Manual English (3.0 edition), section 11 Specification: "
                "Width 542 mm, Height 65 mm, Depth 339 mm"
            ),
            "wire": (
                "none published: the unit drives Traktor over HID and has no MIDI message list; every control's "
                "midi is null (wire unavailable), nothing is invented"
            ),
            "positions": (
                "measured trace of the official overview rendering, normalised for the renderer; "
                "reference: devices/traktor-s4-mk3/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "every control is unwired (midi null) and inert: no MIDI list exists, so nothing is bound and nothing lights",
            "shift_midi: the manual documents SHIFT functions but no wires, so every shifted row is null",
            "the deck display (section 5.1 callout 15), level meters and input LEDs are not controls",
            "the jog rim and plate are one control: the renderer has no ring type yet",
            "TEMPO is a vertical fader drawn from its rectangle; the renderer has no orientation hint",
            "front panel (crossfader curve and assign switches, sockets) and rear panel",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    n = len(doc["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({n} controls, repeat spread {REPEAT_SPREAD_MM} mm)")
