#!/usr/bin/env python3
"""Build DDJ-1000 midi.json + layout.json from FLX10 template + DDJ-1000 PDF deltas."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "devices" / "ddj-1000"
FLX = ROOT / "devices" / "ddj-flx10" / "midi.json"
TXT = DEV / "source" / "DDJ-1000_MIDI_Message_List_E1.txt"
MIDI_OUT = DEV / "midi.json"
LAYOUT_OUT = DEV / "layout.json"
PLATE = "source/plate-top.png"

# One hit target per fig on the official plate (left-deck / center representative).
POS: dict[str, tuple[float, float, float, float, str]] = {
    "B1": (46.0, 3.5, 5.0, 7.0, "encoder"),
    "B2": (51.0, 5.0, 3.2, 3.8, "button"),
    "B3": (54.5, 5.0, 3.2, 3.8, "button"),
    "D1": (13.5, 78.5, 4.0, 5.5, "button"),
    "D2": (9.0, 72.5, 3.5, 4.5, "button"),
    "D3": (11.5, 38.0, 15.5, 30.0, "jog"),
    "D4": (29.5, 40.0, 2.8, 28.0, "fader"),
    "D5": (3.5, 28.0, 4.0, 8.0, "button"),
    "D6": (28.0, 70.0, 3.5, 4.0, "button"),
    "D7": (28.0, 75.0, 3.5, 3.5, "button"),
    "D8": (28.0, 79.5, 3.5, 3.5, "button"),
    "D9": (7.5, 8.0, 3.5, 3.5, "button"),
    "D10": (11.5, 8.0, 3.5, 3.5, "button"),
    "D11": (15.5, 8.0, 3.5, 3.5, "button"),
    "D12": (21.5, 8.0, 3.5, 3.5, "button"),
    "D13": (25.5, 8.0, 3.5, 3.5, "button"),
    "D14": (7.5, 13.0, 3.5, 3.5, "button"),
    "D15": (11.5, 13.0, 3.5, 3.5, "button"),
    "D16": (15.5, 13.0, 3.5, 3.5, "button"),
    "D17": (21.5, 13.0, 3.5, 3.5, "button"),
    "D18": (25.5, 13.0, 3.5, 3.5, "button"),
    "D19": (30.0, 13.0, 3.5, 3.5, "button"),
    "P1": (15.5, 72.0, 3.2, 4.0, "pad"),
    "P2": (19.0, 72.0, 3.2, 4.0, "pad"),
    "P3": (22.5, 72.0, 3.2, 4.0, "pad"),
    "P4": (26.0, 72.0, 3.2, 4.0, "pad"),
    "P5": (15.5, 76.5, 3.2, 4.0, "pad"),
    "P6": (19.0, 76.5, 3.2, 4.0, "pad"),
    "P7": (22.5, 76.5, 3.2, 4.0, "pad"),
    "P8": (26.0, 76.5, 3.2, 4.0, "pad"),
    "P9": (15.0, 66.5, 2.8, 3.2, "button"),
    "P10": (18.3, 66.5, 2.8, 3.2, "button"),
    "P11": (21.6, 66.5, 2.8, 3.2, "button"),
    "P12": (24.9, 66.5, 2.8, 3.2, "button"),
    "P13": (28.2, 66.5, 2.8, 3.2, "button"),
    "P14": (31.5, 66.5, 2.8, 3.2, "button"),
    "M1": (47.5, 88.0, 12.0, 4.5, "fader"),
    "M2": (41.5, 55.0, 2.5, 22.0, "fader"),
    "M3": (41.5, 28.0, 3.0, 4.0, "knob"),
    "M4": (41.5, 34.0, 3.0, 4.0, "knob"),
    "M5": (41.5, 40.0, 3.0, 4.0, "knob"),
    "M6": (41.5, 46.0, 3.0, 4.0, "knob"),
    "M7": (41.5, 52.0, 3.0, 3.5, "button"),
    "M8": (69.5, 6.0, 3.5, 5.0, "knob"),
    "M9": (69.5, 28.0, 3.0, 3.5, "button"),
    "M10": (69.5, 14.0, 3.5, 5.0, "knob"),
    "M11": (44.5, 78.0, 3.0, 3.5, "switch"),
    "M12": (47.5, 72.0, 3.5, 4.5, "knob"),
    "M13": (52.0, 72.0, 3.5, 4.5, "knob"),
    "M14": (45.5, 22.0, 3.0, 3.5, "knob"),
    "M15": (49.5, 22.0, 3.0, 3.5, "knob"),
    "M16": (84.0, 92.0, 8.0, 5.0, "switch"),
    "M17": (45.5, 12.0, 3.5, 4.0, "switch"),
    "M18": (45.5, 32.0, 3.0, 3.5, "switch"),
    "M19": (47.5, 62.0, 3.0, 3.5, "button"),
    "M20": (51.5, 62.0, 3.5, 4.5, "knob"),
    "M21": (69.5, 20.0, 4.0, 6.0, "meter"),
    "M22": (41.5, 24.0, 3.0, 3.0, "meter"),
    "F1": (53.5, 40.0, 3.0, 4.0, "knob"),
    "F2": (47.5, 36.0, 2.8, 3.2, "button"),
    "F3": (50.5, 36.0, 2.8, 3.2, "button"),
    "F4": (47.5, 40.0, 2.8, 3.2, "button"),
    "F5": (50.5, 40.0, 2.8, 3.2, "button"),
    "F6": (47.5, 44.0, 2.8, 3.2, "button"),
    "F7": (50.5, 44.0, 2.8, 3.2, "button"),
    "F8": (69.5, 36.0, 3.0, 3.2, "encoder"),
    "F9": (65.5, 48.0, 3.0, 3.2, "button"),
    "F10": (69.5, 48.0, 3.0, 3.2, "knob"),
    "F11": (65.5, 56.0, 3.0, 3.2, "button"),
}

LABELS = {
    "B1": "BROWSE",
    "B2": "BACK",
    "B3": "VIEW",
    "D1": "PLAY/PAUSE",
    "D2": "CUE",
    "D3": "JOG",
    "D4": "TEMPO",
    "D5": "MASTER TEMPO",
    "D6": "DECK SELECT",
    "D7": "BEAT SYNC",
    "D8": "KEY SYNC",
    "D9": "KEY RESET",
    "D10": "LOOP IN",
    "D11": "LOOP OUT",
    "D12": "4 BEAT LOOP/EXIT",
    "D13": "QUANTIZE",
    "D14": "SLIP",
    "D15": "SLIP REVERSE",
    "D16": "SEARCH <<",
    "D17": "SEARCH >>",
    "D18": "MEMORY",
    "D19": "SHIFT",
    "P1": "PAD 1",
    "P2": "PAD 2",
    "P3": "PAD 3",
    "P4": "PAD 4",
    "P5": "PAD 5",
    "P6": "PAD 6",
    "P7": "PAD 7",
    "P8": "PAD 8",
    "P9": "HOT CUE MODE",
    "P10": "PAD FX 1 MODE",
    "P11": "BEAT JUMP MODE",
    "P12": "SAMPLER MODE",
    "P13": "PAGE <",
    "P14": "PAGE >",
    "M1": "CROSSFADER",
    "M2": "CH FADER",
    "M3": "TRIM",
    "M4": "EQ HI",
    "M5": "EQ MID",
    "M6": "EQ LOW",
    "M7": "CUE",
    "M8": "MASTER LEVEL",
    "M9": "MASTER CUE",
    "M10": "BOOTH LEVEL",
    "M11": "CRF ASSIGN",
    "M12": "HEADPHONES MIX",
    "M13": "HEADPHONES LEVEL",
    "M14": "MIC EQ HI",
    "M15": "MIC EQ LOW",
    "M16": "LINE/PHONO",
    "M17": "INPUT SELECT",
    "M18": "MIC OFF/ON",
    "M19": "SAMPLER CUE",
    "M20": "SAMPLER VOL",
    "M21": "MASTER LEVEL METER",
    "M22": "CH LEVEL METER",
    "F1": "COLOR FX",
    "F2": "D-ECHO",
    "F3": "PITCH",
    "F4": "NOISE",
    "F5": "FILTER",
    "F6": "BEAT <",
    "F7": "BEAT >",
    "F8": "FX SELECT",
    "F9": "CH SELECT",
    "F10": "LEVEL/DEPTH",
    "F11": "FX ON/OFF",
}

FIGS = set(POS.keys())
FLX10_ONLY = {f"D{i}" for i in range(20, 26)} | {f"F{i}" for i in range(12, 17)}


def _fig(notes: str) -> str | None:
    m = re.search(r"\[([BDFMP]\d{1,2})\]", notes or "")
    return m.group(1) if m else None


def _retag(notes: str, fig: str) -> str:
    if _fig(notes):
        return notes
    return f"{notes} [{fig}]"


def _from_flx10() -> list[dict]:
    flx = json.loads(FLX.read_text())
    out: list[dict] = []
    for c in flx["controls"]:
        fig = _fig(c.get("notes", ""))
        if not fig or fig in FLX10_ONLY or fig not in FIGS:
            continue
        row = dict(c)
        out.append(row)
    return out


def _ddj1000_overrides() -> list[dict]:
    """Controls that differ from FLX10 or need DDJ-1000-specific rows."""
    return [
        # D19 is SHIFT on DDJ-1000 (FLX10 uses D25; D19 is mix point link there)
        {
            "name": "shift_button",
            "section": "deck",
            "type": "note",
            "ch": 1,
            "code": 63,
            "notes": "SHIFT press; per-deck channels 1-4 (PDF lists one row for ch 1/2/3/4) [D19]",
        },
        # F1: four COLOR FX parameter knobs (ch7 MSB/LSB pairs 23/55, 24/56, 25/57, 26/58)
        {
            "name": "color_fx_ch1",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 23,
            "notes": "14-bit MSB, LSB pair partner 0x37; COLOR FX parameter CH1 [F1]",
        },
        {
            "name": "color_fx_ch1_lsb",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 55,
            "notes": "14-bit LSB, MSB pair partner 0x17; COLOR FX parameter CH1 [F1]",
        },
        {
            "name": "color_fx_ch2",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 24,
            "notes": "14-bit MSB, LSB pair partner 0x38; COLOR FX parameter CH2 [F1]",
        },
        {
            "name": "color_fx_ch2_lsb",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 56,
            "notes": "14-bit LSB, MSB pair partner 0x18; COLOR FX parameter CH2 [F1]",
        },
        {
            "name": "color_fx_ch3",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 25,
            "notes": "14-bit MSB, LSB pair partner 0x39; COLOR FX parameter CH3 [F1]",
        },
        {
            "name": "color_fx_ch3_lsb",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 57,
            "notes": "14-bit LSB, MSB pair partner 0x19; COLOR FX parameter CH3 [F1]",
        },
        {
            "name": "color_fx_ch4",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 26,
            "notes": "14-bit MSB, LSB pair partner 0x3A; COLOR FX parameter CH4 [F1]",
        },
        {
            "name": "color_fx_ch4_lsb",
            "section": "effect",
            "type": "cc",
            "ch": 7,
            "code": 58,
            "notes": "14-bit LSB, MSB pair partner 0x1A; COLOR FX parameter CH4 [F1]",
        },
        # F8 FX SELECT encoder (many NOTE rows on ch5; primary LOW CUT)
        {
            "name": "fx_select_low_cut",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 32,
            "notes": "FX SELECT rotate primary (LOW CUT); ch5 effect channel [F8]",
        },
        {
            "name": "fx_select_echo",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 33,
            "notes": "FX SELECT (ECHO) [F8]",
        },
        {
            "name": "fx_select_mt_delay",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 34,
            "notes": "FX SELECT (MT DELAY) [F8]",
        },
        {
            "name": "fx_select_spiral",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 35,
            "notes": "FX SELECT (SPIRAL) [F8]",
        },
        {
            "name": "fx_select_reverb",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 36,
            "notes": "FX SELECT (REVERB) [F8]",
        },
        # F9 CH SELECT encoder
        {
            "name": "fx_ch_select_ch1",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 16,
            "notes": "CH SELECT (CH1) rotate [F9]",
        },
        {
            "name": "fx_ch_select_ch2",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 17,
            "notes": "CH SELECT (CH2) [F9]",
        },
        {
            "name": "fx_ch_select_ch3",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 18,
            "notes": "CH SELECT (CH3) [F9]",
        },
        {
            "name": "fx_ch_select_ch4",
            "section": "effect",
            "type": "note",
            "ch": 5,
            "code": 19,
            "notes": "CH SELECT (CH4) [F9]",
        },
        # M21 master level meter (no MIDI-IN)
        {
            "name": "master_level_meter",
            "section": "mixer",
            "type": "note",
            "ch": 7,
            "code": 0,
            "notes": "Master level meter LED display without MIDI-IN control [M21]",
        },
    ]


def build_midi() -> dict:
    controls = _from_flx10()
    # Drop FLX10 D19 mix_point_link rows; DDJ-1000 D19 is SHIFT
    controls = [c for c in controls if _fig(c.get("notes", "")) != "D19"]
    # Drop FLX10 F1/F8/F9 single-knob rows; replaced by DDJ-1000 variants
    controls = [c for c in controls if _fig(c.get("notes", "")) not in {"F1", "F8", "F9"}]
    controls.extend(_ddj1000_overrides())

    # Ensure every layout fig has at least one tagged row
    tagged = {_fig(c["notes"]) for c in controls if _fig(c.get("notes", ""))}
    missing = FIGS - tagged
    if missing:
        raise SystemExit(f"midi build missing fig tags: {sorted(missing, key=lambda x: (x[0], int(x[1:])))}")

    return {
        "source": "docs/controller/reference/DDJ-1000_MIDI_Message_List_E1.pdf",
        "archived": "tools/deck-diagrams/devices/ddj-1000/source/DDJ-1000_MIDI_Message_List_E1.pdf",
        "device": "DDJ-1000",
        "vendor": "AlphaTheta / Pioneer DJ",
        "channel_conventions": {
            "deck_rows": "PDF lists deck controls as one row for ch 1/2/3/4; entries carry ch=1 with note 'per-deck channels 1-4'",
            "pad_rows": "PDF lists pads as one row for ch 8/10/12/14 (unshifted) and 9/11/13/15 (shifted); entries carry ch=8 or ch=9 with expansion in notes",
            "channel_assignment": "ch1-4 deck 1-4 (non-pad), ch5 effect, ch6 unused, ch7 browser + global mixer, ch8-15 performance pads (deck1 8/9, deck2 10/11, deck3 12/13, deck4 14/15; odd = shifted), ch16 MIDI-OUT illumination control",
        },
        "controls": controls,
    }


def _primary(ctrls: list[dict]) -> dict:
    for c in ctrls:
        n = c["name"]
        if n.endswith("_shift") or n.endswith("_lsb"):
            continue
        return c
    return ctrls[0]


def _shift_name(ctrls: list[dict], primary: dict) -> str | None:
    base = primary["name"]
    for c in ctrls:
        if c["name"] == f"{base}_shift":
            return c["name"]
    for c in ctrls:
        if c["name"].endswith("_shift"):
            return c["name"]
    return None


def build_layout(midi: dict) -> dict:
    by_fig: dict[str, list[dict]] = defaultdict(list)
    for c in midi["controls"]:
        fig = _fig(c.get("notes", ""))
        if fig:
            by_fig[fig].append(c)

    controls = []
    for fig in sorted(POS, key=lambda x: (x[0], int(x[1:]))):
        x, y, w, h, kind = POS[fig]
        ctrls = by_fig[fig]
        primary = _primary(ctrls)
        shift = _shift_name(ctrls, primary)
        controls.append(
            {
                "id": f"ddj-1000-{fig.lower()}",
                "fig": fig,
                "label": LABELS[fig],
                "kind": kind,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "layer": "both" if shift else "base",
                "section": primary.get("section", "unknown"),
                "midi": {
                    "name": primary["name"],
                    "type": primary["type"],
                    "ch": primary["ch"],
                    "code": primary["code"],
                    "shift_name": shift,
                },
                "notes": primary.get("notes", ""),
            }
        )

    from PIL import Image

    pw, ph = Image.open(DEV / PLATE).size
    return {
        "device": "DDJ-1000",
        "vendor": "AlphaTheta / Pioneer DJ",
        "plate": {"image": PLATE, "width": pw, "height": ph},
        "layers": ["base", "shift"],
        "out_of_scope": [
            "jog_screen_bitmap_hid",
            "vu_meter_led_animation_hid",
            "right_deck_mirror_labels_same_figs",
        ],
        "controls": controls,
    }


def main() -> None:
    midi = build_midi()
    MIDI_OUT.write_text(json.dumps(midi, indent=2) + "\n")
    layout = build_layout(midi)
    LAYOUT_OUT.write_text(json.dumps(layout, indent=2) + "\n")
    figs = sorted({_fig(c["notes"]) for c in midi["controls"] if _fig(c.get("notes", ""))}, key=lambda x: (x[0], int(x[1:])))
    print(f"wrote {MIDI_OUT.name}: {len(midi['controls'])} rows, {len(figs)} fig tags")
    print(f"wrote {LAYOUT_OUT.name}: {len(layout['controls'])} controls")


if __name__ == "__main__":
    main()
