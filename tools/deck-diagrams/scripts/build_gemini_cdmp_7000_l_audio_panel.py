"""Build the Gemini CDMP-7000 L audio practice-replica layout pair (ADR-0137, issue #10913).

Writes tools/deck-diagrams/devices/gemini-cdmp-7000-l-audio/panel.reference.json (the measurement
of record, raw trace) and panel.json (the same trace normalised for the renderer). Fractional
tables DECK_FRAC and MIXER_FRAC are the single source of position truth on the schematic crops.

Sources:
- Panel size 720 x 315 mm (W x D): Gemini CDMP-7000 user manual Specifications.
- Positions: traced from the top-view schematics on pages 7-8, composited at 200 dpi
  (composite PNG 3975 x 810 px; pdf and render sha256 in TRACE). Deck plate 1325 x 810 left
  and right; mixer strip 1325 x 695 centred; COMP_W=3975, COMP_H=810.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/gemini-cdmp-7000-l-audio.ts
  (GEMINI_CDMP7000_L_MAP): 14 deck bindings plus hint wires on deck 1; deck 2 mirrored
  unwired; analog mixer unwired.

Run: uv run --locked python tools/deck-diagrams/scripts/build_gemini_cdmp_7000_l_audio_panel.py
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 720.0
PANEL_D_MM = 315.0
DEVICE_ID = "gemini-cdmp-7000-l-audio"
MAP_CONST = "GEMINI_CDMP7000_L_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE_ID
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

COMP_W = 3975
COMP_H = 810
DECK_W = 1325
DECK_H = 810
MIXER_W = 1325
MIXER_H = 695
MIXER_X = 1325
MIXER_Y = 57
DECK2_X = 2650

IMAGE_PX = (COMP_W, COMP_H)
PANEL_PX = {"x0": 0, "x1": COMP_W - 1, "y0": 0, "y1": COMP_H - 1}
MM_PER_PX_X = round(PANEL_W_MM / COMP_W, 5)
MM_PER_PX_Y = round(PANEL_D_MM / COMP_H, 5)
SOURCE_PDF_SHA256 = "cec06edcbc28f996c118b5a458dc67949b8d253d9b0cb224a9824d4ebe94b210"
SOURCE_IMAGE_SHA256 = "8ca16047ec0432273105ce81f3e94bfc131503be4223d9e627b9922eba71082f"
ALIGN_TOL_MM = 1.5
MIDI_CH = 1


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


DECK_FRAC: dict[str, list[float]] = {
    "auto-cue": [0.2, 0.084, 0.0498, 0.0407],
    "back": [0.7698, 0.084, 0.0498, 0.0407],
    "bpm-btn": [0.3796, 0.084, 0.0498, 0.0407],
    "browse": [0.6996, 0.1198, 0.0604, 0.0593],
    "browse-left": [0.6596, 0.1938, 0.04, 0.0407],
    "browse-right": [0.76, 0.1938, 0.04, 0.0407],
    "cue": [0.0845, 0.679, 0.0498, 0.0802],
    "eject": [0.8294, 0.084, 0.0506, 0.0407],
    "fx-1": [0.2498, 0.3938, 0.0498, 0.0457],
    "fx-2": [0.3094, 0.3938, 0.0506, 0.0457],
    "fx-3": [0.3698, 0.3938, 0.0498, 0.0457],
    "fx-drywet": [0.52, 0.3938, 0.0498, 0.0506],
    "fx-hold": [0.4294, 0.3938, 0.0506, 0.0457],
    "hotcue-1": [0.0747, 0.2346, 0.0498, 0.0494],
    "hotcue-2": [0.0747, 0.184, 0.0498, 0.0506],
    "hotcue-3": [0.0747, 0.1346, 0.0498, 0.0494],
    "jog": [0.2694, 0.5198, 0.3502, 0.3494],
    "jog-adj": [0.0543, 0.4099, 0.0506, 0.0494],
    "jog-brake": [0.6996, 0.2593, 0.0498, 0.0506],
    "jog-start": [0.6996, 0.2, 0.0498, 0.0494],
    "load": [0.7094, 0.2543, 0.0506, 0.0506],
    "lock": [0.44, 0.084, 0.0498, 0.0407],
    "loop-in": [0.3298, 0.4691, 0.0498, 0.0457],
    "loop-out": [0.3894, 0.4691, 0.0506, 0.0457],
    "master-tempo": [0.7894, 0.4741, 0.0506, 0.0457],
    "memo": [0.0747, 0.3, 0.0498, 0.0444],
    "pitch": [0.8996, 0.2494, 0.0453, 0.5099],
    "pitch-down": [0.7894, 0.7, 0.0506, 0.0444],
    "pitch-up": [0.7894, 0.6395, 0.0506, 0.0444],
    "play": [0.0845, 0.7741, 0.0498, 0.0802],
    "range": [0.7894, 0.4198, 0.0506, 0.0444],
    "reloop": [0.4498, 0.4691, 0.0498, 0.0457],
    "reverse": [0.0845, 0.4741, 0.0604, 0.0457],
    "search-back": [0.0845, 0.5938, 0.0302, 0.0605],
    "search-fwd": [0.1147, 0.5938, 0.0302, 0.0605],
    "skip-back": [0.0845, 0.5346, 0.0302, 0.0494],
    "skip-fwd": [0.1147, 0.5346, 0.0302, 0.0494],
    "slip": [0.0845, 0.3543, 0.0498, 0.0506],
    "source-cd": [0.8845, 0.2593, 0.0498, 0.0407],
    "source-midi": [0.8845, 0.3148, 0.0498, 0.0395],
    "source-sd": [0.8845, 0.1494, 0.0498, 0.0395],
    "source-usb": [0.8845, 0.2049, 0.0498, 0.0395],
    "text-mode": [0.2596, 0.084, 0.0498, 0.0407],
    "touch-on": [0.32, 0.084, 0.0498, 0.0407],
    "vinyl": [0.7894, 0.3642, 0.0506, 0.0457],
}

MIXER_FRAC: dict[str, list[float]] = {
    "ch1-cue": [0.0694, 0.3842, 0.0506, 0.0576],
    "ch1-eq-hi": [0.0747, 0.1209, 0.0498, 0.059],
    "ch1-eq-lo": [0.0747, 0.2849, 0.0498, 0.0576],
    "ch1-eq-mid": [0.0747, 0.2029, 0.0498, 0.0576],
    "ch1-fader": [0.0845, 0.4647, 0.0302, 0.4777],
    "ch1-gain": [0.08, 0.0403, 0.0498, 0.0576],
    "ch1-input": [0.0596, -0.036, 0.0702, 0.0576],
    "ch2-cue": [0.3894, 0.3842, 0.0506, 0.0576],
    "ch2-eq-hi": [0.3947, 0.1209, 0.0498, 0.059],
    "ch2-eq-lo": [0.3947, 0.2849, 0.0498, 0.0576],
    "ch2-eq-mid": [0.3947, 0.2029, 0.0498, 0.0576],
    "ch2-fader": [0.4045, 0.4647, 0.0302, 0.4777],
    "ch2-gain": [0.4, 0.0403, 0.0498, 0.0576],
    "ch2-input": [0.3796, -0.036, 0.0702, 0.0576],
    "ch3-cue": [0.7094, 0.3842, 0.0506, 0.0576],
    "ch3-eq-hi": [0.7147, 0.1209, 0.0498, 0.059],
    "ch3-eq-lo": [0.7147, 0.2849, 0.0498, 0.0576],
    "ch3-eq-mid": [0.7147, 0.2029, 0.0498, 0.0576],
    "ch3-fader": [0.7245, 0.4647, 0.0302, 0.4777],
    "ch3-gain": [0.72, 0.0403, 0.0498, 0.0576],
    "ch3-input": [0.6996, -0.036, 0.0702, 0.0576],
    "crossfader": [0.2, 1.0187, 0.5494, 0.046],
    "master-balance": [0.84, 0.3597, 0.0498, 0.059],
    "master-vol": [0.84, 0.0101, 0.0498, 0.059],
    "master-vu": [0.8543, 0.0863, 0.0604, 0.2504],
    "phones-mix": [0.84, 0.4417, 0.0498, 0.059],
    "phones-switch": [0.8498, 0.5986, 0.0649, 0.0417],
    "phones-vol": [0.84, 0.5237, 0.0498, 0.0576],
    "xf-curve": [0.0294, 0.9727, 0.04, 0.0403],
    "xf-rev-1": [0.1698, 0.9727, 0.04, 0.0403],
    "xf-rev-2": [0.4898, 0.9727, 0.04, 0.0403],
    "xf-rev-3": [0.8098, 0.9727, 0.04, 0.0403],
    "xf-start-1": [0.12, 0.9727, 0.04, 0.0403],
    "xf-start-2": [0.44, 0.9727, 0.04, 0.0403],
    "xf-start-3": [0.76, 0.9727, 0.04, 0.0403],
}

REPEAT_FRAC: dict[str, list[float]] = {
    "ch1-fader": [0.0883, 0.4647, 0.0302, 0.4777],
    "crossfader": [0.203, 1.0187, 0.5494, 0.046],
    "jog": [0.2725, 0.5198, 0.3502, 0.3494],
    "master-tempo": [0.7925, 0.4741, 0.0506, 0.0457],
    "pitch": [0.9026, 0.2494, 0.0453, 0.5099],
    "play": [0.0875, 0.7741, 0.0498, 0.0802],
}

# suffix, fig, type, label, does, wire or None
_D1_ROWS: list[tuple[str, str, str, str, str, Wire | None]] = [
    ("play", "2", "button", "PLAY/PAUSE", "Play or pause the loaded track", Wire(1, "note", 2)),
    ("cue", "3", "button", "CUE", "Cue to the stored point", Wire(1, "note", 1)),
    ("search-back", "4", "button", "SEARCH <<", "Search backward in the track", Wire(1, "note", 9)),
    ("search-fwd", "4", "button", "SEARCH >>", "Search forward in the track", Wire(1, "note", 10)),
    ("skip-back", "5", "button", "TRACK SKIP <<", "Skip to the previous track", Wire(1, "note", 34)),
    ("skip-fwd", "5", "button", "TRACK SKIP >>", "Skip to the next track", Wire(1, "note", 35)),
    ("reverse", "6", "button", "REVERSE", "Play the track backward", Wire(1, "note", 30)),
    ("jog-adj", "7", "knob", "JOG ADJUST", "Jog sensitivity (local)", None),
    ("loop-in", "8", "button", "LOOP IN", "Set manual loop in point", Wire(1, "note", 16)),
    ("loop-out", "8", "button", "LOOP OUT", "Set manual loop out point", Wire(1, "note", 17)),
    ("reloop", "8", "button", "RELOOP/EXIT", "Reloop or exit the active loop", Wire(1, "note", 18)),
    ("memo", "9", "button", "MEMO", "Store or recall a memo point", Wire(1, "note", 8)),
    ("hotcue-1", "10", "pad", "HOT CUE 1", "Jump to hot cue slot 1", Wire(1, "note", 5)),
    ("hotcue-2", "10", "pad", "HOT CUE 2", "Jump to hot cue slot 2", Wire(1, "note", 6)),
    ("hotcue-3", "10", "pad", "HOT CUE 3", "Jump to hot cue slot 3", Wire(1, "note", 7)),
    ("auto-cue", "11", "button", "AUTO CUE/TIME", "LCD overlay control", None),
    ("text-mode", "12", "button", "TEXT MODE", "LCD text mode", None),
    ("touch-on", "12", "button", "TOUCH ON/OFF", "LCD touch on/off", None),
    ("bpm-btn", "13", "button", "BPM", "Toggle BPM display mode", Wire(1, "note", 19)),
    ("lock", "14", "button", "LOCK", "Lock pitch/tempo", Wire(1, "note", 28)),
    ("fx-1", "16", "button", "EFFECT 1", "Select effect 1", Wire(1, "note", 20)),
    ("fx-2", "16", "button", "EFFECT 2", "Select effect 2", Wire(1, "note", 21)),
    ("fx-3", "16", "button", "EFFECT 3", "Select effect 3", Wire(1, "note", 22)),
    ("fx-hold", "16", "button", "HOLD", "Hold the selected effect", Wire(1, "note", 36)),
    ("browse", "17", "encoder", "SELECT", "Browse encoder", None),
    ("browse-left", "17", "button", "BROWSE LEFT", "Move up in the track list", Wire(1, "note", 11)),
    ("browse-right", "17", "button", "BROWSE RIGHT", "Move down in the track list", Wire(1, "note", 12)),
    ("load", "15", "button", "LOAD", "Load the highlighted track", Wire(1, "note", 15)),
    ("back", "18", "button", "BACK", "Leave the browser folder", None),
    ("eject", "19", "button", "EJECT", "Eject the media tray", None),
    ("source-sd", "20", "button", "SD", "Select the SD source", None),
    ("source-usb", "20", "button", "USB", "Select the USB source", None),
    ("source-cd", "20", "button", "CD", "Select the CD source", None),
    ("source-midi", "20", "button", "MIDI", "Select the MIDI source", None),
    ("jog-start", "21", "knob", "START TIME", "Jog start time", None),
    ("jog-brake", "22", "knob", "BRAKE", "Jog brake time", None),
    ("fx-drywet", "23", "knob", "DRY/WET", "Effect dry/wet mix", Wire(1, "cc", 41)),
    ("vinyl", "24", "button", "VINYL", "Vinyl jog mode", Wire(1, "note", 14)),
    ("slip", "25", "knob", "SLIP", "Slip mode", Wire(1, "note", 31)),
    ("range", "26", "button", "RANGE", "Tempo range", Wire(1, "note", 29)),
    ("master-tempo", "27", "button", "MASTER TEMPO", "Master tempo lock", Wire(1, "note", 13)),
    ("pitch-down", "28", "button", "PITCH -", "Momentary pitch bend down", Wire(1, "note", 4)),
    ("pitch-up", "28", "button", "PITCH +", "Momentary pitch bend up", Wire(1, "note", 3)),
    ("pitch", "29", "fader", "PITCH", "Pitch/tempo slider", Wire(1, "cc", 33)),
    ("jog", "30", "jog", "JOG", "Scratch or nudge the platter", Wire(1, "cc", 32)),
]

_MIXER_ROWS: list[tuple[str, str, str, str, str]] = [
    ("ch1-input", "31", "button", "CH1 INPUT", "Channel 1 input selector"),
    ("ch2-input", "31", "button", "CH2 INPUT", "Channel 2 input selector"),
    ("ch3-input", "31", "button", "CH3 INPUT", "Channel 3 input selector"),
    ("ch1-gain", "45", "knob", "CH1 GAIN", "Channel 1 gain"),
    ("ch2-gain", "45", "knob", "CH2 GAIN", "Channel 2 gain"),
    ("ch3-gain", "45", "knob", "CH3 GAIN", "Channel 3 gain"),
    ("ch1-eq-hi", "44", "knob", "CH1 HI", "Channel 1 high EQ"),
    ("ch1-eq-mid", "44", "knob", "CH1 MID", "Channel 1 mid EQ"),
    ("ch1-eq-lo", "44", "knob", "CH1 LO", "Channel 1 low EQ"),
    ("ch2-eq-hi", "44", "knob", "CH2 HI", "Channel 2 high EQ"),
    ("ch2-eq-mid", "44", "knob", "CH2 MID", "Channel 2 mid EQ"),
    ("ch2-eq-lo", "44", "knob", "CH2 LO", "Channel 2 low EQ"),
    ("ch3-eq-hi", "44", "knob", "CH3 HI", "Channel 3 high EQ"),
    ("ch3-eq-mid", "44", "knob", "CH3 MID", "Channel 3 mid EQ"),
    ("ch3-eq-lo", "44", "knob", "CH3 LO", "Channel 3 low EQ"),
    ("ch1-cue", "35", "button", "CH1 CUE", "Cue channel 1 to headphones"),
    ("ch2-cue", "35", "button", "CH2 CUE", "Cue channel 2 to headphones"),
    ("ch3-cue", "35", "button", "CH3 CUE", "Cue channel 3 to headphones"),
    ("ch1-fader", "43", "fader", "CH1 FADER", "Channel 1 fader"),
    ("ch2-fader", "43", "fader", "CH2 FADER", "Channel 2 fader"),
    ("ch3-fader", "43", "fader", "CH3 FADER", "Channel 3 fader"),
    ("master-vol", "32", "knob", "MASTER", "Master volume"),
    ("master-vu", "33", "button", "VU METER", "VU meter display (visual only)"),
    ("master-balance", "34", "knob", "BALANCE", "Master balance"),
    ("phones-mix", "36", "knob", "PHONES MIX", "Headphone mix"),
    ("phones-vol", "37", "knob", "PHONES VOL", "Headphone volume"),
    ("phones-switch", "38", "button", "PHONES SWITCH", "Headphone output switch"),
    ("xf-start-1", "39", "button", "XF START 1", "Crossfader start channel 1"),
    ("xf-start-2", "39", "button", "XF START 2", "Crossfader start channel 2"),
    ("xf-start-3", "39", "button", "XF START 3", "Crossfader start channel 3"),
    ("xf-rev-1", "40", "button", "XF REV 1", "Crossfader reverse channel 1"),
    ("xf-rev-2", "40", "button", "XF REV 2", "Crossfader reverse channel 2"),
    ("xf-rev-3", "40", "button", "XF REV 3", "Crossfader reverse channel 3"),
    ("xf-curve", "42", "knob", "XF CURVE", "Crossfader curve"),
    ("crossfader", "41", "crossfader", "CROSSFADER", "Blend the three channels"),
]


def _frac_box(frac: list[float], ox: int, oy: int, w: int, h: int) -> list[int]:
    fx, fy, fw, fh = frac
    x0 = int(round(fx * w)) + ox
    y0 = int(round(fy * h)) + oy
    x1 = int(round((fx + fw) * w)) - 1 + ox
    y1 = int(round((fy + fh) * h)) - 1 + oy
    return [x0, y0, x1, y1]


def _mirror(px: list[int]) -> list[int]:
    x0, y0, x1, y1 = px
    return [DECK2_X + (DECK_W - 1 - x1), y0, DECK2_X + (DECK_W - 1 - x0), y1]


def _build_px() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for suf, frac in DECK_FRAC.items():
        px = _frac_box(frac, 0, 0, DECK_W, DECK_H)
        out[f"d1-{suf}"] = px
        out[f"d2-{suf}"] = _mirror(px)
    for cid, frac in MIXER_FRAC.items():
        out[cid] = _frac_box(frac, MIXER_X, MIXER_Y, MIXER_W, MIXER_H)
    return out


def _repeat_px() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for key, rfrac in REPEAT_FRAC.items():
        if key in MIXER_FRAC or key.startswith("ch") or key == "crossfader":
            cid = key
            out[cid] = _frac_box(rfrac, MIXER_X, MIXER_Y, MIXER_W, MIXER_H)
        else:
            cid = f"d1-{key}"
            out[cid] = _frac_box(rfrac, 0, 0, DECK_W, DECK_H)
    return out


def _repeat_spread_mm(primary: dict[str, list[int]], repeat: dict[str, list[int]]) -> float:
    worst = 0.0
    for cid, rpx in repeat.items():
        p0 = primary[cid]
        pcx = (p0[0] + p0[2]) / 2 * MM_PER_PX_X
        pcy = (p0[1] + p0[3]) / 2 * MM_PER_PX_Y
        rcx = (rpx[0] + rpx[2]) / 2 * MM_PER_PX_X
        rcy = (rpx[1] + rpx[3]) / 2 * MM_PER_PX_Y
        worst = max(worst, math.hypot(rcx - pcx, rcy - pcy))
    return round(worst, 1)


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    return (
        round((x0 - PANEL_PX["x0"]) * MM_PER_PX_X, 2),
        round((y0 - PANEL_PX["y0"]) * MM_PER_PX_Y, 2),
        round((x1 - x0 + 1) * MM_PER_PX_X, 2),
        round((y1 - y0 + 1) * MM_PER_PX_Y, 2),
    )


TRACE: dict[str, object] = {
    "source": "Gemini CDMP-7000 user manual pages 7-8 top-view schematics",
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/Gemini-CDMP-7000-MIDI-Device-Messages-Table.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
    "method": "Manual trace on cropped manual schematic plates at 200 dpi",
    "issue": "#10913",
    "date": "Thu 12 Nov 2026",
}


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


def _deck1() -> list[Control]:
    return [
        Control(f"d1-{suf}", fig, typ, label, does, 0, 0, 0, 0, "deck", midi, None)
        for suf, fig, typ, label, does, midi in _D1_ROWS
    ]


def _deck2() -> list[Control]:
    d2_labels = {
        "hotcue-1": "HOT CUE 2",
        "hotcue-2": "HOT CUE 2",
        "hotcue-3": "HOT CUE 3",
    }
    out: list[Control] = []
    for suf, fig, typ, label, does, _midi in _D1_ROWS:
        label = d2_labels.get(suf, label)
        out.append(Control(f"d2-{suf}", fig, typ, label, does, 0, 0, 0, 0, "deck", None, None))
    return out


def _mixer() -> list[Control]:
    return [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, "mixer", None, None)
        for cid, fig, typ, label, does in _MIXER_ROWS
    ]


def _controls() -> list[Control]:
    return [*_deck1(), *_deck2(), *_mixer()]


def build_reference() -> dict[str, object]:
    px_map = _build_px()
    global _PX  # noqa: PLW0603
    _PX = px_map
    repeat = _repeat_px()
    spread = _repeat_spread_mm(px_map, repeat)
    trace = dict(TRACE)
    trace["repeat_spread_mm"] = spread
    trace["repeat_pass"] = [
        "ch1-fader",
        "crossfader",
        "d1-jog",
        "d1-master-tempo",
        "d1-pitch",
        "d1-play",
    ]
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": trace,
        "controls": [_mm_row(c.id) for c in controls],
    }


def _mm_row(cid: str) -> dict[str, object]:
    x, y, w, h = _mm_box(_PX[cid])
    return {"id": cid, "px": list(_PX[cid]), "x": x, "y": y, "w": w, "h": h}


_PX: dict[str, list[int]] = {}


def build() -> dict[str, object]:
    px_map = _build_px()
    global _PX  # noqa: PLW0603
    _PX = px_map
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {c.x},{c.y},{c.w},{c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE_ID,
        "display_name": "Dual-deck media workstation (Deck 1 / L port)",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": MAP_CONST,
        "sources": {
            "panel_size": "Gemini CDMP-7000 user manual Specifications: 720 x 315 x 100 mm (W x D x H)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/gemini-cdmp-7000-l-audio.ts",
            "positions": "Manual trace on pages 7-8 top-view schematics; see panel.reference.json",
        },
        "out_of_scope": [
            "Deck 2 layout only until issue #10829 lands the R-port map",
            "Analog mixer sends no MIDI rows on this hardware",
            "LCD touch display is visual only on the practice route",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")
