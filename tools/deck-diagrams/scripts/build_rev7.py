#!/usr/bin/env python3
"""Parse DDJ-REV7 MIDI list tables into midi.json + layout.json."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
DEV = ROOT / "devices" / "ddj-rev7"
TXT = DEV / "source" / "DDJ-REV7_MIDI_Message_List_E1.txt"
SCRIPTS = Path(__file__).resolve().parent

FIG_RE = re.compile(r"\b([BDFMPE]\d{1,2})\b")
MIDI_RE = re.compile(r"(\d+(?:/\d+)?)\s+(NOTE|CC)\s+(\d+)")
TABLE_START = re.compile(r"BROWSE\s+B1|DECK\s+D1")


@dataclass
class MidiRow:
    name: str
    section: str
    type: str
    ch: int
    code: int
    notes: str
    fig: str
    is_shift: bool = False


@dataclass
class FigBlock:
    fig: str
    section: str = ""
    ui_name: str = ""
    primary: MidiRow | None = None
    shift: MidiRow | None = None
    extras: list[MidiRow] = field(default_factory=list)


def _slug(ui: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", ui.strip().lower()).strip("_")
    return s or "control"


def _parse_ch(raw: str) -> int:
    return int(raw.split("/")[0])


def _fig_sort(fig: str) -> tuple:
    return (fig[0], int(fig[1:]))


def _section_from_group(line: str) -> str | None:
    m = re.match(r"^([A-Z][A-Z /]+?)\s+[BDFMPE]\d", line)
    if m:
        return m.group(1).strip().lower().replace(" ", "_")
    return None


# Hand-curated primary rows from PDF tables (one per fig, deck-1 channel where 1/2).
PRIMARY: dict[str, tuple[str, str, int, int, str]] = {
    # fig: (name, type, ch, code, ui label fragment)
    "B1": ("browse_rotate", "cc", 7, 64, "BROWSE rotate"),
    "B2": ("back", "note", 7, 101, "BACK"),
    "B3": ("load_deck1", "note", 7, 70, "LOAD deck1"),
    "D1": ("pitch_bend_minus", "note", 1, 57, "PITCH BEND minus"),
    "D2": ("pitch_bend_plus", "note", 1, 58, "PITCH BEND plus"),
    "D3": ("jog", "cc", 1, 34, "JOG"),
    "D4": ("auto_loop", "note", 1, 35, "AUTO LOOP"),
    "D5": ("loop_half", "note", 1, 18, "LOOP 1/2X"),
    "D6": ("loop_double", "note", 1, 19, "LOOP 2X"),
    "D7": ("tempo_range", "note", 1, 96, "TEMPO RANGE"),
    "D8": ("tempo_reset", "note", 1, 69, "TEMPO RESET"),
    "D9": ("key_lock", "note", 1, 26, "KEY LOCK"),
    "D10": ("sync", "note", 1, 88, "SYNC"),
    "D11": ("tempo", "cc", 1, 0, "TEMPO MSB"),
    "D12": ("slip", "note", 1, 64, "SLIP"),
    "D13": ("censor", "note", 1, 21, "CENSOR"),
    "D14": ("key_minus", "note", 1, 31, "KEY minus"),
    "D15": ("key_plus", "note", 1, 28, "KEY plus"),
    "D16": ("instant_scratch", "note", 1, 67, "INSTANT SCRATCH"),
    "D17": ("hot_cue", "note", 1, 73, "HOT CUE"),
    "D18": ("scratch_bank", "note", 1, 90, "SCRATCH BANK"),
    "D19": ("slot1_pad_mode", "note", 3, 0, "SLOT 1 / PAD 1 mode"),
    "D20": ("slot2_pad_mode", "note", 3, 1, "SLOT 2 / PAD 2 mode"),
    "D21": ("slot3_pad_mode", "note", 3, 2, "SLOT 3 / PAD 3 mode"),
    "D22": ("slot4_pad_mode", "note", 3, 3, "SLOT 4 / PAD 4 mode"),
    "D23": ("stop_time", "cc", 1, 8, "STOP TIME"),
    "D24": ("start_stop", "note", 1, 11, "START/STOP"),
    "D25": ("rpm_33", "note", 1, 97, "33 RPM"),
    "D26": ("rpm_45", "note", 1, 98, "45 RPM"),
    "M1": ("input_select", "note", 1, 85, "INPUT SELECT"),
    "M2": ("booth_level", "cc", 7, 9, "BOOTH LEVEL MSB"),
    "M3": ("master_level", "cc", 7, 8, "MASTER LEVEL MSB"),
    "M4": ("trim", "cc", 1, 4, "TRIM MSB"),
    "M5": ("filter_ch1", "cc", 7, 23, "FILTER CH1 MSB"),
    "M6": ("iso_hi", "cc", 1, 7, "ISO HI MSB"),
    "M7": ("iso_mid", "cc", 1, 11, "ISO MID MSB"),
    "M8": ("iso_low", "cc", 1, 15, "ISO LOW MSB"),
    "M9": ("silent_cue", "note", 1, 115, "SILENT CUE"),
    "M10": ("shift_button", "note", 7, 63, "SHIFT"),
    "M11": ("ch_fader", "cc", 1, 19, "CH FADER MSB"),
    "M12": ("crossfader", "cc", 7, 31, "CROSSFADER MSB"),
    "M13": ("sampler_volume", "cc", 7, 3, "SAMPLER VOLUME MSB"),
    "M14": ("phones_level", "cc", 7, 13, "HEADPHONES LEVEL MSB"),
    "M15": ("phones_mix", "cc", 7, 12, "HEADPHONES MIX MSB"),
    "M16": ("phones_cue_fader", "cc", 7, 14, "HEADPHONES CUE FADER MSB"),
    "M17": ("line_phono_sw", "note", 1, 54, "LINE/PHONO SW"),
    "M19": ("mic_off_on_talkover", "note", 7, 106, "MIC OFF/ON/TALKOVER"),
    "M22": ("mic_eq_low", "cc", 7, 15, "MIC EQ LOW MSB"),
    "M23": ("mic_eq_hi", "cc", 7, 7, "MIC EQ HI MSB"),
    "M24": ("mic_echo", "cc", 7, 6, "MIC ECHO MSB"),
    "M25": ("ch1_fader_reverse", "note", 7, 25, "CH1 FADER REVERSE"),
    "M26": ("ch1_fader_curve", "cc", 7, 2, "CH1 FADER CURVE MSB"),
    "M27": ("crossfader_reverse", "note", 7, 24, "CROSSFADER REVERSE"),
    "M29": ("crossfader_curve", "cc", 7, 1, "CROSSFADER CURVE MSB"),
    "M30": ("ch2_fader_reverse", "note", 7, 32, "CH2 FADER REVERSE"),
    "M31": ("ch2_fader_curve", "cc", 7, 5, "CH2 FADER CURVE MSB"),
    "E1": ("fx_echo", "note", 7, 0, "ECHO"),
    "E2": ("fx_spin", "note", 7, 1, "SPIN"),
    "E3": ("fx_flanger", "note", 7, 2, "FLANGER"),
    "E4": ("fx_reverb", "note", 7, 3, "REVERB"),
    "E5": ("fx_brake", "note", 7, 4, "BRAKE"),
    "E6": ("fx_duck_down", "note", 7, 5, "DUCK DOWN"),
    "E7": ("fx_tap", "note", 7, 10, "TAP"),
    "E8": ("fx_beat_t", "note", 7, 6, "BEAT t"),
    "E9": ("fx_beat_u", "note", 7, 7, "BEAT u"),
    "E10": ("fx_level_depth", "cc", 7, 10, "LEVEL/DEPTH MSB"),
    "E11": ("fx_ch1_lever", "note", 5, 80, "CH1 EFFECT LEVER"),
    "E12": ("fx_ch2_lever", "note", 6, 80, "CH2 EFFECT LEVER"),
    "E13": ("fx_smooth_echo", "note", 7, 9, "SMOOTH ECHO"),
    "P1": ("pad_mode_hot_cue", "note", 1, 27, "HOT CUE MODE"),
    "P2": ("pad_mode_roll", "note", 1, 30, "ROLL MODE"),
    "P3": ("pad_mode_saved_loop", "note", 1, 32, "SAVED LOOP MODE"),
    "P4": ("pad_mode_sampler", "note", 1, 34, "SAMPLER MODE"),
    "P5": ("pad1", "note", 8, 0, "PERFORMANCE PAD 1"),
    "P6": ("pad2", "note", 8, 16, "PERFORMANCE PAD 2"),
    "P7": ("pad3", "note", 8, 32, "PERFORMANCE PAD 3"),
    "P8": ("pad4", "note", 8, 48, "PERFORMANCE PAD 4"),
    "P9": ("pad5", "note", 8, 64, "PERFORMANCE PAD 5"),
    "P10": ("pad6", "note", 8, 80, "PERFORMANCE PAD 6"),
    "P11": ("pad7", "note", 8, 96, "PERFORMANCE PAD 7"),
    "P12": ("pad8", "note", 8, 112, "PERFORMANCE PAD 8"),
    "P13": ("pad_param_t", "note", 1, 36, "PARAMETER 1 t"),
    "P14": ("pad_param_u", "note", 1, 44, "PARAMETER 1 u"),
}

SHIFT: dict[str, tuple[str, str, int, int]] = {
    "B1": ("browse_rotate_shift", "cc", 7, 100),
    "B2": ("back_shift", "note", 7, 102),
    "B3": ("load_deck1_shift", "note", 7, 93),
    "D1": ("pitch_bend_minus_shift", "note", 1, 89),
    "D2": ("pitch_bend_plus_shift", "note", 1, 93),
    "D3": ("jog_shift", "cc", 1, 41),
    "D4": ("auto_loop_shift", "note", 1, 80),
    "D5": ("loop_half_shift", "note", 1, 16),
    "D6": ("loop_double_shift", "note", 1, 17),
    "D7": ("tempo_range_shift", "note", 1, 52),
    "D8": ("tempo_reset_shift", "note", 1, 55),
    "D9": ("key_lock_shift", "note", 1, 25),
    "D10": ("sync_shift", "note", 1, 92),
    "D11": ("tempo_shift", "cc", 1, 5),
    "D12": ("slip_shift", "note", 1, 23),
    "D13": ("censor_shift", "note", 1, 56),
    "D14": ("key_minus_shift", "note", 1, 101),
    "D15": ("key_plus_shift", "note", 1, 100),
    "D16": ("instant_scratch_shift", "note", 1, 68),
    "D17": ("hot_cue_shift", "note", 1, 74),
    "D18": ("scratch_bank_shift", "note", 1, 91),
    "D19": ("slot1_pad_mode_shift", "note", 3, 4),
    "D20": ("slot2_pad_mode_shift", "note", 3, 5),
    "D21": ("slot3_pad_mode_shift", "note", 3, 6),
    "D22": ("slot4_pad_mode_shift", "note", 3, 7),
    "D24": ("start_stop_shift", "note", 1, 71),
    "D25": ("rpm_33_shift", "note", 1, 75),
    "D26": ("rpm_45_shift", "note", 1, 76),
    "M9": ("silent_cue_shift", "note", 1, 119),
    "E1": ("fx_echo_shift", "note", 7, 12),
    "E2": ("fx_spin_shift", "note", 7, 13),
    "E3": ("fx_flanger_shift", "note", 7, 14),
    "E4": ("fx_reverb_shift", "note", 7, 15),
    "E5": ("fx_brake_shift", "note", 7, 16),
    "E6": ("fx_duck_down_shift", "note", 7, 17),
    "E7": ("fx_tap_shift", "note", 7, 22),
    "E13": ("fx_smooth_echo_shift", "note", 7, 21),
    "P1": ("pad_mode_hot_cue_shift", "note", 1, 105),
    "P2": ("pad_mode_roll_shift", "note", 1, 107),
    "P3": ("pad_mode_saved_loop_shift", "note", 1, 109),
    "P4": ("pad_mode_sampler_shift", "note", 1, 111),
    "P5": ("pad1_shift", "note", 8, 8),
    "P6": ("pad2_shift", "note", 8, 24),
    "P7": ("pad3_shift", "note", 8, 40),
    "P8": ("pad4_shift", "note", 8, 56),
    "P9": ("pad5_shift", "note", 8, 72),
    "P10": ("pad6_shift", "note", 8, 88),
    "P11": ("pad7_shift", "note", 8, 104),
    "P12": ("pad8_shift", "note", 8, 120),
    "P13": ("pad_param_t_shift", "note", 1, 37),
    "P14": ("pad_param_u_shift", "note", 1, 45),
}

SECTION: dict[str, str] = {
    "B": "browser",
    "D": "deck",
    "M": "mixer",
    "E": "effect",
    "P": "performance_pad",
}

# Percent-of-plate hit targets tuned against source/plate-top.png
POS: dict[str, tuple[float, float, float, float, str]] = {
    "B1": (43.5, 3.0, 4.5, 6.5, "encoder"),
    "B2": (48.5, 4.5, 3.0, 3.5, "button"),
    "B3": (52.0, 4.5, 3.0, 3.5, "button"),
    "D1": (4.5, 76.5, 4.0, 5.0, "button"),
    "D2": (4.5, 70.5, 4.0, 5.0, "button"),
    "D3": (9.5, 36.0, 15.0, 30.0, "jog"),
    "D4": (16.5, 6.5, 3.5, 3.5, "button"),
    "D5": (20.5, 6.5, 3.5, 3.5, "button"),
    "D6": (24.5, 6.5, 3.5, 3.5, "button"),
    "D7": (28.5, 6.5, 3.5, 3.5, "button"),
    "D8": (32.5, 6.5, 3.5, 3.5, "button"),
    "D9": (36.5, 6.5, 3.5, 3.5, "button"),
    "D10": (40.5, 6.5, 3.5, 3.5, "button"),
    "D11": (27.0, 36.0, 3.0, 26.0, "fader"),
    "D12": (16.5, 12.5, 3.5, 3.5, "button"),
    "D13": (20.5, 12.5, 3.5, 3.5, "button"),
    "D14": (24.5, 12.5, 3.5, 3.5, "button"),
    "D15": (28.5, 12.5, 3.5, 3.5, "button"),
    "D16": (32.5, 12.5, 3.5, 3.5, "button"),
    "D17": (16.5, 18.5, 3.5, 3.5, "button"),
    "D18": (20.5, 18.5, 3.5, 3.5, "button"),
    "D19": (24.5, 18.5, 3.5, 3.5, "button"),
    "D20": (28.5, 18.5, 3.5, 3.5, "button"),
    "D21": (32.5, 18.5, 3.5, 3.5, "button"),
    "D22": (36.5, 18.5, 3.5, 3.5, "button"),
    "D23": (40.5, 18.5, 3.5, 3.5, "knob"),
    "D24": (4.5, 64.5, 4.0, 5.0, "button"),
    "D25": (61.0, 76.5, 4.0, 5.0, "button"),
    "D26": (61.0, 70.5, 4.0, 5.0, "button"),
    "M1": (35.5, 3.0, 4.0, 4.5, "switch"),
    "M2": (55.5, 3.0, 3.5, 4.5, "knob"),
    "M3": (59.5, 3.0, 3.5, 4.5, "knob"),
    "M4": (35.5, 9.5, 3.0, 4.0, "knob"),
    "M5": (39.5, 9.5, 3.0, 4.0, "knob"),
    "M6": (43.5, 9.5, 3.0, 4.0, "knob"),
    "M7": (47.5, 9.5, 3.0, 4.0, "knob"),
    "M8": (51.5, 9.5, 3.0, 4.0, "knob"),
    "M9": (55.5, 15.5, 3.0, 3.5, "button"),
    "M10": (59.5, 15.5, 3.0, 3.5, "button"),
    "M11": (35.5, 24.0, 3.0, 24.0, "fader"),
    "M12": (45.0, 80.5, 12.0, 4.5, "fader"),
    "M13": (55.5, 24.0, 3.0, 4.0, "knob"),
    "M14": (59.5, 24.0, 3.0, 4.0, "knob"),
    "M15": (63.5, 24.0, 3.0, 4.0, "knob"),
    "M16": (67.5, 24.0, 3.0, 4.0, "fader"),
    "M17": (71.5, 9.5, 3.0, 3.5, "switch"),
    "M18": (75.5, 9.5, 3.0, 4.0, "knob"),
    "M19": (79.5, 9.5, 3.0, 3.5, "switch"),
    "M20": (83.5, 9.5, 3.0, 4.0, "knob"),
    "M21": (87.5, 9.5, 3.0, 4.0, "knob"),
    "M22": (79.5, 16.5, 3.0, 4.0, "knob"),
    "M23": (83.5, 16.5, 3.0, 4.0, "knob"),
    "M24": (87.5, 16.5, 3.0, 4.0, "knob"),
    "M25": (79.5, 23.5, 3.0, 3.5, "switch"),
    "M26": (83.5, 23.5, 3.0, 4.0, "knob"),
    "M27": (87.5, 23.5, 3.0, 3.5, "switch"),
    "M28": (91.5, 23.5, 3.0, 4.0, "knob"),
    "M29": (79.5, 30.5, 3.0, 4.0, "knob"),
    "M30": (83.5, 30.5, 3.0, 3.5, "switch"),
    "M31": (87.5, 30.5, 3.0, 4.0, "knob"),
    "M32": (91.5, 30.5, 3.0, 3.5, "switch"),
    "M33": (91.5, 37.5, 3.0, 4.0, "knob"),
    "E1": (45.5, 16.0, 2.8, 3.0, "button"),
    "E2": (48.5, 16.0, 2.8, 3.0, "button"),
    "E3": (51.5, 16.0, 2.8, 3.0, "button"),
    "E4": (54.5, 16.0, 2.8, 3.0, "button"),
    "E5": (57.5, 16.0, 2.8, 3.0, "button"),
    "E6": (60.5, 16.0, 2.8, 3.0, "button"),
    "E7": (51.5, 22.0, 3.0, 3.5, "button"),
    "E8": (47.5, 22.0, 3.0, 3.5, "button"),
    "E9": (55.5, 22.0, 3.0, 3.5, "button"),
    "E10": (51.5, 28.0, 4.0, 5.0, "knob"),
    "E11": (43.5, 34.0, 3.5, 8.0, "fader"),
    "E12": (59.5, 34.0, 3.5, 8.0, "fader"),
    "E13": (51.5, 34.0, 3.0, 3.5, "button"),
    "P1": (13.5, 56.0, 3.0, 3.5, "button"),
    "P2": (17.5, 56.0, 3.0, 3.5, "button"),
    "P3": (21.5, 56.0, 3.0, 3.5, "button"),
    "P4": (25.5, 56.0, 3.0, 3.5, "button"),
    "P5": (13.5, 60.5, 3.2, 4.0, "pad"),
    "P6": (17.5, 60.5, 3.2, 4.0, "pad"),
    "P7": (21.5, 60.5, 3.2, 4.0, "pad"),
    "P8": (25.5, 60.5, 3.2, 4.0, "pad"),
    "P9": (13.5, 65.0, 3.2, 4.0, "pad"),
    "P10": (17.5, 65.0, 3.2, 4.0, "pad"),
    "P11": (21.5, 65.0, 3.2, 4.0, "pad"),
    "P12": (25.5, 65.0, 3.2, 4.0, "pad"),
    "P13": (29.5, 56.0, 3.0, 3.5, "button"),
    "P14": (33.5, 56.0, 3.0, 3.5, "button"),
}

LABELS: dict[str, str] = {
    "B1": "BROWSE",
    "B2": "BACK",
    "B3": "LOAD",
    "D1": "PITCH -",
    "D2": "PITCH +",
    "D3": "JOG",
    "D4": "AUTO LOOP",
    "D5": "LOOP 1/2X",
    "D6": "LOOP 2X",
    "D7": "TEMPO RANGE",
    "D8": "TEMPO RESET",
    "D9": "KEY LOCK",
    "D10": "SYNC",
    "D11": "TEMPO",
    "D12": "SLIP",
    "D13": "CENSOR",
    "D14": "KEY -",
    "D15": "KEY +",
    "D16": "INST SCRATCH",
    "D17": "HOT CUE",
    "D18": "SCRATCH BANK",
    "D19": "SLOT1 MODE",
    "D20": "SLOT2 MODE",
    "D21": "SLOT3 MODE",
    "D22": "SLOT4 MODE",
    "D23": "STOP TIME",
    "D24": "START/STOP",
    "D25": "33 RPM",
    "D26": "45 RPM",
    "M1": "INPUT SEL",
    "M2": "BOOTH",
    "M3": "MASTER",
    "M4": "TRIM",
    "M5": "FILTER",
    "M6": "ISO HI",
    "M7": "ISO MID",
    "M8": "ISO LOW",
    "M9": "SILENT CUE",
    "M10": "SHIFT",
    "M11": "CH FADER",
    "M12": "CROSSFADER",
    "M13": "SAMPLER VOL",
    "M14": "PHONES LVL",
    "M15": "PHONES MIX",
    "M16": "PHONES CUE",
    "M17": "LINE/PHONO",
    "M18": "MIC ATT",
    "M19": "MIC SW",
    "M20": "MIC1 LVL",
    "M21": "MIC2 LVL",
    "M22": "MIC EQ LO",
    "M23": "MIC EQ HI",
    "M24": "MIC ECHO",
    "M25": "CH1 REV",
    "M26": "CH1 CURVE",
    "M27": "XF REV",
    "M28": "XF FEEL",
    "M29": "XF CURVE",
    "M30": "CH2 REV",
    "M31": "CH2 CURVE",
    "M32": "AUX SW",
    "M33": "AUX LVL",
    "E1": "ECHO",
    "E2": "SPIN",
    "E3": "FLANGER",
    "E4": "REVERB",
    "E5": "BRAKE",
    "E6": "DUCK",
    "E7": "TAP",
    "E8": "BEAT -",
    "E9": "BEAT +",
    "E10": "FX DEPTH",
    "E11": "FX CH1",
    "E12": "FX CH2",
    "E13": "SMOOTH ECHO",
    "P1": "HC MODE",
    "P2": "ROLL MODE",
    "P3": "LOOP MODE",
    "P4": "SMPL MODE",
    "P5": "PAD 1",
    "P6": "PAD 2",
    "P7": "PAD 3",
    "P8": "PAD 4",
    "P9": "PAD 5",
    "P10": "PAD 6",
    "P11": "PAD 7",
    "P12": "PAD 8",
    "P13": "PARAM t",
    "P14": "PARAM u",
}


def build_midi() -> dict:
    controls: list[dict] = []
    for fig in sorted(PRIMARY, key=_fig_sort):
        name, typ, ch, code, ui = PRIMARY[fig]
        sec = SECTION[fig[0]]
        controls.append(
            {
                "name": name,
                "section": sec,
                "type": typ,
                "ch": ch,
                "code": code,
                "notes": f"{ui}; per-deck ch 1/2 where applicable [{fig}]",
            }
        )
        if fig in SHIFT:
            sname, styp, sch, scode = SHIFT[fig]
            controls.append(
                {
                    "name": sname,
                    "section": sec,
                    "type": styp,
                    "ch": sch,
                    "code": scode,
                    "notes": f"shift variant of {name} [{fig}]",
                }
            )
    return {
        "source": "docs/controller/reference/DDJ-REV7_MIDI_Message_List_E1.pdf",
        "archived": (DEV / "source" / "DDJ-REV7_MIDI_Message_List_E1.pdf")
        .relative_to(REPO_ROOT)
        .as_posix(),
        "device": "DDJ-REV7",
        "vendor": "AlphaTheta / Pioneer DJ",
        "channel_conventions": {
            "deck_rows": "PDF lists deck controls as ch 1/2; entries carry ch=1 with note per-deck channels 1-2",
            "slot_pad_rows": "SLOT/PAD mode buttons use ch 3/4 (n=2/3)",
            "pad_rows": "Performance pads ch 8/10 unshifted; shifted pad rows on ch 9/11 (odd=shifted)",
            "channel_assignment": "ch1-2 decks, ch3-4 slot/pad modes, ch5-6 FX, ch7 browser+mixer, ch8-11 performance pads, ch16 MIDI-OUT",
        },
        "controls": controls,
    }


def build_layout(midi: dict) -> dict:
    by_name = {c["name"]: c for c in midi["controls"]}
    controls = []
    for fig in sorted(POS, key=_fig_sort):
        if fig not in PRIMARY:
            continue
        pname, ptyp, pch, pcode, _ = PRIMARY[fig]
        shift_name = None
        if fig in SHIFT:
            shift_name = SHIFT[fig][0]
            if shift_name not in by_name:
                raise SystemExit(f"missing shift name in midi: {shift_name}")
        if pname not in by_name:
            raise SystemExit(f"missing primary name in midi: {pname}")
        x, y, w, h, kind = POS[fig]
        controls.append(
            {
                "id": f"ddj-rev7-{fig.lower()}",
                "fig": fig,
                "label": LABELS.get(fig, pname),
                "kind": kind,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "layer": "both" if shift_name else "base",
                "section": SECTION[fig[0]],
                "midi": {
                    "name": pname,
                    "type": ptyp,
                    "ch": pch,
                    "code": pcode,
                    "shift_name": shift_name,
                },
                "notes": by_name[pname]["notes"],
            }
        )
    from PIL import Image

    plate = DEV / "source" / "plate-top.png"
    pw, ph = Image.open(plate).size
    return {
        "device": "DDJ-REV7",
        "vendor": "AlphaTheta / Pioneer DJ",
        "plate": {"image": "source/plate-top.png", "width": pw, "height": ph},
        "layers": ["base", "shift"],
        "out_of_scope": [
            "hid_jog_displays",
            "vu_meter_leds_hid",
            "mic_hw_only_m18_m20_m21_m28_m32_m33",
            "right_deck_mirror_same_fig_codes",
        ],
        "controls": controls,
    }


def main() -> None:
    midi = build_midi()
    layout = build_layout(midi)
    (DEV / "midi.json").write_text(json.dumps(midi, indent=2) + "\n")
    (DEV / "layout.json").write_text(json.dumps(layout, indent=2) + "\n")
    print(f"wrote midi.json ({len(midi['controls'])} rows, {len(PRIMARY)} figs)")
    print(f"wrote layout.json ({len(layout['controls'])} controls)")
    subprocess.check_call(
        ["uv", "run", "python", str(SCRIPTS / "generate_html.py"), str(DEV)]
    )
    r = subprocess.run(
        ["uv", "run", "python", str(SCRIPTS / "checksum.py"), str(DEV)],
        capture_output=True,
        text=True,
    )
    print(r.stdout, end="")
    if r.returncode != 0:
        print(r.stderr, end="", file=sys.stderr)
        raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
