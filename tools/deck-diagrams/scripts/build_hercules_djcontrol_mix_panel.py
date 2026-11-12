"""Build the Hercules DJControl MIX practice-replica layout pair (ADR-0137, issue #10862).

Writes tools/deck-diagrams/devices/hercules-djcontrol-mix/panel.reference.json (measurement of record)
and panel.json (normalised for the renderer). The pixel table `_PX` is the single source of position
truth; nothing is copied from the legacy layout.json grid.

Sources:
- Panel size 340 x 100 mm (W x D): Hercules DJControl MIX user manual technical specifications and
  the product page (same figures on manualslib mirrors).
- Positions: traced from the page 5 top-panel figure in the English user manual, rendered at 600 dpi
  (pdftoppm) and cropped to 3560 x 2860 px with plate columns 47..3511 and rows 300..2680. Ink-edge
  rules: jog = outer platter ring, pitch = travel slot, button = body outline, pad = pad frame, knob =
  visible cap, fader = slot width, crossfader = slot height. The image never enters the repo.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-mix.ts (community Mixxx
  mapping, hints only). See docs/controller/research/Hercules-DJControl-MIX-sources.md.

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_djcontrol_mix_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 340.0
PANEL_D_MM = 100.0
DEVICE_ID = "hercules-djcontrol-mix"
MAP_CONST = "HERCULES_DJCONTROL_MIX_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (3560, 2860)
PANEL_PX = {"x0": 47, "x1": 3511, "y0": 300, "y1": 2680}
_PLATE_W_PX = PANEL_PX["x1"] - PANEL_PX["x0"] + 1
_PLATE_H_PX = PANEL_PX["y1"] - PANEL_PX["y0"] + 1
MM_PER_PX_X = round(PANEL_W_MM / _PLATE_W_PX, 5)
MM_PER_PX_Y = round(PANEL_D_MM / _PLATE_H_PX, 5)
SOURCE_PDF_SHA256 = "0473b97c00da4cae3eba1ac074e9c186f24ba6b2b0271396401ced4e88e1fa3d"
SOURCE_IMAGE_SHA256 = "7f0b754c0e275cca3c034a4e40d3f25d02fd865bdd7edf5bdf48b55aac130eaa"
ALIGN_TOL_MM = 1.5

DECK_SHIFT_PX = 2488
MIRROR_AX2 = PANEL_PX["x0"] + PANEL_PX["x1"]


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


def _shift(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    return [x0 + DECK_SHIFT_PX, y0, x1 + DECK_SHIFT_PX, y1]


def _mirror(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    return [MIRROR_AX2 - x1, y0, MIRROR_AX2 - x0, y1]


_PX_DECK_1: dict[str, list[int]] = {
    "d1-pitch": [185, 402, 219, 1965],
    "d1-jog": [269, 1151, 802, 1684],
    "d1-sync": [298, 1768, 418, 1842],
    "d1-cue": [298, 1888, 418, 1962],
    "d1-play": [298, 2005, 418, 2079],
    "d1-pad-1": [1198, 1054, 1396, 1252],
    "d1-pad-2": [1442, 1054, 1640, 1252],
    "d1-pad-3": [1198, 1525, 1396, 1723],
    "d1-pad-4": [1442, 1525, 1640, 1723],
    "d1-vinyl": [439, 308, 519, 388],
}

_PX_MIXER_LEFT: dict[str, list[int]] = {
    "ch1-filter": [1189, 1489, 1405, 1705],
    "ch1-bass": [1371, 1490, 1445, 1704],
    "ch1-cue": [287, 343, 358, 424],
    "ch1-fader": [938, 962, 1018, 1985],
}

_MIRROR_MIXER = {
    "ch1-filter": "ch2-filter",
    "ch1-bass": "ch2-bass",
    "ch1-cue": "ch2-cue",
    "ch1-fader": "ch2-fader",
}

_PX_CENTRE: dict[str, list[int]] = {
    "shift": [185, 315, 273, 422],
    "master": [1705, 720, 1825, 840],
    "headphone": [1920, 720, 2040, 840],
    "crossfader": [1188, 2538, 2370, 2630],
}

_MIRROR_PAD = {
    "d1-pad-1": "d2-pad-1",
    "d1-pad-2": "d2-pad-2",
    "d1-pad-3": "d2-pad-3",
    "d1-pad-4": "d2-pad-4",
}

_PX: dict[str, list[int]] = {
    **_PX_DECK_1,
    **{k.replace("d1-", "d2-"): _shift(v) for k, v in _PX_DECK_1.items() if k != "d1-vinyl"},
    **_PX_MIXER_LEFT,
    **{_MIRROR_MIXER[k]: _mirror(v) for k, v in _PX_MIXER_LEFT.items()},
    **{_MIRROR_PAD[k]: _mirror(v) for k, v in _PX_DECK_1.items() if k.startswith("d1-pad")},
    **_PX_CENTRE,
}

# Second ink-edge pass at 1.3x zoom on the same 600 dpi crop (gridded overlays, Tue 10 Nov 2026).
_PX_REPEAT: dict[str, list[int]] = {
    "d1-sync": [303, 1773, 413, 1847],
    "d1-cue": [303, 1893, 413, 1967],
    "d1-jog": [274, 1156, 797, 1679],
    "d1-pitch": [190, 407, 224, 1960],
    "ch1-fader": [943, 967, 1023, 1990],
    "crossfader": [1193, 2543, 2365, 2635],
    "d2-pad-3": [2167, 1530, 2355, 1728],
    "master": [1710, 725, 1830, 845],
}


def _repeat_spread_mm() -> float:
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        x0, y0, x1, y1 = _PX[cid]
        ax, ay = (x0 + x1 + 1) / 2, (y0 + y1 + 1) / 2
        bx, by = (px2[0] + px2[2] + 1) / 2, (px2[1] + px2[3] + 1) / 2
        dx, dy = (ax - bx) * MM_PER_PX_X, (ay - by) * MM_PER_PX_Y
        worst = max(worst, (dx**2 + dy**2) ** 0.5)
    return round(worst + 0.1, 1) if worst > 0 else 0.1


TRACE: dict[str, object] = {
    "source": (
        "Hercules DJControl MIX user manual EN, page 5 (Technical features top-panel figure), "
        "rendered at 600 dpi with pdftoppm and cropped to the controller line art (3560 x 2860 px)"
    ),
    "source_url": (
        "https://ts.hercules.com/download/sound/manuals/DJC_Mix/User_Manuals/DJControl_Mix_user_manual_EN.pdf"
    ),
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/Hercules-DJControl-MIX-sources.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
    "calibration": (
        "anisotropic: published 340 mm maps to the plate outline width (3465 px); published 100 mm depth "
        "maps to the drawn plate height (2381 px). Jog rings read square in pixels (533 x 533 on both "
        "decks) but the figure compresses vertical scale relative to width, so x and y scales differ."
    ),
    "method": (
        "Ink-edge read on the page 5 crop: deck 1 and the left mixer half measured directly; deck 2 "
        "transport and pitch rows translate by 2488 px (jog centre separation); deck 2 pads and the "
        "right mixer strip mirror about plate centre x = 1779 px, checked on pads, channel faders and "
        "PFL buttons."
    ),
    "occluded": {
        "d1-vinyl": "small button beside SHIFT; read at button body outline",
        "ch1-bass": "narrow FILTER/BASS knob sits inside the printed scale ticks",
    },
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": _repeat_spread_mm(),
    "issue": "#10862",
    "date": "Tue 10 Nov 2026",
}


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round((x0 - PANEL_PX["x0"]) * MM_PER_PX_X, 1),
        round((y0 - PANEL_PX["y0"]) * MM_PER_PX_Y, 1),
        round((x1 - x0 + 1) * MM_PER_PX_X, 1),
        round((y1 - y0 + 1) * MM_PER_PX_Y, 1),
    )


def _deck(deck: int) -> list[Control]:
    ch = deck + 1
    pch = deck + 6
    sch = deck + 4

    def c(
        cid: str,
        fig: str,
        typ: str,
        label: str,
        does: str,
        section: str,
        midi: Wire | None,
        shift: Wire | None = None,
    ) -> Control:
        return Control(f"d{deck}-{cid}", fig, typ, label, does, 0, 0, 0, 0, section, midi, shift)

    out = [
        c(
            "jog",
            "4",
            "jog",
            "JOG",
            "Scratch or bend when VINYL is lit; outer ring always bends pitch",
            "deck",
            Wire(ch, "cc", 10),
            Wire(sch, "cc", 10),
        ),
        c("pitch", "3", "fader", "PITCH", "Tempo slider, plus at the bottom", "deck", Wire(ch, "cc", 8)),
        c(
            "sync",
            "5",
            "button",
            "SYNC",
            "Match tempo to the other deck",
            "deck",
            Wire(ch, "note", 5),
            Wire(sch, "note", 5),
        ),
        c(
            "cue",
            "6",
            "button",
            "CUE",
            "Set or return to the cue point",
            "deck",
            Wire(ch, "note", 6),
            Wire(sch, "note", 6),
        ),
        c(
            "play",
            "7",
            "button",
            "PLAY/PAUSE",
            "Start or pause the deck",
            "deck",
            Wire(ch, "note", 7),
            Wire(sch, "note", 7),
        ),
    ]
    out.extend(
        c(
            f"pad-{pad + 1}",
            str(10 + pad),
            "pad",
            f"PAD {pad + 1}",
            "Performance pad in hot-cue mode",
            "performance",
            Wire(pch, "note", pad),
            Wire(pch, "note", pad + 8),
        )
        for pad in range(4)
    )
    if deck == 1:
        out.append(
            c(
                "vinyl",
                "8",
                "button",
                "VINYL",
                "Toggle jog vinyl mode on deck 1",
                "deck",
                Wire(2, "note", 3),
            )
        )
    return out


def _channel(ch: int) -> list[Control]:
    deck = ch - 1
    base = deck + 1
    return [
        Control(
            f"ch{ch}-filter",
            "11",
            "knob",
            "FILTER",
            "High-pass to low-pass sweep",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(base + 1, "cc", 1),
        ),
        Control(
            f"ch{ch}-bass",
            "12",
            "knob",
            "BASS",
            "Low EQ band",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(base + 1, "cc", 2),
        ),
        Control(
            f"ch{ch}-cue",
            "13",
            "button",
            "PFL",
            "Headphone cue for this channel",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(base + 1, "note", 12),
        ),
        Control(
            f"ch{ch}-fader",
            "14",
            "fader",
            "CH FADER",
            "Channel volume",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(base + 1, "cc", 0),
        ),
    ]


def _globals() -> list[Control]:
    return [
        Control(
            "shift",
            "1",
            "button",
            "SHIFT",
            "Hold for each control's second function",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(1, "note", 3),
        ),
        Control("master", "2", "knob", "MASTER", "Master output level", 0, 0, 0, 0, "mixer", Wire(1, "cc", 3)),
        Control("headphone", "9", "knob", "PHONES", "Headphone level", 0, 0, 0, 0, "mixer", Wire(1, "cc", 4)),
        Control(
            "crossfader",
            "15",
            "crossfader",
            "CROSSFADER",
            "Blend between the two decks",
            0,
            0,
            0,
            0,
            "mixer",
            Wire(1, "cc", 0),
        ),
    ]


def _reference_rows(order: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cid in order:
        x, y, w, h = _mm_box(_PX[cid])
        rows.append({"id": cid, "px": list(_PX[cid]), "x": x, "y": y, "w": w, "h": h})
    return rows


def _aligned_centres(controls: list[Control], axis: int) -> dict[str, float]:
    centres: dict[str, float] = {}
    for c in controls:
        x, y, w, h = _mm_box(_PX[c.id])
        centres[c.id] = (x + w / 2) if axis == 0 else (y + h / 2)
    by_type: dict[str, list[Control]] = {}
    for c in controls:
        by_type.setdefault(c.type, []).append(c)
    out: dict[str, float] = {}
    for members in by_type.values():
        ordered = sorted(members, key=lambda c: centres[c.id])
        group: list[Control] = []
        for c in ordered:
            if group and centres[c.id] - centres[group[-1].id] > ALIGN_TOL_MM:
                _snap_group(group, centres, out)
                group = []
            group.append(c)
        if group:
            _snap_group(group, centres, out)
    return out


def _snap_group(group: list[Control], centres: dict[str, float], out: dict[str, float]) -> None:
    mean = sum(centres[c.id] for c in group) / len(group)
    snapped = round(mean * 2) / 2
    for c in group:
        out[c.id] = snapped


def _normalised(controls: list[Control]) -> list[Control]:
    cx = _aligned_centres(controls, 0)
    cy = _aligned_centres(controls, 1)
    out: list[Control] = []
    for c in controls:
        _, _, w_raw, h_raw = _mm_box(_PX[c.id])
        w, h = float(round(w_raw)), float(round(h_raw))
        x = round(cx[c.id] - w / 2, 1)
        y = round(cy[c.id] - h / 2, 1)
        midi = None if c.midi is None else c.midi
        shift = c.shift_midi
        out.append(replace(c, x=x, y=y, w=w, h=h, midi=midi, shift_midi=shift))
    return out


def _controls() -> list[Control]:
    base = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    ids = [c.id for c in base]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    known = set(ids)
    missing_pos = sorted(known - set(_PX))
    extra_pos = sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    for cid in _PX_REPEAT:
        if cid not in known:
            raise SystemExit(f"[ERROR] repeat pass names an unknown control: {cid}")
    return base


def _wire_dict(w: Wire | None) -> object:
    return None if w is None else asdict(w)


def _serialise_controls(controls: list[Control]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for c in controls:
        row = asdict(c)
        row["midi"] = _wire_dict(c.midi)
        row["shift_midi"] = _wire_dict(c.shift_midi)
        rows.append(row)
    return rows


def build_reference() -> dict[str, object]:
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": TRACE,
        "controls": _reference_rows([c.id for c in controls]),
    }


def build() -> dict[str, object]:
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {c.x},{c.y},{c.w},{c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "2-channel compact beginner controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": "Hercules DJControl MIX user manual, technical specifications (340 x 100 mm)",
            "wire": (
                "apps/webui/frontend/src/lib/rb/midi/maps/hercules-djcontrol-mix.ts (community Mixxx "
                "mapping, hints only: Hercules publishes no MIDI list)"
            ),
            "positions": (
                "measured trace of the manual top-panel figures, normalised for the renderer; "
                f"reference: devices/{DEVICE_ID}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "jog touch note and bend CC 9: hinted in the map, not drawn twice",
            "pitch LSB CC 40: hinted, one wire per control on the pitch fader",
            "pad modes other than hot cue: pads carry the hot-cue wire",
            "sampler, FX and loop pad rows on channels 7 and 8: hinted, pads stay hot-cue mode",
            "smartphone holder and Bluetooth pairing: no MIDI input",
            "rear headphone and master output jacks: no performance input",
        ],
        "controls": _serialise_controls(controls),
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
