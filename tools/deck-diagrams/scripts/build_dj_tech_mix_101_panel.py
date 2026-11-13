"""Build the DJ-Tech MIX-101 practice-replica panel pair (ADR-0137, issue #10903).

Writes tools/deck-diagrams/devices/dj-tech-mix-101/panel.reference.json and panel.json.
The pixel table ``_PX`` is traced from the top-panel product figure on ManualsLib page 3
(static image sha256 in TRACE; image never enters the repo).

Sources:
- Panel size 210.0 x 73.5 mm (W x D): DJ-Tech MIX-101 user manual specifications
  (210 x 140 x 73.5 mm W x H x D).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/dj-tech-mix-101.ts.
- Positions: ``_PX`` on a 485 x 321 px crop of the ManualsLib top view (plate spans the crop).
  Deck B transport is the same physical controls as deck A while A/B is held (no duplicate rows).

Run: uv run --locked python tools/deck-diagrams/scripts/build_dj_tech_mix_101_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 210.0
PANEL_D_MM = 73.5
DEVICE = "dj-tech-mix-101"
MAP_CONST = "DJ_TECH_MIX_101_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

IMAGE_PX = (485, 321)
PLATE_PX = {"x0": 0, "x1": 484, "y0": 0, "y1": 320}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
# No local copy of the source PDF exists (ManualsLib serves page images, not the PDF), so no PDF
# digest is recorded; a hash of a description would not identify the document (issue #13458).
SOURCE_PDF_SHA256: str | None = None
SOURCE_PDF_NOTE = (
    "source PDF bytes unavailable: traced from the ManualsLib page-3 image (source_image_sha256); no PDF digest"
)
SOURCE_IMAGE_SHA256 = "5fd0a64d5aa9672627d5032f9f10ec531793ee15dbda9e65550e46fe39478344"
REPEAT_SPREAD_MM = 0.5
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10903"


@dataclass(frozen=True)
class Wire:
    ch: int
    kind: str  # note | cc
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


# Inclusive pixel boxes on the 485 x 321 plate crop (ManualsLib MIX-101 top view).
_PX: dict[str, tuple[int, int, int, int]] = {
    "ch1-trim": (36, 30, 66, 60),
    "ch1-eq-high": (36, 88, 66, 118),
    "ch1-eq-mid": (36, 146, 66, 176),
    "ch1-eq-low": (36, 204, 66, 234),
    "ch1-pfl": (86, 24, 114, 52),
    "ch1-fader": (80, 92, 100, 228),
    "ch2-trim": (211, 30, 241, 60),
    "ch2-eq-high": (211, 88, 241, 118),
    "ch2-eq-mid": (211, 146, 241, 176),
    "ch2-eq-low": (211, 204, 241, 234),
    "ch2-pfl": (161, 24, 189, 52),
    "ch2-fader": (175, 92, 195, 228),
    "crossfader": (92, 252, 188, 276),
    "browse-encoder": (348, 28, 382, 62),
    "key-1": (288, 88, 322, 122),
    "key-2": (408, 88, 442, 122),
    "load": (352, 44, 376, 68),
    "jog": (302, 128, 428, 254),
    "mode-sampler": (441, 112, 465, 136),
    "mode-loop": (441, 128, 465, 152),
    "mode-fx": (441, 144, 465, 168),
    "mode-cue-vinyl": (441, 160, 465, 184),
    "sync": (441, 236, 465, 260),
    "ab-shift": (298, 264, 332, 298),
    "play": (358, 264, 392, 298),
    "cue": (418, 264, 452, 298),
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "ch1-fader": (82, 95, 98, 225),
    "jog": (305, 132, 425, 250),
    "crossfader": (95, 255, 185, 277),
    "play": (360, 266, 390, 296),
    "ch1-trim": (38, 32, 64, 58),
    "browse-encoder": (350, 30, 380, 60),
}


def _mm_box(px: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round(x0 * MM_PER_PX_X, 1),
        round(y0 * MM_PER_PX_Y, 1),
        round((x1 - x0 + 1) * MM_PER_PX_X, 1),
        round((y1 - y0 + 1) * MM_PER_PX_Y, 1),
    )


def _centre(px: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = px
    return ((x0 + x1 + 1) / 2 * MM_PER_PX_X, (y0 + y1 + 1) / 2 * MM_PER_PX_Y)


def repeat_spread_mm() -> float:
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        (ax, ay), (bx, by) = _centre(_PX[cid]), _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _at(cid: str) -> tuple[float, float, float, float]:
    return _mm_box(_PX[cid])


def _controls() -> list[Control]:
    ch = 1
    rows: list[tuple[str, str, str, str, str, str, Wire | None]] = [
        (
            "ab-shift",
            "A/B",
            "button",
            "A/B",
            "Hold to target deck B on the shared transport section",
            "deck",
            Wire(ch, "note", 9),
        ),
        ("play", "PLAY", "button", "PLAY", "Play or pause deck 1; hold A/B for deck 2", "deck", Wire(ch, "note", 7)),
        ("cue", "CUE", "button", "CUE", "Cue deck 1; hold A/B for deck 2", "deck", Wire(ch, "note", 8)),
        ("sync", "SYNC", "button", "SYNC", "Sync deck 1; hold A/B for deck 2", "deck", Wire(ch, "note", 6)),
        ("key-1", "KEY 1", "pad", "KEY 1", "Controller pad 1; hold A/B for deck 2", "performance", Wire(ch, "note", 3)),
        ("key-2", "KEY 2", "pad", "KEY 2", "Controller pad 2; hold A/B for deck 2", "performance", Wire(ch, "note", 4)),
        (
            "mode-sampler",
            "MODE",
            "button",
            "SAMPLER",
            "Sampler pad mode; hold A/B for deck 2",
            "performance",
            Wire(ch, "note", 17),
        ),
        (
            "mode-loop",
            "MODE",
            "button",
            "LOOP",
            "Auto-loop pad mode; hold A/B for deck 2",
            "performance",
            Wire(ch, "note", 18),
        ),
        (
            "mode-fx",
            "MODE",
            "button",
            "FX",
            "Instant FX pad mode; hold A/B for deck 2",
            "performance",
            Wire(ch, "note", 19),
        ),
        (
            "mode-cue-vinyl",
            "MODE",
            "button",
            "CUE/VINYL",
            "Hot-cue pad mode; hold A/B for deck 2",
            "performance",
            Wire(ch, "note", 20),
        ),
        ("jog", "JOG", "jog", "JOG", "Scratch or nudge; hold A/B for deck 2", "deck", Wire(ch, "cc", 19)),
        (
            "load",
            "LOAD",
            "button",
            "LOAD",
            "Load the browsed track; hold A/B for deck 2",
            "browse",
            Wire(ch, "note", 12),
        ),
        ("browse-encoder", "BROWSE", "encoder", "BROWSE", "Scroll the library", "browse", Wire(ch, "cc", 17)),
        ("ch1-pfl", "PFL", "button", "CH A PFL", "Cue channel A in headphones", "mixer", Wire(ch, "note", 1)),
        ("ch2-pfl", "PFL", "button", "CH B PFL", "Cue channel B in headphones", "mixer", Wire(ch, "note", 2)),
        ("ch1-trim", "GAIN", "knob", "CH A TRIM", "Channel A gain", "mixer", Wire(ch, "cc", 36)),
        ("ch1-eq-high", "EQ", "knob", "CH A HI", "Channel A EQ high", "mixer", Wire(ch, "cc", 38)),
        ("ch1-eq-mid", "EQ", "knob", "CH A MID", "Channel A EQ mid", "mixer", Wire(ch, "cc", 40)),
        ("ch1-eq-low", "EQ", "knob", "CH A LOW", "Channel A EQ low", "mixer", Wire(ch, "cc", 42)),
        ("ch1-fader", "FADER", "fader", "CH A", "Channel A level", "mixer", Wire(ch, "cc", 33)),
        ("ch2-trim", "GAIN", "knob", "CH B TRIM", "Channel B gain", "mixer", Wire(ch, "cc", 37)),
        ("ch2-eq-high", "EQ", "knob", "CH B HI", "Channel B EQ high", "mixer", Wire(ch, "cc", 39)),
        ("ch2-eq-mid", "EQ", "knob", "CH B MID", "Channel B EQ mid", "mixer", Wire(ch, "cc", 41)),
        ("ch2-eq-low", "EQ", "knob", "CH B LOW", "Channel B EQ low", "mixer", Wire(ch, "cc", 43)),
        ("ch2-fader", "FADER", "fader", "CH B", "Channel B level", "mixer", Wire(ch, "cc", 34)),
        ("crossfader", "XFADER", "crossfader", "CROSSFADER", "Blend channels A and B", "mixer", Wire(ch, "cc", 35)),
    ]
    out: list[Control] = []
    for cid, fig, typ, label, does, section, wire in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire))
    return out


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def normalise(controls: list[Control]) -> list[Control]:
    out: list[Control] = []
    for c in controls:
        w, h = float(max(1, round(c.w))), float(max(1, round(c.h)))
        cx = _snap_half(c.x + c.w / 2)
        cy = _snap_half(c.y + c.h / 2)
        x = round(cx - w / 2, 1)
        y = round(cy - h / 2, 1)
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


def build_reference() -> dict[str, object]:
    controls = _controls()
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": ("DJ-Tech MIX-101 User Manual (ManualsLib 1110404), Main Features page top-panel product figure"),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_pdf_note": SOURCE_PDF_NOTE,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "https://www.manualslib.com/manual/1110404/Dj-Tech-Mix-101.html?page=3",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": MM_PER_PX_Y,
            "calibration": ("Width 210 mm over 485 px; depth 73.5 mm over 321 px (product top view crop)."),
            "method": "Manual trace with 20 px grid on the ManualsLib top view; knob cap, button body, fader slot, jog ring.",
            "repeat_spread_mm": spread,
            "repeat_pass": sorted(_PX_REPEAT),
            "repeat_controls": sorted(_PX_REPEAT),
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [{"id": c.id, "px": list(_PX[c.id]), "x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in controls],
    }


def build() -> dict[str, object]:
    controls = normalise(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-channel compact USB MIDI controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": (
                "DJ-Tech MIX-101 product datasheet: Dimension 210 x 140 x 73.5 mm (W x H x D); "
                "top panel W x D = 210 x 73.5 mm"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/dj-tech-mix-101.ts",
            "positions": (
                "measured trace of the ManualsLib top-panel figure, normalised for the renderer; "
                f"reference: devices/{DEVICE}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "map hints only: SELECT 0x05, encoder pushes 0x0B/0x0D, jog touch 0x15, EQ detent notes, MIDI channel 0x0E, SW142/SW143",
            "SCRATCH CC 0x15 shares the jog wheel; panel carries JOG 0x13",
            "deck 2 shifted twins: same physical control while A/B is held",
            "vendor wordmarks on the product photo",
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
