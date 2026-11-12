"""Build DJ-Tech Kontrol One practice-replica panel pair (issue #10860, ADR-0137).

Writes tools/deck-diagrams/devices/dj-tech-kontrol-one/panel.reference.json (measurement)
and panel.json (renderer-normalised layout).

Sources:
- Panel size 160 x 289 mm (W x D): published specifications (vendor manual unobtainable on CI).
- Positions: traced from a straight-on product photo (gearclubdirect.com listing image,
  600 x 418 px JPEG). Image sha256 b6ec4b1ee9abe30c266be5b02307194c08fb431d1516534461dfcf29ad984528.
  Never committed ((private path)).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/dj-tech-kontrol-one.ts (Mixxx tier 2,
  hints only), channel 1 wires on the panel (DECK SELECT switches channel in firmware).
- Mixxx ingest midi.json sha256 8f2c4249a6ac9b32317d1e74e0b25a5a32a724c1c75532c779dd891e7c99a1bd
  (vendor User Manual PDF was Cloudflare-blocked; Path B per issue #10860 plan).

Run: uv run --locked python tools/deck-diagrams/scripts/build_dj_tech_kontrol_one_panel.py
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

PANEL_W_MM = 160.0
PANEL_D_MM = 289.0
DEVICE_ID = "dj-tech-kontrol-one"
MAP_CONST = "DJ_TECH_KONTROL_ONE_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (600, 418)
PANEL_PX = {"x0": 202, "x1": 396, "y0": 37, "y1": 381}
MM_PER_PX_X = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5)
SOURCE_PDF_SHA256 = "8f2c4249a6ac9b32317d1e74e0b25a5a32a724c1c75532c779dd891e7c99a1bd"
SOURCE_IMAGE_SHA256 = "b6ec4b1ee9abe30c266be5b02307194c08fb431d1516534461dfcf29ad984528"
ALIGN_TOL_MM = 1.5

# Second independent trace (fresh read on a 5 px grid; 11 controls across the plate).
_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "deck-select": (211, 49, 231, 69),
    "knob-1": (253, 47, 283, 77),
    "knob-3": (329, 47, 359, 77),
    "pad-1": (240, 88, 264, 108),
    "pad-4": (348, 88, 372, 108),
    "pad-12": (348, 148, 372, 168),
    "pitch-minus": (235, 308, 261, 328),
    "shift": (252, 338, 280, 358),
    "play": (312, 338, 340, 360),
    "jog": (248, 214, 356, 322),
    "pitch-slider": (372, 166, 384, 294),
}

TRACE_ISSUE = "#10860"
TRACE_DATE = "Tue 10 Nov 2026"

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Wire:
    ch: int
    kind: str
    id: int


@dataclass(frozen=True)
class Control:
    id: str
    fig: str
    type: str
    label: str
    does: str
    x: float
    y: float
    w: float
    h: float
    section: str
    midi: Wire | None
    shift_midi: Wire | None = None


# ----------------------------------------------------------------- pixel trace (single source of truth)

_PX: dict[str, Box] = {
    "deck-select": (210, 48, 232, 70),
    "knob-1": (252, 46, 284, 78),
    "knob-2": (290, 46, 322, 78),
    "knob-3": (328, 46, 360, 78),
    "knob-4": (362, 46, 394, 78),
    "pad-1": (239, 87, 265, 109),
    "pad-2": (275, 87, 301, 109),
    "pad-3": (311, 87, 337, 109),
    "pad-4": (347, 87, 373, 109),
    "pad-5": (239, 117, 265, 139),
    "pad-6": (275, 117, 301, 139),
    "pad-7": (311, 117, 337, 139),
    "pad-8": (347, 117, 373, 139),
    "pad-9": (239, 147, 265, 169),
    "pad-10": (275, 147, 301, 169),
    "pad-11": (311, 147, 337, 169),
    "pad-12": (347, 147, 373, 169),
    "pad-13": (239, 177, 265, 199),
    "pad-14": (275, 177, 301, 199),
    "pad-15": (311, 177, 337, 199),
    "pad-16": (347, 177, 373, 199),
    "pitch-minus": (234, 307, 262, 329),
    "pitch-plus": (342, 307, 370, 329),
    "shift": (251, 337, 281, 359),
    "cue": (281, 337, 311, 359),
    "play": (311, 337, 341, 359),
    "sync": (341, 337, 371, 359),
    "jog": (247, 213, 357, 323),
    "pitch-slider": (371, 165, 385, 295),
}

CH = 1

# id, fig, type, label, does, section, wire or None
Row = tuple[str, str, str, str, str, str, Wire | None]

_ROWS: list[Row] = [
    ("deck-select", "1", "encoder", "DECK SELECT", "Choose active deck channel 1-4", "deck", None),
    ("knob-1", "2", "encoder", "KNOB 1", "FX or filter encoder (LED ring)", "effect", Wire(CH, "cc", 0x37)),
    ("knob-2", "3", "encoder", "KNOB 2", "FX or filter encoder (LED ring)", "effect", Wire(CH, "cc", 0x36)),
    ("knob-3", "4", "encoder", "KNOB 3", "FX or filter encoder (LED ring)", "effect", Wire(CH, "cc", 0x35)),
    ("knob-4", "5", "encoder", "KNOB 4", "FX or filter encoder (LED ring)", "effect", Wire(CH, "cc", 0x34)),
    ("pad-1", "10", "pad", "REC1", "Record row pad 1", "performance", Wire(CH, "note", 0x09)),
    ("pad-2", "11", "pad", "REC2", "Record row pad 2", "performance", Wire(CH, "note", 0x08)),
    ("pad-3", "12", "pad", "REC3", "Record row pad 3", "performance", Wire(CH, "note", 0x07)),
    ("pad-4", "13", "pad", "REC4", "Record row pad 4", "performance", Wire(CH, "note", 0x06)),
    ("pad-5", "14", "pad", "SAMPLE1", "Sampler row pad 1", "performance", Wire(CH, "note", 0x0E)),
    ("pad-6", "15", "pad", "SAMPLE2", "Sampler row pad 2", "performance", Wire(CH, "note", 0x0D)),
    ("pad-7", "16", "pad", "SAMPLE3", "Sampler row pad 3", "performance", Wire(CH, "note", 0x0C)),
    ("pad-8", "17", "pad", "SAMPLE4", "Sampler row pad 4", "performance", Wire(CH, "note", 0x0B)),
    ("pad-9", "18", "pad", "CUE1", "Hot cue row pad 1", "performance", Wire(CH, "note", 0x13)),
    ("pad-10", "19", "pad", "CUE2", "Hot cue row pad 2", "performance", Wire(CH, "note", 0x12)),
    ("pad-11", "20", "pad", "CUE3", "Hot cue row pad 3", "performance", Wire(CH, "note", 0x11)),
    ("pad-12", "21", "pad", "CUE4", "Hot cue row pad 4", "performance", Wire(CH, "note", 0x10)),
    ("pad-13", "22", "pad", "LOOP1", "Loop row pad 1", "performance", Wire(CH, "note", 0x18)),
    ("pad-14", "23", "pad", "LOOP2", "Loop row pad 2", "performance", Wire(CH, "note", 0x17)),
    ("pad-15", "24", "pad", "LOOP3", "Loop row pad 3", "performance", Wire(CH, "note", 0x16)),
    ("pad-16", "25", "pad", "LOOP4", "Loop row pad 4", "performance", Wire(CH, "note", 0x15)),
    ("pitch-minus", "30", "button", "PITCH -", "Momentary pitch nudge down", "deck", Wire(CH, "note", 0x19)),
    ("pitch-plus", "31", "button", "PITCH +", "Momentary pitch nudge up", "deck", Wire(CH, "note", 0x1E)),
    ("shift", "32", "button", "SHIFT", "Modifier for secondary pad or knob functions", "deck", Wire(CH, "note", 0x1A)),
    ("cue", "33", "button", "CUE", "Return to cue point or set cue", "deck", Wire(CH, "note", 0x1C)),
    ("play", "34", "button", "PLAY", "Start or pause playback", "deck", Wire(CH, "note", 0x1B)),
    ("sync", "35", "button", "SYNC", "Match tempo to master or other deck", "deck", Wire(CH, "note", 0x1D)),
    ("jog", "36", "jog", "JOG", "Scratch or bend tempo via touch-sensitive platter", "deck", Wire(CH, "cc", 0x20)),
    ("pitch-slider", "37", "fader", "PITCH", "Absolute tempo slider", "deck", Wire(CH, "cc", 0x21)),
]


# ----------------------------------------------------------------- geometry


def _mm(box: Box) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    x = (x0 - PANEL_PX["x0"]) * MM_PER_PX_X
    y = (y0 - PANEL_PX["y0"]) * MM_PER_PX_Y
    w = (x1 - x0 + 1) * MM_PER_PX_X
    h = (y1 - y0 + 1) * MM_PER_PX_Y
    return round(x, 1), round(y, 1), round(w, 1), round(h, 1)


def _normalised_box(box: Box) -> tuple[float, float, float, float]:
    x, y, w, h = _mm(box)
    cx, cy = round((x + w / 2) * 2) / 2, round((y + h / 2) * 2) / 2
    w_n, h_n = max(1.0, float(round(w))), max(1.0, float(round(h)))
    return round(cx - w_n / 2, 1), round(cy - h_n / 2, 1), w_n, h_n


def _controls() -> list[Control]:
    out: list[Control] = []
    for cid, fig, typ, label, does, section, wire in _ROWS:
        x, y, w, h = _normalised_box(_PX[cid])
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire, None))
    missing = set(_PX) - {c.id for c in out}
    if missing:
        raise SystemExit(f"[ERROR] _PX entries without rows: {sorted(missing)}")
    return out


def _reference_rows(controls: list[Control]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for c in controls:
        x, y, w, h = _mm(_PX[c.id])
        rows.append({"id": c.id, "px": list(_PX[c.id]), "x": x, "y": y, "w": w, "h": h})
    return rows


def _centre(box: Box) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return ((x0 + x1 + 1) / 2 * MM_PER_PX_X, (y0 + y1 + 1) / 2 * MM_PER_PX_Y)


def repeat_spread_mm() -> float:
    """Largest centre distance between the two traces, in mm."""
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        ax, ay = _centre(_PX[cid])
        bx, by = _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return math.ceil(worst * 10) / 10


REPEAT_SPREAD_MM = repeat_spread_mm()


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    controls = _controls()
    unknown = sorted(set(_PX_REPEAT) - set(_PX))
    if unknown:
        raise SystemExit(f"[ERROR] repeat trace names unknown controls: {unknown}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "DJ-Tech Kontrol One product photo (gearclubdirect.com listing, top view); "
                "Mixxx DJ-Tech Kontrol One.midi.xml (tier 2; vendor PDF unobtainable)"
            ),
            "source_url": "https://www.gearclubdirect.com/dj-tech-kontrol-one-professional-dj-controller-with-touch-sensitive-jog-wheel/",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PANEL_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "method": (
                "Button: lit body or bezel outline. Pad: pad frame between separator lines. "
                "Knob: outer LED ring. Encoder deck-select: knob cap. Jog: outer platter ring. "
                "Pitch slider: travel slot width. Repeat pass: eleven controls re-read on a 5 px grid."
            ),
            "repeat_spread_mm": spread,
            "repeat_pass": {k: list(v) for k, v in _PX_REPEAT.items()},
            "repeat_controls": sorted(_PX_REPEAT),
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": _reference_rows(controls),
    }


def build() -> dict[str, object]:
    controls = _controls()
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: {c}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "single-deck modular controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": ("DJ-Tech Kontrol One User Manual, Specifications: 160 (W) x 289 (D) x 107.5 (H) mm"),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/dj-tech-kontrol-one.ts (Mixxx hints only, Path B)",
            "positions": f"measured product photo trace; reference devices/{DEVICE_ID}/panel.reference.json",
        },
        "out_of_scope": [
            "decks 2-4: panel carries channel 1 wires; DECK SELECT chooses the active channel",
            "knob push (notes 0x01..0x04): hinted on the map, no separate panel control",
            "jog touch (note 0x1F): hinted on the map, shares the jog platter",
            "LED rings and pad backlight feedback",
            "Mixxx modifier layers beyond the vendor MIDI MAP",
        ],
        "controls": [
            {
                **asdict(c),
                "midi": None if c.midi is None else asdict(c.midi),
                "shift_midi": None,
            }
            for c in controls
        ],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT.name} and {PANEL_OUT.name} ({len(doc['controls'])} controls)")
