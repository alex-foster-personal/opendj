"""Build the Numark Mixstream Pro practice-replica layout pair (ADR-0137, issue #10870).

Writes tools/deck-diagrams/devices/numark-mixstream-pro/panel.reference.json (the measurement
of record, raw trace) and panel.json (the same trace normalised for the renderer). The pixel
table ``_PX`` below is the single source of position truth.

Sources:
- Panel size 566 x 284 mm (W x D): Numark Mixstream Pro / Pro+ / Pro Go User Guide v5.0.0,
  Technical Specifications (page 43): "Dimensions (W x D x H) 22.3 x 11.2 x 3.1 in /
  566.0 x 284.0 x 78.6 mm (Mixstream Pro)".
- Positions: traced from the "Features > Top Panel" figure on pages 6-7 of that guide, rendered
  at 600 dpi (page 6 PNG 4961 x 7017 px, sha256 in TRACE). Every ``_PX`` row is an inclusive pixel
  box on the full page in that render, read as ink edges. The top-view plate spans y 1325..3365;
  callouts 33-35 (speakers and front-face level) sit below the plate line and stay out of scope
  or unwired. Neither the PDF nor the render enters the repo.
- Wire identities: community Mixxx ``Numark-Mixstream-Pro.midi.xml`` (tier 2, hints only in
  ``NUMARK_MIXSTREAM_PRO_MAP``); see docs/controller/research/Numark-Mixstream-Pro-sources.md.
  SHIFT is software state in the Mixxx JS mapping, so every control keeps ``shift_midi`` null.
- ``fig`` is the callout number of the User Guide top-panel figure (1..35 where drawn).

Deck 2 transport, pads and pitch are deck 1 translated by 1625 px (pitch cap and pad row
checked on the figure). The deck 2 jog outer ring is read separately at x 3650..4249 (shifting
the deck 1 jog by 1625 px lands on the channel 1 fader). Channel 2 mixer boxes are traced on
the right of the figure (not mirrored from channel 1: the channel 1 EQ column straddles plate
centre x = 2488 px). Channel fader slots were read at x 3032 and 3475.

Normalisation (panel.json only): centres snapped to 0.5 mm, sizes to whole mm, and row or
column mates (same control type, centres within 1.5 mm) placed on one line.

Run: uv run --locked python tools/deck-diagrams/scripts/build_numark_mixstream_pro_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 566.0
PANEL_D_MM = 284.0
DEVICE_ID = "numark-mixstream-pro"
MAP_CONST = "NUMARK_MIXSTREAM_PRO_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (4961, 7017)
PANEL_PX = {"x0": 450, "x1": 4522, "y0": 1300, "y1": 3365}
MM_PER_PX = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
SOURCE_PDF_SHA256 = "2f0b1c9c20a53bb0444d2c3cd0f87de636274fcf358cbce73d3930ac0cdded61"
SOURCE_IMAGE_SHA256 = "903dd22ee1bddd8ea2b0c3a28d5f952271d6d07f44e745f3b8444beafed05153"
ALIGN_TOL_MM = 1.5

DECK_SHIFT_PX = 1625
# Extra after DECK_SHIFT so deck 2 clears the centre mixer; jog outer ring is read at 3650 px.
DECK2_EXTRA_PX = 3650 - (1384 + DECK_SHIFT_PX)


@dataclass(frozen=True)
class Wire:
    ch: int
    kind: str  # note | cc
    id: int


@dataclass(frozen=True)
class Control:
    id: str
    fig: str
    type: str  # jog | fader | knob | pad | button | crossfader | encoder
    label: str
    does: str
    x: float
    y: float
    w: float
    h: float
    section: str
    midi: Wire | None
    shift_midi: Wire | None = None


# ----------------------------------------------------------------- the trace (pixels)

_PX_DECK_1: dict[str, list[int]] = {
    "d1-jog": [1384, 2035, 1983, 2638],
    "d1-pitch": [2149, 2280, 2302, 2580],
    "d1-pitch-bend-down": [1900, 2575, 2005, 2705],
    "d1-pitch-bend-up": [1900, 2775, 2005, 2907],
    "d1-scratch": [1831, 2327, 1946, 2443],
    "d1-sync": [1098, 2225, 1222, 2347],
    "d1-cue": [1098, 2484, 1222, 2610],
    "d1-play": [1098, 2738, 1222, 2865],
    "d1-shift": [1831, 2525, 1945, 2650],
    "d1-mode-hotcue": [452, 2822, 563, 2938],
    "d1-mode-saved-loop": [452, 2655, 563, 2779],
    "d1-mode-autoloop": [453, 2495, 563, 2626],
    "d1-mode-roll": [453, 2385, 570, 2510],
    "d1-pad-1": [762, 2825, 936, 2941],
    "d1-pad-2": [971, 2825, 1140, 2941],
    "d1-pad-3": [1180, 2825, 1351, 2941],
    "d1-pad-4": [1387, 2825, 1560, 2941],
    "d1-pad-5": [762, 2705, 936, 2791],
    "d1-pad-6": [965, 2705, 1138, 2791],
    "d1-pad-7": [1182, 2705, 1352, 2791],
    "d1-pad-8": [1384, 2700, 1560, 2791],
}

_CH1_MIXER: dict[str, list[int]] = {
    "ch1-level": [2277, 2061, 2392, 2185],
    "ch1-eq-high": [2428, 2061, 2543, 2188],
    "ch1-eq-mid": [2578, 2062, 2693, 2185],
    "ch1-eq-low": [2751, 2066, 2867, 2185],
    "ch1-filter": [2967, 2164, 3094, 2315],
    "ch1-pfl": [3001, 2300, 3147, 2471],
    "ch1-fader": [3032, 1785, 3091, 2932],
}

_CH2_MIXER: dict[str, list[int]] = {
    "ch2-level": [3120, 2061, 3195, 2185],
    "ch2-eq-high": [3200, 2061, 3275, 2188],
    "ch2-eq-mid": [3280, 2062, 3355, 2185],
    "ch2-eq-low": [3360, 2066, 3435, 2185],
    "ch2-filter": [3390, 2164, 3517, 2315],
    "ch2-pfl": [3520, 2300, 3645, 2471],
    "ch2-fader": [3475, 1785, 3539, 2895],
}

# Deck 2 jog (read on page 6); deck 1 + 1625 px overlaps the mixer centre.
_PX_DECK_2_JOG = [3650, 2035, 4249, 2638]

# Pitch and bend caps sit in the outer column (read on page 6); a uniform shift overflows the plate.
_PX_DECK_2_OUTER: dict[str, list[int]] = {
    "d2-pitch": [3774, 2280, 3927, 2580],
    "d2-pitch-bend-down": [3525, 2575, 3630, 2705],
    "d2-pitch-bend-up": [3525, 2775, 3630, 2907],
}


def _shift_deck2(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    dx = DECK_SHIFT_PX + DECK2_EXTRA_PX
    return [x0 + dx, y0, x1 + dx, y1]


_PX_FX: dict[str, list[int]] = {
    "fx-echo": [2420, 1628, 2555, 1755],
    "fx-delay": [2575, 1625, 2700, 1750],
    "fx-flanger": [2745, 1625, 2870, 1750],
    "fx-phaser": [2900, 1625, 3030, 1750],
    "fx-toggle-1": [3180, 1630, 3280, 1735],
    "fx-toggle-2": [3275, 1630, 3375, 1735],
}

_PX_GLOBAL: dict[str, list[int]] = {
    "browse": [3070, 1623, 3194, 1738],
    "load-1": [4151, 1320, 4267, 1413],
    "load-2": [4320, 1310, 4479, 1419],
    "back": [3905, 1365, 3937, 1433],
    "view": [3527, 1365, 3559, 1433],
    "master-gain": [4035, 1360, 4172, 1490],
    "speaker-level": [4326, 1350, 4461, 1497],
    "head-mix": [4164, 1399, 4258, 1487],
    "head-gain": [4378, 1380, 4461, 1519],
    "crossfader": [3050, 2785, 3450, 2865],
}


def _shift(box: list[int]) -> list[int]:
    x0, y0, x1, y1 = box
    return [x0 + DECK_SHIFT_PX, y0, x1 + DECK_SHIFT_PX, y1]


_PX: dict[str, list[int]] = {
    **_PX_DECK_1,
    **{
        k.replace("d1-", "d2-"): (
            _PX_DECK_2_JOG
            if k == "d1-jog"
            else _shift_deck2(v)
            if k.replace("d1-", "d2-") not in _PX_DECK_2_OUTER
            else _PX_DECK_2_OUTER[k.replace("d1-", "d2-")]
        )
        for k, v in _PX_DECK_1.items()
    },
    **_CH1_MIXER,
    **_CH2_MIXER,
    **_PX_FX,
    **_PX_GLOBAL,
}

_PX_REPEAT: dict[str, list[int]] = {
    "d1-sync": [1100, 2228, 1220, 2345],
    "d1-pitch": [2152, 2282, 2298, 2578],
    "ch1-fader": [3098, 1788, 3145, 2892],
    "browse": [3073, 1620, 3190, 1735],
    "d2-jog": [3650, 2038, 4249, 2635],
    "fx-toggle-2": [3278, 1633, 3372, 1732],
}

TRACE: dict[str, object] = {
    "source": (
        "Numark Mixstream Pro User Guide v5.0.0, pages 6-7 (Features > Top Panel figure), vector "
        "drawing rendered at 600 dpi (page-06.png 4961 x 7017 px; page-07 continues callout text only)"
    ),
    "source_url": (
        "https://cdn.inmusicbrands.com/Software/ENDJ5/Mixstream%20Pro%2C%20Mixstream%20Pro%2B%2C%20"
        "Mixstream%20Pro%20Go%20-%20User%20Guide%20-%20v5.0.0.pdf"
    ),
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/Numark-Mixstream-Pro-sources.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX, "y": MM_PER_PX},
    "calibration": (
        "isotropic: one scale from the published 566 mm width over the plate outline 4069 px "
        "(x 450..4522); top-view depth 2041 px = 283.9 mm of the published 284 mm depth. The "
        "drawing below y 3365 is front-face art (callout 33 speakers); it is not part of the "
        "top-view trace."
    ),
    "method": (
        "Ink-edge read of the 600 dpi render: button = body outline, pad = pad face, knob = outer "
        "ring, jog = outer platter ring, vertical fader = travel slot with cap width, crossfader = "
        "slot with cap height, effect toggle = switch body. A second pass (_PX_REPEAT) re-read six "
        "controls from gridded crops; the largest centre difference is browse (1.8 mm)."
    ),
    "occluded": {
        "touchscreen": "callout 1: full-colour display, not a MIDI control",
        "main-meters": "callout 17: LEDs only",
        "speakers": "callout 33: speaker grilles below the top-view plate",
        "lighting": "callout 7: no wire in the Mixxx map",
        "control-center": "callout 8: touchscreen menu, not a separate MIDI control",
    },
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": 1.8,
    "issue": "#10870",
    "date": "Tue 10 Nov 2026",
}


# ----------------------------------------------------------------- helpers


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    s = MM_PER_PX
    return (
        round((x0 - PANEL_PX["x0"]) * s, 1),
        round((y0 - PANEL_PX["y0"]) * s, 1),
        round((x1 - x0 + 1) * s, 1),
        round((y1 - y0 + 1) * s, 1),
    )


# ----------------------------------------------------------------- control rows


_PAD_DOES = (
    "hot cue or stems pad, depending on pad mode",
    "hot cue or stems pad, depending on pad mode",
    "hot cue or stems pad, depending on pad mode",
    "hot cue or stems pad, depending on pad mode",
    "saved loop, auto loop, or roll pad depending on mode",
    "saved loop, auto loop, or roll pad depending on mode",
    "saved loop, auto loop, or roll pad depending on mode",
    "saved loop, auto loop, or roll pad depending on mode",
)


def _deck(deck: int) -> list[Control]:
    ch = 2 + deck  # Mixxx deck transport on channels 3 and 4
    d = f"(deck {deck})"

    def c(
        cid: str,
        fig: str,
        typ: str,
        label: str,
        does: str,
        section: str,
        midi: Wire | None,
    ) -> Control:
        return Control(f"d{deck}-{cid}", fig, typ, label, f"{does} {d}", 0, 0, 0, 0, section, midi, None)

    out: list[Control] = [
        c("jog", "21", "jog", "PLATTER", "Scratch or bend via the capacitive platter", "deck", Wire(ch, "cc", 55)),
        c("pitch", "26", "fader", "PITCH", "Tempo fader", "deck", Wire(ch, "cc", 31)),
        c(
            "pitch-bend-down",
            "27",
            "button",
            "PITCH BEND -",
            "Momentarily slow the track",
            "deck",
            Wire(ch, "note", 29),
        ),
        c(
            "pitch-bend-up",
            "27",
            "button",
            "PITCH BEND +",
            "Momentarily speed the track up",
            "deck",
            Wire(ch, "note", 30),
        ),
        c(
            "scratch",
            "22",
            "button",
            "SCRATCH",
            "Toggle Smart Scratch / vinyl platter mode",
            "deck",
            Wire(ch, "note", 35),
        ),
        c("sync", "23", "button", "SYNC", "Activate or deactivate sync", "deck", Wire(ch, "note", 8)),
        c("cue", "24", "button", "CUE", "Cue point and preview", "deck", Wire(ch, "note", 9)),
        c("play", "25", "button", "PLAY/PAUSE", "Start or pause the deck", "deck", Wire(ch, "note", 10)),
        c(
            "shift",
            "9",
            "button",
            "SHIFT",
            "Hold for secondary functions (software state in Mixxx)",
            "deck",
            Wire(ch, "note", 28),
        ),
        c(
            "mode-hotcue",
            "29",
            "button",
            "CUE",
            "Pad mode: hot cues or stems bank",
            "performance",
            Wire(ch, "note", 11),
        ),
        c(
            "mode-saved-loop",
            "30",
            "button",
            "SAVED LOOP",
            "Pad mode: saved loops",
            "performance",
            Wire(ch, "note", 12),
        ),
        c("mode-autoloop", "31", "button", "AUTO LOOP", "Pad mode: auto loops", "performance", Wire(ch, "note", 14)),
        c("mode-roll", "32", "button", "ROLL", "Pad mode: roll or sampler", "performance", Wire(ch, "note", 13)),
    ]
    pad_notes: tuple[int | None, ...] = (15, 16, 17, 18, None, None, None, None)
    for pad in range(8):
        wire = None if pad_notes[pad] is None else Wire(ch, "note", pad_notes[pad])
        out.append(
            c(
                f"pad-{pad + 1}",
                "28",
                "pad",
                f"PAD {pad + 1}",
                _PAD_DOES[pad],
                "performance",
                wire,
            )
        )
    return out


def _channel(ch: int) -> list[Control]:
    deck_ch = ch
    rows: list[tuple[str, str, str, str, str, Wire]] = [
        ("level", "10", "knob", "LEVEL", "Pre-fader channel gain", Wire(deck_ch, "cc", 3)),
        ("eq-high", "11", "knob", "HIGH", "EQ high band", Wire(deck_ch, "cc", 8)),
        ("eq-mid", "11", "knob", "MID", "EQ mid band", Wire(deck_ch, "cc", 6)),
        ("eq-low", "11", "knob", "LOW", "EQ low band", Wire(deck_ch, "cc", 4)),
        ("filter", "12", "knob", "FILTER", "Low-pass left, high-pass right", Wire(deck_ch, "cc", 11)),
        ("pfl", "18", "button", "CUE/PFL", "Headphone preview for this channel", Wire(deck_ch, "note", 13)),
        ("fader", "15", "fader", "CH FADER", "Channel volume", Wire(deck_ch, "cc", 14)),
    ]
    return [
        Control(f"ch{ch}-{cid}", fig, typ, label, f"{does} (channel {ch})", 0, 0, 0, 0, "mixer", midi, None)
        for cid, fig, typ, label, does, midi in rows
    ]


def _effects() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, Wire]] = [
        ("fx-echo", "13", "button", "ECHO", "Select the echo effect", Wire(5, "note", 0)),
        ("fx-delay", "13", "button", "DELAY", "Select the delay effect", Wire(5, "note", 1)),
        ("fx-flanger", "13", "button", "FLANGER", "Select the flanger effect", Wire(5, "note", 2)),
        ("fx-phaser", "13", "button", "PHASER", "Select the phaser effect", Wire(5, "note", 3)),
        ("fx-toggle-1", "14", "button", "FX 1", "Latch or momentary effect toggle", Wire(5, "note", 4)),
        ("fx-toggle-2", "14", "button", "FX 2", "Latch or momentary effect toggle for deck 2", Wire(6, "note", 4)),
    ]
    return [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, "effect", midi, None)
        for cid, fig, typ, label, does, midi in rows
    ]


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, Wire | None]] = [
        (
            "browse",
            "2",
            "encoder",
            "BROWSE",
            "Turn to scroll the library; press to select or load",
            "browse",
            Wire(16, "cc", 5),
        ),
        ("load-1", "3", "button", "LOAD 1", "Load the selected track to deck 1", "browse", Wire(16, "note", 1)),
        ("load-2", "4", "button", "LOAD 2", "Load the selected track to deck 2", "browse", Wire(16, "note", 2)),
        ("back", "5", "button", "BACK", "Move back in the touchscreen", "browse", Wire(16, "note", 3)),
        ("view", "6", "button", "VIEW", "Cycle library and performance views", "browse", Wire(16, "note", 16)),
        ("master-gain", "34", "knob", "MAIN LEVEL", "Main output volume", "mixer", Wire(16, "cc", 15)),
        ("speaker-level", "35", "knob", "SPEAKER LEVEL", "Built-in speaker volume", "mixer", Wire(16, "cc", 18)),
        ("head-mix", "19", "knob", "HEADPHONE MIX", "Blend cue and master in headphones", "mixer", Wire(16, "cc", 9)),
        ("head-gain", "20", "knob", "HEADPHONE VOL", "Headphone cue volume", "mixer", Wire(16, "cc", 10)),
        (
            "crossfader",
            "16",
            "crossfader",
            "CROSSFADER",
            "Blend between deck 1 and deck 2",
            "mixer",
            Wire(16, "cc", 14),
        ),
    ]
    return [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, section, midi, None)
        for cid, fig, typ, label, does, section, midi in rows
    ]


# ----------------------------------------------------------------- geometry


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
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


# ----------------------------------------------------------------- build


def _controls() -> list[Control]:
    base = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_effects(), *_globals()]
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
        "display_name": "2-deck standalone with touchscreen",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": "Numark Mixstream Pro User Guide v5.0.0, Technical Specifications page 43 (566 x 284 mm)",
            "wire": ("community Mixxx Numark-Mixstream-Pro.midi.xml (hints only via NUMARK_MIXSTREAM_PRO_MAP)"),
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                f"reference: devices/{DEVICE_ID}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "Touchscreen (fig 1): multi-touch display, no MIDI",
            "Lighting (fig 7): no Mixxx wire",
            "Control Center (fig 8): touchscreen menu",
            "Main Level Meters (fig 17): LEDs only",
            "Speakers (fig 33): below the top-view plate",
            "Booth gain: tier-2 map only; unwired on the replica",
            "Auxiliary 1 play (play_aux1): optional tier-2 wire, not drawn",
            "Pitch LSB (CC 75), jog LSB (CC 77), jog touch notes: folded into one control each",
            "Browse press (GoToItem note 6): same encoder, second wire listed here only",
            "Performance pads 5-8: eight pads on hardware; Mixxx maps only notes 15-18 (pads 1-4)",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
