from __future__ import annotations

from dataclasses import dataclass


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
