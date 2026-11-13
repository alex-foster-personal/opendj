"""Build the Hercules DJ Console Mk4 practice-replica panel pair (ADR-0137, issue #10920).

Traces the top-panel figure in the Hercules DJ Console Mk4 Operation Manual section 4.1
(page 10/32, rendered at 200 dpi). Wire identities follow
apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-mk4.ts.

Run: uv run --locked python tools/deck-diagrams/scripts/build_hercules_dj_console_mk4_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 266.0
PANEL_D_MM = 185.0
DEVICE = "hercules-dj-console-mk4"
MAP_CONST = "HERCULES_DJ_CONSOLE_MK4_MAP"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

SOURCE_PDF_SHA256 = "914feabd4b8e1f8ac5acd084ca6ce09d2575a75acc7e679d73721af36057a03c"
SOURCE_IMAGE_SHA256 = "1bbce74116880303d74f86d5cb5240ec78b1630fbf13fb4da72b33b3ed7762cd"
IMAGE_PX = (1242, 1653)
PLATE_PX = {"x0": 175, "x1": 905, "y0": 255, "y1": 820}
PLATE_W_PX = PLATE_PX["x1"] - PLATE_PX["x0"] + 1
PLATE_H_PX = PLATE_PX["y1"] - PLATE_PX["y0"] + 1
MM_PER_PX_X = round(PANEL_W_MM / PLATE_W_PX, 5)
MM_PER_PX_Y = MM_PER_PX_X
REPEAT_SPREAD_MM = 1.0
TRACE_DATE = "Thu 12 Nov 2026"
TRACE_ISSUE = "#10920"
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
    midi: Wire | None
    shift_midi: Wire | None = None


def _box(rel: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rel
    return (
        PLATE_PX["x0"] + x0,
        PLATE_PX["y0"] + y0,
        PLATE_PX["x0"] + x1,
        PLATE_PX["y0"] + y1,
    )


def _mirror(rel: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rel
    return (PLATE_W_PX - x1, y0, PLATE_W_PX - x0, y1)


# Plate-relative inclusive pixel boxes on the 731 x 566 crop (y flipped from manual figure).
_PX_D1: dict[str, tuple[int, int, int, int]] = {
    "d1-pitch": (26, 30, 70, 74),
    "d1-source": (22, 76, 92, 104),
    "d1-tempo-nudge-minus": (22, 116, 58, 146),
    "d1-tempo-nudge-plus": (60, 116, 96, 146),
    "d1-sync-toggle": (22, 150, 72, 176),
    "d1-shift": (196, 26, 252, 54),
    "d1-pad-1": (188, 70, 224, 106),
    "d1-pad-2": (232, 70, 268, 106),
    "d1-pad-3": (188, 110, 224, 146),
    "d1-pad-4": (232, 110, 268, 146),
    "d1-prev": (184, 152, 220, 178),
    "d1-next": (228, 152, 264, 178),
    "d1-jog": (48, 246, 188, 386),
    "d1-play-toggle": (70, 430, 120, 468),
    "d1-cue": (126, 430, 176, 468),
    "d1-load-1": (298, 32, 356, 60),
    "d1-shift-state": (196, 54, 252, 68),
}

_PX_D2: dict[str, tuple[int, int, int, int]] = {
    ("d2-" + k[3:]): _mirror(v) for k, v in _PX_D1.items() if k != "d1-load-1"
}
_PX_D2["d2-load-2"] = _mirror((375, 32, 433, 60))

_PX_MIXER: dict[str, tuple[int, int, int, int]] = {
    "ch1-eq-high": (266, 276, 302, 312),
    "ch1-eq-mid": (266, 336, 302, 372),
    "ch1-eq-low": (266, 396, 302, 432),
    "ch1-fader": (206, 262, 236, 448),
    "ch1-ch-cue": (312, 112, 362, 142),
    "ch2-eq-high": (428, 276, 464, 312),
    "ch2-eq-mid": (428, 336, 464, 372),
    "ch2-eq-low": (428, 396, 464, 432),
    "ch2-fader": (494, 262, 524, 448),
    "ch2-ch-cue": (368, 112, 418, 142),
    "crossfader": (262, 458, 468, 478),
    "phones-mix": (334, 420, 378, 456),
}

_PX_GLOBAL: dict[str, tuple[int, int, int, int]] = {
    "browse-menu-up": (346, 68, 378, 98),
    "browse-menu-down": (346, 100, 378, 130),
    "browse-folders": (308, 84, 340, 114),
    "browse-files": (408, 84, 440, 114),
    "scratch": (348, 20, 398, 50),
}

_PX: dict[str, tuple[int, int, int, int]] = {
    **{k: _box(v) for k, v in _PX_D1.items()},
    **{k: _box(v) for k, v in _PX_D2.items()},
    **{k: _box(v) for k, v in _PX_MIXER.items()},
    **{k: _box(v) for k, v in _PX_GLOBAL.items()},
}

_PX_REPEAT: dict[str, tuple[int, int, int, int]] = {
    "d1-jog": _box((50, 248, 190, 388)),
    "d1-play-toggle": _box((72, 432, 122, 470)),
    "ch1-eq-high": _box((267, 277, 303, 313)),
    "crossfader": _box((263, 459, 469, 479)),
    "d1-pad-2": _box((233, 71, 269, 107)),
    "browse-files": _box((409, 85, 441, 115)),
    "d2-jog": _box(_mirror((48, 246, 188, 386))),
    "phones-mix": _box((335, 421, 379, 457)),
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
    p = f"d{deck}"
    pad_slots = (1, 2, 3, 4)
    base_notes = (1, 2, 3, 4) if deck == 1 else (21, 22, 23, 24)
    shift_notes = (5, 6, 7, 8) if deck == 1 else (25, 26, 27, 28)
    out: list[Control] = []
    rows: list[tuple[str, str, str, str, str, Wire | None, Wire | None]] = [
        (
            f"{p}-play-toggle",
            "14",
            "button",
            "PLAY/PAUSE",
            f"Start or pause deck {deck}",
            Wire(CH, "note", 15 if deck == 1 else 35),
            None,
        ),
        (
            f"{p}-cue",
            "15",
            "button",
            "CUE",
            f"Set or jump to the cue point on deck {deck}",
            Wire(CH, "note", 14 if deck == 1 else 34),
            None,
        ),
        (
            f"{p}-sync-toggle",
            "13",
            "button",
            "SYNC",
            f"Beat-sync deck {deck} to the other deck",
            Wire(CH, "note", 18 if deck == 1 else 38),
            None,
        ),
        (
            f"{p}-tempo-nudge-minus",
            "1",
            "button",
            "TEMPO -",
            f"Momentary tempo decrease on deck {deck}",
            Wire(CH, "note", 10 if deck == 1 else 30),
            None,
        ),
        (
            f"{p}-tempo-nudge-plus",
            "1",
            "button",
            "TEMPO +",
            f"Momentary tempo increase on deck {deck}",
            Wire(CH, "note", 11 if deck == 1 else 31),
            None,
        ),
        (
            f"{p}-jog",
            "16",
            "jog",
            "JOG",
            f"Scratch or nudge deck {deck}",
            Wire(CH, "cc", 48 if deck == 1 else 49),
            None,
        ),
        (
            f"{p}-load-{'1' if deck == 1 else '2'}",
            "18",
            "button",
            "LOAD",
            f"Load the browser selection to deck {deck}",
            Wire(CH, "note", 17 if deck == 1 else 37),
            None,
        ),
        (
            f"{p}-pitch",
            "3",
            "knob",
            "PITCH",
            f"Relative pitch encoder on deck {deck} (hint only in map)",
            Wire(CH, "cc", 50 if deck == 1 else 51),
            None,
        ),
        (
            f"{p}-source",
            "2",
            "button",
            "SOURCE",
            f"Toggle computer vs line input on deck {deck} (hint)",
            Wire(CH, "note", 19 if deck == 1 else 39),
            None,
        ),
        (
            f"{p}-shift",
            "4",
            "button",
            "SHIFT",
            f"Loops vs effects pad layer on deck {deck} (hint)",
            Wire(CH, "note", 9 if deck == 1 else 29),
            None,
        ),
        (
            f"{p}-shift-state",
            "4",
            "button",
            "SHIFT STATE",
            f"Shift layer state LED wire on deck {deck} (hint)",
            Wire(CH, "note", 20 if deck == 1 else 40),
            None,
        ),
        (
            f"{p}-prev",
            "12",
            "button",
            "PREV",
            f"Browse to the previous track for deck {deck} (hint)",
            Wire(CH, "note", 12 if deck == 1 else 32),
            None,
        ),
        (
            f"{p}-next",
            "12",
            "button",
            "NEXT",
            f"Browse to the next track for deck {deck} (hint)",
            Wire(CH, "note", 13 if deck == 1 else 33),
            None,
        ),
    ]
    for cid, fig, typ, label, does, midi, shift in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, "deck", midi, shift))
    for slot, base_n, shift_n in zip(pad_slots, base_notes, shift_notes, strict=True):
        cid = f"{p}-pad-{slot}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                "11",
                "pad",
                str(slot),
                f"Hot cue {chr(64 + slot)} on deck {deck}; SHIFT sends slot {chr(64 + slot + 4)}",
                x,
                y,
                w,
                h,
                "performance",
                Wire(CH, "note", base_n),
                Wire(CH, "note", shift_n),
            )
        )
    return out


def _mixer() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int]] = [
        ("ch1-ch-cue", "20", "button", "CH CUE A", "Headphone cue deck 1", "note", 16),
        ("ch2-ch-cue", "20", "button", "CH CUE B", "Headphone cue deck 2", "note", 36),
        ("ch1-fader", "17", "fader", "CH FADER A", "Channel fader deck 1", "cc", 52),
        ("ch2-fader", "17", "fader", "CH FADER B", "Channel fader deck 2", "cc", 57),
        ("ch1-eq-high", "5", "knob", "TREBLE A", "High EQ deck 1", "cc", 53),
        ("ch1-eq-mid", "5", "knob", "MEDIUM A", "Mid EQ deck 1", "cc", 54),
        ("ch1-eq-low", "5", "knob", "BASS A", "Low EQ deck 1", "cc", 55),
        ("ch2-eq-high", "5", "knob", "TREBLE B", "High EQ deck 2", "cc", 58),
        ("ch2-eq-mid", "5", "knob", "MEDIUM B", "Mid EQ deck 2", "cc", 59),
        ("ch2-eq-low", "5", "knob", "BASS B", "Low EQ deck 2", "cc", 60),
        ("crossfader", "19", "crossfader", "CROSSFADER", "Blend deck A and deck B", "cc", 56),
        ("phones-mix", "front", "knob", "PHONES MIX", "Cue vs program mix (front-panel knob)", "cc", 61),
    ]
    out: list[Control] = []
    for cid, fig, typ, label, does, kind, num in rows:
        x, y, w, h = _at(cid)
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
                "mixer",
                Wire(CH, kind, num),
                None,
            )
        )
    return out


def _browse() -> list[Control]:
    rows = [
        ("browse-menu-up", "8", "MENU ▲", "Move up in the browser", 41),
        ("browse-menu-down", "8", "MENU ▼", "Move down in the browser", 42),
        ("browse-folders", "6", "FOLDERS", "Browse parent folder", 44),
        ("browse-files", "10", "FILES", "Browse files in folder", 43),
        ("scratch", "7", "SCRATCH", "Toggle scratch mode", 45),
    ]
    out: list[Control] = []
    for cid, fig, label, does, note_id in rows:
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                fig,
                "button",
                label,
                does,
                x,
                y,
                w,
                h,
                "browse",
                Wire(CH, "note", note_id),
                None,
            )
        )
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
    controls = [*_deck(1), *_deck(2), *_mixer(), *_browse()]
    known = {c.id for c in controls}
    missing = sorted(known - set(_PX))
    extra = sorted(set(_PX) - known)
    if missing or extra:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing} extra={extra}")
    dupes = sorted({c.id for c in controls if [x.id for x in controls].count(c.id) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate ids: {dupes}")
    return controls


def build_reference() -> dict[str, object]:
    spread = repeat_spread_mm()
    if spread != REPEAT_SPREAD_MM:
        raise SystemExit(f"[ERROR] REPEAT_SPREAD_MM {REPEAT_SPREAD_MM} but traces give {spread}")
    raw = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": ("Hercules DJ Console Mk4 Operation Manual section 4.1 The top panel, page 10/32"),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "Hercules DJ Console Mk4 Operation Manual (ENG), ts.hercules.com",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PLATE_PX),
            "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
            "method": (
                "Manual trace on 200 dpi page render with a 50 px grid. Knob: visible cap. "
                "Button and pad: outer body. Jog: outer platter ring. Fader slot: travel with cap."
            ),
            "repeat_spread_mm": spread,
            "repeat_controls": sorted(_PX_REPEAT),
            "repeat_pass": ["primary trace", "second independent crop trace"],
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
            for c in raw
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
        "display_name": "2-channel computer DJ console",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": (
                "Hercules DJ Console Mk4 Operation Manual and retail specs: Dimensions (W x D x H) 266 x 185 x 63 mm"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/hercules-dj-console-mk4.ts",
            "positions": (
                "measured trace of manual section 4.1 top panel; "
                f"reference: tools/deck-diagrams/devices/{DEVICE}/panel.reference.json"
            ),
        },
        "out_of_scope": [
            "Mic On/Off and other software-only surfaces with no section-10 MIDI wire",
            "phones-mix sits on the front panel physically; placed at the crossfader row for teaching",
            "rear and front panel jacks and level LEDs",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference = build_reference()
    REFERENCE_OUT.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    doc = build()
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    n = len(doc["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {REFERENCE_OUT.name} and {PANEL_OUT.name} ({n} controls)")
