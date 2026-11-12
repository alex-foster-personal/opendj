"""Build the Kontrol DJ KDJ500 practice-replica panel pair (ADR-0137, issue #10936).

Writes tools/deck-diagrams/devices/kontrol-dj-kdj500/panel.reference.json (measurement) and
panel.json (normalised layout). Positions are traced on the manufacturer's Konverter template
\"Traktor Dj v3 (FX)\" overlay (kdj_traktor3_template.pdf). The PDF draws the left deck and its
mixer strip; deck 2 is a horizontal mirror on the 400 mm panel.

Sources:
- Panel size 400 x 170 mm (W x D): KDJ500 user manual Part III technical specifications.
- Positions: first trace on the rendered template PDF (1653 x 2339 px at 200 dpi).
- Wires: apps/webui/frontend/src/lib/rb/midi/maps/kontrol-dj-kdj500.ts (Konverter [T3]).
- PH. VOL. / FX KNOB 1 is one knob: trim (CC 0x01) with bank-2 filter (CC 0x0E) on shift_midi.

Run: uv run --locked python tools/deck-diagrams/scripts/build_kontrol_dj_kdj500_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 400.0
PANEL_D_MM = 170.0
HALF_W_MM = PANEL_W_MM / 2
DEVICE = "kontrol-dj-kdj500"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "203e530aaf8663df61ae5df7742a1938c6b981b03bb5a6a8f18a25a5fcbb114d"
SOURCE_IMAGE_SHA256 = "a191d472aaabed372f84b252421b59d0ada682d6101cada81d2a6e414a4312da"
IMAGE_PX = (1653, 2339)
PLATE_PX = {"x0": 79, "x1": 1652, "y0": 503, "y1": 1840}
MM_PER_PX_X = round(HALF_W_MM / (PLATE_PX["x1"] - PLATE_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PLATE_PX["y1"] - PLATE_PX["y0"] + 1), 5)
PLATE_ROW_SCALE_MM_PER_PX = MM_PER_PX_Y
REPEAT_SPREAD_MM = 0.4
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10936"

CH = 1


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


def _mirror_control(c: Control) -> Control:
    x = round(PANEL_W_MM - (c.x + c.w), 1)
    return replace(c, x=x)


# Inclusive pixel boxes on the rendered [T3] PDF (full image coordinates).
_PX: dict[str, tuple[int, int, int, int]] = {
    "d1-beat-loop-1": (277, 541, 357, 621),
    "d1-beat-loop-4": (397, 541, 477, 621),
    "d1-beat-loop-8": (517, 541, 597, 621),
    "d1-beat-loop-16": (637, 541, 717, 621),
    "d1-cue": (397, 671, 477, 751),
    "d1-play-toggle": (597, 671, 677, 751),
    "d1-sync-toggle": (1374, 548, 1414, 623),
    "d1-load-1": (817, 681, 897, 761),
    "d1-pitch": (947, 701, 1017, 1401),
    "d1-jog": (317, 951, 777, 1411),
    "ch1-ch-cue": (1297, 531, 1367, 621),
    "ch1-trim": (1147, 721, 1227, 801),
    "ch1-eq-high": (1147, 841, 1227, 921),
    "ch1-eq-mid": (1147, 961, 1227, 1041),
    "ch1-eq-low": (1147, 1081, 1227, 1161),
    "ch1-fader": (1367, 721, 1427, 1401),
    "crossfader": (937, 1721, 1417, 1791),
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-play-toggle": (599, 673, 679, 753),
    "d1-jog": (319, 953, 779, 1413),
    "ch1-trim": (1149, 723, 1229, 803),
    "crossfader": (939, 1723, 1419, 1793),
}


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


def _deck1() -> list[Control]:
    x, y, w, h = _at("d1-play-toggle")
    return [
        Control(
            "d1-play-toggle",
            "PLAY",
            "button",
            "PLAY/PAUSE",
            "Start or pause deck 1",
            x,
            y,
            w,
            h,
            "deck",
            Wire(CH, "note", 0x10),
            Wire(CH, "note", 0x11),
        ),
        Control(
            "d1-cue",
            "CUE",
            "button",
            "CUE",
            "Cue deck 1",
            *_at("d1-cue"),
            "deck",
            Wire(CH, "note", 0x0E),
            Wire(CH, "note", 0x0F),
        ),
        Control(
            "d1-sync-toggle",
            "SYNC",
            "button",
            "SYNC",
            "Beat sync deck 1 (chart CUE area)",
            *_at("d1-sync-toggle"),
            "deck",
            Wire(CH, "note", 0x2D),
            None,
        ),
        Control(
            "d1-beat-loop-1",
            "1",
            "button",
            "LOOP 1",
            "1-beat loop (SHIFT layer on FX BUT. 1)",
            *_at("d1-beat-loop-1"),
            "deck",
            Wire(CH, "note", 0x09),
            None,
        ),
        Control(
            "d1-beat-loop-4",
            "4",
            "button",
            "LOOP 4",
            "4-beat loop",
            *_at("d1-beat-loop-4"),
            "deck",
            Wire(CH, "note", 0x0A),
            None,
        ),
        Control(
            "d1-beat-loop-8",
            "8",
            "button",
            "LOOP 8",
            "8-beat loop",
            *_at("d1-beat-loop-8"),
            "deck",
            Wire(CH, "note", 0x0B),
            None,
        ),
        Control(
            "d1-beat-loop-16",
            "16",
            "button",
            "LOOP 16",
            "16-beat loop",
            *_at("d1-beat-loop-16"),
            "deck",
            Wire(CH, "note", 0x0C),
            None,
        ),
        Control(
            "d1-pitch",
            "PITCH",
            "fader",
            "TEMPO",
            "Pitch fader deck 1",
            *_at("d1-pitch"),
            "deck",
            Wire(CH, "cc", 0x09),
            Wire(CH, "cc", 0x23),
        ),
        Control(
            "d1-jog",
            "JOG",
            "jog",
            "JOG",
            "Jog wheel deck 1 (VINYL CC)",
            *_at("d1-jog"),
            "deck",
            Wire(CH, "cc", 0x28),
            Wire(CH, "cc", 0x29),
        ),
        Control(
            "d1-load-1",
            "LOAD",
            "button",
            "LOAD",
            "Load to deck 1 (track encoder push)",
            *_at("d1-load-1"),
            "browse",
            Wire(CH, "note", 0x14),
            Wire(CH, "note", 0x15),
        ),
    ]


def _channel1() -> list[Control]:
    return [
        Control(
            "ch1-ch-cue",
            "CUE",
            "button",
            "CH CUE",
            "Headphone cue channel 1 (round CUE)",
            *_at("ch1-ch-cue"),
            "mixer",
            Wire(CH, "note", 0x2C),
            None,
        ),
        Control(
            "ch1-trim",
            "PH-VOL",
            "knob",
            "TRIM",
            "Trim / PH. VOL.; bank 2 is FX KNOB 1 (filter)",
            *_at("ch1-trim"),
            "mixer",
            Wire(CH, "cc", 0x01),
            Wire(CH, "cc", 0x0E),
        ),
        Control(
            "ch1-eq-high",
            "HIGH",
            "knob",
            "HIGH",
            "EQ high band channel 1",
            *_at("ch1-eq-high"),
            "mixer",
            Wire(CH, "cc", 0x02),
            Wire(CH, "cc", 0x1C),
        ),
        Control(
            "ch1-eq-mid",
            "MID",
            "knob",
            "MID",
            "EQ mid channel 1",
            *_at("ch1-eq-mid"),
            "mixer",
            Wire(CH, "cc", 0x03),
            Wire(CH, "cc", 0x1D),
        ),
        Control(
            "ch1-eq-low",
            "LOW",
            "knob",
            "LOW",
            "EQ low channel 1",
            *_at("ch1-eq-low"),
            "mixer",
            Wire(CH, "cc", 0x04),
            Wire(CH, "cc", 0x1E),
        ),
        Control(
            "ch1-fader",
            "VOL",
            "fader",
            "CH FADER",
            "Channel fader 1",
            *_at("ch1-fader"),
            "mixer",
            Wire(CH, "cc", 0x0B),
            Wire(CH, "cc", 0x25),
        ),
    ]


def _deck2_and_ch2(base: list[Control], ch: list[Control]) -> tuple[list[Control], list[Control]]:
    d2 = []
    ch2 = []
    rewires = {
        "d1-play-toggle": (0x26, 0x27),
        "d1-cue": (0x24, 0x25),
        "d1-sync-toggle": (0x2F, None),
        "d1-beat-loop-1": (0x1F, None),
        "d1-beat-loop-4": (0x20, None),
        "d1-beat-loop-8": (0x21, None),
        "d1-beat-loop-16": (0x22, None),
        "d1-pitch": ((0x0A, 0x24), "cc"),
        "d1-jog": ((0x2C, 0x2D), "cc"),
        "d1-load-1": (0x28, 0x29),
        "ch1-ch-cue": (0x2E, None),
        "ch1-trim": ((0x05, 0x12), "cc"),
        "ch1-eq-high": ((0x06, 0x20), "cc"),
        "ch1-eq-mid": ((0x07, 0x21), "cc"),
        "ch1-eq-low": ((0x08, 0x22), "cc"),
        "ch1-fader": ((0x0C, 0x26), "cc"),
    }
    for c in base:
        mid = c.id.replace("d1-", "d2-")
        if c.id == "d1-load-1":
            mid = "d2-load-2"
        m = _mirror_control(c)
        spec = rewires[c.id]
        if isinstance(spec[0], tuple):
            mnote, sh = spec[0]
            midi = Wire(CH, "cc", mnote)
            shift = Wire(CH, "cc", sh) if sh is not None else None
        else:
            mnote, sh = spec
            midi = Wire(CH, "note", mnote)
            shift = Wire(CH, "note", sh) if sh is not None else None
        d2.append(
            replace(
                m,
                id=mid,
                midi=midi,
                shift_midi=shift,
                does=c.does.replace("deck 1", "deck 2").replace("Deck 1", "deck 2"),
            )
        )
    for c in ch:
        mid = c.id.replace("ch1-", "ch2-")
        m = _mirror_control(c)
        spec = rewires[c.id]
        if isinstance(spec[0], tuple):
            mnote, sh = spec[0]
            midi = Wire(CH, "cc", mnote)
            shift = Wire(CH, "cc", sh) if sh is not None else None
        else:
            mnote, sh = spec
            midi = Wire(CH, "note", mnote)
            shift = Wire(CH, "note", sh) if sh is not None else None
        ch2.append(
            replace(
                m,
                id=mid,
                midi=midi,
                shift_midi=shift,
                does=c.does.replace("channel 1", "channel 2").replace("Channel 1", "channel 2"),
            )
        )
    return d2, ch2


def _crossfader() -> Control:
    x, y, w, h = _at("crossfader")
    cx = round((PANEL_W_MM - w) / 2, 1)
    return Control(
        "crossfader",
        "X-FADER",
        "crossfader",
        "CROSSFADER",
        "Crossfader",
        cx,
        y,
        w,
        h,
        "mixer",
        Wire(CH, "cc", 0x0D),
        Wire(CH, "cc", 0x27),
    )


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


def _wire_dict(w: Wire | None) -> dict[str, int | str] | None:
    if w is None:
        return None
    return {"ch": w.ch, "kind": w.kind, "id": w.id}


def _controls_raw() -> list[Control]:
    d1 = _deck1()
    ch1 = _channel1()
    d2, ch2 = _deck2_and_ch2(d1, ch1)
    xf = _crossfader()
    controls = [*d1, *d2, *ch1, *ch2, xf]
    # Fold filter bindings into trim shift_midi (no separate ch1-filter / ch2-filter control).
    ids = [c.id for c in controls]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    return controls


def _controls() -> list[Control]:
    return normalise(_controls_raw())


def _px_from_mm(x: float, y: float, w: float, h: float) -> list[int]:
    return [
        PLATE_PX["x0"] + round(x / MM_PER_PX_X),
        PLATE_PX["y0"] + round(y / MM_PER_PX_Y),
        PLATE_PX["x0"] + round((x + w) / MM_PER_PX_X) - 1,
        PLATE_PX["y0"] + round((y + h) / MM_PER_PX_Y) - 1,
    ]


def _traced_px_key(cid: str) -> str | None:
    if cid.startswith("d2-") or cid.startswith("ch2-") or cid == "crossfader":
        return None
    if cid in _PX:
        return cid
    return None


def _reference_row(c: Control) -> dict[str, object]:
    tkey = _traced_px_key(c.id)
    px = list(_PX[tkey]) if tkey else _px_from_mm(c.x, c.y, c.w, c.h)
    return {
        "id": c.id,
        "px": px,
        "x": round((px[0] - PLATE_PX["x0"]) * MM_PER_PX_X, 1),
        "y": round((px[1] - PLATE_PX["y0"]) * MM_PER_PX_Y, 1),
        "w": round((px[2] - px[0] + 1) * MM_PER_PX_X, 1),
        "h": round((px[3] - px[1] + 1) * MM_PER_PX_Y, 1),
    }


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    controls = _controls_raw()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": "Konverter template Traktor Dj v3 (FX), kdj_traktor3_template.pdf overlay chart",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "Kontrol Dj legacy downloads (Konverter Application for Windows.zip)",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "plate_row_scale_mm_per_px": PLATE_ROW_SCALE_MM_PER_PX,
            "calibration": (
                "Left half 200 mm over the template plate width; deck 2 mirrored on the 400 mm panel. "
                "y scale matches x (0.127 mm/px)."
            ),
            "method": "Manual trace on the rendered template PDF at 200 dpi.",
            "repeat_spread_mm": spread,
            "repeat_pass": {k: list(v) for k, v in _PX_REPEAT.items()},
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [_reference_row(c) for c in controls],
    }


def build() -> dict[str, object]:
    controls = _controls()
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: {(c.x, c.y, c.w, c.h)}")
    out_controls = []
    for c in controls:
        row = asdict(c)
        row["midi"] = _wire_dict(c.midi)
        row["shift_midi"] = _wire_dict(c.shift_midi)
        out_controls.append(row)
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-channel MIDI rack controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "KONTROL_DJ_KDJ500_MAP",
        "sources": {
            "panel_size": (
                "KDJ500 user manual (c) 2006 Kontrol Dj, Part III: Compact size 400 mm. (W) x 170 mm. (D) x 45 mm. (H)"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/kontrol-dj-kdj500.ts",
            "positions": (
                "measured trace of kdj_traktor3_template.pdf; reference: "
                "devices/kontrol-dj-kdj500/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "SELECT bank buttons, JOG MODE lever, FX BUT 1-4 bank-1 layers, browse encoder turn",
            "shift_midi encodes Konverter bank-2 or SHIFT where the map binds a single wire",
            "trim and filter share PH. VOL. / FX KNOB 1 (filter on shift_midi CC 0x0E / 0x12)",
            "hints are not separate controls",
            "logo and wordmark areas",
        ],
        "controls": out_controls,
    }


if __name__ == "__main__":
    ref = build_reference()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")
