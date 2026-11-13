"""Build the Hercules DJControl Jogvision practice-replica panel pair (ADR-0137, issue #10934).

Trace: Hercules DJControl Jogvision Quick Start Guide (DJCJogvision_QSG_EN.pdf), page 4 top-panel
figure, rendered at 300 dpi (plate crop 1308 x 1050 px; plate ink box x 70..1180, y 95..705).
Pads, SHIFT/SYNC/CUE/PLAY and the crossfader were retraced on Fri 13 Nov 2026 (issue #13558) so
no pad box covers a transport button; plate px = page px minus (78, 125).

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_djcontrol_jogvision_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 455.0
PANEL_D_MM = 254.0
DEVICE = "hercules-djcontrol-jogvision"
MAP_CONST = "HERCULES_DJCONTROL_JOGVISION_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

PLATE_PX = {"x0": 70, "x1": 1180, "y0": 95, "y1": 705}
IMAGE_PX = (1418, 1890)
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
SOURCE_PDF_SHA256 = "98c2bc3df2149aa1a5ea21387c465458201a1a89c203e13fdc82f7c9a22b1987"
SOURCE_IMAGE_SHA256 = "0424fbeacb9d766b6eb9a7869d4e0a5780ea0c3d2508683353e03e5e21b60cb3"
REPEAT_SPREAD_MM = 0.2
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10934"


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


_PX: dict[str, tuple[int, int, int, int]] = {
    "browse-encoder": (560, 285, 698, 423),
    "ch1-ch-cue": (458, 404, 532, 448),
    "ch1-eq-high": (458, 148, 532, 222),
    "ch1-eq-low": (458, 316, 532, 390),
    "ch1-eq-mid": (458, 232, 532, 306),
    "ch1-fader": (458, 458, 532, 598),
    "ch2-ch-cue": (718, 404, 792, 448),
    "ch2-eq-high": (718, 148, 792, 222),
    "ch2-eq-low": (718, 316, 792, 390),
    "ch2-eq-mid": (718, 232, 792, 306),
    "ch2-fader": (718, 458, 792, 598),
    "crossfader": (580, 600, 684, 655),
    "d1-cue": (344, 607, 395, 645),
    "d1-hot-cue-a": (90, 562, 149, 620),
    "d1-hot-cue-b": (160, 562, 219, 620),
    "d1-hot-cue-c": (90, 633, 149, 692),
    "d1-hot-cue-d": (160, 633, 219, 692),
    "d1-jog-c1-cc64": (100, 181, 399, 519),
    "d1-jog-c1-cc65": (100, 181, 399, 519),
    "d1-load-1": (538, 168, 612, 218),
    "d1-pitch": (92, 104, 129, 399),
    "d1-play-toggle": (412, 607, 465, 645),
    "d1-shift": (234, 610, 275, 645),
    "d1-sync-toggle": (290, 613, 327, 642),
    "d2-cue": (902, 607, 954, 645),
    "d2-hot-cue-a": (1042, 562, 1100, 620),
    "d2-hot-cue-b": (1114, 562, 1171, 620),
    "d2-hot-cue-c": (1042, 633, 1100, 692),
    "d2-hot-cue-d": (1114, 633, 1171, 692),
    "d2-jog-c2-cc64": (851, 181, 1150, 519),
    "d2-jog-c2-cc65": (851, 181, 1150, 519),
    "d2-load-2": (637, 168, 711, 218),
    "d2-pitch": (1121, 104, 1158, 399),
    "d2-play-toggle": (972, 607, 1024, 645),
    "d2-shift": (792, 610, 834, 645),
    "d2-sync-toggle": (848, 613, 884, 642),
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "browse-encoder": (564, 289, 694, 419),
    "ch1-eq-high": (462, 152, 528, 218),
    "crossfader": (582, 602, 682, 653),
    "d1-jog-c1-cc64": (104, 185, 395, 515),
    "d1-pitch": (96, 108, 125, 395),
    "d1-play-toggle": (413, 608, 464, 644),
    "d2-cue": (903, 608, 953, 645),
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
    cx = (x0 + x1 + 1) / 2
    cy = (y0 + y1 + 1) / 2
    return (cx * MM_PER_PX_X, cy * MM_PER_PX_Y)


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


# The unit has four physical pads per deck (QSG figure 3); slots E-H (0x14-0x17) have no pad.
_HOT_FIG = "abcd"
_HOT_SLOT = "ABCD"


def _deck(deck: 1 | 2) -> list[Control]:
    ch = deck
    out: list[Control] = []
    jog_tag = "c1" if deck == 1 else "c2"
    deck_rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        ("26", "pitch", "fader", "PITCH", "Tempo range slider", "deck", Wire(ch, "cc", 0x02), None),
        (
            "4",
            "shift",
            "button",
            "SHIFT",
            "Hold for each control's second function",
            "deck",
            Wire(ch, "note", 0x56),
            None,
        ),
        ("5", "sync-toggle", "button", "SYNC", "Beat sync on", "deck", Wire(ch, "note", 0x30), Wire(ch, "note", 0x35)),
        (
            "6",
            "cue",
            "button",
            "CUE",
            "Return to the cue point",
            "deck",
            Wire(ch, "note", 0x31),
            Wire(ch, "note", 0x36),
        ),
        (
            "7",
            "play-toggle",
            "button",
            "PLAY/PAUSE",
            "Start or pause",
            "deck",
            Wire(ch, "note", 0x32),
            Wire(ch, "note", 0x37),
        ),
        (
            "1",
            f"jog-{jog_tag}-cc64",
            "jog",
            "JOG",
            "Scratch wheel (relative CC)",
            "deck",
            Wire(ch, "cc", 0x40),
            Wire(ch, "cc", 0x42),
        ),
        (
            "1",
            f"jog-{jog_tag}-cc65",
            "jog",
            "JOG SCRATCH",
            "Scratch wheel alternate CC",
            "deck",
            Wire(ch, "cc", 0x41),
            Wire(ch, "cc", 0x43),
        ),
    ]
    for fig, tail, typ, label, does, section, wire, shift in deck_rows:
        cid = f"d{deck}-{tail}"
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, section, wire, shift))
    for i, slot in enumerate(_HOT_SLOT):
        cid = f"d{deck}-hot-cue-{_HOT_FIG[i]}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                "3",
                "pad",
                f"HOT CUE {slot}",
                f"Hot cue pad {slot} (deck {deck})",
                x,
                y,
                w,
                h,
                "performance",
                Wire(ch, "note", 0x10 + i),
                Wire(ch, "note", 0x18 + i),
            )
        )
    load_cid = f"d{deck}-load-{deck}"
    x, y, w, h = _at(load_cid)
    out.append(
        Control(
            load_cid,
            "12" if deck == 1 else "13",
            "button",
            f"LOAD {'A' if deck == 1 else 'B'}",
            f"Load the browser selection to deck {deck}",
            x,
            y,
            w,
            h,
            "browse",
            Wire(ch, "note", 0x51),
            Wire(ch, "note", 0x53),
        )
    )
    return out


def _channel(deck: 1 | 2) -> list[Control]:
    ch = deck
    shifted = {"ch-cue": Wire(ch, "note", 0x38)}
    defs = [
        ("10", "eq-high", "knob", "TREBLE", "EQ high band", Wire(ch, "cc", 0x03)),
        ("10", "eq-mid", "knob", "MEDIUM", "EQ mid band", Wire(ch, "cc", 0x04)),
        ("10", "eq-low", "knob", "BASS", "EQ low band", Wire(ch, "cc", 0x05)),
        ("14", "ch-cue", "button", "PFL", "Headphone cue for deck", Wire(ch, "note", 0x33)),
        ("15", "fader", "fader", "CHANNEL FADER", "Channel volume", Wire(ch, "cc", 0x01)),
    ]
    out: list[Control] = []
    for fig, tail, typ, label, does, wire in defs:
        cid = f"ch{deck}-{tail}"
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, "mixer", wire, shifted.get(tail)))
    return out


def _globals() -> list[Control]:
    x, y, w, h = _at("crossfader")
    cross = Control(
        "crossfader",
        "21",
        "crossfader",
        "CROSSFADER",
        "Blend deck 1 and deck 2",
        x,
        y,
        w,
        h,
        "mixer",
        Wire(1, "cc", 0x00),
        None,
    )
    x, y, w, h = _at("browse-encoder")
    browse = Control(
        "browse-encoder",
        "11",
        "encoder",
        "BROWSE",
        "Scroll the library",
        x,
        y,
        w,
        h,
        "browse",
        Wire(1, "cc", 0x4E),
        Wire(1, "cc", 0x4F),
    )
    return [cross, browse]


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


def _controls() -> list[Control]:
    controls = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    known = {c.id for c in controls}
    missing = sorted(known - set(_PX))
    extra = sorted(set(_PX) - known)
    if missing or extra:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing} extra={extra}")
    return controls


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    measured = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "Hercules DJControl Jogvision Quick Start Guide (DJCJogvision_QSG_EN.pdf), "
                "page 4 top-panel figure, 300 dpi render"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "method": (
                "Manual trace on a 50 px grid over the QSG line drawing. Pad: outer frame. "
                "Button: body outline. Knob: visible cap. Jog: outer platter ring. "
                "Fader: travel slot. Encoder: outer dial."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [
            {
                "id": c.id,
                "px": list(_PX[c.id]),
                **{k: v for k, v in asdict(c).items() if k in ("x", "y", "w", "h")},
            }
            for c in measured
        ],
    }


def build() -> dict[str, object]:
    controls = normalise(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-deck controller with in-jog displays",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": (
                "Hercules DJControl Jogvision Quick Start Guide (DJCJogvision_QSG_EN.pdf), "
                "mechanical specifications: enclosure 17.9 x 10 in (45.5 x 25.4 cm)"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-jogvision.ts",
            "positions": (
                "measured trace of the official top-panel drawing, normalised for the renderer; "
                f"reference: devices/{DEVICE}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "Firmware SHIFT rows are hints only: shifted controls pair a shift_midi wire the map leaves inert",
            "Hot cues E-H (0x14-0x17): the unit has four pads per deck, so only slots A-D get a pad",
            "LOOP, FX, SAMPLER, AIR and global utility buttons remain map hints, not panel controls",
            "Front and rear panel jacks, AIR IR sensor, and in-jog LCD bitmaps",
            "VU meters and pad MODE selectors send no bound MIDI in the P0 map",
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
