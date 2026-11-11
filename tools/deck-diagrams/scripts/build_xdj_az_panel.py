"""Build the XDJ-AZ practice-replica panel pair (ADR-0137, deck-replica checklist).

Writes two files under tools/deck-diagrams/devices/xdj-az/:

- panel.reference.json: the measurement of record. One pixel box per control, read off
  the top-panel photo embedded in the official "XDJ-AZ List of MIDI messages" PDF
  (page 1, image 0, 1217 x 710 px), converted to millimetres with separate x and y
  scales. The photo itself never enters the repo; only the numbers do.
- panel.json: the same trace normalised for the renderer (0.5 mm centres, whole-mm
  sizes, row and column mates snapped onto one line) with the wire identity each
  control sends, read from XDJ_AZ_MAP (apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts).

Sources:
- Panel size 895 x 504.1 mm: XDJ-AZ Instruction Manual, Specifications, "Max. external
  dimensions (W x D x H) 895 x 504.1 x 133.4 mm".
- Wire identities and fig numbers: the official MIDI message list (sha256 in
  docs/controller/research/XDJ-MIDI-Message-Lists.md), as transcribed into XDJ_AZ_MAP.
- Positions: measured trace of that PDF's top-panel photo (calibration below).

Layout facts the trace established:
- The right deck is a translation of the left deck by 730 px (591 mm), not a mirror.
  Every right-deck box is the left-deck box shifted; 22 right-deck controls were read
  independently to measure the repeat spread.
- The left deck carries deck 1 wires (channel 1) and the right deck deck 2 wires
  (channel 2), the unit's power-on selection. DECK SELECT boxes carry the channel of
  the deck they select.
- The four mixer strips sit in the order 3, 1, 2, 4 from left to right, as on the panel.

Run: uv run --locked python tools/deck-diagrams/scripts/build_xdj_az_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PANEL_W_MM = 895.0
PANEL_D_MM = 504.1
DEVICE = "xdj-az"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = OUT_DIR / "panel.json"
REFERENCE_OUT = OUT_DIR / "panel.reference.json"

# ----------------------------------------------------------------- calibration

IMAGE_PX = (1217, 710)
# Outer footprint of the unit on the photo: body x 56..1160, screen housing top y 54,
# front edge y 654 (inclusive pixel edges).
PANEL_PX = {"x0": 56, "x1": 1160, "y0": 54, "y1": 654}
MM_PER_PX = {
    "x": round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"]), 5),
    "y": round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"]), 5),
}
SOURCE_PDF_SHA256 = "2a56db4cc52c5bbf2371c33cbb0188a15ed28daf841f712cac343f4025f54216"
SOURCE_IMAGE_SHA256 = "4bfd616d2289b8fc3932d9b4de1a01d48ff1fb5cf35326cf7110b200dadb767c"
RIGHT_DECK_DX_PX = 730

Px = tuple[int, int, int, int]  # inclusive pixel box x0, y0, x1, y1


def _circle(cx: int, cy: int, d: int) -> Px:
    r = d // 2
    return (cx - r, cy - r, cx - r + d, cy - r + d)


# ----------------------------------------------------------------- traced boxes (left deck, image px)

# Button: body bounding box. Knob and encoder: visible cap. Pad: the pad square.
# Jog: outer platter ring. Vertical fader: travel slot with cap width. Crossfader
# and X-PAD: travel slot with cap height.
_LEFT_DECK_PX: dict[str, Px] = {
    "loop-in": _circle(80, 243, 22),
    "loop-out": _circle(112, 243, 22),
    "reloop-exit": _circle(149, 242, 14),
    "deck-select-a": (207, 233, 241, 252),
    "deck-select-b": (245, 233, 279, 252),
    "call-prev": _circle(322, 243, 12),
    "call-next": _circle(347, 243, 12),
    "delete": _circle(376, 242, 14),
    "memory": _circle(402, 242, 14),
    "4-beat": _circle(80, 274, 16),
    "8-beat": _circle(112, 274, 16),
    "beat-jump-rev": (70, 300, 89, 310),
    "beat-jump-fwd": (103, 300, 122, 310),
    "vinyl-speed-adjust": _circle(402, 274, 12),
    "jog-mode": (392, 303, 412, 313),
    "beat-sync": (386, 333, 399, 345),
    "sync-master": (406, 333, 419, 345),
    "key-sync": (391, 357, 415, 366),
    "jog": (118, 265, 378, 525),
    "slip": _circle(80, 352, 12),
    "slip-reverse": (70, 387, 94, 400),
    "shift": (84, 452, 108, 464),
    "track-search-rev": _circle(85, 489, 16),
    "track-search-fwd": _circle(106, 489, 16),
    "search-rev": _circle(85, 517, 16),
    "search-fwd": _circle(106, 517, 16),
    "cue": _circle(96, 564, 40),
    "play-pause": _circle(96, 616, 40),
    "tempo-range": _circle(397, 434, 12),
    "master-tempo": _circle(397, 463, 12),
    "tempo": (395, 484, 416, 632),
    "tempo-reset": _circle(356, 557, 14),
    "pad-mode-hot-cue": (168, 541, 200, 549),
    "pad-mode-beat-loop": (208, 541, 240, 549),
    "pad-mode-slip-loop": (248, 541, 280, 549),
    "pad-mode-beat-jump": (287, 541, 319, 549),
    "pad-1": (168, 563, 200, 595),
    "pad-2": (208, 563, 240, 595),
    "pad-3": (247, 563, 279, 595),
    "pad-4": (287, 563, 319, 595),
    "pad-5": (168, 602, 200, 634),
    "pad-6": (208, 602, 240, 634),
    "pad-7": (247, 602, 279, 634),
    "pad-8": (287, 602, 319, 634),
}

# Second, independent reading of right-deck controls (image px), used only to measure
# repeat_spread_mm against the translated left-deck boxes. Not written to the files.
_RIGHT_DECK_CHECK_PX: dict[str, Px] = {
    "loop-in": _circle(810, 243, 22),
    "loop-out": _circle(842, 243, 22),
    "reloop-exit": _circle(879, 242, 14),
    "deck-select-a": (940, 233, 975, 252),
    "deck-select-b": (976, 233, 1012, 252),
    "call-prev": _circle(1052, 243, 12),
    "call-next": _circle(1077, 243, 12),
    "delete": _circle(1106, 242, 14),
    "memory": _circle(1131, 242, 14),
    "4-beat": _circle(810, 274, 16),
    "8-beat": _circle(842, 274, 16),
    "beat-jump-rev": (800, 300, 819, 310),
    "vinyl-speed-adjust": _circle(1131, 274, 12),
    "jog-mode": (1121, 303, 1141, 313),
    "beat-sync": (1116, 333, 1129, 345),
    "sync-master": (1136, 333, 1149, 345),
    "key-sync": (1121, 357, 1145, 366),
    "tempo-range": _circle(1127, 434, 12),
    "master-tempo": _circle(1127, 463, 12),
    "tempo": (1123, 484, 1146, 632),
    "tempo-reset": _circle(1087, 557, 14),
    "cue": _circle(826, 564, 40),
    "play-pause": _circle(826, 616, 40),
    "pad-1": (898, 563, 930, 595),
    "pad-4": (1016, 563, 1048, 595),
}

# Centre of the four mixer strips, left to right: channels 3, 1, 2, 4.
_STRIP_CX = {3: 512, 1: 565, 2: 617, 4: 670}

# Everything outside the two decks (image px).
_GLOBAL_PX: dict[str, Px] = {
    # browse
    "browse": (741, 136, 773, 170),
    "back": (749, 110, 766, 122),
    "tag-track": (768, 110, 785, 122),
    "track-filter": (749, 188, 766, 200),
    "load-1": (750, 247, 764, 260),
    "load-2": (769, 247, 783, 260),
    "load-3": (751, 226, 763, 237),
    "load-4": (771, 226, 783, 237),
    "quantize": _circle(401, 172, 12),
    # beat fx column
    "x-pad": (704, 305, 787, 321),
    "beat-left": (714, 337, 731, 346),
    "beat-right": (758, 337, 775, 346),
    "auto-tap": _circle(720, 366, 12),
    "tap": _circle(745, 363, 22),
    "fx-quantize": _circle(770, 366, 12),
    "freq-low": (713, 389, 730, 398),
    "freq-mid": (736, 389, 753, 398),
    "freq-hi": (760, 389, 777, 398),
    "fx-select": _circle(744, 432, 18),
    "ch-select-3": (710, 474, 723, 481),
    "ch-select-1": (729, 474, 742, 481),
    "ch-select-2": (749, 474, 762, 481),
    "ch-select-4": (769, 474, 782, 481),
    "ch-select-mic": (710, 498, 723, 506),
    "ch-select-cf-a": (729, 498, 742, 506),
    "ch-select-cf-b": (749, 498, 762, 506),
    "ch-select-master": (769, 498, 782, 506),
    "fx-time": _circle(744, 530, 18),
    "level-depth": _circle(744, 569, 16),
    "fx-on-off": _circle(744, 614, 24),
    # sound color fx + parameter
    "scfx-space": (436, 372, 452, 381),
    "scfx-dub-echo": (460, 372, 477, 381),
    "scfx-crush": (436, 395, 452, 404),
    "scfx-sweep": (460, 395, 477, 404),
    "scfx-noise": (436, 419, 452, 428),
    "scfx-filter": (460, 419, 477, 428),
    "parameter": _circle(456, 455, 16),
    # headphones, master, booth, mic
    "link-cue": (446, 495, 466, 503),
    "phones-mix": _circle(456, 528, 16),
    "phones-level": _circle(456, 571, 16),
    "master-level": _circle(869, 152, 16),
    "master-cue": (856, 184, 880, 196),
    "master-eq-low": _circle(906, 186, 16),
    "master-eq-mid": _circle(943, 186, 16),
    "master-eq-hi": _circle(981, 186, 16),
    "booth-level": _circle(1023, 186, 14),
    "mic1-switch": _circle(150, 156, 10),
    "mic2-switch": _circle(150, 190, 10),
    "talkover": _circle(183, 172, 10),
    "mic1-eq-low": _circle(270, 154, 16),
    "mic1-eq-mid": _circle(310, 154, 16),
    "mic1-eq-hi": _circle(349, 154, 16),
    "mic2-eq-low": _circle(270, 188, 16),
    "mic2-eq-mid": _circle(310, 188, 16),
    "mic2-eq-hi": _circle(349, 188, 16),
    "crossfader": (558, 610, 620, 637),
}


def _strip_px(ch: int) -> dict[str, Px]:
    cx = _STRIP_CX[ch]
    return {
        f"ch{ch}-trim": _circle(cx, 303, 16),
        f"ch{ch}-hi": _circle(cx, 341, 20),
        f"ch{ch}-mid": _circle(cx, 376, 20),
        f"ch{ch}-low": _circle(cx, 411, 20),
        f"ch{ch}-color": _circle(cx, 457, 26),
        f"ch{ch}-cue": (cx - 12, 484, cx + 12, 493),
        f"ch{ch}-fader": (cx - 12, 522, cx + 12, 582),
    }


# ----------------------------------------------------------------- wires


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
    midi: Wire
    shift_midi: Wire | None = None


# fig, type, label, does, kind, number, shift number (deck channel rows)
DeckRow = tuple[str, str, str, str, str, int, int | None]

_DECK_ROWS: dict[str, DeckRow] = {
    "loop-in": ("1-19", "button", "IN/CUE", "Set the loop in point", "note", 6, None),
    "loop-out": ("1-19", "button", "OUT", "Set the loop out point", "note", 7, None),
    "reloop-exit": (
        "1-20",
        "button",
        "RELOOP/EXIT",
        "Exit or return to the loop",
        "note",
        8,
        None,
    ),
    "call-prev": (
        "1-21",
        "button",
        "CALL <",
        "Call the previous cue or loop",
        "note",
        12,
        None,
    ),
    "call-next": (
        "1-21",
        "button",
        "CALL >",
        "Call the next cue or loop",
        "note",
        11,
        None,
    ),
    "delete": (
        "1-22",
        "button",
        "DELETE",
        "Delete the called cue or loop",
        "note",
        13,
        None,
    ),
    "memory": ("1-23", "button", "MEMORY", "Store the cue or loop", "note", 10, None),
    "4-beat": ("1-18", "button", "4 BEAT", "Four-beat loop", "note", 67, None),
    "8-beat": ("1-18", "button", "8 BEAT", "Eight-beat loop", "note", 68, None),
    "beat-jump-rev": (
        "1-17",
        "button",
        "BEAT JUMP <",
        "Jump back by the beat length",
        "note",
        24,
        25,
    ),
    "beat-jump-fwd": (
        "1-17",
        "button",
        "BEAT JUMP >",
        "Jump forward by the beat length",
        "note",
        22,
        23,
    ),
    "vinyl-speed-adjust": (
        "1-11",
        "button",
        "VINYL SPEED",
        "Vinyl speed adjust on or off",
        "note",
        42,
        None,
    ),
    "jog-mode": (
        "1-9",
        "button",
        "JOG MODE",
        "Vinyl or CDJ jog mode",
        "note",
        18,
        None,
    ),
    "beat-sync": (
        "1-7",
        "button",
        "BEAT SYNC",
        "Match tempo to the master deck",
        "note",
        31,
        None,
    ),
    "sync-master": (
        "1-7",
        "button",
        "MASTER",
        "Make this deck the sync master",
        "note",
        30,
        None,
    ),
    "key-sync": (
        "1-6",
        "button",
        "KEY SYNC",
        "Match key to the master deck",
        "note",
        20,
        None,
    ),
    "jog": ("1-1", "jog", "JOG", "Scratch or nudge the track", "cc", 34, 41),
    "slip": ("1-16", "button", "SLIP", "Slip mode on or off", "note", 44, None),
    "slip-reverse": (
        "1-15",
        "button",
        "SLIP REV",
        "Slip reverse while held",
        "note",
        33,
        None,
    ),
    "shift": (
        "1-14",
        "button",
        "SHIFT",
        "Hold for each control's second function",
        "note",
        63,
        None,
    ),
    "track-search-rev": (
        "1-13",
        "button",
        "TRACK <",
        "Previous track",
        "note",
        5,
        None,
    ),
    "track-search-fwd": ("1-13", "button", "TRACK >", "Next track", "note", 4, None),
    "search-rev": (
        "1-12",
        "button",
        "SEARCH <",
        "Search backwards while held",
        "note",
        3,
        None,
    ),
    "search-fwd": (
        "1-12",
        "button",
        "SEARCH >",
        "Search forwards while held",
        "note",
        2,
        None,
    ),
    "cue": ("1-25", "button", "CUE", "Set or return to the cue point", "note", 1, None),
    "play-pause": (
        "1-24",
        "button",
        "PLAY/PAUSE",
        "Start or pause the deck",
        "note",
        0,
        None,
    ),
    "tempo-range": (
        "1-5",
        "button",
        "TEMPO RANGE",
        "Cycle the tempo fader range",
        "note",
        16,
        None,
    ),
    "master-tempo": (
        "1-4",
        "button",
        "MASTER TEMPO",
        "Keep the key while the tempo changes",
        "note",
        17,
        None,
    ),
    "tempo": ("1-2", "fader", "TEMPO", "Tempo fader", "cc", 0, None),
    "tempo-reset": (
        "1-3",
        "button",
        "TEMPO RESET",
        "Return the tempo to zero",
        "note",
        19,
        None,
    ),
    "pad-mode-hot-cue": (
        "5-1",
        "button",
        "HOT CUE",
        "Pad mode: hot cues",
        "note",
        34,
        38,
    ),
    "pad-mode-beat-loop": (
        "5-2",
        "button",
        "BEAT LOOP",
        "Pad mode: beat loops",
        "note",
        35,
        39,
    ),
    "pad-mode-slip-loop": (
        "5-3",
        "button",
        "SLIP LOOP",
        "Pad mode: slip loops",
        "note",
        36,
        40,
    ),
    "pad-mode-beat-jump": (
        "5-4",
        "button",
        "BEAT JUMP",
        "Pad mode: beat jump",
        "note",
        37,
        41,
    ),
}
_DECK_ORDER = list(_DECK_ROWS)

# AZ pads: pads 1-4 on channel 5 + deck, pads 5-8 on channel 9 + deck; hot cue note
# 19 * (pad index within the row); +SHIFT sends the next note (pioneer-xdj.ts azPadWire).
# Derived rows: e7ce5a5758e8688b33ca2ad5d5690ce65dec5574 midi.json,
# blob ab10e82c8f6a541e659f3da848521838e95440a1; vendor checksum SOURCE_PDF_SHA256.
# Preserve the source FX page names and unavailable SLIP LOOP base.
PAD_MODE_OFFSETS: dict[str, tuple[str, int | None, int | None]] = {
    "hot_cue": ("HOT CUE", 0, 1),
    "beat_loop": ("BEAT LOOP", 2, 3),
    "pad_fx1_page1": ("PAD FX1 PAGE1", 4, None),
    "pad_fx1_page2": ("PAD FX1 PAGE2", 5, None),
    "pad_fx2_page1": ("PAD FX2 PAGE1", 6, None),
    "pad_fx2_page2": ("PAD FX2 PAGE2", 7, None),
    "slip_loop": ("SLIP LOOP", None, 8),
    "beat_jump": ("BEAT JUMP", 9, 10),
    "gate_cue": ("GATE CUE", 11, 12),
    "beat_loop2": ("BEAT LOOP2", 13, 14),
    "key_shift": ("KEY SHIFT", 15, 16),
    "beat_jump2": ("BEAT JUMP2", 17, 18),
}
PAD_MODE_SELECTORS = {
    "hot-cue": ("hot_cue", "gate_cue"),
    "beat-loop": ("beat_loop", "beat_loop2"),
    "slip-loop": ("slip_loop", "key_shift"),
    "beat-jump": ("beat_jump", "beat_jump2"),
}

_PAD_FIGS = ["5-5", "5-6", "5-7", "5-8", "5-9", "5-10", "5-11", "5-12"]

# fig, type, label, does, section, ch, kind, number, shift number
GlobalRow = tuple[str, str, str, str, str, int, str, int, int | None]
_GLOBAL_ROWS: dict[str, GlobalRow] = {
    "browse": (
        "4-1",
        "encoder",
        "BROWSE",
        "Scroll the library",
        "browse",
        1,
        "cc",
        79,
        96,
    ),
    "back": (
        "4-2",
        "button",
        "BACK",
        "Back one level in the browser",
        "browse",
        1,
        "note",
        50,
        None,
    ),
    "tag-track": (
        "4-3",
        "button",
        "TAG TRACK",
        "Tag or untag the selected track",
        "browse",
        1,
        "note",
        48,
        None,
    ),
    "track-filter": (
        "4-4",
        "button",
        "TRACK FILTER",
        "Filter the track list",
        "browse",
        1,
        "note",
        49,
        None,
    ),
    "load-1": (
        "1-28",
        "button",
        "LOAD 1",
        "Load the selected track to deck 1",
        "browse",
        1,
        "note",
        81,
        None,
    ),
    "load-2": (
        "1-28",
        "button",
        "LOAD 2",
        "Load the selected track to deck 2",
        "browse",
        2,
        "note",
        81,
        None,
    ),
    "load-3": (
        "1-28",
        "button",
        "LOAD 3",
        "Load the selected track to deck 3",
        "browse",
        3,
        "note",
        81,
        None,
    ),
    "load-4": (
        "1-28",
        "button",
        "LOAD 4",
        "Load the selected track to deck 4",
        "browse",
        4,
        "note",
        81,
        None,
    ),
    "quantize": (
        "1-27",
        "button",
        "QUANTIZE",
        "Deck quantize on or off",
        "deck",
        1,
        "note",
        26,
        None,
    ),
    "x-pad": (
        "2-1",
        "crossfader",
        "X-PAD",
        "Beat effect touch strip",
        "effect",
        5,
        "cc",
        116,
        None,
    ),
    "beat-left": (
        "2-2",
        "button",
        "BEAT <",
        "Shorter effect beat",
        "effect",
        5,
        "cc",
        76,
        None,
    ),
    "beat-right": (
        "2-2",
        "button",
        "BEAT >",
        "Longer effect beat",
        "effect",
        5,
        "cc",
        77,
        None,
    ),
    "auto-tap": (
        "2-3",
        "button",
        "AUTO/TAP",
        "Automatic or tapped effect tempo",
        "effect",
        5,
        "cc",
        69,
        None,
    ),
    "tap": (
        "2-4",
        "button",
        "TAP",
        "Tap the effect tempo",
        "effect",
        5,
        "cc",
        78,
        None,
    ),
    "fx-quantize": (
        "2-5",
        "button",
        "FX QUANTIZE",
        "Quantize the beat effect",
        "effect",
        5,
        "note",
        118,
        None,
    ),
    "freq-low": (
        "2-8",
        "button",
        "LOW",
        "Effect frequency: low band",
        "effect",
        5,
        "cc",
        104,
        None,
    ),
    "freq-mid": (
        "2-7",
        "button",
        "MID",
        "Effect frequency: mid band",
        "effect",
        5,
        "cc",
        103,
        None,
    ),
    "freq-hi": (
        "2-6",
        "button",
        "HI",
        "Effect frequency: high band",
        "effect",
        5,
        "cc",
        102,
        None,
    ),
    "fx-select": (
        "2-9",
        "knob",
        "FX SELECT",
        "Pick the beat effect (DELAY position)",
        "effect",
        5,
        "cc",
        42,
        None,
    ),
    "ch-select-3": (
        "2-10",
        "button",
        "CH 3",
        "Beat effect on channel 3",
        "effect",
        5,
        "cc",
        34,
        None,
    ),
    "ch-select-1": (
        "2-10",
        "button",
        "CH 1",
        "Beat effect on channel 1",
        "effect",
        5,
        "cc",
        35,
        None,
    ),
    "ch-select-2": (
        "2-10",
        "button",
        "CH 2",
        "Beat effect on channel 2",
        "effect",
        5,
        "cc",
        36,
        None,
    ),
    "ch-select-4": (
        "2-10",
        "button",
        "CH 4",
        "Beat effect on channel 4",
        "effect",
        5,
        "cc",
        37,
        None,
    ),
    "ch-select-mic": (
        "2-10",
        "button",
        "MIC",
        "Beat effect on the microphones",
        "effect",
        5,
        "cc",
        28,
        None,
    ),
    "ch-select-cf-a": (
        "2-10",
        "button",
        "A",
        "Beat effect on crossfader side A",
        "effect",
        5,
        "cc",
        39,
        None,
    ),
    "ch-select-cf-b": (
        "2-10",
        "button",
        "B",
        "Beat effect on crossfader side B",
        "effect",
        5,
        "cc",
        40,
        None,
    ),
    "ch-select-master": (
        "2-10",
        "button",
        "MST",
        "Beat effect on the master",
        "effect",
        5,
        "cc",
        41,
        None,
    ),
    "fx-time": (
        "2-11",
        "encoder",
        "TIME",
        "Beat effect time",
        "effect",
        5,
        "cc",
        45,
        None,
    ),
    "level-depth": (
        "2-12",
        "knob",
        "LEVEL/DEPTH",
        "Beat effect amount",
        "effect",
        5,
        "cc",
        91,
        None,
    ),
    "fx-on-off": (
        "2-13",
        "button",
        "ON/OFF",
        "Beat effect on or off",
        "effect",
        5,
        "cc",
        114,
        None,
    ),
    "parameter": (
        "2-15",
        "knob",
        "PARAMETER",
        "Sound color effect amount",
        "effect",
        5,
        "cc",
        108,
        None,
    ),
    "scfx-space": (
        "2-16",
        "button",
        "SPACE",
        "Sound color: space",
        "effect",
        5,
        "cc",
        105,
        None,
    ),
    "scfx-dub-echo": (
        "2-17",
        "button",
        "DUB ECHO",
        "Sound color: dub echo",
        "effect",
        5,
        "cc",
        107,
        None,
    ),
    "scfx-crush": (
        "2-18",
        "button",
        "CRUSH",
        "Sound color: crush",
        "effect",
        5,
        "cc",
        86,
        None,
    ),
    "scfx-sweep": (
        "2-19",
        "button",
        "SWEEP",
        "Sound color: sweep",
        "effect",
        5,
        "cc",
        106,
        None,
    ),
    "scfx-noise": (
        "2-20",
        "button",
        "NOISE",
        "Sound color: noise",
        "effect",
        5,
        "cc",
        85,
        None,
    ),
    "scfx-filter": (
        "2-21",
        "button",
        "FILTER",
        "Sound color: filter",
        "effect",
        5,
        "cc",
        87,
        None,
    ),
    "crossfader": (
        "3-1",
        "crossfader",
        "CROSSFADER",
        "Blend side A and side B",
        "mixer",
        5,
        "cc",
        11,
        None,
    ),
    "phones-mix": (
        "3-9",
        "knob",
        "MIX",
        "Headphone cue and master blend",
        "mixer",
        5,
        "cc",
        27,
        None,
    ),
    "phones-level": (
        "3-10",
        "knob",
        "LEVEL",
        "Headphone level",
        "mixer",
        5,
        "cc",
        26,
        None,
    ),
    "link-cue": (
        "3-12",
        "button",
        "LINK CUE",
        "Hear the PRO DJ LINK source in headphones",
        "mixer",
        5,
        "cc",
        84,
        None,
    ),
    "master-cue": (
        "3-13",
        "button",
        "CUE",
        "Hear the master in headphones",
        "mixer",
        5,
        "cc",
        74,
        None,
    ),
    "master-level": (
        "3-14",
        "knob",
        "MASTER",
        "Master output level",
        "mixer",
        5,
        "cc",
        24,
        None,
    ),
    "master-eq-low": (
        "3-15",
        "knob",
        "LOW",
        "Master EQ low band",
        "mixer",
        5,
        "cc",
        49,
        None,
    ),
    "master-eq-mid": (
        "3-16",
        "knob",
        "MID",
        "Master EQ mid band",
        "mixer",
        5,
        "cc",
        48,
        None,
    ),
    "master-eq-hi": (
        "3-17",
        "knob",
        "HI",
        "Master EQ high band",
        "mixer",
        5,
        "cc",
        47,
        None,
    ),
    "booth-level": (
        "3-18",
        "knob",
        "BOOTH",
        "Booth output level",
        "mixer",
        5,
        "cc",
        25,
        None,
    ),
    "mic1-switch": (
        "3-19",
        "button",
        "MIC 1",
        "Microphone 1 on or off",
        "mixer",
        5,
        "cc",
        75,
        None,
    ),
    "mic2-switch": (
        "3-19",
        "button",
        "MIC 2",
        "Microphone 2 on or off",
        "mixer",
        5,
        "cc",
        79,
        None,
    ),
    "talkover": (
        "3-20",
        "button",
        "TALKOVER",
        "Duck the music under the microphone",
        "mixer",
        5,
        "cc",
        89,
        None,
    ),
    "mic1-eq-low": (
        "3-22",
        "knob",
        "LOW",
        "Microphone 1 EQ low band",
        "mixer",
        5,
        "cc",
        31,
        None,
    ),
    "mic1-eq-mid": (
        "3-23",
        "knob",
        "MID",
        "Microphone 1 EQ mid band",
        "mixer",
        5,
        "cc",
        98,
        None,
    ),
    "mic1-eq-hi": (
        "3-24",
        "knob",
        "HI",
        "Microphone 1 EQ high band",
        "mixer",
        5,
        "cc",
        30,
        None,
    ),
    "mic2-eq-low": (
        "3-22",
        "knob",
        "LOW",
        "Microphone 2 EQ low band",
        "mixer",
        5,
        "cc",
        97,
        None,
    ),
    "mic2-eq-mid": (
        "3-23",
        "knob",
        "MID",
        "Microphone 2 EQ mid band",
        "mixer",
        5,
        "cc",
        99,
        None,
    ),
    "mic2-eq-hi": (
        "3-24",
        "knob",
        "HI",
        "Microphone 2 EQ high band",
        "mixer",
        5,
        "cc",
        96,
        None,
    ),
}

_TRIM_CC = {1: 1, 2: 6, 3: 12, 4: 80}
_HI_CC = {1: 2, 2: 7, 3: 14, 4: 81}
_MID_CC = {1: 3, 2: 8, 3: 15, 4: 92}
_LOW_CC = {1: 4, 2: 9, 3: 21, 4: 82}
_COLOR_CC = {1: 5, 2: 10, 3: 22, 4: 83}
_FADER_CC = {1: 17, 2: 18, 3: 19, 4: 20}
_CH_CUE_CC = {1: 70, 2: 71, 3: 72, 4: 73}
_STRIP_ORDER = [3, 1, 2, 4]

# Row and column mates snapped to one line in panel.json (centre coordinate shared).
_ALIGN_ROWS: list[list[str]] = [
    [
        "loop-in",
        "loop-out",
        "reloop-exit",
        "deck-select-a",
        "deck-select-b",
        "call-prev",
        "call-next",
        "delete",
        "memory",
    ],
    ["4-beat", "8-beat", "vinyl-speed-adjust"],
    ["beat-jump-rev", "beat-jump-fwd"],
    ["beat-sync", "sync-master"],
    ["track-search-rev", "track-search-fwd"],
    ["search-rev", "search-fwd"],
    [
        "pad-mode-hot-cue",
        "pad-mode-beat-loop",
        "pad-mode-slip-loop",
        "pad-mode-beat-jump",
    ],
    ["pad-1", "pad-2", "pad-3", "pad-4"],
    ["pad-5", "pad-6", "pad-7", "pad-8"],
]
_ALIGN_COLS: list[list[str]] = [
    ["loop-in", "4-beat", "slip"],
    ["loop-out", "8-beat"],
    ["cue", "play-pause"],
    ["tempo-range", "master-tempo"],
    ["pad-mode-hot-cue", "pad-1", "pad-5"],
    ["pad-mode-beat-loop", "pad-2", "pad-6"],
    ["pad-mode-slip-loop", "pad-3", "pad-7"],
    ["pad-mode-beat-jump", "pad-4", "pad-8"],
    ["track-search-rev", "search-rev"],
    ["track-search-fwd", "search-fwd"],
]
_GLOBAL_ALIGN_ROWS: list[list[str]] = [
    ["back", "tag-track"],
    ["load-1", "load-2"],
    ["load-3", "load-4"],
    ["beat-left", "beat-right"],
    ["auto-tap", "tap", "fx-quantize"],
    ["freq-low", "freq-mid", "freq-hi"],
    ["ch-select-3", "ch-select-1", "ch-select-2", "ch-select-4"],
    ["ch-select-mic", "ch-select-cf-a", "ch-select-cf-b", "ch-select-master"],
    ["scfx-space", "scfx-dub-echo"],
    ["scfx-crush", "scfx-sweep"],
    ["scfx-noise", "scfx-filter"],
    ["master-eq-low", "master-eq-mid", "master-eq-hi", "booth-level"],
    ["mic1-eq-low", "mic1-eq-mid", "mic1-eq-hi"],
    ["mic2-eq-low", "mic2-eq-mid", "mic2-eq-hi"],
    [f"ch{c}-trim" for c in _STRIP_ORDER],
    [f"ch{c}-hi" for c in _STRIP_ORDER],
    [f"ch{c}-mid" for c in _STRIP_ORDER],
    [f"ch{c}-low" for c in _STRIP_ORDER],
    [f"ch{c}-color" for c in _STRIP_ORDER],
    [f"ch{c}-cue" for c in _STRIP_ORDER],
    [f"ch{c}-fader" for c in _STRIP_ORDER],
]
_GLOBAL_ALIGN_COLS: list[list[str]] = [
    ["fx-select", "fx-time", "level-depth", "fx-on-off", "tap"],
    ["phones-mix", "phones-level", "parameter"],
    ["scfx-space", "scfx-crush", "scfx-noise"],
    ["scfx-dub-echo", "scfx-sweep", "scfx-filter"],
    ["mic1-eq-low", "mic2-eq-low"],
    ["mic1-eq-mid", "mic2-eq-mid"],
    ["mic1-eq-hi", "mic2-eq-hi"],
    *[[f"ch{c}-{p}" for p in ("trim", "hi", "mid", "low", "color", "cue", "fader")] for c in _STRIP_ORDER],
]


# ----------------------------------------------------------------- geometry helpers


def _shift_px(box: Px, dx: int) -> Px:
    x0, y0, x1, y1 = box
    return (x0 + dx, y0, x1 + dx, y1)


def px_to_mm(box: Px) -> tuple[float, float, float, float]:
    """Raw conversion of an inclusive pixel box to a millimetre rectangle (0.1 mm)."""
    x0, y0, x1, y1 = box
    sx, sy = MM_PER_PX["x"], MM_PER_PX["y"]
    x = (x0 - PANEL_PX["x0"]) * sx
    y = (y0 - PANEL_PX["y0"]) * sy
    return (
        round(x, 1),
        round(y, 1),
        round((x1 - x0 + 1) * sx, 1),
        round((y1 - y0 + 1) * sy, 1),
    )


def _half(v: float) -> float:
    return round(v * 2) / 2


def _normalise(
    raw: dict[str, tuple[float, float, float, float]],
    rows: list[list[str]],
    cols: list[list[str]],
) -> dict[str, tuple[float, float, float, float]]:
    """Renderer normalisation: whole-mm sizes, 0.5 mm centres, mates on one line."""
    size = {k: (max(1.0, round(w)), max(1.0, round(h))) for k, (_, _, w, h) in raw.items()}
    cx = {k: x + w / 2 for k, (x, _, w, _) in raw.items()}
    cy = {k: y + h / 2 for k, (_, y, _, h) in raw.items()}
    for group in rows:
        mean = sum(cy[k] for k in group) / len(group)
        for k in group:
            cy[k] = mean
    for group in cols:
        mean = sum(cx[k] for k in group) / len(group)
        for k in group:
            cx[k] = mean
    out: dict[str, tuple[float, float, float, float]] = {}
    for k, (w, h) in size.items():
        out[k] = (round(_half(cx[k]) - w / 2, 1), round(_half(cy[k]) - h / 2, 1), w, h)
    return out


def _deck_px(deck: int) -> dict[str, Px]:
    dx = 0 if deck == 1 else RIGHT_DECK_DX_PX
    return {f"d{deck}-{k}": _shift_px(v, dx) for k, v in _LEFT_DECK_PX.items()}


def _deck_groups(deck: int, groups: list[list[str]]) -> list[list[str]]:
    return [[f"d{deck}-{k}" for k in g] for g in groups]


def all_px() -> dict[str, Px]:
    """Every control's traced pixel box, in output order."""
    out: dict[str, Px] = {}
    out.update(_deck_px(1))
    out.update(_deck_px(2))
    for ch in _STRIP_ORDER:
        out.update(_strip_px(ch))
    out.update(_GLOBAL_PX)
    return out


def repeat_spread_mm() -> float:
    """Largest centre disagreement between the translated left-deck trace and the
    independent right-deck reading, in millimetres."""
    worst = 0.0
    for k, box in _RIGHT_DECK_CHECK_PX.items():
        ref = _shift_px(_LEFT_DECK_PX[k], RIGHT_DECK_DX_PX)
        dx = ((box[0] + box[2]) - (ref[0] + ref[2])) / 2 * MM_PER_PX["x"]
        dy = ((box[1] + box[3]) - (ref[1] + ref[3])) / 2 * MM_PER_PX["y"]
        worst = max(worst, (dx * dx + dy * dy) ** 0.5)
    return round(worst, 1)


# ----------------------------------------------------------------- controls


def _deck_controls(deck: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    out: list[Control] = []
    for key in _DECK_ORDER:
        fig, typ, label, does, kind, num, shift_num = _DECK_ROWS[key]
        cid = f"d{deck}-{key}"
        x, y, w, h = pos[cid]
        section = "performance" if key.startswith("pad-mode") else "deck"
        shift = Wire(deck, kind, shift_num) if shift_num is not None else None
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (deck {deck})",
                x,
                y,
                w,
                h,
                section,
                Wire(deck, kind, num),
                shift,
            )
        )
    # DECK SELECT: the box carries the channel of the deck it selects.
    select = {1: (3, 1), 2: (2, 4)}[deck]
    for suffix, target in zip(("a", "b"), select, strict=True):
        cid = f"d{deck}-deck-select-{suffix}"
        x, y, w, h = pos[cid]
        out.append(
            Control(
                cid,
                "1-10",
                "button",
                f"DECK {target}",
                f"Select deck {target} on this side",
                x,
                y,
                w,
                h,
                "deck",
                Wire(target, "note", 21),
            )
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = pos[cid]
        ch = (5 if pad < 4 else 9) + deck
        note = 19 * (pad % 4)
        out.append(
            Control(
                cid,
                _PAD_FIGS[pad],
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(ch, "note", note),
                Wire(ch, "note", note + 1),
            )
        )
    return out


def _strip_controls(ch: int, pos: dict[str, tuple[float, float, float, float]]) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int]] = [
        ("trim", "3-5", "knob", "TRIM", "Channel gain", _TRIM_CC[ch]),
        ("hi", "3-6", "knob", "HI", "EQ high band", _HI_CC[ch]),
        ("mid", "3-7", "knob", "MID", "EQ mid band", _MID_CC[ch]),
        ("low", "3-8", "knob", "LOW", "EQ low band", _LOW_CC[ch]),
        ("color", "2-14", "knob", "COLOR", "Sound color filter", _COLOR_CC[ch]),
        (
            "cue",
            "3-11",
            "button",
            "CUE",
            "Headphone cue for this channel",
            _CH_CUE_CC[ch],
        ),
        ("fader", "3-2", "fader", "CH FADER", "Channel volume", _FADER_CC[ch]),
    ]
    out: list[Control] = []
    for key, fig, typ, label, does, cc in rows:
        cid = f"ch{ch}-{key}"
        x, y, w, h = pos[cid]
        section = "effect" if key == "color" else "mixer"
        out.append(
            Control(
                cid,
                fig,
                typ,
                label,
                f"{does} (channel {ch})",
                x,
                y,
                w,
                h,
                section,
                Wire(5, "cc", cc),
            )
        )
    return out


def _global_controls(
    pos: dict[str, tuple[float, float, float, float]],
) -> list[Control]:
    out: list[Control] = []
    for cid, (
        fig,
        typ,
        label,
        does,
        section,
        ch,
        kind,
        num,
        shift_num,
    ) in _GLOBAL_ROWS.items():
        x, y, w, h = pos[cid]
        shift = Wire(ch, kind, shift_num) if shift_num is not None else None
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
                Wire(ch, kind, num),
                shift,
            )
        )
    return out


def _positions() -> tuple[
    dict[str, tuple[float, float, float, float]],
    dict[str, tuple[float, float, float, float]],
]:
    px = all_px()
    raw = {k: px_to_mm(v) for k, v in px.items()}
    rows = _deck_groups(1, _ALIGN_ROWS) + _GLOBAL_ALIGN_ROWS
    cols = _deck_groups(1, _ALIGN_COLS) + _GLOBAL_ALIGN_COLS
    pos = _normalise({k: v for k, v in raw.items() if not k.startswith("d2-")}, rows, cols)
    # The right deck is the normalised left deck translated by one 0.5 mm offset, so the
    # two decks stay an exact translation after rounding.
    dx_mm = _half(RIGHT_DECK_DX_PX * MM_PER_PX["x"])
    for key in _LEFT_DECK_PX:
        x, y, w, h = pos[f"d1-{key}"]
        pos[f"d2-{key}"] = (round(x + dx_mm, 1), y, w, h)
    return raw, pos


def _check(controls: list[Control], pos: dict[str, tuple[float, float, float, float]]) -> None:
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    missing = sorted(set(pos) - set(ids))
    extra = sorted(set(ids) - set(pos))
    if missing or extra:
        raise SystemExit(f"[ERROR] trace and controls disagree: untraced={extra} unplaced={missing}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")


def _control_document(control: Control) -> dict[str, object]:
    row = asdict(control)
    if control.type == "pad":
        deck = int(control.id[1])
        pad = int(control.id.rsplit("-", 1)[1]) - 1
        ch = (5 if pad < 4 else 9) + deck
        note = 19 * (pad % 4)
        row["pad_layer_group"] = f"d{deck}"
        row["pad_layers"] = {
            mode: {
                "label": f"{label} PAD {pad + 1}",
                "does": f"Inspect {label} pad {pad + 1} (deck {deck})",
                "midi": asdict(Wire(ch, "note", note + base)) if base is not None else None,
                "shift_midi": asdict(Wire(ch, "note", note + shift)) if shift is not None else None,
            }
            for mode, (label, base, shift) in PAD_MODE_OFFSETS.items()
        }
    elif "-pad-mode-" in control.id:
        base, shift = PAD_MODE_SELECTORS[control.id.split("-pad-mode-", 1)[1]]
        row["pad_layer_group"] = control.id[:2]
        row["pad_layer_select"] = {"base": base, "shift": shift}
    return row


def build() -> tuple[dict[str, object], dict[str, object]]:
    """Return (panel.json document, panel.reference.json document)."""
    raw, pos = _positions()
    controls = [
        *_deck_controls(1, pos),
        *_deck_controls(2, pos),
        *[c for ch in _STRIP_ORDER for c in _strip_controls(ch, pos)],
        *_global_controls(pos),
    ]
    _check(controls, pos)
    px = all_px()
    reference = {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": {
            "source": "XDJ-AZ List of MIDI messages E1, page 1, embedded image 0 (top-panel photo, 1217 x 710 px)",
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "source_image_sha256": SOURCE_IMAGE_SHA256,
            "provenance": "docs/controller/research/XDJ-MIDI-Message-Lists.md",
            "image_px": list(IMAGE_PX),
            "panel_px": dict(PANEL_PX),
            "mm_per_px": dict(MM_PER_PX),
            "method": (
                "Button: body bounding box. Knob and encoder: visible cap. Pad: the pad square. "
                "Jog: outer platter ring. Vertical fader: travel slot with cap width. Crossfader and "
                "X-PAD: travel slot with cap height. Right deck: left-deck boxes translated by "
                f"{RIGHT_DECK_DX_PX} px; {len(_RIGHT_DECK_CHECK_PX)} right-deck controls re-read independently "
                "give repeat_spread_mm. Panel footprint includes the screen housing (depth 504.1 mm)."
            ),
            "repeat_spread_mm": repeat_spread_mm(),
            "issue": "#9366",
            "date": "Sun 8 Nov 2026",
        },
        "controls": [
            {
                "id": c.id,
                "px": list(px[c.id]),
                "x": raw[c.id][0],
                "y": raw[c.id][1],
                "w": raw[c.id][2],
                "h": raw[c.id][3],
            }
            for c in controls
        ],
    }
    panel = {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel all-in-one system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "XDJ_AZ_MAP",
        "sources": {
            "panel_size": "XDJ-AZ Instruction Manual, Specifications: max. external dimensions (W x D x H) 895 x 504.1 x 133.4 mm",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts (XDJ-AZ List of MIDI messages E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/xdj-az/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "deck 3 and deck 4 transport: the left deck carries deck 1 wires and the right deck deck 2 wires "
            "(power-on selection); DECK SELECT boxes carry the selected deck's channel",
            "MSB/LSB companion CCs: TEMPO carries its MSB",
            "hardware-only controls with no MIDI row: JOG FEEL, JOG RING, MIC 1/2 LEVEL, SHORT CUT, MIXER SETTINGS, the six screen buttons, USB STOP",
            "FX SELECT is a rotary switch with one CC per position; the control carries the DELAY position",
            "touch screen, needle search on the overall waveform, level meters and the jog displays",
            "crossfader assign and input select switches (utility settings, not panel controls)",
        ],
        "pad_layer_groups": {
            f"d{deck}": {
                "label": f"{'Left' if deck == 1 else 'Right'} pad layer",
                "default_mode": "hot_cue",
                "modes": [{"id": mode, "label": row[0]} for mode, row in PAD_MODE_OFFSETS.items()],
            }
            for deck in (1, 2)
        },
        "controls": [_control_document(c) for c in controls],
    }
    return panel, reference


if __name__ == "__main__":
    panel_doc, reference_doc = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PANEL_OUT.write_text(json.dumps(panel_doc, indent=1) + "\n", encoding="utf-8")
    REFERENCE_OUT.write_text(json.dumps(reference_doc, indent=1) + "\n", encoding="utf-8")
    n = len(panel_doc["controls"])  # type: ignore[arg-type]
    print(
        f"[OK] wrote {PANEL_OUT} and {REFERENCE_OUT} ({n} controls, repeat spread {reference_doc['trace']['repeat_spread_mm']} mm)"
    )  # type: ignore[index]
