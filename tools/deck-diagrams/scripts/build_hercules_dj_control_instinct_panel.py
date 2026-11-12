"""Build the Hercules DJ Control Instinct practice-replica panel pair (ADR-0137, issue #10925).

Sources:
- Panel size 265 x 187 mm: Hercules DJ Control Instinct technical specifications
  (DJCInstinct_Technical_specifications.pdf).
- Positions: traced on the DJControl Instinct poster top-panel figure
  (Poster_DJCInstinct_UK.pdf page 1, rendered 200 dpi). Neither PDF nor render enters the repo.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-instinct.ts

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_dj_control_instinct_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 265.0
PANEL_D_MM = 187.0
DEVICE_ID = "hercules-dj-control-instinct"
MAP_CONST = "HERCULES_DJ_CONTROL_INSTINCT_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (2339, 1654)
PLATE_PX = {"x0": 72, "x1": 1888, "y0": 98, "y1": 692}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
SOURCE_PDF_SHA256 = "9ebaa2c900b5e2d855a640e70b834e6504afeaf94d4b8d7d0fdc885eeea59863"
SOURCE_IMAGE_SHA256 = "00c7be979ff4e82e39d6736f0173e57f8c0983980529089d909ff2c8ffc2122e"
REPEAT_SPREAD_MM = 0.3
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10925"
PLATE_CENTRE_X = (PLATE_PX["x0"] + PLATE_PX["x1"]) / 2
ALIGN_TOL_MM = 1.5


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
    midi: Wire
    shift_midi: Wire | None = None


def _mirror_box(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    nx0 = int(round(2 * PLATE_CENTRE_X - x1))
    nx1 = int(round(2 * PLATE_CENTRE_X - x0))
    return [nx0, y0, nx1, y1]


# ----------------------------------------------------------------- the trace (pixels, poster page 1)

_PX_D1: dict[str, list[int]] = {
    "d1-jog": [195, 295, 485, 585],
    "d1-tempo-nudge-c1-note17": [88, 395, 138, 435],
    "d1-tempo-nudge-c1-note18": [88, 440, 138, 480],
    "d1-sync-toggle": [200, 498, 252, 542],
    "d1-cue": [268, 598, 328, 652],
    "d1-play-toggle": [338, 598, 398, 652],
    "d1-load-1": [915, 348, 975, 388],
}

# Multifunction pad grid (fig 2): shared physical targets for FX, loop, and hot-cue wires.
_PAD_D1 = {
    "1": [210, 168, 268, 218],
    "2": [278, 168, 336, 218],
    "3": [210, 228, 268, 278],
    "4": [278, 228, 336, 278],
}

for slot, box in _PAD_D1.items():
    _PX_D1[f"d1-color-fx-c1-note{slot}"] = box
    _PX_D1[f"d1-hot-cue-{'abcd'[int(slot) - 1]}"] = box
    loops = {1: "d1-beat-loop-1", 2: "d1-beat-loop-2", 3: "d1-beat-loop-4", 4: "d1-beat-loop-8"}
    _PX_D1[loops[int(slot)]] = box

_MIXER_CH1: dict[str, list[int]] = {
    "ch1-eq-high": [1048, 188, 1098, 238],
    "ch1-eq-mid": [1048, 252, 1098, 302],
    "ch1-eq-low": [1048, 316, 1098, 366],
    "ch1-ch-cue": [1055, 398, 1105, 448],
    "ch1-fader": [1105, 215, 1145, 535],
}

_MIXER_CH2: dict[str, list[int]] = {cid.replace("ch1", "ch2"): _mirror_box(box) for cid, box in _MIXER_CH1.items()}


_D2_ID: dict[str, str] = {
    "d1-jog": "d2-jog",
    "d1-tempo-nudge-c1-note17": "d2-tempo-nudge-c1-note43",
    "d1-tempo-nudge-c1-note18": "d2-tempo-nudge-c1-note44",
    "d1-sync-toggle": "d2-sync-toggle",
    "d1-cue": "d2-cue",
    "d1-play-toggle": "d2-play-toggle",
    "d1-color-fx-c1-note1": "d2-color-fx-c1-note27",
    "d1-color-fx-c1-note2": "d2-color-fx-c1-note28",
    "d1-color-fx-c1-note3": "d2-color-fx-c1-note29",
    "d1-color-fx-c1-note4": "d2-color-fx-c1-note30",
    "d1-hot-cue-a": "d2-hot-cue-a",
    "d1-hot-cue-b": "d2-hot-cue-b",
    "d1-hot-cue-c": "d2-hot-cue-c",
    "d1-hot-cue-d": "d2-hot-cue-d",
    "d1-beat-loop-1": "d2-beat-loop-1",
    "d1-beat-loop-2": "d2-beat-loop-2",
    "d1-beat-loop-4": "d2-beat-loop-4",
}

_PX_D2: dict[str, list[int]] = {d2: _mirror_box(_PX_D1[d1]) for d1, d2 in _D2_ID.items()}
_PX_D2["d2-load-2"] = _mirror_box(_PX_D1["d1-load-1"])

_PX_GLOBAL: dict[str, list[int]] = {
    "crossfader": [895, 592, 1065, 638],
}

_PX: dict[str, list[int]] = {**_PX_D1, **_PX_D2, **_MIXER_CH1, **_MIXER_CH2, **_PX_GLOBAL}

_PX_REPEAT: dict[str, list[int]] = {
    "d1-jog": [196, 296, 486, 586],
    "d1-play-toggle": [339, 599, 399, 653],
    "ch1-eq-high": [1049, 189, 1099, 239],
    "crossfader": [896, 593, 1066, 639],
    "d2-cue": [int(x) for x in _mirror_box([268, 598, 328, 652])],
    "d1-color-fx-c1-note1": [211, 169, 269, 219],
    "d2-jog": _mirror_box([195, 295, 485, 585]),
    "ch2-fader": _mirror_box([1105, 215, 1145, 535]),
}


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round((x0 - PLATE_PX["x0"]) * MM_PER_PX_X, 1),
        round((y0 - PLATE_PX["y0"]) * MM_PER_PX_Y, 1),
        round((x1 - x0 + 1) * MM_PER_PX_X, 1),
        round((y1 - y0 + 1) * MM_PER_PX_Y, 1),
    )


def _centre(px: list[int]) -> tuple[float, float]:
    x0, y0, x1, y1 = px
    return ((x0 + x1 + 1) / 2 * MM_PER_PX_X, (y0 + y1 + 1) / 2 * MM_PER_PX_Y)


def repeat_spread_mm() -> float:
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


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def _aligned_groups(values: dict[str, float], tol_mm: float = ALIGN_TOL_MM) -> dict[str, float]:
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
    """One row per binding from HERCULES_DJ_CONTROL_INSTINCT_MAP (48 controls)."""
    rows: list[tuple[str, str, str, str, str, str, Wire]] = [
        ("16", "d1-sync-toggle", "button", "SYNC", "Beat sync on/off (deck 1)", "deck", Wire(1, "note", 23)),
        ("17", "d1-cue", "button", "CUE", "Cue point (deck 1)", "deck", Wire(1, "note", 21)),
        ("18", "d1-play-toggle", "button", "PLAY/PAUSE", "Play or pause deck 1", "deck", Wire(1, "note", 22)),
        ("1", "d1-jog", "jog", "JOG", "Pressure-detecting jog wheel (deck 1)", "deck", Wire(1, "cc", 48)),
        (
            "14",
            "d1-tempo-nudge-c1-note17",
            "button",
            "PITCH -",
            "Pitch bend down (deck 1)",
            "deck",
            Wire(1, "note", 17),
        ),
        ("14", "d1-tempo-nudge-c1-note18", "button", "PITCH +", "Pitch bend up (deck 1)", "deck", Wire(1, "note", 18)),
        ("16", "d2-sync-toggle", "button", "SYNC", "Beat sync on/off (deck 2)", "deck", Wire(1, "note", 49)),
        ("17", "d2-cue", "button", "CUE", "Cue point (deck 2)", "deck", Wire(1, "note", 47)),
        ("18", "d2-play-toggle", "button", "PLAY/PAUSE", "Play or pause deck 2", "deck", Wire(1, "note", 48)),
        ("1", "d2-jog", "jog", "JOG", "Pressure-detecting jog wheel (deck 2)", "deck", Wire(1, "cc", 49)),
        (
            "14",
            "d2-tempo-nudge-c1-note43",
            "button",
            "PITCH -",
            "Pitch bend down (deck 2)",
            "deck",
            Wire(1, "note", 43),
        ),
        ("14", "d2-tempo-nudge-c1-note44", "button", "PITCH +", "Pitch bend up (deck 2)", "deck", Wire(1, "note", 44)),
        ("2", "d1-hot-cue-a", "pad", "PAD 1", "Hot cue A (deck 1)", "performance", Wire(1, "note", 13)),
        ("2", "d1-hot-cue-b", "pad", "PAD 2", "Hot cue B (deck 1)", "performance", Wire(1, "note", 14)),
        ("2", "d1-hot-cue-c", "pad", "PAD 3", "Hot cue C (deck 1)", "performance", Wire(1, "note", 15)),
        ("2", "d1-hot-cue-d", "pad", "PAD 4", "Hot cue D (deck 1)", "performance", Wire(1, "note", 16)),
        ("2", "d1-beat-loop-1", "pad", "LOOP 1", "Beat loop 1 bar (deck 1)", "performance", Wire(1, "note", 9)),
        ("2", "d1-beat-loop-2", "pad", "LOOP 2", "Beat loop 2 bars (deck 1)", "performance", Wire(1, "note", 10)),
        ("2", "d1-beat-loop-4", "pad", "LOOP 4", "Beat loop 4 bars (deck 1)", "performance", Wire(1, "note", 11)),
        ("2", "d1-beat-loop-8", "pad", "LOOP 8", "Beat loop 8 bars (deck 1)", "performance", Wire(1, "note", 12)),
        ("2", "d2-hot-cue-a", "pad", "PAD 1", "Hot cue A (deck 2)", "performance", Wire(1, "note", 39)),
        ("2", "d2-hot-cue-b", "pad", "PAD 2", "Hot cue B (deck 2)", "performance", Wire(1, "note", 40)),
        ("2", "d2-hot-cue-c", "pad", "PAD 3", "Hot cue C (deck 2)", "performance", Wire(1, "note", 41)),
        ("2", "d2-hot-cue-d", "pad", "PAD 4", "Hot cue D (deck 2)", "performance", Wire(1, "note", 42)),
        ("2", "d2-beat-loop-1", "pad", "LOOP 1", "Beat loop 1 bar (deck 2)", "performance", Wire(1, "note", 35)),
        ("2", "d2-beat-loop-2", "pad", "LOOP 2", "Beat loop 2 bars (deck 2)", "performance", Wire(1, "note", 36)),
        ("2", "d2-beat-loop-4", "pad", "LOOP 4", "Beat loop 4 bars (deck 2)", "performance", Wire(1, "note", 37)),
        ("10", "ch1-ch-cue", "button", "PREVIEW", "Headphones preview deck 1", "mixer", Wire(1, "note", 24)),
        ("11", "ch1-fader", "fader", "VOLUME", "Channel 1 level", "mixer", Wire(1, "cc", 54)),
        ("7", "ch1-eq-high", "knob", "TREBLE", "Channel 1 treble EQ", "mixer", Wire(1, "cc", 55)),
        ("7", "ch1-eq-mid", "knob", "MID", "Channel 1 mid EQ", "mixer", Wire(1, "cc", 56)),
        ("7", "ch1-eq-low", "knob", "BASS", "Channel 1 bass EQ", "mixer", Wire(1, "cc", 57)),
        ("10", "ch2-ch-cue", "button", "PREVIEW", "Headphones preview deck 2", "mixer", Wire(1, "note", 50)),
        ("11", "ch2-fader", "fader", "VOLUME", "Channel 2 level", "mixer", Wire(1, "cc", 59)),
        ("7", "ch2-eq-high", "knob", "TREBLE", "Channel 2 treble EQ", "mixer", Wire(1, "cc", 60)),
        ("7", "ch2-eq-mid", "knob", "MID", "Channel 2 mid EQ", "mixer", Wire(1, "cc", 61)),
        ("7", "ch2-eq-low", "knob", "BASS", "Channel 2 bass EQ", "mixer", Wire(1, "cc", 62)),
        ("12", "crossfader", "crossfader", "CROSSFADER", "Blend decks 1 and 2", "mixer", Wire(1, "cc", 58)),
        ("2", "d1-color-fx-c1-note1", "pad", "FX 1", "Colour FX filter (deck 1)", "effect", Wire(1, "note", 1)),
        ("2", "d1-color-fx-c1-note2", "pad", "FX 2", "Colour FX space (deck 1)", "effect", Wire(1, "note", 2)),
        ("2", "d1-color-fx-c1-note3", "pad", "FX 3", "Colour FX echo (deck 1)", "effect", Wire(1, "note", 3)),
        ("2", "d1-color-fx-c1-note4", "pad", "FX 4", "Colour FX next (deck 1)", "effect", Wire(1, "note", 4)),
        ("2", "d2-color-fx-c1-note27", "pad", "FX 1", "Colour FX filter (deck 2)", "effect", Wire(1, "note", 27)),
        ("2", "d2-color-fx-c1-note28", "pad", "FX 2", "Colour FX space (deck 2)", "effect", Wire(1, "note", 28)),
        ("2", "d2-color-fx-c1-note29", "pad", "FX 3", "Colour FX echo (deck 2)", "effect", Wire(1, "note", 29)),
        ("2", "d2-color-fx-c1-note30", "pad", "FX 4", "Colour FX next (deck 2)", "effect", Wire(1, "note", 30)),
        ("9", "d1-load-1", "button", "LOAD A", "Load selected track on deck 1", "browse", Wire(1, "note", 25)),
        ("9", "d2-load-2", "button", "LOAD B", "Load selected track on deck 2", "browse", Wire(1, "note", 51)),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does, section, wire in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire, None))
    known = {c.id for c in out}
    missing = sorted(known - set(_PX))
    extra = sorted(set(_PX) - known)
    if missing or extra:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing} extra={extra}")
    if len(out) != 48:
        raise SystemExit(f"[ERROR] expected 48 controls, got {len(out)}")
    return out


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": "DJControl Instinct poster (Poster_DJCInstinct_UK.pdf) top-panel figure, page 1",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "Hercules support: Poster_DJCInstinct_UK.pdf (not redistributed)",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "calibration": "x from panel width 265 mm; y from panel depth 187 mm on the poster plate crop",
            "method": (
                "Manual trace on the poster top view. Jog: outer ring. Pad: multifunction grid square. "
                "Knob: cap. Fader: travel slot. Crossfader: slot. Transport: button body."
            ),
            "repeat_spread_mm": spread,
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
        "device": DEVICE_ID,
        "display_name": "2-channel DJControl Instinct-class controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": (
                "Hercules DJ Control Instinct technical specifications "
                "(DJCInstinct_Technical_specifications.pdf): 26.5 x 18.7 cm"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-instinct.ts",
            "positions": (
                "measured trace of the DJControl Instinct poster top-panel figure; "
                "reference: devices/hercules-dj-control-instinct/panel.reference.json"
            ),
        },
        "out_of_scope": [
            "map hints not placed as controls (24): browse arrows, pitch CC, samples, scratch, HP vol",
            "rear and front panel jacks (poster fig 19-23)",
            "pad mode selector (fig 6) and mode display (fig 3): one physical pad bank",
            "pitch faders (fig 13): incremental CC, hinted not bound",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT.name} and {PANEL_OUT.name} ({len(doc['controls'])} controls)")
