"""Build the Hercules DJControl Compact practice-replica panel pair (ADR-0137, issue #10929).

Writes tools/deck-diagrams/devices/hercules-djcontrol-compact/panel.reference.json and panel.json.

Sources:
- Panel size 340.0 x 100.0 mm: Hercules product specifications (13.4 x 3.9 in).
- Top-panel schematic: DJControl Compact MIDI Mapping Version 1.1 PDF page 1 diagram
  (sha256 3c42f8172aac1568a95df90cfba998180131dd0e888113a2592ad28ce1c887c2).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-compact.ts.

Run: uv run python tools/deck-diagrams/scripts/build_hercules_djcontrol_compact_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 340.0
PANEL_D_MM = 100.0
DEVICE = "hercules-djcontrol-compact"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "3c42f8172aac1568a95df90cfba998180131dd0e888113a2592ad28ce1c887c2"
SOURCE_IMAGE_SHA256 = "37bebc3fcc8f108c0e7d064d48386b99dfb701d0e241d1aca94fdb7e5a2a5f3c"
IMAGE_PX = (2760, 817)
PLATE_PX = {"x0": 0, "x1": 2759, "y0": 0, "y1": 816}
MM_PER_PX_X = round(PANEL_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = MM_PER_PX_X
REPEAT_SPREAD_MM = 0.6
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10929"


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
    "d1-jog": (52, 205, 547, 644),
    "d2-jog": (2212, 205, 2707, 644),
    "d1-sync-toggle": (100, 620, 238, 759),
    "d1-cue": (240, 620, 378, 759),
    "d1-play-toggle": (380, 620, 518, 759),
    "d2-play-toggle": (2390, 620, 2528, 759),
    "d2-cue": (2530, 620, 2668, 759),
    "d2-sync-toggle": (2670, 620, 2708, 759),
    "d1-pad-1": (759, 488, 840, 538),
    "d1-pad-2": (884, 488, 978, 538),
    "d1-pad-3": (759, 555, 860, 628),
    "d1-pad-4": (877, 555, 978, 628),
    "d2-pad-1": (1796, 488, 1873, 538),
    "d2-pad-2": (1898, 488, 2000, 538),
    "d2-pad-3": (1780, 555, 1881, 628),
    "d2-pad-4": (1898, 555, 2000, 628),
    "ch1-fader": (1000, 50, 1121, 187),
    "ch2-fader": (1638, 50, 1758, 187),
    "ch1-eq-mid": (1244, 125, 1343, 234),
    "ch1-eq-low": (1244, 317, 1343, 426),
    "ch2-eq-mid": (1416, 125, 1513, 234),
    "ch2-eq-low": (1416, 317, 1513, 426),
    "crossfader": (1197, 680, 1467, 740),
    "shift-modifier": (2071, 280, 2138, 359),
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-jog": (54, 207, 549, 646),
    "d1-play-toggle": (382, 622, 520, 761),
    "d1-pad-2": (886, 490, 980, 540),
    "ch1-eq-mid": (1246, 127, 1345, 236),
    "crossfader": (1199, 682, 1469, 742),
    "d2-jog": (2214, 207, 2709, 646),
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
        ax, ay = _centre(_PX[cid])
        bx, by = _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _at(cid: str) -> tuple[float, float, float, float]:
    return _mm_box(_PX[cid])


def _deck_offset(deck: int) -> int:
    return 0x30 if deck == 2 else 0


PAD_LAYER_MODES: dict[str, tuple[str, int | None, int | None]] = {
    "hot_cue": ("HOT CUE", 0x01, 0x05),
    "instant_fx": ("FX", 0x09, None),
    "sampler": ("SAMPLER", None, None),
    "beat_loop": ("LOOP", 0x19, 0x1D),
}


def _pad_layers(deck: int, pad: int) -> dict[str, object]:
    o = _deck_offset(deck)
    idx = pad - 1
    out: dict[str, object] = {}
    for mode, (label, base, shift) in PAD_LAYER_MODES.items():
        layer: dict[str, object] = {
            "label": f"{label} PAD {pad}",
            "does": f"{label} pad {pad} (deck {deck})",
        }
        if base is not None:
            layer["midi"] = asdict(Wire(1, "note", base + idx + o))
        else:
            layer["midi"] = None
        if shift is not None:
            layer["shift_midi"] = asdict(Wire(1, "note", shift + idx + o))
        else:
            layer["shift_midi"] = None
        out[mode] = layer
    return out


def _deck(deck: int) -> list[Control]:
    o = _deck_offset(deck)
    rows: list[tuple[str, str, str, str, str, str, Wire, Wire | None]] = [
        (
            "jog",
            "JOG",
            "jog",
            "JOG",
            "Scratch or nudge; shifted jog is browse",
            "deck",
            Wire(1, "cc", 0x30 if deck == 1 else 0x31),
            Wire(1, "cc", 0x37 if deck == 1 else 0x38),
        ),
        (
            "sync-toggle",
            "SYNC",
            "button",
            "SYNC",
            "Beat sync on/off",
            "deck",
            Wire(1, "note", 0x23 + o),
            None,
        ),
        ("cue", "CUE", "button", "CUE", "Cue point", "deck", Wire(1, "note", 0x22 + o), None),
        (
            "play-toggle",
            "PLAY",
            "button",
            "PLAY/PAUSE",
            "Start or pause",
            "deck",
            Wire(1, "note", 0x21 + o),
            None,
        ),
    ]
    out: list[Control] = []
    for tail, fig, typ, label, does, section, midi, shift in rows:
        cid = f"d{deck}-{tail}"
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, section, midi, shift))
    for pad in range(1, 5):
        cid = f"d{deck}-pad-{pad}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                str(pad),
                "pad",
                f"PAD {pad}",
                f"Performance pad {pad} (deck {deck}; MODE selects CUE/FX/SAM/LOOP)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(1, "note", 0x01 + pad - 1 + o),
                Wire(1, "note", 0x05 + pad - 1 + o),
            )
        )
    return out


def _mixer() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, Wire]] = [
        ("ch1-fader", "VOL-DA", "knob", "VOL", "Deck A volume", "mixer", Wire(1, "cc", 0x39)),
        ("ch2-fader", "VOL-DB", "knob", "VOL", "Deck B volume", "mixer", Wire(1, "cc", 0x3D)),
        ("ch1-eq-mid", "MEDIUM-DA", "knob", "MID", "Deck A mid EQ", "mixer", Wire(1, "cc", 0x3B)),
        ("ch1-eq-low", "BASS-DA", "knob", "BASS", "Deck A bass EQ", "mixer", Wire(1, "cc", 0x3C)),
        ("ch2-eq-mid", "MEDIUM-DB", "knob", "MID", "Deck B mid EQ", "mixer", Wire(1, "cc", 0x3F)),
        ("ch2-eq-low", "BASS-DB", "knob", "BASS", "Deck B bass EQ", "mixer", Wire(1, "cc", 0x40)),
        ("crossfader", "XFADER", "crossfader", "CROSSFADER", "Blend decks", "mixer", Wire(1, "cc", 0x36)),
    ]
    out: list[Control] = []
    for cid, fig, typ, label, does, section, midi in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, midi, None))
    return out


def _global() -> list[Control]:
    x, y, w, h = _at("shift-modifier")
    return [
        Control(
            "shift-modifier",
            "SH",
            "button",
            "SHIFT",
            "Hardware shift; each pad and jog has a shifted twin on other notes",
            x,
            y,
            w,
            h,
            "deck",
            Wire(1, "note", 0x2F),
            None,
        )
    ]


def _snap_half(v: float) -> float:
    return round(v * 2) / 2


def _aligned_groups(values: dict[str, float], tol_mm: float = 1.2) -> dict[str, float]:
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
    controls = [*_deck(1), *_deck(2), *_mixer(), *_global()]
    known = {c.id for c in controls}
    if set(_PX) != known:
        missing = sorted(known - set(_PX))
        extra = sorted(set(_PX) - known)
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing} extra={extra}")
    return controls


def _control_document(control: Control) -> dict[str, object]:
    row: dict[str, object] = asdict(control)
    if control.type == "pad":
        deck = int(control.id[1])
        pad = int(control.id.rsplit("-", 1)[1])
        row["pad_layer_group"] = f"d{deck}"
        row["pad_layers"] = _pad_layers(deck, pad)
    return row


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    raw_controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "Hercules DJControl Compact MIDI Mapping Version 1.1 PDF page 1 "
                "embedded top-panel schematic (2760 x 817 px at 6x render)"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "method": (
                "Button: body bounding box. Knob: visible cap. Pad: pad square. "
                "Jog: outer ring. Crossfader: slot with cap. VOL is the deck volume pot."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "repeat_pass": [
                "d1-jog",
                "d1-play-toggle",
                "d1-pad-2",
                "ch1-eq-mid",
                "crossfader",
                "d2-jog",
            ],
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
            for c in raw_controls
        ],
    }


def build() -> dict[str, object]:
    controls = normalise(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-channel portable DJ controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "HERCULES_DJCONTROL_COMPACT_MAP",
        "sources": {
            "panel_size": ("Hercules DJControl Compact product specifications: 13.4 x 3.9 in / 340 x 100 mm (W x D)"),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-compact.ts",
            "positions": (
                "measured trace of the official schematic, normalised for the renderer; "
                "reference: devices/hercules-djcontrol-compact/panel.reference.json"
            ),
        },
        "out_of_scope": [
            "MODE, RECORD and AUTO are physical but hint-only in the live map",
            "SAMPLER pad MIDI rows are hints; pads render inert in SAMPLER layer",
            "shifted FX pad rows are hints",
            "shifted transport on PLAY/CUE/SYNC is hardware-encoded on other notes and stays hint-only",
        ],
        "pad_layer_groups": {
            f"d{deck}": {
                "label": f"Deck {deck} pad layer",
                "default_mode": "hot_cue",
                "modes": [{"id": mode, "label": row[0]} for mode, row in PAD_LAYER_MODES.items()],
            }
            for deck in (1, 2)
        },
        "controls": [_control_document(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    panel = build()
    PANEL_OUT.write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    n = len(panel["controls"])
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({n} controls, repeat spread {REPEAT_SPREAD_MM} mm)")
