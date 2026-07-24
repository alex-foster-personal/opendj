#!/usr/bin/env python3
"""Build devices/ddj-flx10/layout.json from midi.json + plate fig positions."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "devices" / "ddj-flx10"
MIDI_PATH = DEV / "midi.json"
OUT = DEV / "layout.json"
PLATE = "source/plate-top.png"

# Percent-of-plate hit targets aligned to official MIDI-list plate photo.
# Iterate against overlay.html (red) until clusters match the reference.
# Keys = vendor fig codes from the PDF.
POS: dict[str, tuple[float, float, float, float, str]] = {
    # fig: (x, y, w, h, kind)
    # --- browser (top center) ---
    "B1": (46.5, 4.0, 4.5, 6.5, "encoder"),
    "B2": (51.5, 5.5, 3.0, 3.5, "button"),
    "B3": (55.0, 5.5, 3.0, 3.5, "button"),
    # --- left deck transport / jog ---
    "D1": (14.0, 78.0, 4.0, 5.5, "button"),  # PLAY/PAUSE
    "D2": (9.5, 72.0, 3.5, 4.5, "button"),  # CUE
    "D3": (12.0, 38.0, 16.0, 30.0, "jog"),  # jog
    "D4": (30.0, 40.0, 3.0, 28.0, "fader"),  # tempo
    "D5": (4.0, 28.0, 4.0, 8.0, "button"),  # deck select 3/1
    "D6": (28.5, 70.0, 3.5, 4.0, "button"),  # sync
    "D7": (28.5, 75.0, 3.5, 3.5, "button"),  # tempo reset
    "D8": (28.5, 79.5, 3.5, 3.5, "button"),  # key sync
    # --- stems / loop row (upper left deck) ---
    "D9": (8.0, 8.0, 3.5, 3.5, "button"),
    "D10": (12.0, 8.0, 3.5, 3.5, "button"),
    "D11": (16.0, 8.0, 3.5, 3.5, "button"),
    "D12": (22.0, 8.0, 3.5, 3.5, "button"),
    "D13": (26.0, 8.0, 3.5, 3.5, "button"),
    "D14": (8.0, 13.0, 3.5, 3.5, "button"),
    "D15": (12.0, 13.0, 3.5, 3.5, "button"),
    "D16": (16.0, 13.0, 3.5, 3.5, "button"),
    "D17": (22.0, 13.0, 3.5, 3.5, "button"),
    "D18": (26.0, 13.0, 3.5, 3.5, "button"),
    "D19": (30.5, 13.0, 3.5, 3.5, "button"),
    "D20": (4.5, 20.0, 4.0, 4.0, "button"),
    "D21": (34.0, 20.0, 3.5, 3.5, "button"),
    "D22": (38.0, 20.0, 3.5, 3.5, "button"),
    "D23": (6.0, 84.0, 3.5, 4.0, "button"),
    "D24": (10.0, 84.0, 3.5, 4.0, "button"),
    "D25": (4.0, 78.0, 4.0, 5.0, "button"),  # SHIFT
    # --- performance pads (left) ---
    "P1": (16.0, 72.0, 3.2, 4.0, "pad"),
    "P2": (19.5, 72.0, 3.2, 4.0, "pad"),
    "P3": (23.0, 72.0, 3.2, 4.0, "pad"),
    "P4": (26.5, 72.0, 3.2, 4.0, "pad"),
    "P5": (16.0, 76.5, 3.2, 4.0, "pad"),
    "P6": (19.5, 76.5, 3.2, 4.0, "pad"),
    "P7": (23.0, 76.5, 3.2, 4.0, "pad"),
    "P8": (26.5, 76.5, 3.2, 4.0, "pad"),
    "P9": (15.5, 66.5, 2.8, 3.2, "button"),
    "P10": (18.8, 66.5, 2.8, 3.2, "button"),
    "P11": (22.1, 66.5, 2.8, 3.2, "button"),
    "P12": (25.4, 66.5, 2.8, 3.2, "button"),
    "P13": (28.7, 66.5, 2.8, 3.2, "button"),
    "P14": (32.0, 66.5, 2.8, 3.2, "button"),
    # --- mixer / master ---
    "M1": (48.0, 88.0, 12.0, 4.5, "fader"),  # crossfader
    "M2": (42.0, 55.0, 2.5, 22.0, "fader"),  # ch fader (repr ch1)
    "M3": (42.0, 28.0, 3.0, 4.0, "knob"),  # trim
    "M4": (42.0, 34.0, 3.0, 4.0, "knob"),
    "M5": (42.0, 40.0, 3.0, 4.0, "knob"),
    "M6": (42.0, 46.0, 3.0, 4.0, "knob"),
    "M7": (42.0, 52.0, 3.0, 3.5, "button"),  # ch cue
    "M8": (70.0, 6.0, 3.5, 5.0, "knob"),  # master
    "M9": (70.0, 28.0, 3.0, 3.5, "button"),
    "M10": (70.0, 14.0, 3.5, 5.0, "knob"),  # booth
    "M11": (45.0, 78.0, 3.0, 3.5, "switch"),  # xfader assign
    "M12": (48.0, 72.0, 3.5, 4.5, "knob"),  # phones mix
    "M13": (52.5, 72.0, 3.5, 4.5, "knob"),  # phones level
    "M14": (46.0, 22.0, 3.0, 3.5, "knob"),
    "M15": (50.0, 22.0, 3.0, 3.5, "knob"),
    "M16": (85.0, 92.0, 8.0, 5.0, "switch"),  # rear line/phono (callout)
    "M17": (46.0, 12.0, 3.5, 4.0, "switch"),
    "M18": (46.0, 32.0, 3.0, 3.5, "switch"),
    "M19": (48.0, 62.0, 3.0, 3.5, "button"),
    "M20": (52.0, 62.0, 3.5, 4.5, "knob"),
    "M22": (42.0, 24.0, 3.0, 3.0, "meter"),
    # --- FX ---
    "F1": (54.0, 40.0, 3.0, 4.0, "knob"),  # color FX (repr)
    "F2": (48.0, 36.0, 2.8, 3.2, "button"),
    "F3": (51.0, 36.0, 2.8, 3.2, "button"),
    "F4": (48.0, 40.0, 2.8, 3.2, "button"),
    "F5": (51.0, 40.0, 2.8, 3.2, "button"),
    "F6": (48.0, 44.0, 2.8, 3.2, "button"),
    "F7": (51.0, 44.0, 2.8, 3.2, "button"),
    "F8": (70.0, 36.0, 3.0, 3.2, "button"),
    "F9": (66.0, 48.0, 3.0, 3.2, "button"),
    "F10": (70.0, 48.0, 3.0, 3.2, "button"),
    "F11": (66.0, 56.0, 3.0, 3.2, "button"),
    "F12": (70.0, 56.0, 3.0, 3.2, "button"),
    "F13": (72.0, 62.0, 4.0, 5.0, "encoder"),  # beat fx select
    "F14": (72.0, 70.0, 4.0, 4.0, "button"),
    "F15": (72.0, 76.0, 3.5, 5.0, "knob"),
    "F16": (72.0, 83.0, 3.5, 4.0, "button"),
}

LABELS = {
    "B1": "BROWSE",
    "B2": "BACK",
    "B3": "VIEW",
    "D1": "PLAY/PAUSE",
    "D2": "CUE",
    "D3": "JOG",
    "D4": "TEMPO",
    "D5": "DECK SELECT",
    "D6": "BEAT SYNC",
    "D7": "TEMPO RESET",
    "D8": "KEY SYNC",
    "D9": "ACTIVE PART DRUMS",
    "D10": "ACTIVE PART VOCAL",
    "D11": "ACTIVE PART INST",
    "D12": "CUE/LOOP CALL <",
    "D13": "CUE/LOOP CALL >",
    "D14": "LOOP IN / 1/2X",
    "D15": "LOOP OUT / 2X",
    "D16": "4 BEAT / EXIT",
    "D17": "MIX POINT SELECT <",
    "D18": "MIX POINT SELECT >",
    "D19": "MIX POINT LINK",
    "D20": "SLIP REVERSE",
    "D21": "QUANTIZE",
    "D22": "SLIP",
    "D23": "BEAT JUMP <",
    "D24": "BEAT JUMP >",
    "D25": "SHIFT",
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
    "M7": "CH CUE",
    "M8": "MASTER LEVEL",
    "M9": "MASTER CUE",
    "M10": "BOOTH LEVEL",
    "M11": "CROSSFADER ASSIGN",
    "M12": "HEADPHONES MIX",
    "M13": "HEADPHONES LEVEL",
    "M14": "MIC EQ HI",
    "M15": "MIC EQ LOW",
    "M16": "LINE/PHONO CH3/4",
    "M17": "INPUT SELECT",
    "M18": "MIC SWITCH",
    "M19": "SAMPLER CUE",
    "M20": "SAMPLER VOL",
    "M22": "CH LEVEL METER",
    "F1": "COLOR FX",
    "F2": "SCF SPACE",
    "F3": "SCF D.ECHO",
    "F4": "SCF CRUSH",
    "F5": "SCF PITCH",
    "F6": "SCF NOISE",
    "F7": "SCF FILTER",
    "F8": "FX PART VOCAL",
    "F9": "FX PART DRUMS",
    "F10": "FX PART INST",
    "F11": "BEAT <",
    "F12": "BEAT >",
    "F13": "BEAT FX SELECT",
    "F14": "BEAT FX CH SELECT",
    "F15": "LEVEL/DEPTH",
    "F16": "BEAT FX ON/OFF",
}


def _fig(notes: str) -> str | None:
    m = re.search(r"\[([BDFMP]\d+)\]", notes or "")
    return m.group(1) if m else None


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
    # browse etc: multiple shift variants - pick first *_shift
    for c in ctrls:
        if c["name"].endswith("_shift"):
            return c["name"]
    return None


def main() -> None:
    midi = json.loads(MIDI_PATH.read_text())
    by_fig: dict[str, list[dict]] = defaultdict(list)
    for c in midi["controls"]:
        fig = _fig(c.get("notes", ""))
        if fig:
            by_fig[fig].append(c)

    missing_pos = sorted(set(by_fig) - set(POS), key=lambda x: (x[0], int(x[1:])))
    extra_pos = sorted(set(POS) - set(by_fig), key=lambda x: (x[0], int(x[1:])))
    if missing_pos:
        raise SystemExit(f"POS missing figs present in midi: {missing_pos}")
    if extra_pos:
        raise SystemExit(f"POS has figs absent from midi: {extra_pos}")

    controls = []
    for fig in sorted(POS, key=lambda x: (x[0], int(x[1:]))):
        x, y, w, h, kind = POS[fig]
        ctrls = by_fig[fig]
        primary = _primary(ctrls)
        shift = _shift_name(ctrls, primary)
        controls.append(
            {
                "id": f"flx10-{fig.lower()}",
                "fig": fig,
                "label": LABELS.get(fig, primary["name"]),
                "kind": kind,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "layer": "both" if shift else "base",
                "section": primary.get("section", ""),
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

    plate_path = DEV / PLATE
    pw, ph = Image.open(plate_path).size

    layout = {
        "device": "DDJ-FLX10",
        "vendor": "AlphaTheta / Pioneer DJ",
        "plate": {"image": PLATE, "width": pw, "height": ph},
        "layers": ["base", "shift"],
        "out_of_scope": [
            "jog_screen_bitmap_hid",
            "vu_meter_leds_hid",
            "stem_fx_indicator_hid",
            "right_deck_mirror_labels_same_figs",
        ],
        "controls": controls,
    }
    OUT.write_text(json.dumps(layout, indent=2) + "\n")
    print(f"wrote {OUT} ({len(controls)} figs)")


if __name__ == "__main__":
    main()
