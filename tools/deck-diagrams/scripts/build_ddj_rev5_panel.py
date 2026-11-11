"""Build the DDJ-REV5 practice-replica panel pair (ADR-0137, epic #9366).

Writes tools/deck-diagrams/devices/ddj-rev5/panel.reference.json (the measurement of record)
and panel.json (the renderer layout) from ONE table of pixel boxes traced on the official
top-panel figure. The direction is trace -> reference -> layout, never the reverse.

Sources:
- Panel size 736.0 x 377.2 mm: DDJ-REV5 product specifications, pioneerdj.com (width x depth;
  height 73.4 mm is not modelled).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev5.ts, itself transcribed from
  the official "DDJ-REV5 List of MIDI Messages" PDF E1 (fig ids kept as `fig`).
- Positions: traced on page 1, embedded image 0 (1852 x 1038 px, the top-panel figure) of that
  PDF. `_PX` holds the inclusive pixel box of each control, read off 2x gridded crops; deck 2 is
  deck 1 translated by the offset that normalised cross-correlation of every deck-1 crop found
  inside the deck-2 region (+1092 px, 0 px, identical for all 24 deck controls), and channel 2
  knobs and fader are the channel-1 crops mirrored about the plate centre and matched the same
  way. The plate body is pixels x 91..1760, y 95..928 (non-black run of the figure), so the x and
  y scales are calibrated separately. Neither the PDF nor the image enters the repo.

Normalisation for panel.json: centres snapped to 0.5 mm, sizes to whole mm, and centres that
agree within 1.5 mm on an axis snapped onto one line, so row and column mates render aligned.
The reference keeps the raw conversion, which is why the position check scores a small non-zero
error instead of 0.

Run: uv run --locked python tools/deck-diagrams/scripts/build_ddj_rev5_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 736.0
PANEL_D_MM = 377.2
DEVICE = "ddj-rev5"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE

IMAGE_PX = (1852, 1038)
PANEL_PX = {"x0": 91, "x1": 1760, "y0": 95, "y1": 928}
MM_PER_PX = {
    "x": round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5),
    "y": round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5),
}
SOURCE_PDF_SHA256 = "577af3726e3e97fcaee25dc104a4e4ef42d418f6808f46bfb3eb284a17b67791"
SOURCE_IMAGE_SHA256 = "8790b9a4924f68cbdc70675cc4fe0da7521a7d6ae5f79a29b9d7b80e8647724f"
DECK2_DX_PX = 1092

ALIGN_MM = 1.5
SIZE_ALIGN_MM = 1.0


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
    midi: Wire
    shift_midi: Wire | None = None


Box = tuple[int, int, int, int]

# ----------------------------------------------------------------- traced pixel boxes

# Deck 1 (left), channel 1, FX and globals: inclusive [x0, y0, x1, y1] on the source image.
_PX_LEFT: dict[str, Box] = {
    "d1-tempo": (132, 135, 415, 182),
    "d1-range": (124, 208, 152, 236),
    "d1-key-lock": (177, 207, 203, 234),
    "d1-sync": (227, 209, 254, 236),
    "d1-bpm-select": (377, 214, 422, 231),
    "d1-bpm-start": (454, 210, 480, 236),
    "d1-auto-loop": (127, 272, 205, 297),
    "d1-loop-half": (265, 272, 307, 297),
    "d1-loop-double": (310, 272, 345, 297),
    "d1-slip": (415, 272, 450, 297),
    "d1-censor": (457, 272, 492, 297),
    "d1-key-minus": (555, 272, 587, 297),
    "d1-key-plus": (597, 272, 632, 297),
    "d1-stems": (127, 322, 205, 345),
    "d1-vocal": (265, 322, 312, 345),
    "d1-melody": (325, 322, 372, 345),
    "d1-bass": (385, 322, 432, 345),
    "d1-drums": (445, 322, 492, 345),
    "d1-hot-cue": (555, 322, 590, 345),
    "d1-scratch-bank": (597, 322, 632, 345),
    "d1-deck-select": (127, 455, 155, 482),
    "d1-jog": (134, 375, 625, 869),
    "d1-cue": (605, 760, 640, 795),
    "d1-play-pause": (576, 823, 634, 881),
}

# Pad section: both decks sit in the mixer centre, traced individually.
_PX_PADS: dict[str, Box] = {
    "d1-mode-hot-cue": (677, 462, 727, 480),
    "d1-mode-roll": (740, 462, 787, 480),
    "d1-mode-saved-loop": (800, 462, 850, 480),
    "d1-mode-sampler": (862, 462, 912, 480),
    "d1-pad-1": (675, 502, 727, 550),
    "d1-pad-2": (737, 502, 790, 550),
    "d1-pad-3": (800, 502, 852, 550),
    "d1-pad-4": (862, 502, 915, 550),
    "d1-pad-5": (675, 556, 727, 602),
    "d1-pad-6": (737, 556, 790, 602),
    "d1-pad-7": (800, 556, 852, 602),
    "d1-pad-8": (862, 556, 915, 602),
    "d1-param-prev": (675, 405, 702, 420),
    "d1-param-next": (707, 405, 735, 420),
    "d2-mode-hot-cue": (940, 462, 990, 480),
    "d2-mode-roll": (1002, 462, 1050, 480),
    "d2-mode-saved-loop": (1062, 462, 1112, 480),
    "d2-mode-sampler": (1125, 462, 1175, 480),
    "d2-pad-1": (937, 502, 990, 550),
    "d2-pad-2": (1000, 502, 1052, 550),
    "d2-pad-3": (1062, 502, 1115, 550),
    "d2-pad-4": (1125, 502, 1177, 550),
    "d2-pad-5": (937, 556, 990, 602),
    "d2-pad-6": (1000, 556, 1052, 602),
    "d2-pad-7": (1062, 556, 1115, 602),
    "d2-pad-8": (1125, 556, 1177, 602),
    "d2-param-prev": (1120, 405, 1147, 420),
    "d2-param-next": (1152, 405, 1180, 420),
}

_PX_MIXER: dict[str, Box] = {
    "ch1-input-select": (687, 140, 722, 160),
    "ch1-trim": (690, 192, 720, 222),
    "ch1-filter": (688, 248, 722, 282),
    "ch1-hi": (775, 133, 808, 165),
    "ch1-mid": (775, 188, 808, 220),
    "ch1-low": (775, 245, 808, 278),
    "ch1-ch-cue": (1120, 645, 1142, 657),
    "ch1-ch-fader": (808, 662, 851, 767),
    "ch2-input-select": (1132, 140, 1167, 160),
    "ch2-trim": (1131, 192, 1161, 222),
    "ch2-filter": (1129, 248, 1163, 282),
    "ch2-hi": (1043, 133, 1076, 165),
    "ch2-mid": (1043, 188, 1076, 220),
    "ch2-low": (1043, 245, 1076, 278),
    "ch2-ch-cue": (1152, 645, 1175, 657),
    "ch2-ch-fader": (1000, 662, 1043, 767),
    "browse": (569, 134, 621, 186),
    "back": (555, 207, 582, 240),
    "load-1": (592, 207, 632, 240),
    "load-2": (1684, 207, 1724, 240),
    "master": (907, 133, 942, 168),
    "booth": (907, 195, 942, 230),
    "sampler-level": (687, 637, 715, 665),
    "crossfader": (862, 832, 987, 878),
    "shift": (910, 410, 941, 427),
    "fx1-1": (675, 322, 702, 350),
    "fx1-2": (707, 322, 735, 350),
    "fx1-3": (740, 322, 767, 350),
    "fx2-1": (1087, 322, 1115, 350),
    "fx2-2": (1120, 322, 1147, 350),
    "fx2-3": (1152, 322, 1180, 350),
    "beat-prev": (867, 282, 902, 300),
    "beat-next": (950, 282, 985, 300),
    "fx-lever-1": (797, 336, 851, 388),
    "fx-lever-2": (1000, 336, 1057, 388),
    "level-depth": (907, 345, 942, 380),
}


def _px_table() -> dict[str, Box]:
    px: dict[str, Box] = dict(_PX_LEFT)
    for cid, (x0, y0, x1, y1) in _PX_LEFT.items():
        px[cid.replace("d1-", "d2-", 1)] = (x0 + DECK2_DX_PX, y0, x1 + DECK2_DX_PX, y1)
    px.update(_PX_PADS)
    px.update(_PX_MIXER)
    return px


_PX = _px_table()

# ----------------------------------------------------------------- mm conversion


def _mm(box: Box) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    sx, sy = MM_PER_PX["x"], MM_PER_PX["y"]
    return (
        round((x0 - PANEL_PX["x0"]) * sx, 1),
        round((y0 - PANEL_PX["y0"]) * sy, 1),
        round((x1 - x0 + 1) * sx, 1),
        round((y1 - y0 + 1) * sy, 1),
    )


def _clusters(values: dict[str, float], tol: float) -> dict[str, float]:
    """Snap values that chain within `tol` onto their cluster mean."""
    order = sorted(values, key=values.__getitem__)
    out: dict[str, float] = {}
    group: list[str] = []
    for cid in order:
        if group and values[cid] - values[group[-1]] > tol:
            mean = sum(values[g] for g in group) / len(group)
            out.update({g: mean for g in group})
            group = []
        group.append(cid)
    if group:
        mean = sum(values[g] for g in group) / len(group)
        out.update({g: mean for g in group})
    return out


def _normalised() -> dict[str, tuple[float, float, float, float]]:
    raw = {cid: _mm(box) for cid, box in _PX.items()}
    cx = _clusters({c: x + w / 2 for c, (x, _y, w, _h) in raw.items()}, ALIGN_MM)
    cy = _clusters({c: y + h / 2 for c, (_x, y, _w, h) in raw.items()}, ALIGN_MM)
    ws = _clusters({c: w for c, (_x, _y, w, _h) in raw.items()}, SIZE_ALIGN_MM)
    hs = _clusters({c: h for c, (_x, _y, _w, h) in raw.items()}, SIZE_ALIGN_MM)
    out: dict[str, tuple[float, float, float, float]] = {}
    for cid in raw:
        w = max(1.0, float(round(ws[cid])))
        h = max(1.0, float(round(hs[cid])))
        x = round(cx[cid] * 2) / 2 - w / 2
        y = round(cy[cid] * 2) / 2 - h / 2
        out[cid] = (round(x, 1), round(y, 1), w, h)
    return out


_POS = _normalised()


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _POS[cid]
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing traced box for {cid!r}") from e


# ----------------------------------------------------------------- control rows

Row = tuple[str, str, str, str, str, str, int, tuple[str, int] | None]

# suffix, fig, type, label, does, kind, id (deck channel), shifted (kind, id) or None
_DECK_ROWS: list[Row] = [
    ("tempo", "D4", "fader", "TEMPO", "Tempo slider", "cc", 0x00, ("cc", 0x05)),
    ("range", "D5", "button", "RANGE", "Tempo range", "note", 0x08, ("note", 0x18)),
    ("key-lock", "D6", "button", "KEY LOCK", "Keep the key while the tempo changes", "note", 0x09, ("note", 0x19)),
    ("sync", "D7", "button", "SYNC", "Match tempo to the other deck", "note", 0x0A, ("note", 0x1A)),
    ("bpm-start", "D10", "button", "START", "Start the BPM transition", "note", 0x31, ("note", 0x33)),
    ("auto-loop", "D11", "button", "AUTO LOOP", "Start or exit an auto loop", "note", 0x04, ("note", 0x14)),
    ("loop-half", "D12", "button", "1/2X", "Halve the loop", "note", 0x01, ("note", 0x10)),
    ("loop-double", "D13", "button", "2X", "Double the loop", "note", 0x03, ("note", 0x12)),
    ("slip", "D14", "button", "SLIP", "Slip mode", "note", 0x28, ("note", 0x38)),
    ("censor", "D15", "button", "CENSOR", "Reverse while held", "note", 0x29, ("note", 0x39)),
    ("key-minus", "D16", "button", "KEY -", "Key down a semitone", "note", 0x2A, ("note", 0x3A)),
    ("key-plus", "D17", "button", "KEY +", "Key up a semitone", "note", 0x2B, ("note", 0x3B)),
    ("stems", "D18", "button", "STEMS", "Stems separate mode", "note", 0x25, ("note", 0x35)),
    ("vocal", "D20", "button", "VOCAL", "Vocal stem on or off (STEMS mode)", "note", 0x2C, None),
    ("melody", "D21", "button", "MELODY", "Melody stem on or off (STEMS mode)", "note", 0x2D, None),
    ("bass", "D22", "button", "BASS", "Bass stem on or off (STEMS mode)", "note", 0x2E, None),
    ("drums", "D23", "button", "DRUMS", "Drums stem on or off (STEMS mode)", "note", 0x2F, None),
    ("hot-cue", "D24", "button", "HOT CUE", "Pad row: hot cues", "note", 0x26, ("note", 0x36)),
    ("scratch-bank", "D25", "button", "SCRATCH BANK", "Pad row: scratch bank", "note", 0x27, ("note", 0x37)),
    ("jog", "D3", "jog", "JOG", "Scratch or nudge the track", "cc", 0x22, ("cc", 0x1F)),
    ("cue", "D1", "button", "CUE", "Set or return to the cue point", "note", 0x0C, ("note", 0x1C)),
    ("play-pause", "D2", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B, ("note", 0x1B)),
    ("mode-hot-cue", "P1", "button", "HOT CUE", "Pad mode: hot cues", "note", 0x40, ("note", 0x50)),
    ("mode-roll", "P2", "button", "ROLL", "Pad mode: roll", "note", 0x41, ("note", 0x51)),
    ("mode-saved-loop", "P3", "button", "SAVED LOOP", "Pad mode: saved loops", "note", 0x42, ("note", 0x52)),
    ("mode-sampler", "P4", "button", "SAMPLER", "Pad mode: sampler", "note", 0x43, ("note", 0x53)),
    ("param-prev", "P6", "button", "PARAMETER <", "Previous pad parameter", "note", 0x60, ("note", 0x48)),
    ("param-next", "P7", "button", "PARAMETER >", "Next pad parameter", "note", 0x70, ("note", 0x5A)),
]


def _deck(deck: int) -> list[Control]:
    pad_ch = 6 + 2 * deck
    out: list[Control] = []
    for suffix, fig, typ, label, does, kind, num, shifted in _DECK_ROWS:
        cid = f"d{deck}-{suffix}"
        x, y, w, h = _at(cid)
        section = "performance" if fig.startswith("P") else "deck"
        shift = Wire(deck, shifted[0], shifted[1]) if shifted else None
        out.append(
            Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, section, Wire(deck, kind, num), shift)
        )
    # [PDF] D9 BPM TRANSITION SELECT sends the same global wire from either deck.
    x, y, w, h = _at(f"d{deck}-bpm-select")
    out.append(
        Control(
            f"d{deck}-bpm-select",
            "D9",
            "button",
            "SELECT",
            f"Pick the BPM transition length (deck {deck})",
            x,
            y,
            w,
            h,
            "deck",
            Wire(7, "note", 0x3E),
            Wire(7, "note", 0x30),
        )
    )
    # [PDF] D26 DECK 1/3 (deck 1) or DECK 2/4 (deck 2): the layer's channel, note 6.
    x, y, w, h = _at(f"d{deck}-deck-select")
    out.append(
        Control(
            f"d{deck}-deck-select",
            "D26",
            "button",
            f"DECK {deck + 2}",
            f"Switch this side to deck {deck + 2} (deck {deck})",
            x,
            y,
            w,
            h,
            "deck",
            Wire(deck + 2, "note", 0x06),
            Wire(7, "note", 0x7C if deck == 1 else 0x7D),
        )
    )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        px, py, pw, ph = _at(cid)
        out.append(
            Control(
                cid,
                "P5",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                px,
                py,
                pw,
                ph,
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch + 1, "note", pad),
            )
        )
    return out


def _channel(deck: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int, int, tuple[str, int] | None]] = [
        ("input-select", "M1", "button", "INPUT SELECT", "USB A or USB B source", "note", deck, 0x0E, None),
        ("trim", "M2", "knob", "TRIM", "Channel gain", "cc", deck, 0x04, None),
        ("hi", "M4", "knob", "HI", "EQ high band", "cc", deck, 0x07, None),
        ("mid", "M5", "knob", "MID", "EQ mid band", "cc", deck, 0x0B, None),
        ("low", "M6", "knob", "LOW", "EQ low band", "cc", deck, 0x0F, None),
        ("filter", "M3", "knob", "FILTER", "Color filter", "cc", 7, 0x17 if deck == 1 else 0x18, None),
        (
            "ch-cue",
            "M11" if deck == 1 else "M12",
            "button",
            f"CUE CH{deck}",
            "Headphone cue for this channel",
            "note",
            deck,
            0x07,
            ("note", 0x1F),
        ),
        ("ch-fader", "M13", "fader", "CH FADER", "Channel volume", "cc", deck, 0x13, None),
    ]
    out: list[Control] = []
    for suffix, fig, typ, label, does, kind, ch, num, shifted in rows:
        cid = f"ch{deck}-{suffix}"
        x, y, w, h = _at(cid)
        shift = Wire(ch, shifted[0], shifted[1]) if shifted else None
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {deck})", x, y, w, h, "mixer", Wire(ch, kind, num), shift)
        )
    return out


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int, str, int, Wire | None]] = [
        ("browse", "B1", "encoder", "BROWSE", "Scroll the library", "browse", 7, "cc", 0x40, Wire(7, "cc", 0x64)),
        (
            "back",
            "B2",
            "button",
            "BACK",
            "Back one level in the browser",
            "browse",
            7,
            "note",
            0x65,
            Wire(7, "note", 0x66),
        ),
        (
            "load-1",
            "B3",
            "button",
            "LOAD",
            "Load the selected track to deck 1",
            "browse",
            7,
            "note",
            0x46,
            Wire(7, "note", 0x5D),
        ),
        (
            "load-2",
            "B3",
            "button",
            "LOAD",
            "Load the selected track to deck 2",
            "browse",
            7,
            "note",
            0x47,
            Wire(7, "note", 0x6D),
        ),
        ("master", "M7", "knob", "MASTER LEVEL", "Master output level", "mixer", 7, "cc", 0x08, None),
        ("booth", "M8", "knob", "BOOTH LEVEL", "Booth output level", "mixer", 7, "cc", 0x09, None),
        ("sampler-level", "M9", "knob", "SAMPLER VOLUME", "Sampler level", "mixer", 7, "cc", 0x03, None),
        ("crossfader", "M14", "crossfader", "CROSSFADER", "Blend deck 1 and deck 2", "mixer", 7, "cc", 0x1F, None),
        ("shift", "E12", "button", "SHIFT", "Hold for each control's second function", "mixer", 7, "note", 0x3F, None),
        (
            "fx1-1",
            "E1",
            "button",
            "FX1 1",
            "Select beat effect 1 on unit 1",
            "effect",
            5,
            "note",
            0x70,
            Wire(7, "note", 0x30),
        ),
        (
            "fx1-2",
            "E2",
            "button",
            "FX1 2",
            "Select beat effect 2 on unit 1",
            "effect",
            5,
            "note",
            0x71,
            Wire(7, "note", 0x31),
        ),
        (
            "fx1-3",
            "E3",
            "button",
            "FX1 3",
            "Select beat effect 3 on unit 1",
            "effect",
            5,
            "note",
            0x72,
            Wire(7, "note", 0x32),
        ),
        (
            "fx2-1",
            "E4",
            "button",
            "FX2 1",
            "Select beat effect 1 on unit 2",
            "effect",
            6,
            "note",
            0x70,
            Wire(7, "note", 0x33),
        ),
        (
            "fx2-2",
            "E5",
            "button",
            "FX2 2",
            "Select beat effect 2 on unit 2",
            "effect",
            6,
            "note",
            0x71,
            Wire(7, "note", 0x34),
        ),
        (
            "fx2-3",
            "E6",
            "button",
            "FX2 3",
            "Select beat effect 3 on unit 2",
            "effect",
            6,
            "note",
            0x72,
            Wire(7, "note", 0x35),
        ),
        (
            "beat-prev",
            "E7",
            "button",
            "BEAT <",
            "Shorter effect beat",
            "effect",
            5,
            "note",
            0x06,
            Wire(7, "note", 0x36),
        ),
        ("beat-next", "E8", "button", "BEAT >", "Longer effect beat", "effect", 5, "note", 0x07, Wire(7, "note", 0x37)),
        ("fx-lever-1", "E9", "button", "CH1 LEVER", "Effect lever for channel 1", "effect", 5, "note", 0x50, None),
        ("fx-lever-2", "E10", "button", "CH2 LEVER", "Effect lever for channel 2", "effect", 6, "note", 0x50, None),
        ("level-depth", "E11", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", 5, "cc", 0x02, None),
    ]
    out: list[Control] = []
    for cid, fig, typ, label, does, section, ch, kind, num, shift in rows:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), shift))
    return out


# ----------------------------------------------------------------- documents


def build() -> dict[str, object]:
    controls = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    missing = sorted(set(_PX) - set(ids))
    if missing:
        raise SystemExit(f"[ERROR] traced boxes with no control row: {missing}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {(c.x, c.y, c.w, c.h)}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "2-deck scratch-style controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "REV5_MAP",
        "sources": {
            "panel_size": "DDJ-REV5 product specifications, pioneerdj.com (736.0 x 377.2 mm, width x depth)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-rev5.ts (DDJ-REV5 List of MIDI Messages E1)",
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                f"reference: devices/{DEVICE}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity",
            "MSB/LSB companion CCs: each control carries its MSB",
            "decks 3 and 4: the DECK buttons are drawn, the second layer is not",
            "front-panel controls (mic, crossfader curve and reverse, headphones, aux) and the JOG FEEL knob, LOCK ON levers and level meters: not on the MIDI list or not on the top plate",
        ],
        "controls": [asdict(c) for c in controls],
    }


def build_reference(panel: dict[str, object]) -> dict[str, object]:
    controls = panel["controls"]
    assert isinstance(controls, list)
    rows = []
    for c in controls:
        box = _PX[c["id"]]
        x, y, w, h = _mm(box)
        rows.append({"id": c["id"], "px": list(box), "x": x, "y": y, "w": w, "h": h})
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": (
                "DDJ-REV5 List of MIDI Messages E1, page 1, embedded image 0 "
                f"(top-panel figure, {IMAGE_PX[0]} x {IMAGE_PX[1]} px)"
            ),
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/DDJ-REV5-MIDI-Message-List.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PANEL_PX),
            "mm_per_px": dict(MM_PER_PX),
            "method": (
                "Button: body bounding box. Knob and encoder: visible cap. Pad: pad square with bezel. "
                "Jog: outer platter rim. TEMPO and channel faders: travel slot with cap height or width. "
                "Crossfader: travel slot with cap height. Effect lever: lever body. Input select: switch body. "
                f"Deck 2 = deck 1 translated by the cross-correlation offset ({DECK2_DX_PX}, 0) px; "
                "channel 2 knobs and fader = channel 1 mirrored about the plate centre and matched the same way; "
                "pads, pad-mode buttons, parameter arrows and the headphone cue buttons traced per deck."
            ),
            "repeat_spread_mm": 0.5,
            "issue": "#9366",
            "date": "Sun 8 Nov 2026",
        },
        "controls": rows,
    }


if __name__ == "__main__":
    panel = build()
    reference = build_reference(panel)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "panel.json").write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    (OUT_DIR / "panel.reference.json").write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    n = len(panel["controls"])  # type: ignore[arg-type]
    print(f"[OK] wrote {OUT_DIR / 'panel.json'} and panel.reference.json ({n} controls)")
