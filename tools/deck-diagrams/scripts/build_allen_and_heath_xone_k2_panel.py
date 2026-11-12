"""Build Allen & Heath XONE:K2 practice-replica panel pair (ADR-0137, issue #10892).

Writes tools/deck-diagrams/devices/allen-and-heath-xone-k2/panel.reference.json and panel.json.

Sources:
- Panel size 358 x 135 mm (W x D): Allen & Heath XONE:K2 User Guide AP8509, Specifications page 29.
- Positions: traced on page 9 "MIDI CONTROLS" schematic (600 dpi render of AP8509).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/allen-and-heath-xone-k2.ts.

Run: uv run --locked python tools/deck-diagrams/scripts/build_allen_and_heath_xone_k2_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 358.0
PANEL_D_MM = 135.0
DEVICE_ID = "allen-and-heath-xone-k2"
MAP_CONST = "XONE_K2_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"
TRACE_DATA = DEVICE_DIR / "trace_data.json"

IMAGE_PX = (4959, 7017)
SOURCE_PDF_SHA256 = "9a10a3267a29ea32c28e24e6797c052025c0b867deadf2fc1e7b3f26a6817df8"
SOURCE_IMAGE_SHA256 = "32033d393406fe8e104226207016a1f57a36e478fc65c5865b95f0e78c910bbd"
ALIGN_TOL_MM = 1.5
TRACE_DATE = "Wed 12 Nov 2026"
TRACE_ISSUE = "#10892"

_CTRL_ROWS: list[tuple[str, str, str, str, str, str, int, str, int]] = [
    ("d1-jog", "1", "encoder", "DECK 1 JOG", "Nudge or scratch deck 1 (relative encoder)", "deck", 15, "cc", 0),
    ("d1-pitch", "4", "fader", "DECK 1 PITCH", "Tempo / pitch slider for deck 1", "deck", 15, "cc", 16),
    ("d2-jog", "1", "encoder", "DECK 2 JOG", "Nudge or scratch deck 2 (relative encoder)", "deck", 15, "cc", 3),
    ("d2-pitch", "4", "fader", "DECK 2 PITCH", "Tempo / pitch slider for deck 2", "deck", 15, "cc", 19),
    ("ch1-trim", "2", "knob", "TRIM", "Channel 1 input gain", "mixer", 15, "cc", 4),
    ("ch1-filter", "2", "knob", "FILTER", "Deck 1 color FX filter", "mixer", 15, "cc", 8),
    ("ch1-eq-high", "2", "knob", "EQ HIGH", "Channel 1 high EQ", "mixer", 15, "cc", 5),
    ("ch1-eq-mid", "2", "knob", "EQ MID", "Channel 1 mid EQ", "mixer", 15, "cc", 9),
    ("ch1-eq-low", "2", "knob", "EQ LOW", "Channel 1 low EQ", "mixer", 15, "cc", 13),
    ("ch1-fader", "4", "fader", "CH FADER", "Channel 1 level", "mixer", 15, "cc", 17),
    ("ch1-ch-cue", "3", "button", "PFL", "Channel 1 headphone cue", "mixer", 15, "note", 41),
    ("ch2-trim", "2", "knob", "TRIM", "Channel 2 input gain", "mixer", 15, "cc", 7),
    ("ch2-filter", "2", "knob", "FILTER", "Deck 2 color FX filter", "mixer", 15, "cc", 11),
    ("ch2-eq-high", "2", "knob", "EQ HIGH", "Channel 2 high EQ", "mixer", 15, "cc", 6),
    ("ch2-eq-mid", "2", "knob", "EQ MID", "Channel 2 mid EQ", "mixer", 15, "cc", 10),
    ("ch2-eq-low", "2", "knob", "EQ LOW", "Channel 2 low EQ", "mixer", 15, "cc", 14),
    ("ch2-fader", "4", "fader", "CH FADER", "Channel 2 level", "mixer", 15, "cc", 18),
    ("ch2-ch-cue", "3", "button", "PFL", "Channel 2 headphone cue", "mixer", 15, "note", 42),
    ("crossfader", "2", "knob", "CROSSFADER", "Rotary crossfader (column 1)", "mixer", 15, "cc", 12),
    ("master", "2", "knob", "MASTER", "Master output level", "mixer", 15, "cc", 15),
    ("browse-encoder", "6", "encoder", "SCROLL", "Browse library (relative)", "browse", 15, "cc", 21),
    ("d1-hot-cue-a", "5", "pad", "HOT CUE A", "Deck 1 hot cue A", "deck", 15, "note", 36),
    ("d1-hot-cue-b", "5", "pad", "HOT CUE B", "Deck 1 hot cue B", "deck", 15, "note", 37),
    ("d1-hot-cue-c", "5", "pad", "HOT CUE C", "Deck 1 hot cue C", "deck", 15, "note", 32),
    ("d1-hot-cue-d", "5", "pad", "HOT CUE D", "Deck 1 hot cue D", "deck", 15, "note", 33),
    ("d2-hot-cue-a", "5", "pad", "HOT CUE A", "Deck 2 hot cue A", "deck", 15, "note", 38),
    ("d2-hot-cue-b", "5", "pad", "HOT CUE B", "Deck 2 hot cue B", "deck", 15, "note", 39),
    ("d2-hot-cue-c", "5", "pad", "HOT CUE C", "Deck 2 hot cue C", "deck", 15, "note", 34),
    ("d2-hot-cue-d", "5", "pad", "HOT CUE D", "Deck 2 hot cue D", "deck", 15, "note", 35),
    ("d1-load-1", "5", "button", "LOAD", "Load selected track to deck 1", "browse", 15, "note", 28),
    ("d2-load-2", "5", "button", "LOAD", "Load selected track to deck 2", "browse", 15, "note", 30),
    ("d1-sync-toggle", "5", "button", "SYNC", "Toggle deck 1 sync", "deck", 15, "note", 29),
    ("d2-sync-toggle", "5", "button", "SYNC", "Toggle deck 2 sync", "deck", 15, "note", 31),
    ("d1-cue", "5", "button", "CUE", "Deck 1 cue / preview", "deck", 15, "note", 24),
    ("d1-play-toggle", "5", "button", "PLAY", "Deck 1 play / pause", "deck", 15, "note", 25),
    ("d2-cue", "5", "button", "CUE", "Deck 2 cue / preview", "deck", 15, "note", 26),
    ("d2-play-toggle", "5", "button", "PLAY", "Deck 2 play / pause", "deck", 15, "note", 27),
    (
        "hint-enc2-turn",
        "1",
        "encoder",
        "ENCODER 2",
        "Column 2 top encoder turn (hint only)",
        "performance",
        15,
        "cc",
        1,
    ),
    (
        "hint-enc3-turn",
        "1",
        "encoder",
        "ENCODER 3",
        "Column 3 top encoder turn (hint only)",
        "performance",
        15,
        "cc",
        2,
    ),
    ("hint-enc-push-col1", "1", "button", "ENC 1 PUSH", "Column 1 encoder push switch", "performance", 15, "note", 52),
    ("hint-enc-push-col2", "1", "button", "ENC 2 PUSH", "Column 2 encoder push switch", "performance", 15, "note", 53),
    ("hint-enc-push-col3", "1", "button", "ENC 3 PUSH", "Column 3 encoder push switch", "performance", 15, "note", 54),
    ("hint-enc-push-col4", "1", "button", "ENC 4 PUSH", "Column 4 encoder push switch", "performance", 15, "note", 55),
    ("hint-sw1-col1", "3", "button", "SW ROW 1", "Switch under pot row 1 column 1", "performance", 15, "note", 48),
    ("hint-sw1-col2", "3", "button", "SW ROW 1", "Switch under pot row 1 column 2", "performance", 15, "note", 49),
    ("hint-sw1-col3", "3", "button", "SW ROW 1", "Switch under pot row 1 column 3", "performance", 15, "note", 50),
    ("hint-sw1-col4", "3", "button", "SW ROW 1", "Switch under pot row 1 column 4", "performance", 15, "note", 51),
    ("hint-sw2-col1", "3", "button", "SW ROW 2", "Switch under pot row 2 column 1", "performance", 15, "note", 44),
    ("hint-sw2-col2", "3", "button", "SW ROW 2", "Switch under pot row 2 column 2", "performance", 15, "note", 45),
    ("hint-sw2-col3", "3", "button", "SW ROW 2", "Switch under pot row 2 column 3", "performance", 15, "note", 46),
    ("hint-sw2-col4", "3", "button", "SW ROW 2", "Switch under pot row 2 column 4", "performance", 15, "note", 47),
    ("hint-sw3-col1", "3", "button", "SW ROW 3", "Switch under pot row 3 column 1", "performance", 15, "note", 40),
    ("hint-sw3-col4", "3", "button", "SW ROW 3", "Switch under pot row 3 column 4", "performance", 15, "note", 43),
    ("hint-layer", "6", "button", "LAYER", "Latching layer toggle (hint only)", "performance", 15, "note", 12),
    ("hint-setup-push", "6", "button", "SETUP PUSH", "Bottom-left encoder push", "performance", 15, "note", 13),
    ("hint-setup-turn", "6", "encoder", "SETUP", "Bottom-left encoder turn (hint only)", "performance", 15, "cc", 20),
    ("hint-scroll-push", "6", "button", "SEL", "Scroll encoder push", "browse", 15, "note", 14),
    ("hint-shift", "6", "button", "SHIFT", "Shift modifier (hint only)", "performance", 15, "note", 15),
]


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


def _load_trace() -> tuple[dict[str, int], dict[str, tuple[int, int, int, int]], dict[str, tuple[int, int, int, int]]]:
    data = json.loads(TRACE_DATA.read_text(encoding="utf-8"))
    plate = data["PLATE"]
    px = {k: tuple(v) for k, v in data["_PX"].items()}
    repeat = {k: tuple(v) for k, v in data["_PX_REPEAT"].items()}
    return plate, px, repeat


def _mm_box(
    px: tuple[int, int, int, int], plate: dict[str, int], mm_x: float, mm_y: float
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round((x0 - plate["x0"]) * mm_x, 1),
        round((y0 - plate["y0"]) * mm_y, 1),
        round((x1 - x0 + 1) * mm_x, 1),
        round((y1 - y0 + 1) * mm_y, 1),
    )


def _centre(px: tuple[int, int, int, int], plate: dict[str, int], mm_x: float, mm_y: float) -> tuple[float, float]:
    x0, y0, x1, y1 = px
    return (
        (x0 + x1 + 1) / 2 * mm_x,
        (y0 + y1 + 1) / 2 * mm_y,
    )


def repeat_spread_mm(
    px: dict[str, tuple[int, int, int, int]],
    repeat: dict[str, tuple[int, int, int, int]],
    plate: dict[str, int],
    mm_x: float,
    mm_y: float,
) -> float:
    worst = 0.0
    for cid, px2 in repeat.items():
        (ax, ay), (bx, by) = _centre(px[cid], plate, mm_x, mm_y), _centre(px2, plate, mm_x, mm_y)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def _aligned_groups(values: dict[str, float], tol_mm: float = 1.5) -> dict[str, float]:
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


def _controls(
    plate: dict[str, int], px: dict[str, tuple[int, int, int, int]], mm_x: float, mm_y: float
) -> list[Control]:
    ids = {r[0] for r in _CTRL_ROWS}
    if ids != set(px):
        raise SystemExit(
            f"[ERROR] trace_data ids mismatch missing={sorted(set(px) - ids)} extra={sorted(ids - set(px))}"
        )
    out: list[Control] = []
    for cid, fig, typ, label, does, section, ch, kind, num in _CTRL_ROWS:
        x, y, w, h = _mm_box(px[cid], plate, mm_x, mm_y)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), None))
    return out


def build_reference() -> dict[str, object]:
    plate, px, repeat = _load_trace()
    pw, ph = plate["x1"] - plate["x0"] + 1, plate["y1"] - plate["y0"] + 1
    mm_x, mm_y = round(PANEL_W_MM / pw, 5), round(PANEL_D_MM / ph, 5)
    controls = _controls(plate, px, mm_x, mm_y)
    spread = repeat_spread_mm(px, repeat, plate, mm_x, mm_y)
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": "Allen & Heath XONE:K2 User Guide AP8509, page 9 MIDI CONTROLS schematic",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "Issue #10892; juno-hosted mirror of AP8509 (PDF not committed)",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(plate),
            "mm_per_px": {"x": mm_x, "y": mm_y},
            "method": "Grid trace on 600 dpi page-9 render with centroid refinement; separate x/y scale to WxD.",
            "repeat_spread_mm": spread,
            "repeat_pass": {k: list(v) for k, v in repeat.items()},
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [{"id": c.id, "px": list(px[c.id]), "x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in controls],
    }


def build() -> dict[str, object]:
    plate, px, _repeat = _load_trace()
    pw, ph = plate["x1"] - plate["x0"] + 1, plate["y1"] - plate["y0"] + 1
    mm_x, mm_y = round(PANEL_W_MM / pw, 5), round(PANEL_D_MM / ph, 5)
    controls = normalise(_controls(plate, px, mm_x, mm_y))
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "2-deck MIDI controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": (
                "Allen & Heath XONE:K2 User Guide AP8509 Issue 3, Specifications (page 29): Width 358 mm, Depth 135 mm"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/allen-and-heath-xone-k2.ts",
            "positions": ("measured trace of the AP8509 page 9 schematic; reference in panel.reference.json"),
        },
        "out_of_scope": [
            "Latching layer colours and setup mode UI",
            "Vendor wordmarks on the AP8509 drawing (not reproduced in the replica skin)",
            "X:LINK RJ45 ports on the rear panel",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    spread = reference["trace"]["repeat_spread_mm"]
    n = len(doc["controls"])
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({n} controls, repeat spread {spread} mm)")
