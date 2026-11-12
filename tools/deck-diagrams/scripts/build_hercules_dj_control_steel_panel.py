"""Build Hercules DJ Control Steel practice-replica panel pair (ADR-0137, issue #10928).

Writes tools/deck-diagrams/devices/hercules-dj-control-steel/panel.reference.json (measured
trace) and panel.json (normalised layout for the renderer).

Sources:
- Panel 350 x 252 mm (W x D): Hercules DJ Control Steel product specifications.
- Trace: English user manual section 4.1 top face (page 6). PDF sha256 below; crop 780x520 at
  offset +95+115 on page-06.png (full render, not committed).
- Wires: HERCULES_DJ_CONTROL_STEEL_MAP (manufacturer section 9 via device-maps.json). Every
  message is CC channel 1; shift_midi is always null.

Run: uv run python tools/deck-diagrams/scripts/build_hercules_dj_control_steel_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 350.0
PANEL_D_MM = 252.0
DEVICE_ID = "hercules-dj-control-steel"
MAP_CONST = "HERCULES_DJ_CONTROL_STEEL_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (780, 520)
PLATE_PX = {"x0": 0, "y0": 0, "x1": 779, "y1": 519}
MM_PER_PX = round(PANEL_W_MM / IMAGE_PX[0], 5)
SOURCE_PDF_SHA256 = "8ef3d215050552e57747a09fbb35614aab0d639d57e49d8730dd7ca815af47a2"
SOURCE_IMAGE_SHA256 = "42ac5044c071a8457b48c386ea2b59dd6bc859e5ec2f17cf4128d306fd659f46"
ALIGN_TOL_MM = 1.5
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10928"
_PLATE_W_PX = IMAGE_PX[0]


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


def _mirror_px(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = box
    return (_PLATE_W_PX - 1 - x1, y0, _PLATE_W_PX - 1 - x0, y1)


# Inclusive pixel boxes on the 780 x 520 plate crop (deck 1 + shared centre traced; deck 2 mirrored).
_PX_DECK_1: dict[str, tuple[int, int, int, int]] = {
    "d1-fx-control-1": (58, 62, 94, 98),
    "d1-fx-control-2": (98, 62, 134, 98),
    "d1-shift": (108, 64, 132, 88),
    "d1-trim": (118, 66, 142, 90),
    "d1-kill-treble": (418, 72, 432, 86),
    "d1-kill-medium": (418, 112, 432, 126),
    "d1-kill-bass": (418, 152, 432, 166),
    "d1-eq-high": (438, 66, 462, 90),
    "d1-eq-mid": (438, 106, 462, 130),
    "d1-eq-low": (438, 146, 462, 170),
    "d1-hot-cue-a": (268, 188, 292, 212),
    "d1-hot-cue-b": (298, 188, 322, 212),
    "d1-hot-cue-c": (328, 188, 352, 212),
    "d1-hot-cue-d": (268, 218, 292, 242),
    "d1-hot-cue-e": (298, 218, 322, 242),
    "d1-hot-cue-f": (328, 218, 352, 242),
    "d1-key-sync-toggle": (296, 261, 327, 290),
    "d1-sync-toggle": (296, 308, 327, 337),
    "d1-play-toggle": (296, 357, 327, 385),
    "d1-cue": (336, 357, 360, 381),
    "d1-stop": (336, 308, 360, 332),
    "d1-reset": (336, 259, 360, 283),
    "d1-previous": (368, 357, 392, 381),
    "d1-next": (368, 317, 392, 341),
    "d1-source": (336, 335, 360, 359),
    "d1-pitch": (24, 268, 52, 408),
    "d1-jog": (88, 355, 212, 479),
    "d1-load-1": (278, 398, 322, 422),
    "ch1-fader": (301, 424, 345, 519),
    "ch1-ch-cue": (268, 398, 292, 422),
}

_PX_GLOBAL: dict[str, tuple[int, int, int, int]] = {
    "fx-apply-deck1": (52, 28, 96, 48),
    "fx-apply-select": (350, 28, 430, 48),
    "fx-apply-deck2": (684, 28, 728, 48),
    "balance": (366, 66, 414, 114),
    "browse-select-a": (298, 66, 322, 90),
    "browse-select-b": (458, 66, 482, 90),
    "phones-mix": (592, 66, 616, 90),
    "phones-level": (632, 66, 656, 90),
    "scratch": (372, 148, 408, 184),
    "menu-up": (376, 190, 404, 210),
    "menu-down": (376, 230, 404, 250),
    "menu-left": (348, 208, 368, 232),
    "menu-right": (412, 208, 432, 232),
    "talkover": (450, 148, 474, 172),
    "master": (372, 358, 408, 518),
    "crossfader": (318, 462, 462, 492),
}

_DECK_1_TO_2: dict[str, str] = {
    "d1-fx-control-1": "d2-fx-control-1",
    "d1-fx-control-2": "d2-fx-control-2",
    "d1-shift": "d2-shift",
    "d1-trim": "d2-trim",
    "d1-kill-treble": "d2-kill-treble",
    "d1-kill-medium": "d2-kill-medium",
    "d1-kill-bass": "d2-kill-bass",
    "d1-eq-high": "d2-eq-high",
    "d1-eq-mid": "d2-eq-mid",
    "d1-eq-low": "d2-eq-low",
    "d1-key-sync-toggle": "d2-key-sync-toggle",
    "d1-sync-toggle": "d2-sync-toggle",
    "d1-play-toggle": "d2-play-toggle",
    "d1-cue": "d2-cue",
    "d1-stop": "d2-stop",
    "d1-reset": "d2-reset",
    "d1-previous": "d2-previous",
    "d1-next": "d2-next",
    "d1-source": "d2-source",
    "d1-pitch": "d2-pitch",
    "d1-jog": "d2-jog",
    "d1-load-1": "d2-load-2",
    "ch1-fader": "ch2-fader",
    "ch1-ch-cue": "ch2-ch-cue",
    **{f"d1-hot-cue-{s}": f"d2-hot-cue-{s}" for s in "abcdef"},
}


def _assemble_px() -> dict[str, tuple[int, int, int, int]]:
    px: dict[str, tuple[int, int, int, int]] = dict(_PX_GLOBAL)
    px.update(_PX_DECK_1)
    for k1, k2 in _DECK_1_TO_2.items():
        px[k2] = _mirror_px(_PX_DECK_1[k1])
    px["d1-hot-cue-g"] = px["d1-hot-cue-a"]
    px["d1-hot-cue-h"] = px["d1-hot-cue-b"]
    px["d2-hot-cue-g"] = px["d2-hot-cue-a"]
    px["d2-hot-cue-h"] = px["d2-hot-cue-b"]
    for slot in "cdef":
        px[f"d1-shift-hot-cue-{slot}"] = px[f"d1-hot-cue-{slot}"]
        px[f"d2-shift-hot-cue-{slot}"] = px[f"d2-hot-cue-{slot}"]
    if len(px) != 88:
        raise RuntimeError(f"expected 88 traced controls, got {len(px)}")
    return px


_PX: dict[str, tuple[int, int, int, int]] = _assemble_px()

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-jog": (89, 356, 213, 480),
    "d1-sync-toggle": (297, 309, 328, 338),
    "d1-hot-cue-a": (269, 189, 293, 213),
    "ch1-fader": (302, 425, 346, 520),
    "crossfader": (319, 463, 463, 493),
    "balance": (367, 67, 415, 115),
}

# fig, type, label, does, section, midi cc (channel 1)
_META: dict[str, tuple[str, str, str, str, str, int]] = {
    "d1-sync-toggle": ("15", "button", "SYNC", "Toggle deck sync (deck 1)", "deck", 7),
    "d1-key-sync-toggle": ("15", "button", "BEATLOCK", "Toggle master tempo / key sync (deck 1)", "deck", 8),
    "d1-play-toggle": ("18", "button", "PLAY/PAUSE", "Start or pause deck 1", "deck", 11),
    "d1-cue": ("20", "button", "CUE", "Cue point and preview deck 1", "deck", 12),
    "d1-jog": ("17", "jog", "JOG WHEEL", "Scratch or bend deck 1", "deck", 47),
    "d1-pitch": ("14", "fader", "PITCH", "Tempo slider deck 1", "deck", 49),
    "d2-sync-toggle": ("15", "button", "SYNC", "Toggle deck sync (deck 2)", "deck", 31),
    "d2-key-sync-toggle": ("15", "button", "BEATLOCK", "Toggle master tempo / key sync (deck 2)", "deck", 21),
    "d2-play-toggle": ("18", "button", "PLAY/PAUSE", "Start or pause deck 2", "deck", 35),
    "d2-cue": ("20", "button", "CUE", "Cue point and preview deck 2", "deck", 36),
    "d2-jog": ("17", "jog", "JOG WHEEL", "Scratch or bend deck 2", "deck", 48),
    "d2-pitch": ("14", "fader", "PITCH", "Tempo slider deck 2", "deck", 59),
    "d1-shift": ("2", "button", "SHIFT", "Hold for secondary pad functions (deck 1)", "deck", 115),
    "d2-shift": ("2", "button", "SHIFT", "Hold for secondary pad functions (deck 2)", "deck", 116),
    "d1-hot-cue-a": ("9", "pad", "KEY 1", "Hot cue A (deck 1)", "performance", 1),
    "d1-hot-cue-b": ("9", "pad", "KEY 2", "Hot cue B (deck 1)", "performance", 2),
    "d1-hot-cue-c": ("9", "pad", "KEY 3", "Hot cue C (deck 1)", "performance", 3),
    "d1-hot-cue-d": ("9", "pad", "KEY 4", "Hot cue D (deck 1)", "performance", 4),
    "d1-hot-cue-e": ("9", "pad", "KEY 5", "Hot cue E (deck 1)", "performance", 5),
    "d1-hot-cue-f": ("9", "pad", "KEY 6", "Hot cue F (deck 1)", "performance", 6),
    "d1-hot-cue-g": ("9", "pad", "KEY 7", "Hot cue G shift layer (deck 1)", "performance", 100),
    "d1-hot-cue-h": ("9", "pad", "KEY 8", "Hot cue H shift layer (deck 1)", "performance", 101),
    "d2-hot-cue-a": ("9", "pad", "KEY 1", "Hot cue A (deck 2)", "performance", 25),
    "d2-hot-cue-b": ("9", "pad", "KEY 2", "Hot cue B (deck 2)", "performance", 26),
    "d2-hot-cue-c": ("9", "pad", "KEY 3", "Hot cue C (deck 2)", "performance", 27),
    "d2-hot-cue-d": ("9", "pad", "KEY 4", "Hot cue D (deck 2)", "performance", 28),
    "d2-hot-cue-e": ("9", "pad", "KEY 5", "Hot cue E (deck 2)", "performance", 29),
    "d2-hot-cue-f": ("9", "pad", "KEY 6", "Hot cue F (deck 2)", "performance", 30),
    "d2-hot-cue-g": ("9", "pad", "KEY 7", "Hot cue G shift layer (deck 2)", "performance", 106),
    "d2-hot-cue-h": ("9", "pad", "KEY 8", "Hot cue H shift layer (deck 2)", "performance", 107),
    "ch1-ch-cue": ("21", "button", "CUE SELECT", "Headphone preview deck 1", "mixer", 20),
    "ch1-fader": ("22", "fader", "VOL", "Deck 1 channel fader", "mixer", 50),
    "d1-trim": ("3", "knob", "GAIN", "Channel gain deck 1", "mixer", 51),
    "d1-eq-high": ("5", "knob", "TREBLE", "EQ high deck 1", "mixer", 52),
    "d1-eq-mid": ("5", "knob", "MEDIUM", "EQ mid deck 1", "mixer", 53),
    "d1-eq-low": ("5", "knob", "BASS", "EQ low deck 1", "mixer", 54),
    "ch2-ch-cue": ("21", "button", "CUE SELECT", "Headphone preview deck 2", "mixer", 24),
    "ch2-fader": ("22", "fader", "VOL", "Deck 2 channel fader", "mixer", 60),
    "d2-trim": ("3", "knob", "GAIN", "Channel gain deck 2", "mixer", 61),
    "d2-eq-high": ("5", "knob", "TREBLE", "EQ high deck 2", "mixer", 62),
    "d2-eq-mid": ("5", "knob", "MEDIUM", "EQ mid deck 2", "mixer", 63),
    "d2-eq-low": ("5", "knob", "BASS", "EQ low deck 2", "mixer", 64),
    "master": ("23", "fader", "MAIN", "Main output volume", "mixer", 56),
    "crossfader": ("24", "crossfader", "CROSSFADER", "Blend between decks", "mixer", 57),
    "phones-mix": ("10", "knob", "MONITOR", "Headphone cue mix", "mixer", 58),
    "phones-level": ("11", "knob", "HP VOL", "Headphone volume", "mixer", 71),
    "d1-load-1": ("25", "button", "LOAD", "Load track to deck 1", "browse", 18),
    "d2-load-2": ("25", "button", "LOAD", "Load track to deck 2", "browse", 22),
    "browse-select-a": ("1", "encoder", "SELECT A", "Browse encoder deck A", "browse", 69),
    "browse-select-b": ("1", "encoder", "SELECT B", "Browse encoder deck B", "browse", 70),
    "d1-previous": ("16", "button", "PREVIOUS", "Move back in the track (deck 1)", "deck", 9),
    "d1-next": ("16", "button", "NEXT", "Move forward in the track (deck 1)", "deck", 10),
    "d1-stop": ("19", "button", "STOP", "Stop deck 1 without moving cue", "deck", 13),
    "d1-reset": ("14", "button", "RESET", "Reset pitch state (deck 1)", "deck", 17),
    "d1-source": ("9", "button", "SOURCE", "Switch deck 1 input source", "deck", 19),
    "d1-kill-treble": ("4", "button", "KILL TREBLE", "Kill treble deck 1", "mixer", 14),
    "d1-kill-medium": ("4", "button", "KILL MEDIUM", "Kill mids deck 1", "mixer", 15),
    "d1-kill-bass": ("4", "button", "KILL BASS", "Kill bass deck 1", "mixer", 16),
    "d1-shift-hot-cue-c": ("2", "pad", "KEY 3 + SHIFT", "Shift on keypad 3 deck 1", "performance", 102),
    "d1-shift-hot-cue-d": ("2", "pad", "KEY 4 + SHIFT", "Shift on keypad 4 deck 1", "performance", 103),
    "d1-shift-hot-cue-e": ("2", "pad", "KEY 5 + SHIFT", "Shift on keypad 5 deck 1", "performance", 104),
    "d1-shift-hot-cue-f": ("2", "pad", "KEY 6 + SHIFT", "Shift on keypad 6 deck 1", "performance", 105),
    "d1-fx-control-1": ("1", "encoder", "CONTROL 1", "FX control 1 deck 1", "effect", 65),
    "d1-fx-control-2": ("1", "encoder", "CONTROL 2", "FX control 2 deck 1", "effect", 66),
    "d2-previous": ("16", "button", "PREVIOUS", "Move back in the track (deck 2)", "deck", 33),
    "d2-next": ("16", "button", "NEXT", "Move forward in the track (deck 2)", "deck", 34),
    "d2-stop": ("19", "button", "STOP", "Stop deck 2 without moving cue", "deck", 37),
    "d2-reset": ("14", "button", "RESET", "Reset pitch state (deck 2)", "deck", 32),
    "d2-source": ("9", "button", "SOURCE", "Switch deck 2 input source", "deck", 23),
    "d2-kill-treble": ("4", "button", "KILL TREBLE", "Kill treble deck 2", "mixer", 38),
    "d2-kill-medium": ("4", "button", "KILL MEDIUM", "Kill mids deck 2", "mixer", 39),
    "d2-kill-bass": ("4", "button", "KILL BASS", "Kill bass deck 2", "mixer", 40),
    "d2-shift-hot-cue-c": ("2", "pad", "KEY 3 + SHIFT", "Shift on keypad 3 deck 2", "performance", 108),
    "d2-shift-hot-cue-d": ("2", "pad", "KEY 4 + SHIFT", "Shift on keypad 4 deck 2", "performance", 109),
    "d2-shift-hot-cue-e": ("2", "pad", "KEY 5 + SHIFT", "Shift on keypad 5 deck 2", "performance", 110),
    "d2-shift-hot-cue-f": ("2", "pad", "KEY 6 + SHIFT", "Shift on keypad 6 deck 2", "performance", 111),
    "d2-fx-control-1": ("1", "encoder", "CONTROL 1", "FX control 1 deck 2", "effect", 67),
    "d2-fx-control-2": ("1", "encoder", "CONTROL 2", "FX control 2 deck 2", "effect", 68),
    "scratch": ("7", "button", "SCRATCH", "Toggle jog scratch vs seek", "browse", 41),
    "menu-up": ("8", "button", "MENU UP", "Menu up", "browse", 42),
    "menu-down": ("8", "button", "MENU DOWN", "Menu down", "browse", 43),
    "menu-left": ("8", "button", "MENU LEFT", "Menu left", "browse", 44),
    "menu-right": ("8", "button", "MENU RIGHT", "Menu right", "browse", 45),
    "talkover": ("8", "button", "TALKOVER", "Talkover mic latch", "mixer", 46),
    "balance": ("6", "knob", "BALANCE", "Main left-right balance", "mixer", 55),
    "fx-apply-deck1": ("1", "button", "APPLY A", "Apply FX on deck 1", "effect", 112),
    "fx-apply-deck2": ("1", "button", "APPLY B", "Apply FX on deck 2", "effect", 113),
    "fx-apply-select": ("1", "button", "APPLY SEL", "Apply FX on select", "effect", 114),
}

if set(_META) != set(_PX):
    missing = sorted(set(_META) - set(_PX))
    extra = sorted(set(_PX) - set(_META))
    raise RuntimeError(f"_META/_PX mismatch missing={missing} extra={extra}")


def _mm_box(px: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round(x0 * MM_PER_PX, 1),
        round(y0 * MM_PER_PX, 1),
        round((x1 - x0 + 1) * MM_PER_PX, 1),
        round((y1 - y0 + 1) * MM_PER_PX, 1),
    )


def _centre(px: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = px
    return ((x0 + x1 + 1) / 2 * MM_PER_PX, (y0 + y1 + 1) / 2 * MM_PER_PX)


def repeat_spread_mm() -> float:
    worst = 0.0
    for cid, px2 in _PX_REPEAT.items():
        (ax, ay), (bx, by) = _centre(_PX[cid]), _centre(px2)
        worst = max(worst, ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5)
    return round(worst, 1)


def _controls() -> list[Control]:
    out: list[Control] = []
    for cid, (fig, typ, label, does, section, cc) in _META.items():
        x, y, w, h = _mm_box(_PX[cid])
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                does,
                x,
                y,
                w,
                h,
                section,
                Wire(1, "cc", cc),
                None,
            )
        )
    if len(out) != 88:
        raise SystemExit(f"[ERROR] expected 88 controls, got {len(out)}")
    for cid in _PX_REPEAT:
        if cid not in _META:
            raise SystemExit(f"[ERROR] repeat pass names unknown control {cid!r}")
    return out


def _aligned_centres(controls: list[Control], axis: int) -> dict[str, float]:
    centres = {c.id: (c.x + c.w / 2) if axis == 0 else (c.y + c.h / 2) for c in controls}
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
        w = float(max(1, round(c.w)))
        h = float(max(1, round(c.h)))
        x = round(cx[c.id] - w / 2, 1)
        y = round(cy[c.id] - h / 2, 1)
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


def build_reference() -> dict[str, object]:
    controls = _controls()
    spread = repeat_spread_mm()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": ("Hercules DJ Control Steel English user manual section 4.1 top face (page 6), 780x520 px crop"),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-steel.ts",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX, "y": MM_PER_PX},
            "calibration": f"isotropic: {PANEL_W_MM} mm over {IMAGE_PX[0]} px width",
            "method": (
                "Ink-edge boxes on the manual plate crop; deck 2 mirrored on X. Buttons, pads, "
                "knobs, encoders, jogs, and faders traced; second pass _PX_REPEAT on six controls."
            ),
            "repeat_pass": _PX_REPEAT,
            "repeat_spread_mm": spread,
            "issue": TRACE_ISSUE,
            "date": TRACE_DATE,
        },
        "controls": [{"id": c.id, "px": list(_PX[c.id]), "x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in controls],
    }


def build() -> dict[str, object]:
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} outside panel: x={c.x} y={c.y} w={c.w} h={c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "2-channel DJ controller with steel deck plate",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": ("Hercules DJ Control Steel product specifications (350 x 252 x 68 mm)"),
            "wire": (
                "HERCULES_DJ_CONTROL_STEEL_MAP (apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-control-steel.ts)"
            ),
            "positions": (
                f"measured trace of manual section 4.1; reference: devices/{DEVICE_ID}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "LED feedback: manufacturer MIDI-OUT uses CC on/off; no LedRule wiring yet",
            "Pitch bend +/- buttons (manual fig 12-13): no dedicated wires in the live map",
            "Rear-panel USB (manual callout 26)",
            "Branding print and mounting hardware",
            "shift_midi: Steel uses software shift only; every control keeps shift_midi null",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    spread = repeat_spread_mm()
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls, repeat spread {spread} mm)")
