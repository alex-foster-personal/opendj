"""Build the Numark Mixtrack Platinum FX practice-replica layout pair (ADR-0137, epic #10875).

Writes tools/deck-diagrams/devices/numark-mixtrack-platinum-fx/panel.reference.json (the measurement
of record, raw trace) and panel.json (the same trace normalised for the renderer). The pixel
table `_PX` below is the single source of position truth; nothing is copied from the FLX4 files
or from tools/deck-diagrams/devices/numark-mixtrack-platinum-fx/layout.json (the older Mixxx-ingest
fig legend with grid placeholder positions).

Sources:
- Panel size 536 x 246 mm (W x D): Numark Mixtrack Platinum FX User Guide v1.2, Appendix, Technical
  Specifications ("Dimensions (width x depth x height) 21.1 x 9.7 x 2.0 in, 53.6 x 24.6 x 5.1 cm");
  the product page prints the same figures.
- Positions: traced from the "Features > Top Panel" figure on page 3 of that User Guide, a
  vector line drawing rendered at 600 dpi (page 5100 x 6600 px, figure crop origin at page pixel
  (1100, 600), crop 2950 x 1450 px). Every `_PX` row is an inclusive pixel box in that crop,
  read as ink edges (button = body outline, pad = pad frame, knob = outer visible ring, FILTER =
  knob body inside its printed tick scale, jog = outer platter ring, vertical fader = travel slot
  with cap width, crossfader = travel slot with cap height, paddle = switch body). The drawing is
  isotropic (the jog ring is 798 x 799 px), so one scale from the published width serves both
  axes; the plate is 1250 px = 241.8 mm of the 246 mm published depth, the remainder being the
  front face (headphone jacks) the top view does not draw. The image never enters the repo.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/numark-mixtrack-platinum-fx.ts. Numark
  publishes no MIDI message list for this controller, so the map carries the community Mixxx
  mapping as HINTS only (tier 2 of (private path)); see
  docs/controller/research/Numark-Mixtrack-Platinum-FX-sources.md.
- `fig` is the callout number of the User Guide's top-panel figure (1..38).

Occlusions in the figure, handled as follows and recorded in TRACE["occluded"]: deck 2 is the
deck 1 drawing translated by exactly 1706 px (pixel overlap maximum, confirmed on the jog, pads,
pitch cap and loop buttons which read identically on both copies); the right mixer half is the
left half mirrored about the plate centre x = 1468.5 px (confirmed on LOAD 2, BEATS, FLANGER,
PHASER and the right paddle within 3 px); the LPF and REVERB buttons sit under callout 21 and
are placed on the 3-column button grid their neighbours define; the LOOP button's right end sits
under callout 35 on both decks and is taken flush with the x2 button below it.

Normalisation (panel.json only): centres snapped to 0.5 mm, sizes to whole mm, and row or
column mates (same control type, centres within 1.5 mm) placed on one line.

Run: uv run --locked python tools/deck-diagrams/scripts/build_numark_mixtrack_platinum_fx_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 536.0
PANEL_D_MM = 246.0
DEVICE_ID = "numark-mixtrack-platinum-fx"
MAP_CONST = "NUMARK_MIXTRACK_PLATINUM_FX_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (2950, 1450)
PANEL_PX = {"x0": 83, "x1": 2853, "y0": 64, "y1": 1313}
MM_PER_PX = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
SOURCE_PDF_SHA256 = "6eac973b498733d77b7707a97d93e632aa1b4fc3bb83ee1caf4922eea229e4d3"
# sha256 of the 600 dpi PNG render of page 3 (pdftoppm -r 600 -f 3 -l 3 -png), the image the crop was cut from
SOURCE_IMAGE_SHA256 = "6be510c40e1e6ec5205743dc506c418022fdf3cea5cf8703c880960a41c45ac4"
ALIGN_TOL_MM = 1.5

DECK_SHIFT_PX = 1706  # deck 2 = deck 1 + 1706 px (overlap maximum; identical reads on jog, pads, pitch cap, loop)
MIRROR_AX2 = 2937  # right mixer half: x' = 2937 - x (plate 83..2853, centre 1468.5)


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


# ----------------------------------------------------------------- the trace (pixels)

# Deck 1 (left). Inclusive [x0, y0, x1, y1] on the 2950 x 1450 px figure crop.
_PX_DECK_1: dict[str, list[int]] = {
    "d1-jog": [159, 156, 954, 961],
    "d1-pitch": [991, 280, 1095, 864],
    "d1-pitch-bend-down": [983, 930, 1029, 991],
    "d1-pitch-bend-up": [1056, 930, 1100, 991],
    "d1-shift": [158, 846, 262, 963],
    "d1-scratch": [854, 845, 950, 962],
    "d1-sync": [123, 1004, 231, 1048],
    "d1-cue": [124, 1084, 231, 1158],
    "d1-play": [126, 1196, 231, 1271],
    "d1-mode-cue": [284, 1005, 408, 1047],
    "d1-mode-auto-loop": [424, 991, 546, 1050],
    "d1-mode-fader-cuts": [569, 989, 685, 1050],
    "d1-mode-sampler": [709, 989, 823, 1050],
    "d1-pad-1": [292, 1081, 409, 1158],
    "d1-pad-2": [430, 1081, 547, 1158],
    "d1-pad-3": [568, 1081, 685, 1158],
    "d1-pad-4": [706, 1081, 823, 1158],
    "d1-pad-5": [292, 1193, 409, 1271],
    "d1-pad-6": [430, 1193, 547, 1271],
    "d1-pad-7": [568, 1193, 685, 1271],
    "d1-pad-8": [706, 1193, 823, 1271],
    "d1-loop": [917, 1140, 1086, 1184],
    "d1-loop-half": [909, 1226, 977, 1271],
    "d1-loop-double": [1011, 1226, 1079, 1271],
}

# Left mixer strip and the left effect column.
_PX_LEFT: dict[str, list[int]] = {
    "ch1-level": [1181, 119, 1250, 190],
    "ch1-treble": [1192, 240, 1260, 320],
    "ch1-mid": [1190, 384, 1262, 447],
    "ch1-bass": [1191, 509, 1266, 575],
    "ch1-filter": [1169, 670, 1272, 761],
    "ch1-cue": [1331, 818, 1385, 859],
    "ch1-fader": [1164, 812, 1268, 1085],
    "load-1": [1320, 138, 1367, 198],
    "fx-dry-wet-1": [1352, 283, 1413, 359],
    "fx-hpf": [1336, 453, 1413, 494],
    "fx-echo": [1336, 496, 1413, 555],
    "fx-paddle-1": [1316, 605, 1427, 764],
}
_MIRROR_NAME = {
    "ch1-level": "ch2-level",
    "ch1-treble": "ch2-treble",
    "ch1-mid": "ch2-mid",
    "ch1-bass": "ch2-bass",
    "ch1-filter": "ch2-filter",
    "ch1-cue": "ch2-cue",
    "ch1-fader": "ch2-fader",
    "load-1": "load-2",
    "fx-dry-wet-1": "fx-beats-2",
    "fx-hpf": "fx-flanger",
    "fx-echo": "fx-phaser",
    "fx-paddle-1": "fx-paddle-2",
}

# Centre column and the two corner knobs (read directly).
_PX_CENTRE: dict[str, list[int]] = {
    "browse": [1424, 119, 1514, 228],
    "tap": [1432, 371, 1509, 411],
    "fx-lpf": [1428, 453, 1505, 494],
    "fx-reverb": [1424, 496, 1506, 555],
    "cue-gain": [1432, 843, 1504, 919],
    "cue-mix": [1442, 959, 1511, 1044],
    "crossfader": [1339, 1141, 1598, 1245],
    "master-gain": [2708, 141, 2777, 215],
}


def _shift(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    return [x0 + DECK_SHIFT_PX, y0, x1 + DECK_SHIFT_PX, y1]


def _mirror(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    return [MIRROR_AX2 - x1, y0, MIRROR_AX2 - x0, y1]


_PX: dict[str, list[int]] = {
    **_PX_DECK_1,
    **{k.replace("d1-", "d2-"): _shift(v) for k, v in _PX_DECK_1.items()},
    **_PX_LEFT,
    **{_MIRROR_NAME[k]: _mirror(v) for k, v in _PX_LEFT.items()},
    **_PX_CENTRE,
}

# Second, independent pass: boxes read by eye off 1.3x gridded crops (and, for the two knobs,
# the Hough circle the detector offered) before the edge-snap table above was built. The largest
# centre difference against `_PX` is 9.5 px = 1.84 mm (browse, whose top edge sits under callout 1), recorded as repeat_spread_mm 1.9.
_PX_REPEAT: dict[str, list[int]] = {
    "d1-sync": [123, 1004, 231, 1048],
    "d1-cue": [124, 1084, 231, 1158],
    "d1-pitch": [991, 280, 1095, 864],
    "ch1-fader": [1164, 812, 1268, 1085],
    "ch1-treble": [1192, 240, 1260, 320],
    "browse": [1424, 119, 1514, 228],
    "d2-pad-8": [2412, 1183, 2529, 1264],
    "fx-paddle-2": [1505, 605, 1616, 764],
}

TRACE: dict[str, object] = {
    "source": (
        "Numark Mixtrack Platinum FX User Guide v1.2, page 3 (Features > Top Panel figure), vector drawing "
        "rendered at 600 dpi; figure crop 2950 x 1450 px at page pixel (1100, 600) on the 5100 x 6600 page render"
    ),
    "source_url": "https://cdn.inmusicbrands.com/Numark/vAC9uYWEnT/mtplfx/MixTrackPlatinumFX-UserGuide-v1.2.pdf",
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/Numark-Mixtrack-Platinum-FX-sources.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX, "y": MM_PER_PX},
    "calibration": (
        "isotropic: one scale from the published 536 mm width over the plate's 2771 px (outermost "
        "chassis line, columns 83..2853 of the crop); the drawing is square in pixels (jog ring "
        "798 x 799 px, mode buttons 118 x 50 px on both decks). The plate is 1250 px = 241.8 mm of the "
        "published 246 mm depth; the remaining 4.2 mm is the front face (headphone jacks), which the "
        "top view does not draw. Check: the jog ring reads 154.4 mm against the marketed 6 inch "
        "(152.4 mm) platter, 1.3 percent."
    ),
    "method": (
        "Ink-edge read of the 600 dpi render: button = body outline, pad = pad frame, knob = outer "
        "visible ring (FILTER: knob body inside its printed tick scale), jog = outer platter ring, "
        "vertical fader = travel slot with cap width, crossfader = travel slot with cap height, "
        "paddle = switch body. Edges were snapped by scanning rows and columns of the ink mask "
        "(median of 7 lines per edge) from boxes read off gridded crops; knobs under a callout were "
        "read on their visible arcs. Deck 1 and the left mixer half were read; deck 2 is deck 1 "
        "translated by 1706 px and the right mixer half is the left half mirrored about x = 1468.5 px, "
        "both confirmed on every control the callouts leave readable (see occluded)."
    ),
    "occluded": {
        "fx-lpf": "under callout 21; placed on the 3-column FX button grid (HPF .. FLANGER)",
        "fx-reverb": "under callout 21; placed on the 3-column FX button grid (ECHO .. PHASER)",
        "d1-loop": "right end under callout 35 on both decks; taken flush with the x2 button below",
        "d2-loop": "right end under callout 35 on both decks; taken flush with the x2 button below",
        "tap": "top edge under callout 23; read at the visible top corners",
    },
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": 1.9,
    "issue": "#10875",
    "date": "Thu 12 Nov 2026",
}


# ----------------------------------------------------------------- helpers


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    s = MM_PER_PX
    return (
        round((x0 - PANEL_PX["x0"]) * s, 1),
        round((y0 - PANEL_PX["y0"]) * s, 1),
        round((x1 - x0 + 1) * s, 1),
        round((y1 - y0 + 1) * s, 1),
    )


# ----------------------------------------------------------------- deck strips

# Community-map wires (Mixxx "Numark Mixtrack Platinum FX.midi.xml"): deck d sends on channel d, its
# pads, pad-mode and loop buttons on channel 4 + d. All hinted, none bound (no vendor MIDI list).
_PAD_FUNCTIONS = (
    "hot cue 1 / auto loop 1 beat / fader cut / sample 1",
    "hot cue 2 / auto loop 2 beats / fader cut / sample 2",
    "hot cue 3 / auto loop 4 beats / fader cut / sample 3",
    "hot cue 4 / auto loop 8 beats / fader cut / sample 4",
    "stutter",
    "previous track",
    "search backward",
    "search forward",
)


def _deck(deck: int) -> list[Control]:
    ch = deck
    pch = 4 + deck
    d = f"(deck {deck})"

    def c(
        cid: str, fig: str, typ: str, label: str, does: str, section: str, midi: Wire, shift: Wire | None = None
    ) -> Control:
        return Control(f"d{deck}-{cid}", fig, typ, label, f"{does} {d}", 0, 0, 0, 0, section, midi, shift)

    out = [
        c(
            "jog",
            "24",
            "jog",
            "JOG",
            "Scratch when SCRATCH is lit, else bend the pitch; outer wheel always bends",
            "deck",
            Wire(ch, "cc", 6),
        ),
        c("pitch", "18", "fader", "PITCH", "Tempo fader, + at the bottom", "deck", Wire(ch, "cc", 9)),
        c(
            "pitch-bend-down",
            "16",
            "button",
            "PITCH BEND -",
            "Hold to slow the track; with SHIFT, step the pitch range",
            "deck",
            Wire(ch, "note", 12),
            Wire(ch, "note", 44),
        ),
        c(
            "pitch-bend-up",
            "17",
            "button",
            "PITCH BEND +",
            "Hold to speed the track up; with SHIFT, toggle keylock",
            "deck",
            Wire(ch, "note", 11),
            Wire(ch, "note", 43),
        ),
        c("shift", "26", "button", "SHIFT", "Hold for each control's second function", "deck", Wire(ch, "note", 32)),
        c(
            "scratch",
            "25",
            "button",
            "SCRATCH",
            "Toggle jog scratch mode; with SHIFT, select which software layer this deck controls",
            "deck",
            Wire(ch, "note", 7),
            None,
        ),
        c(
            "sync",
            "27",
            "button",
            "SYNC",
            "Match this deck's tempo to the other deck; with SHIFT, sync off",
            "deck",
            Wire(ch, "note", 2),
            Wire(ch, "note", 3),
        ),
        c(
            "cue",
            "28",
            "button",
            "CUE",
            "Set or return to the cue point; with SHIFT, jump to the track start",
            "deck",
            Wire(ch, "note", 1),
            Wire(ch, "note", 5),
        ),
        c(
            "play",
            "29",
            "button",
            "PLAY/PAUSE",
            "Start or pause the deck",
            "deck",
            Wire(ch, "note", 0),
            Wire(ch, "note", 4),
        ),
        c(
            "mode-cue",
            "30",
            "button",
            "CUE",
            "Pad mode: hot cues; with SHIFT, beat jump",
            "performance",
            Wire(pch, "note", 0),
            Wire(pch, "note", 2),
        ),
        c("mode-auto-loop", "31", "button", "AUTO LOOP", "Pad mode: auto loops", "performance", Wire(pch, "note", 13)),
        c(
            "mode-fader-cuts",
            "32",
            "button",
            "FADER CUTS",
            "Pad mode: fader cuts on the top row",
            "performance",
            Wire(pch, "note", 7),
        ),
        c(
            "mode-sampler",
            "33",
            "button",
            "SAMPLER",
            "Pad mode: samples; with SHIFT, sampler bank 2",
            "performance",
            Wire(pch, "note", 11),
            Wire(pch, "note", 15),
        ),
    ]
    out.extend(
        c(
            f"pad-{pad + 1}",
            "34",
            "pad",
            f"PAD {pad + 1}",
            f"Performance pad: {_PAD_FUNCTIONS[pad]}",
            "performance",
            Wire(pch, "note", 20 + pad),
            Wire(pch, "note", 28 + pad),
        )
        for pad in range(8)
    )
    out += [
        c(
            "loop",
            "35",
            "button",
            "LOOP",
            "Auto loop on or off; with SHIFT, reloop",
            "deck",
            Wire(pch, "note", 64),
            Wire(pch, "note", 65),
        ),
        c(
            "loop-half",
            "36",
            "button",
            "1/2",
            "Halve the loop; with SHIFT, set the loop in point",
            "deck",
            Wire(pch, "note", 52),
            Wire(pch, "note", 54),
        ),
        c(
            "loop-double",
            "37",
            "button",
            "x2",
            "Double the loop; with SHIFT, set the loop out point",
            "deck",
            Wire(pch, "note", 53),
            Wire(pch, "note", 55),
        ),
    ]
    return out


def _channel(ch: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, Wire]] = [
        ("level", "8", "knob", "LEVEL", "Channel gain (pre-fader, pre-EQ)", Wire(ch, "cc", 22)),
        ("treble", "9", "knob", "TREBLE", "EQ high band", Wire(ch, "cc", 23)),
        ("mid", "10", "knob", "MID", "EQ mid band", Wire(ch, "cc", 24)),
        ("bass", "11", "knob", "BASS", "EQ low band", Wire(ch, "cc", 25)),
        ("filter", "12", "knob", "FILTER", "Low-pass to the left, high-pass to the right", Wire(ch, "cc", 26)),
        ("cue", "13", "button", "CUE/PFL", "Headphone cue for this channel", Wire(ch, "note", 27)),
        ("fader", "14", "fader", "CH FADER", "Channel volume", Wire(ch, "cc", 28)),
    ]
    return [
        Control(f"ch{ch}-{cid}", fig, typ, label, f"{does} (channel {ch})", 0, 0, 0, 0, "mixer", midi)
        for cid, fig, typ, label, does, midi in rows
    ]


def _effects() -> list[Control]:
    # Effect unit u sends on channel 8 + u. The six FX SELECT buttons are notes 0, 1, 2 (top row,
    # channel 9) and 3, 4, 5 (bottom row, channel 10), left to right.
    rows: list[tuple[str, str, str, str, str, Wire, Wire | None]] = [
        ("fx-dry-wet-1", "20", "knob", "DRY/WET", "Effect wet and dry mix", Wire(9, "cc", 4), None),
        (
            "fx-beats-2",
            "19",
            "knob",
            "BEATS",
            "Effect beat multiplier or time division; with SHIFT, the second parameter",
            Wire(9, "cc", 5),
            None,
        ),
        (
            "tap",
            "23",
            "button",
            "TAP",
            "Tap 4 or more times to set the BPM; with SHIFT, reset to the track's BPM",
            Wire(9, "note", 9),
            Wire(9, "note", 1),
        ),
        ("fx-hpf", "21", "button", "HPF", "Select the high-pass filter effect", Wire(9, "note", 0), None),
        ("fx-lpf", "21", "button", "LPF", "Select the low-pass filter effect", Wire(9, "note", 1), None),
        ("fx-flanger", "21", "button", "FLANGER", "Select the flanger effect", Wire(9, "note", 2), None),
        ("fx-echo", "21", "button", "ECHO", "Select the echo effect", Wire(10, "note", 3), None),
        ("fx-reverb", "21", "button", "REVERB", "Select the reverb effect", Wire(10, "note", 4), None),
        ("fx-phaser", "21", "button", "PHASER", "Select the phaser effect", Wire(10, "note", 5), None),
        (
            "fx-paddle-1",
            "22",
            "button",
            "FX 1",
            "Paddle: up latches the effect on deck 1, down holds it, middle is off",
            Wire(9, "cc", 3),
            None,
        ),
        (
            "fx-paddle-2",
            "22",
            "button",
            "FX 2",
            "Paddle: up latches the effect on deck 2, down holds it, middle is off",
            Wire(10, "cc", 3),
            None,
        ),
    ]
    return [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, "effect", midi, shift)
        for cid, fig, typ, label, does, midi, shift in rows
    ]


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        (
            "browse",
            "1",
            "encoder",
            "BROWSE",
            "Turn to scroll folders and tracks, press to switch crates and library; with SHIFT, fast scroll",
            "browse",
            Wire(16, "cc", 0),
            Wire(16, "cc", 1),
        ),
        (
            "load-1",
            "7",
            "button",
            "LOAD 1",
            "Load the selected track to deck 1 (double-press loads both)",
            "browse",
            Wire(16, "note", 2),
            None,
        ),
        (
            "load-2",
            "7",
            "button",
            "LOAD 2",
            "Load the selected track to deck 2 (double-press loads both)",
            "browse",
            Wire(16, "note", 3),
            None,
        ),
        ("master-gain", "2", "knob", "MASTER", "Master output level", "mixer", Wire(15, "cc", 35), None),
        ("cue-gain", "5", "knob", "CUE GAIN", "Headphone level", "mixer", Wire(15, "cc", 47), None),
        (
            "cue-mix",
            "4",
            "knob",
            "CUE MIX",
            "Headphone blend between cue and master",
            "mixer",
            Wire(15, "cc", 39),
            None,
        ),
        (
            "crossfader",
            "15",
            "crossfader",
            "CROSSFADER",
            "Blend between the two decks",
            "mixer",
            Wire(16, "cc", 8),
            None,
        ),
    ]
    return [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, section, midi, shift)
        for cid, fig, typ, label, does, section, midi, shift in rows
    ]


# ----------------------------------------------------------------- geometry


def _reference_rows(order: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cid in order:
        x, y, w, h = _mm_box(_PX[cid])
        rows.append({"id": cid, "px": list(_PX[cid]), "x": x, "y": y, "w": w, "h": h})
    return rows


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
    for members in by_type.values():
        ordered = sorted(members, key=lambda c: centres[c.id])
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
        x = round(cx[c.id] - w / 2, 1)
        y = round(cy[c.id] - h / 2, 1)
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


# ----------------------------------------------------------------- build


def _controls() -> list[Control]:
    base = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_effects(), *_globals()]
    ids = [c.id for c in base]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    known = set(ids)
    missing_pos = sorted(known - set(_PX))
    extra_pos = sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    for cid in _PX_REPEAT:
        if cid not in known:
            raise SystemExit(f"[ERROR] repeat pass names an unknown control: {cid}")
    return base


def build_reference() -> dict[str, object]:
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": TRACE,
        "controls": _reference_rows([c.id for c in controls]),
    }


def build() -> dict[str, object]:
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {c.x},{c.y},{c.w},{c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "2-deck controller with jog displays and effect paddles",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": "Numark Mixtrack Platinum FX User Guide v1.2, Appendix, Technical Specifications (53.6 x 24.6 cm)",
            "wire": (
                "apps/webui/frontend/src/lib/rb/midi/maps/numark-mixtrack-platinum-fx.ts (community Mixxx mapping, "
                "hints only: Numark publishes no MIDI message list)"
            ),
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                f"reference: devices/{DEVICE_ID}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "MIC GAIN (fig 3): analogue, summed after the master gain, sends no MIDI",
            "MASTER OUTPUT LEDs (fig 6): no performance input",
            "pad modes other than the User Guide's four: pads carry their base wire identity",
            "TEMPO LSB (CC 41) and the jog touch note: each control carries one wire",
            "the crossfader's second message on channel 2 and TAP's twin on channel 10: hinted in the map, not drawn twice",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
