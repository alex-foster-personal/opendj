"""Build the DDJ-FLX6 practice-replica layout pair (ADR-0137, epic #9366).

Writes tools/deck-diagrams/devices/ddj-flx6/panel.reference.json (the measurement of record,
raw trace) and panel.json (the same trace normalised for the renderer). The pixel table
`_PX` below is the single source of position truth; nothing is copied from the FLX4 files or
from tools/deck-diagrams/devices/ddj-flx6/layout.json.

Sources:
- Panel size 676 x 345.7 mm (W x D): DDJ-FLX6 product page, Specifications.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx6.ts, transcribed from the
  official "DDJ-FLX6 List of MIDI messages" PDF E1 (sha256 in
  docs/controller/research/DDJ-FLX6-MIDI-Message-List.md); fig numbers kept as `fig`.
- Positions: traced from the top-panel figure on page 1 of that PDF (embedded image 3,
  1085 x 598 px). Each `_PX` row is the inclusive pixel box read off a gridded 4x or 6x zoom
  of the figure (button = body, knob and encoder = visible cap, pad = pad frame, jog = outer
  bezel, vertical fader = travel slot with cap width, crossfader = travel slot with cap
  height, slide switch = switch body). The right deck was read on its own and agrees with the
  left deck shifted by 630 px on every control checked. Calibration is ISOTROPIC: every round
  control is round in pixels (CUE 46 x 46, knobs 29 x 29, MERGE FX ring 60 x 60, jog
  318 x 319), so one scale from the published width serves both axes; at that scale the
  photographed top plate is 495 px = 335.0 mm of the 345.7 mm published depth, the remainder
  being the front-face overhang that the figure shows below the plate and that carries no
  top-panel control. The image itself never enters the repo.

Normalisation (panel.json only): centres snapped to 0.5 mm, sizes to whole mm, and row or
column mates (same control type, centres within 1.5 mm) placed on one line.

Run: uv run --locked python tools/deck-diagrams/scripts/build_flx6_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 676.0
PANEL_D_MM = 345.7
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / "ddj-flx6"
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (1085, 598)
PANEL_PX = {"x0": 43, "x1": 1041, "y0": 49, "y1": 543}
MM_PER_PX = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
SOURCE_PDF_SHA256 = "a12861c81f1550c35ae446a311951ade2a51cecfc2fd07081f5c405a4f3cfbc2"
SOURCE_IMAGE_SHA256 = (
    "044609eedfb6452e54de6bbf18e9a22a9dacc033f83d03e5978da2ecf4cf9a30"  # pdfimages -png extraction of embedded image 3
)
ALIGN_TOL_MM = 1.5

DECK_SHIFT_PX = 630  # right deck = left deck + 630 px, checked on 10 controls (all within 1 px)


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

# Left deck (deck 1). Inclusive [x0, y0, x1, y1] on the 1085 x 598 px figure.
_PX_DECK_1: dict[str, list[int]] = {
    "d1-in": [65, 68, 87, 90],
    "d1-out": [102, 68, 124, 90],
    "d1-reloop-exit": [142, 71, 157, 86],
    "d1-call-prev": [189, 73, 202, 86],
    "d1-call-next": [220, 73, 233, 86],
    "d1-memory": [252, 73, 265, 86],
    "d1-merge-fx-preset": [338, 68, 353, 75],
    "d1-merge-fx": [339, 88, 398, 147],
    "d1-jog-cutter": [103, 112, 117, 127],
    "d1-deck": [65, 154, 82, 172],
    "d1-jog": [81, 101, 399, 419],
    "d1-shift": [65, 376, 85, 396],
    "d1-beat-sync": [355, 379, 369, 394],
    "d1-master": [377, 379, 392, 394],
    "d1-tempo": [362, 420, 390, 518],
    "d1-hot-cue": [152, 442, 182, 451],
    "d1-pad-fx": [192, 442, 222, 451],
    "d1-beat-jump": [232, 442, 262, 451],
    "d1-sampler": [272, 442, 302, 451],
    "d1-cue": [66, 423, 111, 468],
    "d1-play-pause": [66, 482, 111, 527],
    "d1-pad-1": [152, 460, 182, 487],
    "d1-pad-2": [192, 460, 222, 487],
    "d1-pad-3": [232, 460, 262, 487],
    "d1-pad-4": [272, 460, 302, 487],
    "d1-pad-5": [152, 495, 182, 522],
    "d1-pad-6": [192, 495, 222, 522],
    "d1-pad-7": [232, 495, 262, 522],
    "d1-pad-8": [272, 495, 302, 522],
}

# Right deck (deck 2), read on its own zooms (not derived from deck 1).
_PX_DECK_2: dict[str, list[int]] = {
    "d2-in": [695, 68, 717, 90],
    "d2-out": [732, 68, 754, 90],
    "d2-reloop-exit": [772, 71, 787, 86],
    "d2-call-prev": [819, 73, 832, 86],
    "d2-call-next": [851, 73, 864, 86],
    "d2-memory": [882, 73, 895, 86],
    "d2-merge-fx-preset": [968, 68, 983, 75],
    "d2-merge-fx": [969, 88, 1028, 147],
    "d2-jog-cutter": [733, 112, 748, 127],
    "d2-deck": [695, 154, 712, 172],
    "d2-jog": [711, 101, 1029, 419],
    "d2-shift": [695, 376, 716, 397],
    "d2-beat-sync": [985, 379, 999, 394],
    "d2-master": [1007, 379, 1022, 394],
    "d2-tempo": [992, 420, 1020, 518],
    "d2-hot-cue": [782, 442, 812, 451],
    "d2-pad-fx": [822, 442, 852, 451],
    "d2-beat-jump": [862, 442, 892, 451],
    "d2-sampler": [902, 442, 932, 451],
    "d2-cue": [696, 423, 742, 468],
    "d2-play-pause": [696, 482, 742, 527],
    "d2-pad-1": [782, 460, 812, 487],
    "d2-pad-2": [822, 460, 852, 487],
    "d2-pad-3": [862, 460, 892, 487],
    "d2-pad-4": [902, 460, 932, 487],
    "d2-pad-5": [782, 495, 812, 522],
    "d2-pad-6": [822, 495, 852, 522],
    "d2-pad-7": [862, 495, 892, 522],
    "d2-pad-8": [902, 495, 932, 522],
}

# Mixer strips in their physical order, left to right: CH3, CH1, CH2, CH4 (the figure prints
# 3 1 2 4 under the filters and the LOAD callouts read 4-6 4-4 4-5 4-7).
_STRIP_X: dict[int, dict[str, tuple[int, int]]] = {
    3: {"knob": (427, 455), "cue": (423, 452), "fader": (421, 461), "load": (422, 451)},
    1: {"knob": (480, 508), "cue": (476, 505), "fader": (474, 512), "load": (475, 504)},
    2: {"knob": (533, 561), "cue": (528, 557), "fader": (528, 567), "load": (528, 557)},
    4: {"knob": (586, 614), "cue": (581, 610), "fader": (580, 618), "load": (581, 610)},
}
_STRIP_Y: dict[str, tuple[int, int]] = {
    "trim": (153, 181),
    "hi": (192, 220),
    "mid": (231, 259),
    "low": (272, 300),
    "filter": (314, 343),
    "ch-cue": (365, 376),
    "ch-fader": (396, 477),
    "load": (123, 135),
}

_PX_GLOBAL: dict[str, list[int]] = {
    "browse": [498, 68, 535, 105],
    "back": [468, 80, 481, 89],
    "view": [550, 80, 565, 91],
    "input-select": [584, 78, 606, 90],
    "master": [639, 79, 661, 101],
    "master-cue": [638, 124, 665, 133],
    "booth": [639, 152, 661, 174],
    "crossfader": [481, 499, 551, 537],
    "fx-select": [643, 244, 656, 257],
    "fx-ch-select": [643, 300, 656, 313],
    "beat-prev": [632, 365, 648, 375],
    "beat-next": [652, 365, 668, 375],
    "level-depth": [638, 405, 662, 429],
    "fx-on": [637, 456, 662, 481],
}


def _mixer_px() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for ch, cols in _STRIP_X.items():
        for key in ("trim", "hi", "mid", "low", "filter"):
            x0, x1 = cols["knob"]
            y0, y1 = _STRIP_Y[key]
            out[f"ch{ch}-{key}"] = [x0, y0, x1, y1]
        x0, x1 = cols["cue"]
        y0, y1 = _STRIP_Y["ch-cue"]
        out[f"ch{ch}-ch-cue"] = [x0, y0, x1, y1]
        x0, x1 = cols["fader"]
        y0, y1 = _STRIP_Y["ch-fader"]
        out[f"ch{ch}-ch-fader"] = [x0, y0, x1, y1]
        x0, x1 = cols["load"]
        y0, y1 = _STRIP_Y["load"]
        out[f"load-{ch}"] = [x0, y0, x1, y1]
    return out


_PX: dict[str, list[int]] = {**_PX_DECK_1, **_PX_DECK_2, **_mixer_px(), **_PX_GLOBAL}

# Second, independent pass (6x zoom, 5 px grid, read without the first table in view). The
# largest centre difference against `_PX` is 1.5 px = 1.0 mm, recorded as repeat_spread_mm.
_PX_REPEAT: dict[str, list[int]] = {
    "d1-cue": [66, 423, 111, 468],
    "ch1-trim": [478, 152, 507, 181],
    "crossfader": [482, 498, 549, 537],
    "d2-pad-1": [782, 460, 812, 487],
    "browse": [498, 68, 535, 105],
    "d1-tempo": [362, 420, 390, 517],
}

TRACE: dict[str, object] = {
    "source": ("DDJ-FLX6 List of MIDI messages E1, page 1, embedded image 3 (top-panel figure, 1085 x 598 px)"),
    "source_url": (
        "https://downloads.support.alphatheta.com/software_info/dj-controllers/DDJ-FLX6/"
        "DDJ-FLX6_MIDI_message_List_E1.pdf"
    ),
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/DDJ-FLX6-MIDI-Message-List.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX, "y": MM_PER_PX},
    "calibration": (
        "isotropic: one scale from the published 676 mm width over the plate's 999 px; every round "
        "control is round in the figure (CUE 46 x 46 px, knobs 29 x 29, MERGE FX ring 60 x 60, jog "
        "318 x 319), so no separate y scale is fitted. The plate is 495 px = 335.0 mm of the published "
        "345.7 mm depth; the remaining 10.7 mm is the front-face overhang drawn below the plate, which "
        "carries no top-panel control."
    ),
    "method": (
        "Button: body bounding box. Knob and encoder: visible cap (MERGE FX: its ring). Pad: pad frame. "
        "Jog: outer bezel. Vertical faders: travel slot with cap width. Crossfader: travel slot with "
        "cap height. INPUT SELECT: slide switch body. Read off gridded 4x zooms; right deck read on "
        "its own and found to be the left deck shifted by 630 px."
    ),
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": 1.0,
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
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


def _slug(label: str) -> str:
    named = label.lower().replace("<", "prev").replace(">", "next")
    raw = "".join(c if c.isalnum() else "-" for c in named).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw


# ----------------------------------------------------------------- deck strips

# fig, type, label, does, kind, id, shift kind, shift id (None = no documented shifted row)
_DECK_ROWS: list[tuple[str, str, str, str, str, int, tuple[str, int] | None]] = [
    ("1-7", "button", "IN/-4BEAT", "Set the loop in point; hold for a 4-beat loop", "note", 0x10, ("note", 0x4C)),
    ("1-8", "button", "OUT", "Set the loop out point", "note", 0x11, ("note", 0x77)),
    ("1-9", "button", "RELOOP/EXIT", "Exit or re-enter the loop", "note", 0x4D, ("note", 0x50)),
    ("1-10", "button", "CALL <", "Call the previous cue or loop", "note", 0x51, ("note", 0x61)),
    ("1-11", "button", "CALL >", "Call the next cue or loop", "note", 0x53, ("note", 0x62)),
    ("1-12", "button", "MEMORY", "Store the cue or loop", "note", 0x3D, ("note", 0x3E)),
    ("1-6", "button", "JOG CUTTER", "Toggle jog cutter scratch", "note", 0x1C, ("note", 0x17)),
    ("1-5", "button", "DECK", "Switch this side to its other deck", "note", 0x3C, None),
    ("1-4", "jog", "JOG", "Scratch or nudge the track", "cc", 0x22, ("cc", 0x29)),
    ("1-3", "button", "SHIFT", "Hold for each control's second function", "note", 0x3F, None),
    ("1-15", "button", "BEAT SYNC", "Match tempo to the master deck", "note", 0x58, ("note", 0x5D)),
    ("1-16", "button", "MASTER", "Make this deck the tempo master", "note", 0x5C, ("note", 0x60)),
    ("5-1", "button", "HOT CUE", "Pad mode: hot cues", "note", 0x1B, ("note", 0x69)),
    ("5-2", "button", "PAD FX", "Pad mode: pad effects", "note", 0x1E, ("note", 0x6B)),
    ("5-3", "button", "BEAT JUMP", "Pad mode: beat jump", "note", 0x20, ("note", 0x6D)),
    ("5-4", "button", "SAMPLER", "Pad mode: sampler", "note", 0x22, ("note", 0x6F)),
    ("1-2", "button", "CUE", "Set or return to the cue point", "note", 0x0C, ("note", 0x48)),
    ("1-1", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B, ("note", 0x47)),
]

_DECK_SHIFT_SELECT: dict[int, int] = {1: 0x7C, 2: 0x7D}  # [PDF] 1-5 DECK + SHIFT, channel 7
_ID_OVERRIDE: dict[str, str] = {"IN/-4BEAT": "in"}  # keeps the FLX4's d{n}-in / d{n}-out pair names


def _deck(deck: int) -> list[Control]:
    pad_ch = 6 + 2 * deck
    fx_ch = 4 + deck  # [PDF] 1-13 / 1-14: left MERGE FX on channel 5, right on channel 6
    out: list[Control] = []
    for fig, typ, label, does, kind, num, shift in _DECK_ROWS:
        cid = f"d{deck}-{_ID_OVERRIDE.get(label) or _slug(label)}"
        if cid == f"d{deck}-deck":
            shift_wire: Wire | None = Wire(7, "note", _DECK_SHIFT_SELECT[deck])
        elif shift is None:
            shift_wire = None
        else:
            shift_wire = Wire(deck, shift[0], shift[1])
        out.append(
            Control(
                cid, fig, typ, label, f"{does} (deck {deck})", 0, 0, 0, 0, "deck", Wire(deck, kind, num), shift_wire
            )
        )
    out.append(
        Control(
            f"d{deck}-merge-fx-preset",
            "1-13",
            "button",
            "MERGE FX",
            f"Pick the merge effect preset (deck {deck} side)",
            0,
            0,
            0,
            0,
            "effect",
            Wire(fx_ch, "note", 0x2F),
            Wire(fx_ch, "note", 0x30),
        )
    )
    out.append(
        Control(
            f"d{deck}-merge-fx",
            "1-14",
            "encoder",
            "MERGE FX",
            f"Turn to build the merge effect, press to release it (deck {deck} side)",
            0,
            0,
            0,
            0,
            "effect",
            Wire(fx_ch, "cc", 0x08),
            Wire(fx_ch, "cc", 0x28),
        )
    )
    out.append(
        Control(
            f"d{deck}-tempo",
            "1-17",
            "fader",
            "TEMPO",
            f"Tempo fader (deck {deck})",
            0,
            0,
            0,
            0,
            "deck",
            Wire(deck, "cc", 0x00),
            Wire(deck, "cc", 0x05),
        )
    )
    for pad in range(8):
        out.append(
            Control(
                f"d{deck}-pad-{pad + 1}",
                "5-5",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                0,
                0,
                0,
                0,
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch + 1, "note", pad),
            )
        )
    return out


def _channel(ch: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, int, str, int, Wire | None]] = [
        ("3-5", "knob", "TRIM", "Channel gain", ch, "cc", 0x04, None),
        ("3-6", "knob", "HI", "EQ high band", ch, "cc", 0x07, None),
        ("3-6", "knob", "MID", "EQ mid band", ch, "cc", 0x0B, None),
        ("3-6", "knob", "LOW", "EQ low band", ch, "cc", 0x0F, None),
        ("3-7", "knob", "FILTER", "Channel filter", 7, "cc", 0x16 + ch, None),
        ("3-8", "button", "CH CUE", "Headphone cue for this channel", ch, "note", 0x54, Wire(ch, "note", 0x68)),
        ("3-9", "fader", "CH FADER", "Channel volume", ch, "cc", 0x13, None),
    ]
    out: list[Control] = []
    for fig, typ, label, does, wch, kind, num, shift in rows:
        out.append(
            Control(
                f"ch{ch}-{_slug(label)}",
                fig,
                typ,
                label,
                f"{does} (channel {ch})",
                0,
                0,
                0,
                0,
                "mixer",
                Wire(wch, kind, num),
                shift,
            )
        )
    return out


_LOAD_SHIFT = {1: 0x58, 2: 0x59, 3: 0x60, 4: 0x61}  # [PDF] 4-4 .. 4-7 LOAD + SHIFT


def _loads() -> list[Control]:
    return [
        Control(
            f"load-{deck}",
            f"4-{3 + deck}",
            "button",
            f"LOAD {deck}",
            f"Load the selected track to deck {deck}",
            0,
            0,
            0,
            0,
            "browse",
            Wire(7, "note", 0x45 + deck),
            Wire(7, "note", _LOAD_SHIFT[deck]),
        )
        for deck in (1, 2, 3, 4)
    ]


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        (
            "browse",
            "4-1",
            "encoder",
            "BROWSE",
            "Scroll the library",
            "browse",
            Wire(7, "cc", 0x40),
            Wire(7, "cc", 0x64),
        ),
        (
            "back",
            "4-2",
            "button",
            "BACK",
            "Go back one level in the library",
            "browse",
            Wire(7, "note", 0x65),
            Wire(7, "note", 0x66),
        ),
        (
            "view",
            "4-3",
            "button",
            "VIEW",
            "Switch the library view",
            "browse",
            Wire(7, "note", 0x7A),
            Wire(7, "note", 0x68),
        ),
        (
            "input-select",
            "3-1",
            "button",
            "DECK 4 / SAMPLER",
            "Route channel 4 to deck 4 or the sampler",
            "mixer",
            Wire(4, "note", 0x0D),
            None,
        ),
        ("master", "3-2", "knob", "MASTER LEVEL", "Master output level", "mixer", Wire(7, "cc", 0x08), None),
        (
            "master-cue",
            "3-3",
            "button",
            "MASTER CUE",
            "Hear the master in headphones",
            "mixer",
            Wire(7, "note", 0x63),
            Wire(7, "note", 0x62),
        ),
        ("booth", "3-4", "knob", "BOOTH LEVEL", "Booth output level", "mixer", Wire(7, "cc", 0x09), None),
        (
            "crossfader",
            "3-10",
            "crossfader",
            "CROSSFADER",
            "Blend the left and right channels",
            "mixer",
            Wire(7, "cc", 0x1F),
            None,
        ),
        (
            "fx-select",
            "2-1",
            "button",
            "FX SELECT",
            "Step through the beat effects",
            "effect",
            Wire(5, "note", 0x70),
            None,
        ),
        (
            "fx-ch-select",
            "2-2",
            "button",
            "CH SELECT",
            "Pick the channel the beat effect hits",
            "effect",
            Wire(5, "note", 0x1C),
            None,
        ),
        (
            "beat-prev",
            "2-3",
            "button",
            "BEAT <",
            "Shorter effect beat",
            "effect",
            Wire(5, "note", 0x06),
            Wire(5, "note", 0x66),
        ),
        (
            "beat-next",
            "2-4",
            "button",
            "BEAT >",
            "Longer effect beat",
            "effect",
            Wire(5, "note", 0x07),
            Wire(5, "note", 0x6B),
        ),
        ("level-depth", "2-5", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", Wire(5, "cc", 0x02), None),
        (
            "fx-on",
            "2-6",
            "button",
            "ON/OFF",
            "Beat effect on or off; with SHIFT, release FX",
            "effect",
            Wire(5, "note", 0x47),
            Wire(5, "note", 0x43),
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
        members = sorted(members, key=lambda c: centres[c.id])
        group: list[Control] = []
        for c in members:
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
    base = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_channel(3), *_channel(4), *_loads(), *_globals()]
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
        "device": "ddj-flx6",
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
        "device": "ddj-flx6",
        "display_name": "4-channel performance controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "FLX6_MAP",
        "sources": {
            "panel_size": "DDJ-FLX6 product page, Specifications (676 x 345.7 mm)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-flx6.ts (DDJ-FLX6 List of MIDI messages E1)",
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                "reference: devices/ddj-flx6/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity",
            "MSB/LSB companion CCs: each control carries its MSB",
            "decks 3 and 4: each physical strip carries its deck 1 or deck 2 wire; DECK is a hinted button",
            "jog touch and wheel-side rows",
            "front-panel HEADPHONE LEVEL, HEADPHONE MIX and MIC LEVEL: not on the top panel",
            "level meters, jog and MERGE FX illumination: MIDI-OUT only",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
