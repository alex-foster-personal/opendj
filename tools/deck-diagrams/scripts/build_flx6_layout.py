#!/usr/bin/env python3
"""Build devices/ddj-flx6/midi.json + layout.json from PDF text + plate positions."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
DEV = ROOT / "devices" / "ddj-flx6"
TXT = DEV / "source" / "DDJ-FLX6_MIDI_Message_List_E1.txt"
MIDI_OUT = DEV / "midi.json"
LAYOUT_OUT = DEV / "layout.json"
PLATE = "source/plate-top.png"

# Percent-of-plate hit targets aligned to source/plate-top.png (1118x614).
POS: dict[str, tuple[float, float, float, float, str]] = {
    "1-1": (7.0, 74.0, 4.5, 5.5, "button"),
    "1-2": (11.5, 74.0, 4.0, 5.0, "button"),
    "1-3": (15.5, 74.0, 4.0, 5.0, "button"),
    "1-4": (10.0, 38.0, 17.0, 30.0, "jog"),
    "1-5": (3.5, 32.0, 4.5, 5.5, "button"),
    "1-6": (14.0, 52.0, 4.0, 4.5, "button"),
    "1-7": (4.0, 10.0, 3.5, 4.0, "button"),
    "1-8": (7.5, 10.0, 3.5, 4.0, "button"),
    "1-9": (11.0, 10.0, 3.5, 4.0, "button"),
    "1-10": (14.5, 10.0, 3.5, 4.0, "button"),
    "1-11": (18.0, 10.0, 3.5, 4.0, "button"),
    "1-12": (21.5, 10.0, 3.5, 4.0, "button"),
    "1-13": (25.0, 10.0, 3.5, 4.0, "button"),
    "1-14": (34.0, 8.0, 6.5, 8.0, "knob"),
    "1-15": (30.0, 74.0, 4.0, 5.0, "button"),
    "1-16": (34.5, 74.0, 4.0, 5.0, "button"),
    "1-17": (39.0, 38.0, 3.0, 28.0, "fader"),
    "2-1": (70.0, 8.0, 4.0, 4.5, "button"),
    "2-2": (74.5, 8.0, 4.0, 4.5, "button"),
    "2-3": (69.0, 16.0, 3.5, 4.0, "button"),
    "2-4": (73.0, 16.0, 3.5, 4.0, "button"),
    "2-5": (77.5, 22.0, 4.0, 5.0, "knob"),
    "2-6": (71.5, 28.0, 4.5, 4.5, "button"),
    "3-1": (47.0, 52.0, 3.5, 4.0, "switch"),
    "3-2": (56.0, 8.0, 4.0, 5.5, "knob"),
    "3-3": (60.5, 8.0, 3.5, 4.0, "button"),
    "3-4": (64.5, 8.0, 4.0, 5.5, "knob"),
    "3-5": (46.5, 16.0, 3.0, 4.0, "knob"),
    "3-6": (46.5, 26.0, 3.0, 4.0, "knob"),
    "3-7": (46.5, 36.0, 3.0, 4.0, "knob"),
    "3-8": (44.0, 48.0, 3.0, 3.5, "button"),
    "3-9": (50.0, 48.0, 3.0, 22.0, "fader"),
    "3-10": (48.0, 82.0, 12.0, 4.5, "fader"),
    "3-11": (52.0, 50.0, 8.0, 28.0, "meter"),
    "3-12": (66.0, 72.0, 3.5, 4.5, "knob"),
    "3-13": (62.0, 72.0, 3.5, 4.5, "knob"),
    "3-14": (70.0, 48.0, 3.5, 4.5, "knob"),
    "4-1": (52.0, 4.0, 5.0, 7.0, "encoder"),
    "4-2": (47.5, 5.5, 3.0, 3.5, "button"),
    "4-3": (56.5, 5.5, 3.0, 3.5, "button"),
    "4-4": (45.0, 14.0, 3.5, 3.5, "button"),
    "4-5": (49.0, 14.0, 3.5, 3.5, "button"),
    "4-6": (53.0, 14.0, 3.5, 3.5, "button"),
    "4-7": (57.0, 14.0, 3.5, 3.5, "button"),
    "5-1": (18.0, 66.0, 3.0, 3.5, "button"),
    "5-2": (21.5, 66.0, 3.0, 3.5, "button"),
    "5-3": (25.0, 66.0, 3.0, 3.5, "button"),
    "5-4": (28.5, 66.0, 3.0, 3.5, "button"),
    "5-5": (16.0, 70.0, 14.0, 12.0, "pad"),
}

LABELS: dict[str, str] = {
    "1-1": "PLAY/PAUSE",
    "1-2": "CUE",
    "1-3": "SHIFT",
    "1-4": "JOG DIAL",
    "1-5": "DECK",
    "1-6": "JOG CUTTER",
    "1-7": "IN/-4BEAT",
    "1-8": "OUT/+4BEAT",
    "1-9": "RELOOP/EXIT",
    "1-10": "CUE/LOOP CALL <",
    "1-11": "CUE/LOOP CALL >",
    "1-12": "CUE/LOOP MEMORY",
    "1-13": "MERGE FX PRESET",
    "1-14": "MERGE FX PARAM",
    "1-15": "BEAT SYNC",
    "1-16": "MASTER",
    "1-17": "TEMPO",
    "2-1": "BFX SELECT",
    "2-2": "BFX CH SELECT",
    "2-3": "BEAT <",
    "2-4": "BEAT >",
    "2-5": "LEVEL/DEPTH",
    "2-6": "BFX ON/OFF",
    "3-1": "INPUT SELECT",
    "3-2": "MASTER LEVEL",
    "3-3": "MASTER CUE",
    "3-4": "BOOTH LEVEL",
    "3-5": "TRIM",
    "3-6": "EQ",
    "3-7": "FILTER",
    "3-8": "CH CUE",
    "3-9": "CH FADER",
    "3-10": "CROSSFADER",
    "3-11": "CH LEVEL METER",
    "3-12": "HEADPHONE LEVEL",
    "3-13": "HEADPHONE MIX",
    "3-14": "MIC LEVEL",
    "4-1": "BROWSE",
    "4-2": "BACK",
    "4-3": "VIEW",
    "4-4": "LOAD D1",
    "4-5": "LOAD D2",
    "4-6": "LOAD D3",
    "4-7": "LOAD D4",
    "5-1": "HOT CUE MODE",
    "5-2": "PAD FX MODE",
    "5-3": "BEAT JUMP MODE",
    "5-4": "SAMPLER MODE",
    "5-5": "PERFORMANCE PADS",
}

SECTION: dict[str, str] = {
    **{f"1-{i}": "deck" for i in range(1, 18)},
    **{f"2-{i}": "effect" for i in range(1, 7)},
    **{f"3-{i}": "mixer" for i in range(1, 15)},
    **{f"4-{i}": "browser" for i in range(1, 8)},
    **{f"5-{i}": "performance" for i in range(1, 6)},
}

_FIG_LINE = re.compile(
    r"^\s*(?P<fig>\d+-\d+)(?P<side>[LR])?\s+.*?\+\s*(?P<shift>No|Yes)\s+"
    r".*?\s+(?P<ch>\d+)\s+(?P<kind>Note|CC)\s+(?P<code>\d+|-)\s",
    re.MULTILINE,
)
_FIG_LINE_LOOSE = re.compile(
    r"^\s*(?P<fig>\d+-\d+)(?P<side>[LR])?\b",
    re.MULTILINE,
)
_MIDI_ROW = re.compile(
    r"(?P<shift>No|Yes)\s+.*?\s+(?P<ch>\d+)\s+(?P<kind>Note|CC)\s+(?P<code>\d+|-)\s",
)


def _norm_fig(raw: str) -> str:
    return raw.strip()


def _slug(label: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", label.upper()).strip("_").lower()
    return s[:48] or "control"


def _parse_pdf_rows(text: str) -> dict[str, dict[str, dict]]:
    """Extract primary (No, deck1/L) and shift (Yes) MIDI rows per fig."""
    out: dict[str, dict[str, dict]] = defaultdict(lambda: {"primary": None, "shift": None, "labels": set()})
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _FIG_LINE_LOOSE.match(line)
        if not m:
            i += 1
            continue
        fig = _norm_fig(m.group("fig"))
        side = m.group("side") or ""
        block = line
        j = i + 1
        while j < len(lines) and not _FIG_LINE_LOOSE.match(lines[j]):
            block += "\n" + lines[j]
            j += 1
        # UI label heuristics from block
        for token in ("PLAY/PAUSE", "CUE", "SHIFT", "JOG", "DECK", "MERGE FX", "BEAT SYNC", "MASTER", "TEMPO",
                      "BFX", "TRIM", "FILTER", "BROWSE", "BACK", "VIEW", "LOAD", "HOT CUE", "PAD FX",
                      "CROSSFADER", "HEADPHONE", "MIC LEVEL", "PERFORMANCE PAD"):
            if token in block:
                out[fig]["labels"].add(token)
        for row_m in _MIDI_ROW.finditer(block):
            shift = row_m.group("shift") == "Yes"
            code_s = row_m.group("code")
            if code_s == "-":
                continue
            row = {
                "type": "note" if row_m.group("kind") == "Note" else "cc",
                "ch": int(row_m.group("ch")),
                "code": int(code_s),
            }
            key = "shift" if shift else "primary"
            cur = out[fig][key]
            if cur is None:
                out[fig][key] = row
            elif not side or side == "L":
                out[fig][key] = row
            elif cur is None:
                out[fig][key] = row
        i = j if j > i else i + 1
    return out


def _manual_overrides() -> dict[str, dict]:
    """Curated primary/shift pairs where pdftotext column smash loses codes."""
    # deck 1 / ch1 representative unless global (ch7) or effect (ch5)
    M = [
        ("1-1", "play_pause", "note", 1, 11, "play_pause_shift", 1, 71,
         "per-deck channels 1-4; LED out same code [1-1]"),
        ("1-2", "cue", "note", 1, 12, "cue_shift", 1, 72, "per-deck channels 1-4 [1-2]"),
        ("1-3", "shift_button", "note", 1, 63, None, None, None, "hardware SHIFT; per-deck ch 1-4 [1-3]"),
        ("1-4", "jog_platter_turn", "cc", 1, 34, "jog_platter_turn_shift", 1, 41,
         "Vinyl On CC34/35; shift CC41; touch note 54/103 [1-4]"),
        ("1-5", "deck_select", "note", 1, 60, "deck_select_shift", 7, 124,
         "Deck1/3 toggle ch1/3; shift ch7 note 124 [1-5]"),
        ("1-6", "jog_cutter", "note", 1, 28, "jog_cutter_shift", 1, 23, "per-deck ch 1-4 [1-6]"),
        ("1-7", "loop_in", "note", 1, 16, "loop_in_shift", 1, 76, "long-press note 20; per-deck [1-7]"),
        ("1-8", "loop_out", "note", 1, 17, "loop_out_shift", 1, 119, "per-deck ch 1-4 [1-8]"),
        ("1-9", "reloop_exit", "note", 1, 77, "reloop_exit_shift", 1, 80, "per-deck ch 1-4 [1-9]"),
        ("1-10", "cue_loop_call_prev", "note", 1, 81, "cue_loop_call_prev_shift", 1, 97, "per-deck [1-10]"),
        ("1-11", "cue_loop_call_next", "note", 1, 83, "cue_loop_call_next_shift", 1, 98, "per-deck [1-11]"),
        ("1-12", "cue_loop_memory", "note", 1, 61, "cue_loop_memory_shift", 1, 62, "per-deck [1-12]"),
        ("1-13", "merge_fx_preset", "note", 5, 47, "merge_fx_preset_shift", 5, 48, "ch5 L / ch6 R [1-13]"),
        ("1-14", "merge_fx_param", "cc", 5, 8, "merge_fx_param_shift", 5, 40,
         "turn CC8/28 ch5/6; press note 46/49 [1-14]"),
        ("1-15", "beat_sync", "note", 1, 88, "beat_sync_shift", 1, 93, "per-deck ch 1-4 [1-15]"),
        ("1-16", "master_tempo", "note", 1, 92, "master_tempo_shift", 1, 96, "per-deck ch 1-4 [1-16]"),
        ("1-17", "tempo_slider", "cc", 1, 0, "tempo_slider_shift", 1, 5,
         "14-bit MSB0/LSB32; shift MSB5/LSB37 per-deck [1-17]"),
        ("2-1", "bfx_select", "note", 5, 112, "bfx_select_shift", 5, 112,
         "cycles FX1-1..FX2-3; shift reverses cycle [2-1]"),
        ("2-2", "bfx_ch_select", "note", 5, 28, "bfx_ch_select_shift", 5, 28,
         "CH1-4 + master select notes ch5/6 [2-2]"),
        ("2-3", "beat_fx_prev", "note", 5, 6, "beat_fx_prev_shift", 5, 102, "FX1 ch5 / FX2 ch6 [2-3]"),
        ("2-4", "beat_fx_next", "note", 5, 7, "beat_fx_next_shift", 5, 107, "FX1 ch5 / FX2 ch6 [2-4]"),
        ("2-5", "bfx_level_depth", "cc", 5, 2, "bfx_level_depth_shift", 5, 34,
         "14-bit CC per FX slot; MSB2/4/6 ch5/6 [2-5]"),
        ("2-6", "bfx_on_off", "note", 5, 71, "bfx_on_off_shift", 5, 67, "FX1-3 slots; shift all-FX note [2-6]"),
        ("3-1", "input_select", "note", 4, 13, None, None, None, "CH4 deck/sampler ch4 note 13 [3-1]"),
        ("3-2", "master_level", "cc", 7, 8, "master_level_shift", 7, 40, "14-bit MSB8 LSB40 ch7 [3-2]"),
        ("3-3", "master_cue", "note", 7, 99, "master_cue_shift", 7, 98, "ch7 [3-3]"),
        ("3-4", "booth_level", "cc", 7, 9, "booth_level_shift", 7, 41, "14-bit MSB9 LSB41 ch7 [3-4]"),
        ("3-5", "trim", "cc", 1, 4, "trim_shift", 1, 36, "14-bit MSB4 per-deck ch1-4 [3-5]"),
        ("3-6", "eq_hi", "cc", 1, 7, "eq_hi_shift", 1, 39, "EQ Hi/Mid/Low share fig; MSB7/11/15 [3-6]"),
        ("3-7", "filter", "cc", 7, 23, "filter_shift", 7, 55, "FILTER ch1-4 via ch7 MSB23-26 [3-7]"),
        ("3-8", "ch_cue", "note", 1, 84, "ch_cue_shift", 1, 104, "per-deck ch 1-4 [3-8]"),
        ("3-9", "ch_fader", "cc", 1, 19, "ch_fader_shift", 1, 82, "14-bit MSB19; shift cue-start note [3-9]"),
        ("3-10", "crossfader", "cc", 7, 31, "crossfader_shift", 7, 80,
         "14-bit MSB31; shift xfader-start notes [3-10]"),
        ("3-11", "ch_level_meter", "cc", 1, 2, None, None, None, "MIDI OUT only ch1-4 CC2 [3-11]"),
        ("3-12", "headphone_level", "cc", 7, 13, "headphone_level_shift", 7, 45, "MSB13 LSB45 ch7 [3-12]"),
        ("3-13", "headphone_mix", "cc", 7, 12, "headphone_mix_shift", 7, 44, "MSB12 LSB44 ch7 [3-13]"),
        ("3-14", "mic_level", "cc", 7, 5, "mic_level_shift", 7, 37, "MSB5 LSB37 ch7 [3-14]"),
        ("4-1", "browse_rotate", "cc", 7, 64, "browse_rotate_shift", 7, 100,
         "relative encoder; press load note 65/66 [4-1]"),
        ("4-2", "back", "note", 7, 101, "back_shift", 7, 102, "ch7 [4-2]"),
        ("4-3", "view", "note", 7, 122, "view_shift", 7, 104, "long-press note 103 [4-3]"),
        ("4-4", "load_deck1", "note", 7, 70, "load_deck1_shift", 7, 88, "ch7 [4-4]"),
        ("4-5", "load_deck2", "note", 7, 71, "load_deck2_shift", 7, 89, "ch7 [4-5]"),
        ("4-6", "load_deck3", "note", 7, 72, "load_deck3_shift", 7, 96, "ch7 [4-6]"),
        ("4-7", "load_deck4", "note", 7, 73, "load_deck4_shift", 7, 97, "ch7 [4-7]"),
        ("5-1", "hot_cue_mode", "note", 1, 27, "hot_cue_mode_shift", 1, 105, "per-deck ch 1-4 [5-1]"),
        ("5-2", "pad_fx_mode", "note", 1, 30, "pad_fx_mode_shift", 1, 111, "per-deck ch 1-4 [5-2]"),
        ("5-3", "beat_jump_mode", "note", 1, 32, "beat_jump_mode_shift", 1, 109, "per-deck [5-3]"),
        ("5-4", "sampler_mode", "note", 1, 34, "sampler_mode_shift", 1, 107, "per-deck [5-4]"),
        ("5-5", "performance_pad1", "note", 8, 0, "performance_pad1_shift", 9, 0,
         "pad grid ch8/10/12/14 unshifted; odd ch shifted; modes in midi rows [5-5]"),
    ]
    d: dict[str, dict] = {}
    for row in M:
        fig = row[0]
        d[fig] = {
            "name": row[1],
            "type": row[2],
            "ch": row[3],
            "code": row[4],
            "shift_name": row[5],
            "shift_type": row[2],
            "shift_ch": row[6],
            "shift_code": row[7],
            "notes": row[8],
        }
    return d


def _build_midi() -> dict:
    text = TXT.read_text(errors="replace")
    parsed = _parse_pdf_rows(text)
    overrides = _manual_overrides()
    controls: list[dict] = []

    def add(name: str, section: str, typ: str, ch: int, code: int, notes: str) -> None:
        controls.append(
            {"name": name, "section": section, "type": typ, "ch": ch, "code": code, "notes": notes}
        )

    for fig in sorted(POS, key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1]))):
        sec = SECTION[fig]
        ov = overrides[fig]
        tag = f"[{fig}]"
        note = ov["notes"]
        if tag not in note:
            note = f"{note} {tag}"
        add(ov["name"], sec, ov["type"], ov["ch"], ov["code"], note)
        sn = ov.get("shift_name")
        if sn and ov.get("shift_ch") is not None and ov.get("shift_code") is not None:
            shift_note = f"shift variant of {ov['name']} {tag}"
            add(sn, sec, ov["shift_type"], ov["shift_ch"], ov["shift_code"], shift_note)

        # Extra pad-mode rows sharing fig 5-5 (checksum uses one fig tag each - only on primary)
        if fig == "5-5":
            for mode, base_code in (
                ("hot_cue_pad", 0),
                ("pad_fx_pad", 16),
                ("beat_jump_pad", 32),
                ("sampler_pad", 48),
            ):
                add(
                    f"{mode}_deck1",
                    sec,
                    "note",
                    8,
                    base_code,
                    f"deck1 pad mode row; see performance pad table [{fig}]",
                )

        # Validate parser saw the fig (non-fatal)
        if fig not in parsed and fig not in overrides:
            raise SystemExit(f"fig {fig} missing from PDF parse and overrides")

    return {
        "source": "docs/controller/reference/DDJ-FLX6_MIDI_Message_List_E1.pdf",
        "archived": (DEV / "source" / "DDJ-FLX6_MIDI_Message_List_E1.pdf")
        .relative_to(REPO_ROOT)
        .as_posix(),
        "device": "DDJ-FLX6",
        "vendor": "AlphaTheta / Pioneer DJ",
        "channel_conventions": {
            "deck_rows": "PDF lists deck controls as one row for ch 1/2/3/4; entries carry ch=1 with note per-deck channels 1-4",
            "pad_rows": "Pads ch8/10/12/14 unshifted and 9/11/13/15 shifted (deck1-4)",
            "merge_fx": "ch5 left merge FX, ch6 right merge FX",
            "browser_mixer": "ch7 browser, load, master, booth, phones, mic, crossfader",
            "beat_fx": "ch5 FX1 bank, ch6 FX2 bank",
        },
        "controls": controls,
    }


def _fig_from_notes(notes: str) -> str | None:
    m = re.search(r"\[(\d+-\d+)\]", notes or "")
    return m.group(1) if m else None


def _primary_ctrl(ctrls: list[dict]) -> dict:
    for c in ctrls:
        if not c["name"].endswith("_shift") and "_pad_" not in c["name"]:
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


def _build_layout(midi: dict) -> dict:
    by_fig: dict[str, list[dict]] = defaultdict(list)
    for c in midi["controls"]:
        fig = _fig_from_notes(c.get("notes", ""))
        if fig:
            by_fig[fig].append(c)

    missing = sorted(set(POS) - set(by_fig))
    extra = sorted(set(by_fig) - set(POS))
    if missing:
        raise SystemExit(f"midi missing figs for layout POS: {missing}")
    if extra:
        raise SystemExit(f"midi figs without POS: {extra}")

    controls = []
    for fig in sorted(POS, key=lambda x: (int(x.split("-")[0]), int(x.split("-")[1]))):
        x, y, w, h, kind = POS[fig]
        ctrls = by_fig[fig]
        primary = _primary_ctrl(ctrls)
        shift = _shift_name(ctrls, primary)
        controls.append(
            {
                "id": f"ddj-flx6-{fig}",
                "fig": fig,
                "label": LABELS.get(fig, primary["name"]),
                "kind": kind,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "layer": "both" if shift else "base",
                "section": primary.get("section", SECTION[fig]),
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

    return {
        "device": "DDJ-FLX6",
        "vendor": "AlphaTheta / Pioneer DJ",
        "plate": {"image": PLATE, "width": pw, "height": ph},
        "layers": ["base", "shift"],
        "out_of_scope": [
            "jog_illumination_midi_out",
            "merge_fx_illumination_midi_out",
            "ch_level_meter_led_animation",
            "performance_pad_mode_matrix_rows",
            "right_deck_mirror_same_figs",
        ],
        "controls": controls,
    }


def main() -> None:
    midi = _build_midi()
    layout = _build_layout(midi)
    MIDI_OUT.write_text(json.dumps(midi, indent=2) + "\n")
    LAYOUT_OUT.write_text(json.dumps(layout, indent=2) + "\n")
    print(f"wrote {MIDI_OUT.name} ({len(midi['controls'])} midi rows, {len(POS)} figs)")
    print(f"wrote {LAYOUT_OUT.name} ({len(layout['controls'])} controls)")


if __name__ == "__main__":
    main()
