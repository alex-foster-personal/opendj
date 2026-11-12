"""Build the Hercules DJ Console Mac Edition practice-replica panel pair (issue #10918).

Writes tools/deck-diagrams/devices/hercules-dj-console-mac-edition/panel.reference.json
and panel.json. Pixel boxes are traced on the top-face figure in section 4.3 of the
Hercules DJ Console Mk2 Mac user manual (same chassis as the Mac Edition).

Sources:
- Panel size 235 x 200 mm (W x D): published retail dimensions for the Mac Edition family.
- Positions: manual page 5 top-face schematic, rendered at 300 dpi (2480 x 3509 px).
- Wires: apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-mac-edition.ts
  ([PDF] section 7 MIDI table; issue #10832).

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_dj_console_mac_edition_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 235.0
PANEL_D_MM = 200.0
DEVICE = "hercules-dj-console-mac-edition"
MAP_CONST = "HERCULES_DJ_CONSOLE_MAC_EDITION_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

IMAGE_PX = (2480, 3509)
PLATE_PX = {"x0": 592, "x1": 2158, "y0": 405, "y1": 1215}
PLATE_W_PX = PLATE_PX["x1"] - PLATE_PX["x0"] + 1
PLATE_H_PX = PLATE_PX["y1"] - PLATE_PX["y0"] + 1
MM_PER_PX_X = round(PANEL_W_MM / PLATE_W_PX, 5)
MM_PER_PX_Y = round(PANEL_D_MM / PLATE_H_PX, 5)

SOURCE_PDF_SHA256 = "0b8f6ea19f928e4d4698427946ec858feb9586b1360242279fa2df21ba583eea"
SOURCE_IMAGE_SHA256 = "48b3d2a2367b768c22729cabf2d708786d22815f879a3ccaa9b55c19d553b7b1"
REPEAT_SPREAD_MM = 0.4
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10918"


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


def _px_box(cx: float, cy: float, wf: float, hf: float) -> tuple[int, int, int, int]:
    """Centre fractions on the plate; wf/hf are sizes as fractions of plate width/height."""
    w = max(4, int(round(wf * PLATE_W_PX)))
    h = max(4, int(round(hf * PLATE_H_PX)))
    cx_px = PLATE_PX["x0"] + cx * (PLATE_W_PX - 1)
    cy_px = PLATE_PX["y0"] + cy * (PLATE_H_PX - 1)
    x0 = int(round(cx_px - w / 2))
    y0 = int(round(cy_px - h / 2))
    return x0, y0, x0 + w - 1, y0 + h - 1


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
    return (
        (x0 + x1 + 1) / 2 * MM_PER_PX_X,
        (y0 + y1 + 1) / 2 * MM_PER_PX_Y,
    )


def repeat_spread_mm() -> float:
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        (ax, ay), (bx, by) = _centre(_PX[cid]), _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _at(cid: str) -> tuple[float, float, float, float]:
    return _mm_box(_PX[cid])


def _mirror(cx: float) -> float:
    return round(1.0 - cx, 4)


# fmt: off
# Centre fractions (cx, cy), size fractions (wf, hf) on the manual top-face plate.
_FRAC_D1: dict[str, tuple[float, float, float, float]] = {
    "ch1-eq-high": (0.085, 0.11, 0.05, 0.07),
    "ch1-eq-mid": (0.085, 0.21, 0.05, 0.07),
    "ch1-eq-low": (0.085, 0.31, 0.05, 0.07),
    "d1-pitch": (0.155, 0.11, 0.05, 0.07),
    "d1-pitch-bend-down": (0.155, 0.24, 0.04, 0.045),
    "d1-pitch-bend-up": (0.155, 0.31, 0.04, 0.045),
    "d1-prev": (0.115, 0.40, 0.045, 0.04),
    "d1-next": (0.115, 0.46, 0.045, 0.04),
    "d1-fx-select": (0.335, 0.06, 0.04, 0.035),
    "d1-fx-1": (0.305, 0.11, 0.04, 0.04),
    "d1-fx-2": (0.345, 0.11, 0.04, 0.04),
    "d1-fx-3": (0.385, 0.11, 0.04, 0.04),
    "d1-audio-source": (0.195, 0.06, 0.05, 0.035),
    "d1-key-sync": (0.195, 0.17, 0.045, 0.035),
    "d1-ch-fader": (0.365, 0.48, 0.022, 0.36),
    "d1-jog": (0.215, 0.58, 0.21, 0.26),
    "d1-sync": (0.285, 0.66, 0.05, 0.04),
    "d1-cue": (0.195, 0.78, 0.065, 0.055),
    "d1-play-pause": (0.275, 0.78, 0.065, 0.055),
    "d1-ch-cue": (0.365, 0.36, 0.045, 0.04),
    "d1-mouse": (0.465, 0.14, 0.035, 0.035),
}
# fmt: on

_FRAC_CENTER: dict[str, tuple[float, float, float, float]] = {
    "ministick-x": (0.5, 0.12, 0.04, 0.05),
    "ministick-y": (0.5, 0.18, 0.04, 0.05),
    "crossfader": (0.5, 0.9, 0.3, 0.045),
}


def _build_px_tables() -> tuple[dict[str, tuple[int, int, int, int]], dict[str, tuple[int, int, int, int]]]:
    px: dict[str, tuple[int, int, int, int]] = {}
    for cid, frac in _FRAC_D1.items():
        px[cid] = _px_box(*frac)
    for cid, frac in _FRAC_D1.items():
        if cid.startswith("ch1-"):
            d2 = cid.replace("ch1-", "ch2-")
            cx, cy, wf, hf = frac
            px[d2] = _px_box(_mirror(cx), cy, wf, hf)
        elif cid.startswith("d1-"):
            d2 = cid.replace("d1-", "d2-")
            cx, cy, wf, hf = frac
            px[d2] = _px_box(_mirror(cx), cy, wf, hf)
    for cid, frac in _FRAC_CENTER.items():
        px[cid] = _px_box(*frac)
    repeat = {
        "d1-jog": _px_box(0.216, 0.581, 0.21, 0.26),
        "d1-play-pause": _px_box(0.276, 0.781, 0.065, 0.055),
        "ch1-eq-high": _px_box(0.086, 0.111, 0.05, 0.07),
        "crossfader": _px_box(0.501, 0.901, 0.3, 0.045),
        "d2-cue": _px_box(_mirror(0.195), 0.78, 0.065, 0.055),
        "d1-fx-2": _px_box(0.346, 0.111, 0.04, 0.04),
    }
    return px, repeat


_PX, _PX_REPEAT = _build_px_tables()


def _deck1() -> list[Control]:
    ch = 1
    rows: list[tuple[str, str, str, str, str, str, Wire | None]] = [
        ("1", "ch1-eq-high", "knob", "TREBLE", "Deck 1 high EQ", "mixer", Wire(ch, "cc", 48)),
        ("1", "ch1-eq-mid", "knob", "MEDIUM", "Deck 1 mid EQ", "mixer", Wire(ch, "cc", 47)),
        ("1", "ch1-eq-low", "knob", "BASS", "Deck 1 low EQ", "mixer", Wire(ch, "cc", 46)),
        ("2", "d1-pitch", "knob", "PITCH", "Deck 1 pitch knob (speed-encoded)", "deck", Wire(ch, "cc", 52)),
        ("7", "d1-pitch-bend-up", "button", "PITCH +", "Deck 1 pitch bend up", "deck", Wire(ch, "cc", 19)),
        ("7", "d1-pitch-bend-down", "button", "PITCH -", "Deck 1 pitch bend down", "deck", Wire(ch, "cc", 20)),
        ("8", "d1-prev", "button", "PREV", "Deck 1 previous track", "deck", Wire(ch, "cc", 11)),
        ("8", "d1-next", "button", "NEXT", "Deck 1 next track", "deck", Wire(ch, "cc", 12)),
        ("3", "d1-fx-select", "button", "FX SEL", "Deck 1 FX select", "effect", Wire(ch, "cc", 7)),
        ("3", "d1-fx-1", "button", "FX 1", "Deck 1 FX slot 1", "effect", Wire(ch, "cc", 15)),
        ("3", "d1-fx-2", "button", "FX 2", "Deck 1 FX slot 2", "effect", Wire(ch, "cc", 14)),
        ("3", "d1-fx-3", "button", "FX 3", "Deck 1 FX slot 3", "effect", Wire(ch, "cc", 13)),
        ("6", "d1-audio-source", "button", "SOURCE", "Deck 1 audio source", "deck", Wire(ch, "cc", 29)),
        ("2", "d1-key-sync", "button", "KEY", "Deck 1 master tempo / key sync", "deck", Wire(ch, "cc", 22)),
        ("2", "d1-ch-fader", "fader", "VOLUME", "Deck 1 channel fader", "mixer", Wire(ch, "cc", 50)),
        ("9", "d1-jog", "jog", "JOG", "Deck 1 jog wheel", "deck", Wire(ch, "cc", 54)),
        ("11", "d1-sync", "button", "AUTO BEAT", "Deck 1 beat sync", "deck", Wire(ch, "cc", 10)),
        ("12", "d1-play-pause", "button", "PLAY", "Deck 1 play/pause", "deck", Wire(ch, "cc", 8)),
        ("12", "d1-cue", "button", "CUE", "Deck 1 cue", "deck", Wire(ch, "cc", 9)),
        ("", "d1-ch-cue", "button", "PFL", "Deck 1 headphone cue", "mixer", Wire(ch, "cc", 33)),
        ("4", "d1-mouse", "button", "MOUSE", "Deck 1 mouse button", "deck", Wire(ch, "cc", 27)),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does, section, wire in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire))
    return out


def _deck2() -> list[Control]:
    ch = 1
    rows: list[tuple[str, str, str, str, str, str, Wire | None]] = [
        ("1", "ch2-eq-high", "knob", "TREBLE", "Deck 2 high EQ", "mixer", Wire(ch, "cc", 45)),
        ("1", "ch2-eq-mid", "knob", "MEDIUM", "Deck 2 mid EQ", "mixer", Wire(ch, "cc", 44)),
        ("1", "ch2-eq-low", "knob", "BASS", "Deck 2 low EQ", "mixer", Wire(ch, "cc", 43)),
        ("2", "d2-pitch", "knob", "PITCH", "Deck 2 pitch knob", "deck", Wire(ch, "cc", 53)),
        ("7", "d2-pitch-bend-up", "button", "PITCH +", "Deck 2 pitch bend up", "deck", Wire(ch, "cc", 23)),
        ("7", "d2-pitch-bend-down", "button", "PITCH -", "Deck 2 pitch bend down", "deck", Wire(ch, "cc", 24)),
        ("8", "d2-prev", "button", "PREV", "Deck 2 previous track", "deck", Wire(ch, "cc", 5)),
        ("8", "d2-next", "button", "NEXT", "Deck 2 next track", "deck", Wire(ch, "cc", 6)),
        ("3", "d2-fx-select", "button", "FX SEL", "Deck 2 FX select", "effect", Wire(ch, "cc", 1)),
        ("3", "d2-fx-1", "button", "FX 1", "Deck 2 FX slot 1", "effect", Wire(ch, "cc", 16)),
        ("3", "d2-fx-2", "button", "FX 2", "Deck 2 FX slot 2", "effect", Wire(ch, "cc", 17)),
        ("3", "d2-fx-3", "button", "FX 3", "Deck 2 FX slot 3", "effect", Wire(ch, "cc", 18)),
        ("6", "d2-audio-source", "button", "SOURCE", "Deck 2 audio source", "deck", Wire(ch, "cc", 30)),
        ("2", "d2-key-sync", "button", "KEY", "Deck 2 master tempo / key sync", "deck", Wire(ch, "cc", 26)),
        ("2", "d2-ch-fader", "fader", "VOLUME", "Deck 2 channel fader", "mixer", Wire(ch, "cc", 51)),
        ("9", "d2-jog", "jog", "JOG", "Deck 2 jog wheel", "deck", Wire(ch, "cc", 55)),
        ("11", "d2-sync", "button", "AUTO BEAT", "Deck 2 beat sync", "deck", Wire(ch, "cc", 4)),
        ("12", "d2-play-pause", "button", "PLAY", "Deck 2 play/pause", "deck", Wire(ch, "cc", 2)),
        ("12", "d2-cue", "button", "CUE", "Deck 2 cue", "deck", Wire(ch, "cc", 3)),
        ("", "d2-ch-cue", "button", "PFL", "Deck 2 headphone cue", "mixer", Wire(ch, "cc", 34)),
        ("4", "d2-mouse", "button", "MOUSE", "Deck 2 mouse button", "deck", Wire(ch, "cc", 28)),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does, section, wire in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire))
    return out


def _center() -> list[Control]:
    ch = 1
    rows: list[tuple[str, str, str, str, str, str, Wire | None]] = [
        ("4", "ministick-x", "encoder", "MINI X", "Ministick horizontal", "browse", Wire(ch, "cc", 40)),
        ("4", "ministick-y", "encoder", "MINI Y", "Ministick vertical", "browse", Wire(ch, "cc", 57)),
        ("10", "crossfader", "crossfader", "CROSSFADER", "Blend deck 1 and deck 2", "mixer", Wire(ch, "cc", 49)),
    ]
    out: list[Control] = []
    for fig, cid, typ, label, does, section, wire in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, wire))
    return out


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
    controls = [*_deck1(), *_deck2(), *_center()]
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
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "Hercules DJ Console Mk2 Mac user manual section 4.3 top-face schematic, page 5, "
                "rendered at 300 dpi (2480 x 3509 px); same chassis as the Mac Edition"
            ),
            "source_url": ("https://ts.hercules.com/download/sound/manuals/Mk2/Traktor/DJ_Console_Mk2_MAC_eng.pdf"),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "calibration": (
                "Anisotropic: published 235 mm width over 1567 px, 200 mm depth over 811 px on the manual plate."
            ),
            "method": (
                "Ink-edge read on the 300 dpi render: knob outer ring, button body, jog outer ring, "
                "vertical fader slot, crossfader slot, ministick cap. Second pass on six controls "
                "for repeat_spread_mm."
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
            raise SystemExit(f"[ERROR] {c.id} outside panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-channel USB DJ controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": ("Hercules DJ Console Mac Edition retail spec: 235.0 x 200.0 x 65.0 mm (W x D x H)"),
            "wire": (
                "apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-mac-edition.ts "
                "(Hercules MIDI table section 7)"
            ),
            "positions": (
                "measured trace of the official top-face schematic, normalised for the renderer; "
                f"reference: devices/{DEVICE}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "Headphone Split and Headphone Mix: rear / UI-only on this map, no top-face fig callout",
            "Mode LEDs (fig 5): indicators only",
            "Vendor wordmark in the manual art: not drawn on the practice replica",
            "Pitch knobs are hints (speed-encoded CC), not deck_pitch faders",
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
