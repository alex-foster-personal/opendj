"""Build the Hercules DJ Console RMX 2 practice-replica panel pair (ADR-0137, issue #10921).

Writes tools/deck-diagrams/devices/hercules-dj-console-rmx-2/panel.reference.json (trace of
record) and panel.json (normalised layout). Positions are traced on a crop of the official
Poster_DJCRmx2-EN.pdf top-panel line drawing (callouts 1-30 on the upper schematic).

Sources:
- Panel size 350 x 250 mm: Hercules DJConsole Rmx2 product sheet (Envolvente 35 x 25 cm).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-rmx-2.ts
  (RMX2_MIDI_Commands.pdf Appendix). Multifunction pads fold to one hit target per pad
  (default hot-cue wire); jog scratch pairs with jog via shift_midi.
- Positions: manual trace on a 2280 x 720 px crop of poster page 1 (250 dpi render).

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_dj_console_rmx_2_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 350.0
PANEL_D_MM = 250.0
DEVICE = "hercules-dj-console-rmx-2"
MAP_CONST = "HERCULES_DJ_CONSOLE_RMX_2_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "52a41b2a8f814e60f3675ee74d243d48cd40c6f95852eb426c0b69de41dbf3ef"
SOURCE_IMAGE_SHA256 = "9e9687cb7eb941046321a838c5558d2a11612f6201864a5cefad3f4420082c8d"
IMAGE_PX = (2280, 720)
PLATE_PX = {"x0": 0, "x1": 2279, "y0": 0, "y1": 719}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
REPEAT_SPREAD_MM = 0.4
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10921"


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
    midi: Wire
    shift_midi: Wire | None = None


# Inclusive pixel boxes on the 2280 x 720 poster crop [x0, y0, x1, y1].
_PX: dict[str, tuple[int, int, int, int]] = {
    "d1-pitch": (30, 95, 68, 585),
    "d1-tempo-nudge-minus": (72, 598, 122, 652),
    "d1-tempo-nudge-plus": (72, 662, 122, 716),
    "d1-jog": (195, 345, 478, 628),
    "d1-pad-1": (148, 125, 212, 189),
    "d1-pad-2": (222, 125, 286, 189),
    "d1-pad-3": (148, 198, 212, 262),
    "d1-pad-4": (222, 198, 286, 262),
    "d1-play-toggle": (188, 662, 248, 712),
    "d1-cue": (262, 662, 322, 712),
    "d1-sync-toggle": (336, 662, 396, 712),
    "d1-load": (928, 525, 1008, 572),
    "ch1-trim": (698, 42, 753, 97),
    "ch1-eq-high": (692, 122, 747, 177),
    "ch1-eq-mid": (692, 200, 747, 255),
    "ch1-eq-low": (692, 278, 747, 333),
    "ch1-fader": (718, 395, 768, 588),
    "master": (838, 42, 893, 97),
    "crossfader": (915, 650, 1315, 700),
    "phones-mix": (2048, 42, 2103, 97),
    "ch2-trim": (1527, 42, 1582, 97),
    "ch2-eq-high": (1533, 122, 1588, 177),
    "ch2-eq-mid": (1533, 200, 1588, 255),
    "ch2-eq-low": (1533, 278, 1588, 333),
    "ch2-fader": (1512, 395, 1562, 588),
    "d2-load": (1272, 525, 1352, 572),
    "d2-pitch": (2212, 95, 2250, 585),
    "d2-tempo-nudge-minus": (2158, 598, 2208, 652),
    "d2-tempo-nudge-plus": (2158, 662, 2208, 716),
    "d2-jog": (1802, 345, 2085, 628),
    "d2-pad-1": (1992, 125, 2056, 189),
    "d2-pad-2": (1917, 125, 1981, 189),
    "d2-pad-3": (1992, 198, 2056, 262),
    "d2-pad-4": (1917, 198, 1981, 262),
    "d2-play-toggle": (1992, 662, 2052, 712),
    "d2-cue": (1917, 662, 1977, 712),
    "d2-sync-toggle": (1842, 662, 1902, 712),
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-jog": (196, 346, 479, 629),
    "d1-play-toggle": (189, 663, 249, 713),
    "ch1-eq-high": (693, 123, 748, 178),
    "crossfader": (916, 651, 1316, 701),
    "d2-pad-2": (1918, 126, 1982, 190),
    "d2-pitch": (2213, 96, 2251, 586),
    "master": (839, 43, 894, 98),
    "phones-mix": (2049, 43, 2104, 98),
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


def _deck(deck: int) -> list[Control]:
    w = 0x20 if deck == 1 else 0x31
    pad0 = 0x09 if deck == 1 else 0x19
    out: list[Control] = [
        Control(
            f"d{deck}-play-toggle",
            "30",
            "button",
            "PLAY/PAUSE",
            f"Play or pause deck {deck}",
            *_at(f"d{deck}-play-toggle"),
            "deck",
            Wire(1, "note", w + 0x01),
        ),
        Control(
            f"d{deck}-cue",
            "29",
            "button",
            "CUE",
            f"Temporary cue deck {deck}",
            *_at(f"d{deck}-cue"),
            "deck",
            Wire(1, "note", w + 0x02),
        ),
        Control(
            f"d{deck}-sync-toggle",
            "28",
            "button",
            "SYNC",
            f"Beat sync deck {deck}",
            *_at(f"d{deck}-sync-toggle"),
            "deck",
            Wire(1, "note", w + 0x03),
        ),
        Control(
            f"d{deck}-tempo-nudge-minus",
            "3",
            "button",
            "PITCH -",
            f"Pitch bend minus deck {deck}",
            *_at(f"d{deck}-tempo-nudge-minus"),
            "deck",
            Wire(1, "note", w + 0x0C),
        ),
        Control(
            f"d{deck}-tempo-nudge-plus",
            "3",
            "button",
            "PITCH +",
            f"Pitch bend plus deck {deck}",
            *_at(f"d{deck}-tempo-nudge-plus"),
            "deck",
            Wire(1, "note", w + 0x0D),
        ),
        Control(
            f"d{deck}-jog",
            "1",
            "jog",
            "JOG",
            f"Jog wheel deck {deck}",
            *_at(f"d{deck}-jog"),
            "deck",
            Wire(1, "cc", 0x30 if deck == 1 else 0x31),
            Wire(1, "cc", 0x32 if deck == 1 else 0x33),
        ),
        Control(
            f"d{deck}-pitch",
            "4",
            "fader",
            "PITCH",
            f"Pitch fader deck {deck}",
            *_at(f"d{deck}-pitch"),
            "deck",
            Wire(1, "cc", 0x36 if deck == 1 else 0x38),
        ),
    ]
    for i in range(4):
        slot = ("A", "B", "C", "D")[i]
        out.append(
            Control(
                f"d{deck}-pad-{i + 1}",
                "24",
                "pad",
                f"PAD {i + 1}",
                f"Multifunction pad {i + 1} deck {deck} (hot-cue wire; other pad modes unwired)",
                *_at(f"d{deck}-pad-{i + 1}"),
                "performance",
                Wire(1, "note", pad0 + i),
                Wire(1, "cc", 0x01 + i if deck == 1 else 0x11 + i),
            )
        )
    out.append(
        Control(
            f"d{deck}-load",
            "16",
            "button",
            f"LOAD {'A' if deck == 1 else 'B'}",
            f"Load browser selection to deck {deck}",
            *_at(f"d{deck}-load"),
            "browse",
            Wire(1, "note", 0x24 if deck == 1 else 0x35),
        )
    )
    return out


def _channel(ch: int) -> list[Control]:
    base = 0x3A if ch == 1 else 0x4A
    fig_gain = "10"
    fig_eq = "14"
    fig_fader = "19"
    return [
        Control(
            f"ch{ch}-fader",
            fig_fader,
            "fader",
            "CHANNEL",
            f"Channel {ch} volume fader",
            *_at(f"ch{ch}-fader"),
            "mixer",
            Wire(1, "cc", base),
        ),
        Control(
            f"ch{ch}-eq-high",
            fig_eq,
            "knob",
            "HIGH",
            f"Channel {ch} treble EQ",
            *_at(f"ch{ch}-eq-high"),
            "mixer",
            Wire(1, "cc", base + 2),
        ),
        Control(
            f"ch{ch}-eq-mid",
            fig_eq,
            "knob",
            "MID",
            f"Channel {ch} mid EQ",
            *_at(f"ch{ch}-eq-mid"),
            "mixer",
            Wire(1, "cc", base + 4),
        ),
        Control(
            f"ch{ch}-eq-low",
            fig_eq,
            "knob",
            "LOW",
            f"Channel {ch} bass EQ",
            *_at(f"ch{ch}-eq-low"),
            "mixer",
            Wire(1, "cc", base + 6),
        ),
        Control(
            f"ch{ch}-trim",
            fig_gain,
            "knob",
            "GAIN",
            f"Channel {ch} gain",
            *_at(f"ch{ch}-trim"),
            "mixer",
            Wire(1, "cc", base + 8),
        ),
    ]


def _globals() -> list[Control]:
    return [
        Control(
            "master",
            "11",
            "knob",
            "MAIN",
            "Master output level",
            *_at("master"),
            "mixer",
            Wire(1, "cc", 0x44),
        ),
        Control(
            "phones-mix",
            "21",
            "knob",
            "CUE/MIX",
            "Headphone cue to mix blend",
            *_at("phones-mix"),
            "mixer",
            Wire(1, "cc", 0x46),
        ),
        Control(
            "crossfader",
            "20",
            "crossfader",
            "CROSSFADER",
            "Crossfader between decks",
            *_at("crossfader"),
            "mixer",
            Wire(1, "cc", 0x48),
        ),
    ]


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
    missing_pos = sorted(known - set(_PX))
    extra_pos = sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    unknown_repeat = sorted(set(_PX_REPEAT) - known)
    if unknown_repeat:
        raise SystemExit(f"[ERROR] repeat trace names unknown controls: {unknown_repeat}")
    return controls


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if abs(spread - REPEAT_SPREAD_MM) > 0.15:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "Poster_DJCRmx2-EN.pdf page 1, top-panel schematic (callouts 1-30), 2280 x 720 px crop at 250 dpi"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "https://ts.hercules.com/download/sound/manuals/Rmx2/Poster/Poster_DJCRmx2-EN.pdf",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "calibration": (
                "Independent x and y scales map the poster crop to the 350 x 250 mm product "
                "sheet envelope (35 x 25 cm)."
            ),
            "method": (
                "Manual trace on the poster crop with a 50 px grid. Jog: outer ring. "
                "Pads: outer pad square. Knobs: visible cap. Faders: travel slot width."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "repeat_pass": {cid: list(box) for cid, box in _PX_REPEAT.items()},
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [
            {
                "id": c.id,
                "px": list(_PX[c.id]),
                "x": _mm_box(_PX[c.id])[0],
                "y": _mm_box(_PX[c.id])[1],
                "w": _mm_box(_PX[c.id])[2],
                "h": _mm_box(_PX[c.id])[3],
            }
            for c in controls
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
        "display_name": "2-deck controller with audio interface",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": ("Hercules DJConsole Rmx2 product sheet (Especificaciones mecanicas: Envolvente 35 x 25 cm)"),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-rmx-2.ts",
            "positions": (
                "measured trace of Poster_DJCRmx2-EN.pdf top schematic; "
                "devices/hercules-dj-console-rmx-2/panel.reference.json"
            ),
        },
        "out_of_scope": [
            "pad modes other than hot cue: one hit target per pad carries the hot-cue wire",
            "shift pad CC rows pair as shift_midi on each pad",
            "browse arrow cluster, mic, kill, source, preview, mode encoders, VU and rear I/O",
            "MSB LSB companion CCs: each fader or knob carries its MSB only",
            "rear and front panels on the poster (callouts 31-39)",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    panel = build()
    PANEL_OUT.write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    n = len(panel["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {REFERENCE_OUT.name} and {PANEL_OUT.name} ({n} controls)")
