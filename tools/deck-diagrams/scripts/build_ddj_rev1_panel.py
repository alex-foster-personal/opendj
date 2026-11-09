"""Build the DDJ-REV1 practice-replica panel pair (ADR-0137, epic #9366).

Writes two files under tools/deck-diagrams/devices/ddj-rev1/:
- panel.reference.json: the measurement of record. One pixel box per control, traced by hand
  on the top-panel photo embedded in the official "DDJ-REV1 List of MIDI message" PDF (page 1,
  embedded image 2, 2116 x 1033 px), converted to millimetres with the calibration below.
- panel.json: the layout the practice route renders, derived FROM the reference (never the other
  way round): centres snapped to 0.5 mm, sizes to whole mm, row and column mates aligned.

Sources:
- Panel size 526.0 x 255.5 mm: DDJ-REV1 product page, Specifications (Dimensions W x D x H
  526.0 x 255.5 x 59.2 mm).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev1.ts, itself transcribed from
  the MIDI message list PDF (fig ids kept as `fig`). Nothing comes from devices/ddj-rev1/midi.json,
  a bootstrap stub with invented codes.
- Positions: `_PX` below, the first trace. A second independent trace of eight controls on fresh
  4x crops (no first-pass boxes drawn) gave `REPEAT_SPREAD_MM`.

Calibration: the plate spans image columns 4..2112 (2109 px = 526.0 mm, 0.24941 mm/px). The
plate-derived row scale would be 255.5 / 998 = 0.25601 mm/px, a 2.65% disagreement, but the photo
is isotropic: the round jog platter measures 645 x 643 px and the square pads 72 x 72 px. So the
y scale is set equal to x, and the plate's 998 visible rows cover 248.9 of the 255.5 mm depth; the
front chamfer lies below the photo's bottom edge. The image never enters the repo.

Run: uv run --locked python tools/deck-diagrams/scripts/build_ddj_rev1_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 526.0
PANEL_D_MM = 255.5
DEVICE = "ddj-rev1"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "f7960b58b0115f2b5db7f55ed57e56226168094895123e67318dd7486a3ba025"
SOURCE_IMAGE_SHA256 = "21723a89c12c154338cc5bb98fb18cacc0c02c3c5c45caa9b1dbf578e79c1cd9"
IMAGE_PX = (2116, 1033)
PLATE_PX = {"x0": 4, "x1": 2112, "y0": 30, "y1": 1027}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
PLATE_ROW_SCALE_MM_PER_PX = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
REPEAT_SPREAD_MM = 0.6
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
    midi: Wire
    shift_midi: Wire | None = None


# Traced pixel boxes [x0, y0, x1, y1], inclusive, on the 2116 x 1033 photo. Button: light body
# bounding box (1/2X and 2X: dark body). Knob and encoder: visible cap. Pad: outer pad square.
# Jog: outer platter ring. CH FADER: travel slot with cap width. TEMPO and CROSSFADER (horizontal):
# travel slot with cap height. FX ON/OFF: paddle lever housing. DECK and PLAY/PAUSE: round body.
_PX: dict[str, tuple[int, int, int, int]] = {
    "d1-tempo": (225, 65, 495, 138),
    "d1-deck-select": (73, 187, 118, 232),
    "d1-auto-loop": (212, 195, 269, 223),
    "d1-half-x": (315, 196, 346, 223),
    "d1-double-x": (376, 196, 407, 223),
    "d1-sync": (453, 195, 511, 223),
    "d1-jog": (38, 278, 683, 920),
    "d1-mode-hot-cue": (730, 413, 792, 437),
    "d1-mode-auto-loop": (810, 413, 872, 437),
    "d1-mode-tracking": (892, 413, 953, 437),
    "d1-mode-sampler": (972, 413, 1033, 437),
    "d1-pad-1": (725, 457, 797, 528),
    "d1-pad-2": (807, 457, 877, 528),
    "d1-pad-3": (887, 457, 957, 528),
    "d1-pad-4": (967, 457, 1038, 528),
    "d1-pad-5": (725, 538, 797, 610),
    "d1-pad-6": (807, 538, 877, 610),
    "d1-pad-7": (887, 538, 957, 610),
    "d1-pad-8": (967, 538, 1038, 610),
    "d1-cue": (480, 955, 540, 982),
    "d1-play-pause": (595, 895, 687, 985),
    "d2-tempo": (1623, 65, 1893, 138),
    "d2-deck-select": (2000, 187, 2045, 232),
    "d2-auto-loop": (1608, 195, 1666, 223),
    "d2-half-x": (1712, 196, 1743, 223),
    "d2-double-x": (1773, 196, 1803, 223),
    "d2-sync": (1849, 195, 1907, 223),
    "d2-jog": (1433, 278, 2078, 920),
    "d2-mode-hot-cue": (1083, 413, 1145, 437),
    "d2-mode-auto-loop": (1163, 413, 1225, 437),
    "d2-mode-tracking": (1243, 413, 1307, 437),
    "d2-mode-sampler": (1325, 413, 1387, 437),
    "d2-pad-1": (1078, 457, 1150, 528),
    "d2-pad-2": (1158, 457, 1230, 528),
    "d2-pad-3": (1238, 457, 1310, 528),
    "d2-pad-4": (1318, 457, 1390, 528),
    "d2-pad-5": (1078, 538, 1150, 610),
    "d2-pad-6": (1158, 538, 1230, 610),
    "d2-pad-7": (1238, 538, 1310, 610),
    "d2-pad-8": (1318, 538, 1390, 610),
    "d2-cue": (1875, 955, 1935, 982),
    "d2-play-pause": (1985, 895, 2077, 985),
    "ch1-trim": (615, 63, 668, 120),
    "ch1-hi": (735, 63, 790, 120),
    "ch1-mid": (735, 173, 790, 230),
    "ch1-low": (735, 282, 790, 338),
    "ch1-filter": (615, 173, 668, 230),
    "ch1-ch-cue": (1014, 788, 1047, 820),
    "ch1-ch-fader": (850, 675, 933, 857),
    "ch2-trim": (1448, 63, 1503, 120),
    "ch2-hi": (1328, 63, 1383, 120),
    "ch2-mid": (1328, 173, 1383, 230),
    "ch2-low": (1328, 282, 1383, 338),
    "ch2-filter": (1448, 173, 1503, 230),
    "ch2-ch-cue": (1073, 788, 1105, 820),
    "ch2-ch-fader": (1183, 675, 1267, 857),
    "browse": (1013, 55, 1103, 145),
    "load-1": (878, 86, 942, 116),
    "load-2": (1177, 86, 1238, 116),
    "master-level": (1993, 72, 2048, 127),
    "master-cue": (1029, 703, 1090, 728),
    "phones-level": (67, 72, 122, 127),
    "shift": (1028, 331, 1090, 357),
    "crossfader": (963, 913, 1157, 1000),
    "fx1-on": (865, 158, 955, 260),
    "fx1-1": (837, 330, 870, 360),
    "fx1-2": (893, 330, 927, 360),
    "fx1-3": (950, 330, 983, 360),
    "fx2-on": (1162, 158, 1252, 260),
    "fx2-1": (1135, 330, 1168, 360),
    "fx2-2": (1190, 330, 1223, 360),
    "fx2-3": (1247, 330, 1280, 360),
    "level-depth": (1032, 198, 1087, 257),
}

# Second independent trace (fresh 4x crops, 10 px grid), used only for REPEAT_SPREAD_MM.
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-auto-loop": (210, 195, 270, 223),
    "ch1-trim": (615, 65, 670, 122),
    "d2-pad-3": (1238, 457, 1310, 529),
    "crossfader": (962, 914, 1157, 1000),
    "d1-play-pause": (594, 893, 687, 985),
    "fx1-2": (893, 332, 927, 359),
    "master-level": (1994, 72, 2049, 127),
    "d1-tempo": (227, 68, 495, 136),
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


def _slug(label: str) -> str:
    raw = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
    while "--" in raw:
        raw = raw.replace("--", "-")
    return raw


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _mm_box(_PX[cid])
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _PX entry for {cid!r}") from e


# ----------------------------------------------------------------- deck strips

# fig, id tail, type, label, does, kind, number, shift kind, shift number (None = no documented row used)
DeckRow = tuple[str, str, str, str, str, str, int, tuple[str, int] | None]

_DECK_ROWS: list[DeckRow] = [
    (
        "D4",
        "tempo",
        "fader",
        "TEMPO",
        "Tempo slider, horizontal: minus at the left, plus at the right",
        "cc",
        0x00,
        ("cc", 0x05),
    ),
    (
        "D6",
        "auto-loop",
        "button",
        "AUTO LOOP",
        "Start a 4-beat auto loop; with SHIFT, reloop or exit",
        "note",
        0x14,
        ("note", 0x50),
    ),
    (
        "D7",
        "half-x",
        "button",
        "1/2X",
        "Halve the loop length; with SHIFT, set the loop in point",
        "note",
        0x12,
        ("note", 0x10),
    ),
    (
        "D8",
        "double-x",
        "button",
        "2X",
        "Double the loop length; with SHIFT, set the loop out point",
        "note",
        0x13,
        ("note", 0x11),
    ),
    ("D9", "sync", "button", "SYNC", "Beat sync on; with SHIFT, sync off", "note", 0x58, ("note", 0x5C)),
    ("D3", "jog", "jog", "JOG", "Scratch or nudge the track; with SHIFT, search", "cc", 0x22, ("cc", 0x1F)),
    (
        "P9",
        "mode-hot-cue",
        "button",
        "HOT CUE",
        "Pad mode: hot cue; with SHIFT, beat jump",
        "note",
        0x1B,
        ("note", 0x69),
    ),
    (
        "P10",
        "mode-auto-loop",
        "button",
        "AUTO LOOP",
        "Pad mode: auto loop; with SHIFT, roll",
        "note",
        0x1E,
        ("note", 0x6B),
    ),
    (
        "P11",
        "mode-tracking",
        "button",
        "TRACKING",
        "Pad mode: tracking scratch; with SHIFT, trans",
        "note",
        0x20,
        ("note", 0x6D),
    ),
    (
        "P12",
        "mode-sampler",
        "button",
        "SAMPLER",
        "Pad mode: sampler; with SHIFT, scratch bank",
        "note",
        0x22,
        ("note", 0x6F),
    ),
    ("D2", "cue", "button", "CUE", "Set or return to the cue point", "note", 0x0C, ("note", 0x48)),
    ("D1", "play-pause", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B, ("note", 0x47)),
]


def _deck(deck: int) -> list[Control]:
    ch = deck
    pad_ch = 6 + 2 * deck
    out: list[Control] = []
    # D5: the deck-select button always sends note 0x72 on the channel of the ALTERNATE deck
    # (3 for the left side, 4 for the right); its shifted row sits on this deck's channel.
    alt = deck + 2
    x, y, w, h = _at(f"d{deck}-deck-select")
    out.append(
        Control(
            f"d{deck}-deck-select",
            "D5-L" if deck == 1 else "D5-R",
            "button",
            f"DECK {deck}/{alt}",
            f"Switch this side between deck {deck} and deck {alt} (deck {deck})",
            x,
            y,
            w,
            h,
            "deck",
            Wire(alt, "note", 0x72),
            Wire(ch, "note", 0x17),
        )
    )
    for fig, tail, typ, label, does, kind, num, shift in _DECK_ROWS:
        cid = f"d{deck}-{tail}"
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
                Wire(ch, kind, num),
                Wire(ch, shift[0], shift[1]) if shift else None,
            )
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                f"P{pad + 1}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(pad_ch, "note", pad),
            )
        )
    return out


# ----------------------------------------------------------------- mixer channels


def _channel(deck: int) -> list[Control]:
    ch = deck
    rows: list[tuple[str, str, str, str, str, int, int, Wire | None]] = [
        (f"M3-{deck}", "knob", "TRIM", "Channel gain", "cc", ch, 0x04, None),
        (f"M4-{deck}", "knob", "HI", "EQ high band", "cc", ch, 0x07, None),
        (f"M5-{deck}", "knob", "MID", "EQ mid band", "cc", ch, 0x0B, None),
        (f"M6-{deck}", "knob", "LOW", "EQ low band", "cc", ch, 0x0F, None),
        (f"F1-{deck}", "knob", "FILTER", "Channel filter: LPF left, HPF right", "cc", 7, 0x16 + deck, None),
        (f"M7-{deck}", "button", "CH CUE", "Headphone cue for this channel", "note", ch, 0x54, Wire(ch, "note", 0x68)),
        (f"M2-{deck}", "fader", "CH FADER", "Channel volume", "cc", ch, 0x13, None),
    ]
    out: list[Control] = []
    for fig, typ, label, does, kind, wch, num, shift in rows:
        cid = f"ch{deck}-{_slug(label)}"
        x, y, w, h = _at(cid)
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {deck})", x, y, w, h, "mixer", Wire(wch, kind, num), shift)
        )
    return out


# ----------------------------------------------------------------- globals and effects


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int, str, int, Wire | None]] = [
        ("B1", "browse", "encoder", "BROWSE", "Scroll the library", "browse", 7, "cc", 0x40, Wire(7, "cc", 0x64)),
        (
            "B2-L",
            "load-1",
            "button",
            "LOAD",
            "Load the selected track to the left deck",
            "browse",
            7,
            "note",
            0x46,
            Wire(7, "note", 0x58),
        ),
        (
            "B2-R",
            "load-2",
            "button",
            "LOAD",
            "Load the selected track to the right deck",
            "browse",
            7,
            "note",
            0x47,
            Wire(7, "note", 0x59),
        ),
        ("M9", "master-level", "knob", "MASTER LEVEL", "Master output level", "mixer", 7, "cc", 0x08, None),
        (
            "M8",
            "master-cue",
            "button",
            "MASTER",
            "Hear the master in headphones",
            "mixer",
            7,
            "note",
            0x63,
            Wire(7, "note", 0x62),
        ),
        ("M10", "phones-level", "knob", "LEVEL", "Headphone level", "mixer", 7, "cc", 0x0D, None),
        ("M11", "shift", "button", "SHIFT", "Hold for each control's second function", "mixer", 7, "note", 0x3F, None),
        (
            "M1",
            "crossfader",
            "crossfader",
            "CROSSFADER",
            "Blend the left and right decks",
            "mixer",
            7,
            "cc",
            0x1F,
            None,
        ),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does, section, ch, kind, num, shift in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), shift))
    return out


def _effects() -> list[Control]:
    out: list[Control] = []
    for unit in (1, 2):
        ch = 4 + unit
        paddle_does = f"FX {unit} paddle: pull for lock on, push for momentary on"
        rows: list[tuple[str, str, str, str, int, int]] = [
            # fig, id, label, does, note, shifted note
            (f"F2-{unit}", f"fx{unit}-on", "LOCK ON / ON", paddle_does, 0x50, 0x66),
            (f"F3-{unit}", f"fx{unit}-1", "1", f"FX {unit} slot 1 on; with SHIFT, select the effect", 0x70, 0x63),
            (f"F4-{unit}", f"fx{unit}-2", "2", f"FX {unit} slot 2 on; with SHIFT, beat back", 0x71, 0x06),
            (f"F5-{unit}", f"fx{unit}-3", "3", f"FX {unit} slot 3 on; with SHIFT, beat forward", 0x72, 0x07),
        ]
        for fig, cid, label, does, num, shifted in rows:
            x, y, w, h = _at(cid)
            out.append(
                Control(
                    cid,
                    fig,
                    "button",
                    label,
                    does,
                    x,
                    y,
                    w,
                    h,
                    "effect",
                    Wire(ch, "note", num),
                    Wire(ch, "note", shifted),
                )
            )
    x, y, w, h = _at("level-depth")
    out.append(
        Control(
            "level-depth",
            "F6",
            "knob",
            "LEVEL/DEPTH",
            "Effect amount for the selected FX slot",
            x,
            y,
            w,
            h,
            "effect",
            Wire(5, "cc", 0x02),
        )
    )
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
    controls = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals(), *_effects()]
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
            "source": "DDJ-REV1 List of MIDI message (E1), page 1, embedded image 2 (top-panel photo, 2116 x 1033 px)",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/DDJ-REV1-MIDI-Message-List.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": PLATE_ROW_SCALE_MM_PER_PX,
            "calibration": (
                "x from the plate width (526.0 mm over 2109 px). y set equal to x because the photo is "
                "isotropic (round jog 645 x 643 px, square pads 72 x 72 px); the plate-derived row scale "
                "0.25601 disagrees by 2.65% because the photo shows 998 rows (248.9 mm) of the 255.5 mm "
                "depth, the front chamfer lying below the image."
            ),
            "method": (
                "Manual trace on 3x crops with a 25 px grid. Button: light body bounding box (1/2X, 2X: dark body). "
                "Knob and encoder: visible cap. Pad: outer pad square. Jog: outer platter ring. CH FADER: travel slot "
                "with cap width. TEMPO and CROSSFADER (horizontal): travel slot with cap height. FX ON/OFF: paddle "
                "lever housing. DECK and PLAY/PAUSE: round body."
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
        "display_name": "2-deck scratch-style controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "DDJ_REV1_MAP",
        "sources": {
            "panel_size": "DDJ-REV1 product page, Specifications: Dimensions (W x D x H) 526.0 x 255.5 x 59.2 mm",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev1.ts (DDJ-REV1 List of MIDI message E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/ddj-rev1/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity (AUTO LOOP mode is bound in the map)",
            "MSB/LSB companion CCs: each control carries its MSB",
            "deck 3 and deck 4 layers (DECK SELECT is drawn, inert)",
            "FX paddle levers are drawn as buttons: the renderer has no lever type yet",
            "TEMPO is a horizontal fader: the renderer draws it from its rectangle",
            "jog touch and wheel-side rows, fader-start notes, rear and front panel controls",
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
