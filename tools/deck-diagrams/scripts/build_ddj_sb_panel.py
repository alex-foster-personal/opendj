"""Build the DDJ-SB practice-replica panel pair (ADR-0137, issue #10858).

Writes two files under tools/deck-diagrams/devices/ddj-sb/:
- panel.reference.json: the measurement of record. One pixel box per control, traced by hand
  on the top-panel drawing embedded in the official "DDJ-SB List of MIDI messages" PDF (page 1,
  two stacked 1424 x 400 px plates abutted to 1424 x 800 px), converted to millimetres with the
  calibration below.
- panel.json: the layout the practice route renders, derived FROM the reference (never the other
  way round): centres snapped to 0.5 mm, sizes to whole mm, row and column mates aligned.

Sources:
- Panel size 487.0 x 271.2 mm: Pioneer DJ DDJ-SB archived specifications (Dimensions W x D x H
  487.0 x 271.2 x 58.5 mm; jog wheel 128 mm).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-sb.ts, itself transcribed from
  the MIDI message list PDF (fig ids kept as `fig`). Nothing comes from devices/ddj-sb/midi.json,
  a bootstrap stub that cites the later DDJ-SB3 list.
- Positions: `_PX` below, the first trace. A second independent trace of eight controls on fresh
  crops (no first-pass boxes drawn) gave `REPEAT_SPREAD_MM`.

Calibration: the plate spans image columns 6..1415 (1410 px = 487.0 mm, 0.34539 mm/px). The
plate-derived row scale would be 271.2 / 785 = 0.34548 mm/px, a 0.03% disagreement, and the
drawing is isotropic: the round jog platter measures 371 x 371 px (128.14 mm vs the 128 mm spec)
and the square pads 79 x 79 px. So the y scale is set equal to x. The image never enters the repo.

Run: uv run --locked python tools/deck-diagrams/scripts/build_ddj_sb_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 487.0
PANEL_D_MM = 271.2
DEVICE = "ddj-sb"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "2a8e39d6f5e46ed31e1a9ec93b443dabad244a2060f2f751d336120c481d8107"
SOURCE_IMAGE_SHA256 = "9984f73e30ae7de5e6100033aeeaf2d5358af30b0a178a57b181d957d6480088"
IMAGE_PX = (1424, 800)
PLATE_PX = {"x0": 6, "x1": 1415, "y0": 5, "y1": 789}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
PLATE_ROW_SCALE_MM_PER_PX = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
REPEAT_SPREAD_MM = 0.8
TRACE_DATE = "Tue 10 Nov 2026"
TRACE_ISSUE = "#10858"


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
    midi: Wire | None  # None: the maker publishes no wire identity (unwired, never invented)
    shift_midi: Wire | None = None


# Traced pixel boxes [x0, y0, x1, y1], inclusive, on the 1424 x 800 abutted plate. Button: light
# body bounding box. Knob and encoder: visible cap. Pad: outer pad square. Jog: outer platter
# ring. CH FADER: travel slot with cap width. TEMPO: travel slot with cap width. CROSSFADER:
# travel slot with cap height. Analog MASTER LEVEL / HEADPHONES VOL / FILTER FADE: visible body.
_PX: dict[str, tuple[int, int, int, int]] = {
    "d1-tempo": (56, 62, 92, 258),
    "d1-key-lock": (48, 292, 112, 338),
    "d1-vinyl": (48, 438, 112, 486),
    "d1-jog": (148, 143, 518, 513),
    "fx1-1": (172, 88, 218, 124),
    "fx1-2": (242, 88, 288, 124),
    "fx1-3": (312, 88, 358, 124),
    "fx1-level": (438, 62, 494, 128),
    "d1-mode-hot-cue": (150, 558, 222, 586),
    "d1-mode-auto-loop": (242, 558, 314, 586),
    "d1-mode-manual-loop": (334, 558, 406, 586),
    "d1-mode-sampler": (426, 558, 498, 586),
    "d1-pad-1": (148, 604, 226, 682),
    "d1-pad-2": (240, 604, 318, 682),
    "d1-pad-3": (332, 604, 410, 682),
    "d1-pad-4": (424, 604, 502, 682),
    "d1-play-pause": (152, 688, 228, 756),
    "d1-cue": (244, 688, 320, 756),
    "d1-sync": (336, 688, 412, 756),
    "d1-shift": (428, 688, 504, 756),
    "d2-tempo": (1332, 62, 1368, 258),
    "d2-key-lock": (1312, 292, 1376, 338),
    "d2-vinyl": (1312, 438, 1376, 486),
    "d2-jog": (907, 143, 1277, 513),
    "fx2-1": (948, 88, 994, 124),
    "fx2-2": (1018, 88, 1064, 124),
    "fx2-3": (1088, 88, 1134, 124),
    "fx2-level": (1214, 62, 1270, 128),
    "d2-mode-hot-cue": (926, 558, 998, 586),
    "d2-mode-auto-loop": (1018, 558, 1090, 586),
    "d2-mode-manual-loop": (1110, 558, 1182, 586),
    "d2-mode-sampler": (1202, 558, 1274, 586),
    "d2-pad-1": (924, 604, 1002, 682),
    "d2-pad-2": (1016, 604, 1094, 682),
    "d2-pad-3": (1108, 604, 1186, 682),
    "d2-pad-4": (1200, 604, 1278, 682),
    "d2-play-pause": (928, 688, 1004, 756),
    "d2-cue": (1020, 688, 1096, 756),
    "d2-sync": (1112, 688, 1188, 756),
    "d2-shift": (1204, 688, 1280, 756),
    "ch1-hi": (588, 72, 642, 126),
    "ch1-mid": (588, 142, 642, 196),
    "ch1-low": (588, 218, 642, 272),
    "ch1-filter": (588, 304, 642, 358),
    "ch1-ch-cue": (592, 402, 644, 438),
    "ch1-ch-fader": (598, 458, 648, 638),
    "ch2-hi": (788, 72, 842, 126),
    "ch2-mid": (788, 142, 842, 196),
    "ch2-low": (788, 218, 842, 272),
    "ch2-filter": (788, 304, 842, 358),
    "ch2-ch-cue": (792, 402, 844, 438),
    "ch2-ch-fader": (798, 458, 848, 638),
    "browse": (686, 62, 744, 120),
    "load-1": (642, 42, 688, 64),
    "load-2": (744, 42, 790, 64),
    "back": (688, 148, 742, 178),
    "master-level": (688, 218, 742, 272),
    "phones-mix": (688, 304, 742, 358),
    "phones-vol": (688, 392, 742, 446),
    "filter-fade": (688, 612, 746, 670),
    "crossfader": (638, 698, 792, 760),
}

# Second independent trace (fresh crops, no first-pass boxes drawn), used only for REPEAT_SPREAD_MM.
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-tempo": (57, 64, 93, 256),
    "d1-jog": (147, 144, 517, 514),
    "d1-play-pause": (154, 687, 230, 755),
    "ch1-hi": (589, 73, 643, 127),
    "browse": (685, 63, 743, 121),
    "fx1-level": (439, 61, 495, 127),
    "d2-pad-3": (1109, 605, 1187, 683),
    "crossfader": (637, 699, 791, 761),
}


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


DeckRow = tuple[str, str, str, str, str, str, int, tuple[str, int] | None]

_DECK_ROWS: list[DeckRow] = [
    (
        "5",
        "tempo",
        "fader",
        "TEMPO",
        "Tempo slider, vertical: minus at the top, plus at the bottom",
        "cc",
        0x00,
        ("cc", 0x05),
    ),
    (
        "7",
        "key-lock",
        "button",
        "KEY LOCK",
        "Hold the current key while the tempo changes",
        "note",
        0x1A,
        ("note", 0x60),
    ),
    ("6", "vinyl", "button", "VINYL", "Jog scratch on; with SHIFT, slip", "note", 0x17, ("note", 0x4E)),
    ("4", "jog", "jog", "JOG", "Scratch or nudge the track; with SHIFT, search", "cc", 0x22, ("cc", 0x1F)),
    (
        "35",
        "mode-hot-cue",
        "button",
        "HOT CUE",
        "Pad mode: hot cue; with SHIFT, hot cue 5-8",
        "note",
        0x1B,
        ("note", 0x69),
    ),
    (
        "36",
        "mode-auto-loop",
        "button",
        "AUTO LOOP",
        "Pad mode: auto loop; with SHIFT, roll",
        "note",
        0x1E,
        ("note", 0x6B),
    ),
    (
        "37",
        "mode-manual-loop",
        "button",
        "MANUAL LOOP",
        "Pad mode: manual loop; with SHIFT, hot loop",
        "note",
        0x20,
        ("note", 0x6D),
    ),
    (
        "38",
        "mode-sampler",
        "button",
        "SAMPLER",
        "Pad mode: sampler; with SHIFT, bank",
        "note",
        0x22,
        ("note", 0x6F),
    ),
    ("1", "play-pause", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B, ("note", 0x47)),
    ("2", "cue", "button", "CUE", "Set or return to the cue point", "note", 0x0C, ("note", 0x48)),
    ("3", "sync", "button", "SYNC", "Beat sync on; with SHIFT, sync off", "note", 0x58, ("note", 0x5C)),
    ("8", "shift", "button", "SHIFT", "Hold for each control's second function", "note", 0x3F, None),
]


def _deck(deck: int) -> list[Control]:
    ch = deck
    pad_ch = 7 + deck
    out: list[Control] = []
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
    for pad in range(4):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                str(31 + pad),
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCD'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch, "note", 0x08 + pad),
            )
        )
    return out


def _channel(deck: int) -> list[Control]:
    ch = deck
    rows: list[tuple[str, str, str, str, str, int, int, Wire | None]] = [
        ("11", "knob", "HI", "EQ high band", "cc", ch, 0x07, None),
        ("12", "knob", "MID", "EQ mid band", "cc", ch, 0x0B, None),
        ("13", "knob", "LOW", "EQ low band", "cc", ch, 0x0F, None),
        ("14", "knob", "FILTER", "Channel filter: LPF left, HPF right", "cc", 7, 0x16 + deck, None),
        ("15", "button", "CH CUE", "Headphone cue for this channel", "note", ch, 0x54, Wire(ch, "note", 0x68)),
        ("10", "fader", "CH FADER", "Channel volume", "cc", ch, 0x13, None),
    ]
    out: list[Control] = []
    for fig, typ, label, does, kind, wch, num, shift in rows:
        cid = f"ch{deck}-{_slug(label)}"
        x, y, w, h = _at(cid)
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {deck})", x, y, w, h, "mixer", Wire(wch, kind, num), shift)
        )
    return out


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int | None, str | None, int | None, Wire | None]] = [
        ("27", "browse", "encoder", "BROWSE", "Scroll the library", "browse", 7, "cc", 0x40, Wire(7, "cc", 0x64)),
        (
            "28",
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
            "29",
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
        (
            "30",
            "back",
            "button",
            "BACK",
            "Leave the current library folder",
            "browse",
            7,
            "note",
            0x65,
            Wire(7, "note", 0x66),
        ),
        (
            "16",
            "master-level",
            "knob",
            "MASTER LEVEL",
            "Master output level (analog, no MIDI)",
            "mixer",
            None,
            None,
            None,
            None,
        ),
        (
            "17",
            "phones-mix",
            "knob",
            "HEADPHONES MIX",
            "Blend cue and master in the headphones",
            "mixer",
            7,
            "cc",
            0x05,
            None,
        ),
        (
            "18",
            "phones-vol",
            "knob",
            "HEADPHONES VOL",
            "Headphone level (analog, no MIDI)",
            "mixer",
            None,
            None,
            None,
            None,
        ),
        (
            "19",
            "filter-fade",
            "button",
            "FILTER FADE",
            "Filter-fade on the crossfader (local LED, no MIDI)",
            "mixer",
            None,
            None,
            None,
            None,
        ),
        (
            "9",
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
        wire = None if ch is None or kind is None or num is None else Wire(ch, kind, num)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire, shift))
    return out


def _effects() -> list[Control]:
    out: list[Control] = []
    figs_on = {1: ("20", "22", "25"), 2: ("21", "24", "26")}
    fig_level = {1: "23", 2: "27"}
    for unit in (1, 2):
        ch = 4 + unit
        for slot, fig in enumerate(figs_on[unit], start=1):
            cid = f"fx{unit}-{slot}"
            x, y, w, h = _at(cid)
            out.append(
                Control(
                    cid,
                    fig,
                    "button",
                    str(slot),
                    f"FX {unit} slot {slot} on",
                    x,
                    y,
                    w,
                    h,
                    "effect",
                    Wire(ch, "note", 0x46 + slot),
                    Wire(ch, "note", 0x62 + slot),
                )
            )
        cid = f"fx{unit}-level"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                fig_level[unit],
                "knob",
                "LEVEL/BEATS",
                f"FX {unit} amount; with SHIFT, beat value",
                x,
                y,
                w,
                h,
                "effect",
                Wire(ch, "cc", 0x02),
                Wire(ch, "cc", 0x00),
            )
        )
    return out


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
            "source": (
                "DDJ-SB List of MIDI messages (ver. 1.00), page 1, two stacked 1424 x 400 px "
                "top-panel plates abutted to 1424 x 800 px"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/DDJ-SB-MIDI-Message-List.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": PLATE_ROW_SCALE_MM_PER_PX,
            "calibration": (
                "x from the plate width (487.0 mm over 1410 px). y set equal to x because the drawing is "
                "isotropic (round jog 371 x 371 px = 128.14 mm vs the 128 mm spec, square pads 79 x 79 px); "
                "the plate-derived row scale 0.34548 disagrees by 0.03%."
            ),
            "method": (
                "Manual trace on 3x crops with a 10 px grid. Button: light body bounding box. "
                "Knob and encoder: visible cap. Pad: outer pad square. Jog: outer platter ring. "
                "CH FADER: travel slot with cap width. TEMPO: travel slot with cap width. "
                "CROSSFADER: travel slot with cap height. Analog MASTER LEVEL / HEADPHONES VOL / "
                "FILTER FADE: visible body."
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
        "display_name": "2-channel Serato beginner controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "DDJ_SB_MAP",
        "sources": {
            "panel_size": "Pioneer DJ DDJ-SB archived specifications: Dimensions (W x D x H) 487.0 x 271.2 x 58.5 mm",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-sb.ts (DDJ-SB List of MIDI messages ver. 1.00)",
            "positions": (
                "measured trace of the official top-panel drawing, normalised for the renderer; "
                "reference: devices/ddj-sb/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity (AUTO LOOP mode is bound in the map)",
            "MSB/LSB companion CCs: each control carries its MSB",
            "MASTER LEVEL, HEADPHONES VOL and FILTER FADE send no MIDI: midi null, unwired and inert",
            "FX ON and LEVEL/BEATS are drawn, inert",
            "jog touch and wheel-side rows, fader-start notes, rear and front panel (MIC LEVEL)",
            "TEMPO and VINYL/KEY LOCK sit on the outer edges (mirrored, not translated)",
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
