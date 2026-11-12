"""Build tools/deck-diagrams/devices/xdj-rx3/panel.json, the XDJ-RX3 practice-replica panel layout.

Panel layout format v1 ((private path)), one run of
(private path) (epic #9366; the worked example is DDJ-FLX4, issue #9403).

Sources (public, official):
- Panel size 728.1 x 469.5 mm (W x D): XDJ-RX3 product page, Specifications (Width 728.1 mm,
  Depth 469.5 mm, Height 118.4 mm). The top plate the controls sit on is the front 389 mm of that
  depth; the rear 80 mm is the display housing.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts (XDJ_RX3_MAP),
  transcribed from the official "XDJ-RX3 MIDI Message List" PDF (fig numbers kept as `fig`).
- Positions: traced from the top-panel photo on page 1, embedded image 0, of that PDF
  (sha256 bd9c747ec65c3910d139d33cbbf08033a9ffd57b548978bf5dd51f2c2cdf9c36), one mm-per-px
  scale on both axes (the figure is isotropic within 3.5 % on square pads and round buttons),
  anchored on the measured left, right and front body edges. Normalised for the renderer:
  0.5 mm centres, whole-mm sizes, row and column mates aligned to within 1.5 mm, the jog a
  square of its mean diameter. Deck 2 is a translation of deck 1 (+468.6 mm), not a mirror.
  Scored against `devices/xdj-rx3/panel.reference.json` by
  `scripts/skins/control_position_error.py`.

Folding (checklist step 4): the list's four decks are two physical decks (channels 1 and 2);
the jog's platter and wheel-side rows are one jog; each pad carries its HOT CUE mode wire; the
EFFECT SELECT and CH SELECT rotaries carry their first detent's CC; the AUX switch carries its
OFF row. SHIFT has no wire on this unit (hardware-encoded), so it is not placed.

Run: uv run --locked python tools/deck-diagrams/scripts/build_xdj_rx3_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 728.1
PANEL_D_MM = 469.5
OUT = Path(__file__).resolve().parents[1] / "devices" / "xdj-rx3" / "panel.json"

DECKS = (1, 2)
MIXER_CH = 5
BROWSER_CH = 1


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


# Measured layout (mm). One entry per control id; see the module docstring for the method.
_POS: dict[str, tuple[float, float, float, float]] = {
    "d1-loop-in": (38.5, 158.5, 20.0, 20.0),
    "d1-loop-out": (64.0, 158.5, 20.0, 20.0),
    "d1-reloop-exit": (99.5, 163.0, 11.0, 11.0),
    "d1-call-prev": (144.0, 164.5, 9.0, 8.0),
    "d1-call-next": (166.0, 164.5, 9.0, 8.0),
    "d1-delete": (188.0, 162.5, 12.0, 12.0),
    "d1-memory": (209.0, 164.5, 9.0, 8.0),
    "d1-slip": (22.0, 195.0, 13.0, 13.0),
    "d1-quantize": (48.0, 195.0, 14.0, 13.0),
    "d1-vinyl-speed-adj": (226.0, 195.5, 13.0, 12.0),
    "d1-reverse": (18.5, 223.0, 20.0, 12.0),
    "d1-jog-mode": (222.5, 223.0, 20.0, 12.0),
    "d1-track-search-rev": (13.0, 259.5, 15.0, 12.0),
    "d1-track-search-fwd": (29.5, 259.5, 15.0, 12.0),
    "d1-search-rev": (13.0, 291.0, 15.0, 12.0),
    "d1-search-fwd": (29.5, 291.0, 15.0, 12.0),
    "d1-sync": (217.5, 251.5, 14.0, 12.0),
    "d1-master": (234.0, 251.5, 14.0, 12.0),
    "d1-tempo-range": (228.0, 284.0, 9.0, 9.0),
    "d1-master-tempo": (228.0, 307.5, 9.0, 9.0),
    "d1-jog": (51.5, 209.0, 159.0, 159.0),
    "d1-cue": (16.5, 367.0, 36.0, 36.0),
    "d1-play-pause": (16.5, 415.5, 36.0, 36.0),
    "d1-mode-hot-cue": (72.5, 381.5, 25.0, 7.0),
    "d1-mode-beat-loop": (103.5, 381.5, 24.0, 7.0),
    "d1-mode-slip-loop": (133.0, 381.5, 25.0, 7.0),
    "d1-mode-beat-jump": (163.5, 381.5, 24.0, 7.0),
    "d1-pad-1": (72.0, 399.5, 26.0, 25.0),
    "d1-pad-2": (102.5, 399.5, 26.0, 25.0),
    "d1-pad-3": (132.5, 399.5, 26.0, 25.0),
    "d1-pad-4": (162.5, 399.5, 26.0, 25.0),
    "d1-pad-5": (72.0, 430.5, 26.0, 26.0),
    "d1-pad-6": (102.5, 430.5, 26.0, 26.0),
    "d1-pad-7": (132.5, 430.5, 26.0, 26.0),
    "d1-pad-8": (162.5, 430.5, 26.0, 26.0),
    "d1-tempo": (223.0, 327.5, 19.0, 126.0),
    "d1-load": (490.5, 126.5, 12.0, 11.0),
    "d2-loop-in": (507.5, 158.5, 20.0, 20.0),
    "d2-loop-out": (532.5, 158.5, 20.0, 20.0),
    "d2-reloop-exit": (568.5, 163.0, 11.0, 11.0),
    "d2-call-prev": (612.5, 164.5, 9.0, 8.0),
    "d2-call-next": (634.5, 164.5, 9.0, 8.0),
    "d2-delete": (656.5, 162.5, 12.0, 12.0),
    "d2-memory": (677.5, 164.5, 9.0, 8.0),
    "d2-slip": (490.0, 195.0, 13.0, 13.0),
    "d2-quantize": (516.5, 195.0, 14.0, 13.0),
    "d2-vinyl-speed-adj": (695.0, 195.5, 13.0, 12.0),
    "d2-reverse": (486.5, 223.0, 20.0, 12.0),
    "d2-jog-mode": (691.5, 223.0, 20.0, 12.0),
    "d2-track-search-rev": (481.5, 259.5, 15.0, 12.0),
    "d2-track-search-fwd": (498.5, 259.5, 15.0, 12.0),
    "d2-search-rev": (481.5, 291.0, 15.0, 12.0),
    "d2-search-fwd": (498.5, 291.0, 15.0, 12.0),
    "d2-sync": (686.0, 251.5, 14.0, 12.0),
    "d2-master": (702.5, 251.5, 14.0, 12.0),
    "d2-tempo-range": (697.0, 284.0, 9.0, 9.0),
    "d2-master-tempo": (697.0, 307.5, 9.0, 9.0),
    "d2-jog": (520.0, 209.0, 159.0, 159.0),
    "d2-cue": (484.5, 367.0, 36.0, 36.0),
    "d2-play-pause": (484.5, 415.5, 36.0, 36.0),
    "d2-mode-hot-cue": (541.0, 381.5, 25.0, 7.0),
    "d2-mode-beat-loop": (572.0, 381.5, 24.0, 7.0),
    "d2-mode-slip-loop": (601.5, 381.5, 25.0, 7.0),
    "d2-mode-beat-jump": (632.0, 381.5, 24.0, 7.0),
    "d2-pad-1": (540.5, 399.5, 26.0, 25.0),
    "d2-pad-2": (571.0, 399.5, 26.0, 25.0),
    "d2-pad-3": (601.0, 399.5, 26.0, 25.0),
    "d2-pad-4": (631.0, 399.5, 26.0, 25.0),
    "d2-pad-5": (540.5, 430.5, 26.0, 26.0),
    "d2-pad-6": (571.0, 430.5, 26.0, 26.0),
    "d2-pad-7": (601.0, 430.5, 26.0, 26.0),
    "d2-pad-8": (631.0, 430.5, 26.0, 26.0),
    "d2-tempo": (692.0, 327.5, 19.0, 126.0),
    "d2-load": (505.0, 126.5, 12.0, 11.0),
    "ch1-input-switch": (275.0, 185.0, 19.0, 9.0),
    "ch1-trim": (321.5, 182.0, 16.0, 15.0),
    "ch1-eq-high": (321.5, 209.0, 16.0, 16.0),
    "ch1-eq-mid": (321.5, 238.5, 16.0, 15.0),
    "ch1-eq-low": (321.5, 268.0, 16.0, 16.0),
    "ch1-color": (319.0, 296.5, 21.0, 21.0),
    "ch1-ch-cue": (319.5, 328.0, 20.0, 8.0),
    "ch1-ch-fader": (318.0, 358.5, 23.0, 47.0),
    "ch2-input-switch": (434.5, 185.0, 19.0, 9.0),
    "ch2-trim": (391.0, 182.0, 17.0, 15.0),
    "ch2-eq-high": (391.5, 209.0, 16.0, 16.0),
    "ch2-eq-mid": (391.5, 238.5, 16.0, 15.0),
    "ch2-eq-low": (391.5, 268.0, 16.0, 16.0),
    "ch2-color": (389.0, 296.5, 21.0, 21.0),
    "ch2-ch-cue": (390.0, 328.0, 19.0, 8.0),
    "ch2-ch-fader": (388.0, 358.5, 23.0, 47.0),
    "aux-select": (275.0, 213.0, 19.0, 8.0),
    "aux-trim": (276.0, 237.5, 17.0, 17.0),
    "master-level": (356.0, 181.5, 17.0, 16.0),
    "booth-level": (436.0, 204.0, 16.0, 16.0),
    "beat-prev": (430.0, 239.0, 13.0, 8.0),
    "beat-next": (445.0, 239.0, 13.0, 8.0),
    "tap": (436.5, 258.0, 15.0, 15.0),
    "effect-select": (436.5, 286.0, 15.0, 14.0),
    "ch-select": (437.0, 311.0, 14.0, 14.0),
    "fx-space": (261.5, 292.0, 12.0, 10.0),
    "fx-dub-echo": (278.5, 292.0, 12.0, 10.0),
    "fx-sweep": (295.0, 292.0, 12.0, 10.0),
    "fx-noise": (261.5, 313.5, 12.0, 9.0),
    "fx-crush": (278.5, 313.5, 12.0, 9.0),
    "fx-filter": (295.0, 313.5, 12.0, 9.0),
    "parameter": (276.0, 333.0, 17.0, 16.0),
    "link-cue": (262.0, 364.0, 11.0, 7.0),
    "phones-mix": (276.0, 377.0, 17.0, 16.0),
    "phones-level": (276.0, 407.0, 17.0, 16.0),
    "master-cue": (355.0, 328.0, 19.0, 8.0),
    "crossfader": (341.0, 433.5, 47.0, 23.0),
    "crossfader-curve": (352.5, 375.5, 24.0, 13.0),
    "fx-time": (436.5, 347.0, 15.0, 15.0),
    "fx-level-depth": (436.5, 377.0, 15.0, 16.0),
    "fx-on-off": (432.0, 411.0, 24.0, 23.0),
    "browse": (482.5, 57.0, 40.0, 40.0),
    "back": (486.5, 41.0, 14.0, 12.0),
    "tag-track": (504.0, 41.0, 14.0, 12.0),
    "track-filter": (490.0, 106.5, 13.0, 12.0),
}

# ----------------------------------------------------------------- helpers


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        x, y, w, h = _POS[cid]
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _POS entry for {cid!r}") from e
    return round(x, 1), round(y, 1), w, h


def _shift(ch: int, row: tuple[str, int] | None) -> Wire | None:
    return None if row is None else Wire(ch, row[0], row[1])


# ----------------------------------------------------------------- deck strips

# slug, fig, type, label, does, kind, number, shifted row (kind, number) or None.
# Wire numbers are XDJ_RX3_MAP's (pioneer-xdj.ts); shifted rows are the list's +SHIFT rows the
# map hints. Order is the trace order and must match panel.reference.json.
DeckRow = tuple[str, str, str, str, str, str, int, tuple[str, int] | None]
_DECK_ROWS: list[DeckRow] = [
    ("loop-in", "1-14", "button", "LOOP IN", "Set the loop in point", "note", 6, None),
    ("loop-out", "1-15", "button", "LOOP OUT", "Set the loop out point", "note", 7, None),
    ("reloop-exit", "1-16", "button", "RELOOP/EXIT", "Exit or re-enter the loop", "note", 8, None),
    ("call-prev", "1-17", "button", "CALL <", "Call the previous cue or loop", "note", 12, None),
    ("call-next", "1-17", "button", "CALL >", "Call the next cue or loop", "note", 11, None),
    ("delete", "1-18", "button", "DELETE", "Delete the called cue or loop", "note", 13, None),
    ("memory", "1-19", "button", "MEMORY", "Store the cue or loop", "note", 10, None),
    ("slip", "1-12", "button", "SLIP", "Slip mode on or off", "note", 44, None),
    ("quantize", "1-25", "button", "QUANTIZE", "Quantize on or off", "note", 9, None),
    ("vinyl-speed-adj", "1-8-1", "button", "VINYL SPEED ADJ.", "Vinyl speed adjust on or off", "note", 42, None),
    ("reverse", "1-11", "button", "REV", "Reverse playback", "note", 33, None),
    ("jog-mode", "1-7", "button", "VINYL", "Jog mode: vinyl or CDJ", "note", 18, None),
    ("track-search-rev", "1-9", "button", "TRACK SEARCH <", "Previous track", "note", 5, None),
    ("track-search-fwd", "1-9", "button", "TRACK SEARCH >", "Next track", "note", 4, None),
    ("search-rev", "1-10", "button", "SEARCH <", "Search backwards", "note", 3, None),
    ("search-fwd", "1-10", "button", "SEARCH >", "Search forwards", "note", 2, None),
    ("sync", "1-5", "button", "SYNC", "Beat sync to the master deck", "note", 31, None),
    ("master", "1-6", "button", "MASTER", "Make this deck the sync master", "note", 30, None),
    ("tempo-range", "1-4", "button", "TEMPO RANGE", "Cycle the tempo range", "note", 16, None),
    ("master-tempo", "1-3", "button", "MASTER TEMPO", "Key lock on or off", "note", 17, None),
    ("jog", "1-1", "jog", "JOG", "Platter: scrub when paused, bend when playing", "cc", 34, ("cc", 41)),
    ("cue", "1-21", "button", "CUE", "Set or return to the cue point", "note", 1, None),
    ("play-pause", "1-20", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0, None),
    ("mode-hot-cue", "5-1", "button", "HOT CUE", "Pad mode: hot cue", "note", 34, ("note", 38)),
    ("mode-beat-loop", "5-2", "button", "BEAT LOOP", "Pad mode: beat loop", "note", 35, ("note", 39)),
    ("mode-slip-loop", "5-3", "button", "SLIP LOOP", "Pad mode: slip loop", "note", 36, ("note", 40)),
    ("mode-beat-jump", "5-4", "button", "BEAT JUMP", "Pad mode: beat jump", "note", 37, ("note", 41)),
]


def _deck(deck: int) -> list[Control]:
    ch = deck
    pad_ch = 5 + deck
    out: list[Control] = []
    for slug, fig, typ, label, does, kind, num, shift in _DECK_ROWS:
        cid = f"d{deck}-{slug}"
        x, y, w, h = _at(cid)
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
                "deck",
                Wire(ch, kind, num),
                _shift(ch, shift),
            )
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = _at(cid)
        out.append(
            Control(
                cid,
                f"5-{pad + 5}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                x,
                y,
                w,
                h,
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch, "note", 8 + pad),
            )
        )
    x, y, w, h = _at(f"d{deck}-tempo")
    out.append(
        Control(
            f"d{deck}-tempo",
            "1-2",
            "fader",
            "TEMPO",
            f"Tempo slider (deck {deck})",
            x,
            y,
            w,
            h,
            "deck",
            Wire(ch, "cc", 0),
        )
    )
    x, y, w, h = _at(f"d{deck}-load")
    out.append(
        Control(
            f"d{deck}-load",
            "1-22",
            "button",
            f"LOAD {deck}",
            f"Load the selected track to deck {deck}",
            x,
            y,
            w,
            h,
            "browse",
            Wire(ch, "note", 81),
        )
    )
    return out


# ----------------------------------------------------------------- mixer channels

# slug, fig, type, label, does, channel kind ("deck" = the deck's channel, else mixer), CC per channel.
_CHANNEL_ROWS: list[tuple[str, str, str, str, str, str, dict[int, int]]] = [
    ("input-switch", "3-13", "button", "LINE/PHONO", "Input select: deck, line or phono", "deck", {1: 99, 2: 99}),
    ("trim", "3-4", "knob", "TRIM", "Channel gain", "mixer", {1: 1, 2: 6}),
    ("eq-high", "3-5", "knob", "HI", "EQ or isolator high band", "mixer", {1: 2, 2: 7}),
    ("eq-mid", "3-6", "knob", "MID", "EQ or isolator mid band", "mixer", {1: 3, 2: 8}),
    ("eq-low", "3-7", "knob", "LOW", "EQ or isolator low band", "mixer", {1: 4, 2: 9}),
    ("color", "2-8", "knob", "COLOR", "Sound color FX amount", "mixer", {1: 5, 2: 10}),
    ("ch-cue", "3-10", "button", "CUE", "Headphone cue for this channel", "mixer", {1: 70, 2: 71}),
    ("ch-fader", "3-2", "fader", "CH FADER", "Channel volume", "mixer", {1: 17, 2: 18}),
]


def _channel(deck: int) -> list[Control]:
    out: list[Control] = []
    for slug, fig, typ, label, does, chan, cc in _CHANNEL_ROWS:
        cid = f"ch{deck}-{slug}"
        x, y, w, h = _at(cid)
        ch = deck if chan == "deck" else MIXER_CH
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {deck})", x, y, w, h, "mixer", Wire(ch, "cc", cc[deck]))
        )
    return out


# ----------------------------------------------------------------- globals

# id, fig, type, label, does, section, channel, kind, number, shifted row or None.
GlobalRow = tuple[str, str, str, str, str, str, int, str, int, tuple[str, int] | None]
_GLOBAL_ROWS: list[GlobalRow] = [
    ("aux-select", "3-17", "button", "AUX", "AUX input: off, line or portable", "mixer", MIXER_CH, "cc", 119, None),
    ("aux-trim", "3-18", "knob", "AUX TRIM", "AUX input gain", "mixer", MIXER_CH, "cc", 122, None),
    ("master-level", "3-14", "knob", "MASTER LEVEL", "Master output level", "mixer", MIXER_CH, "cc", 24, None),
    ("booth-level", "3-15", "knob", "BOOTH MONITOR", "Booth output level", "mixer", MIXER_CH, "cc", 25, None),
    ("beat-prev", "2-1", "button", "BEAT <", "Shorter beat FX beat", "effect", MIXER_CH, "cc", 76, None),
    ("beat-next", "2-1", "button", "BEAT >", "Longer beat FX beat", "effect", MIXER_CH, "cc", 77, None),
    ("tap", "2-2", "button", "TAP", "Tap the beat FX tempo", "effect", MIXER_CH, "cc", 78, ("cc", 126)),
    (
        "effect-select",
        "2-3",
        "knob",
        "EFFECT SELECT",
        "Pick the beat effect (one CC per detent)",
        "effect",
        MIXER_CH,
        "cc",
        42,
        None,
    ),
    (
        "ch-select",
        "2-4",
        "knob",
        "CH SELECT",
        "Pick the channel the beat effect hits (one CC per detent)",
        "effect",
        MIXER_CH,
        "cc",
        39,
        None,
    ),
    ("fx-space", "2-10", "button", "SPACE", "Sound color FX: space", "effect", MIXER_CH, "cc", 105, None),
    ("fx-dub-echo", "2-11", "button", "DUB ECHO", "Sound color FX: dub echo", "effect", MIXER_CH, "cc", 107, None),
    ("fx-sweep", "2-12", "button", "SWEEP", "Sound color FX: sweep", "effect", MIXER_CH, "cc", 106, None),
    ("fx-noise", "2-13", "button", "NOISE", "Sound color FX: noise", "effect", MIXER_CH, "cc", 85, None),
    ("fx-crush", "2-14", "button", "CRUSH", "Sound color FX: crush", "effect", MIXER_CH, "cc", 86, None),
    ("fx-filter", "2-15", "button", "FILTER", "Sound color FX: filter", "effect", MIXER_CH, "cc", 87, None),
    ("parameter", "2-9", "knob", "PARAMETER", "Sound color FX parameter", "effect", MIXER_CH, "cc", 108, None),
    ("link-cue", "3-12", "button", "LINK CUE", "Hear the LINK input in headphones", "mixer", MIXER_CH, "cc", 75, None),
    ("phones-mix", "3-8", "knob", "MIXING", "Headphone cue and master blend", "mixer", MIXER_CH, "cc", 27, None),
    ("phones-level", "3-9", "knob", "LEVEL", "Headphone level", "mixer", MIXER_CH, "cc", 26, None),
    ("master-cue", "3-11", "button", "MASTER CUE", "Hear the master in headphones", "mixer", MIXER_CH, "cc", 74, None),
    (
        "crossfader",
        "3-1",
        "crossfader",
        "CROSSFADER",
        "Blend channel 1 and channel 2",
        "mixer",
        MIXER_CH,
        "cc",
        11,
        None,
    ),
    (
        "crossfader-curve",
        "3-3",
        "button",
        "CROSSFADER CURVE",
        "Crossfader curve: thru, smooth or sharp",
        "mixer",
        MIXER_CH,
        "cc",
        95,
        None,
    ),
    ("fx-time", "2-5", "encoder", "TIME", "Beat FX time", "effect", MIXER_CH, "cc", 45, None),
    ("fx-level-depth", "2-6", "knob", "LEVEL/DEPTH", "Beat FX amount", "effect", MIXER_CH, "cc", 91, None),
    ("fx-on-off", "2-7", "button", "ON/OFF", "Beat FX on or off", "effect", MIXER_CH, "cc", 114, ("cc", 52)),
    ("browse", "4-1", "encoder", "ROTARY SELECTOR", "Scroll the library", "browse", BROWSER_CH, "cc", 79, ("cc", 96)),
    ("back", "4-2", "button", "BACK", "Back one browse level", "browse", BROWSER_CH, "note", 50, ("note", 95)),
    (
        "tag-track",
        "4-3",
        "button",
        "TAG TRACK/REMOVE",
        "Tag or untag the selected track",
        "browse",
        BROWSER_CH,
        "note",
        48,
        ("note", 98),
    ),
    (
        "track-filter",
        "4-4",
        "button",
        "TRACK FILTER",
        "Filter the track list",
        "browse",
        BROWSER_CH,
        "note",
        49,
        ("note", 93),
    ),
]


def _globals() -> list[Control]:
    out: list[Control] = []
    for cid, fig, typ, label, does, section, ch, kind, num, shift in _GLOBAL_ROWS:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), _shift(ch, shift)))
    return out


def build() -> dict[str, object]:
    controls = [*_deck(1), *_deck(2), *_channel(1), *_channel(2), *_globals()]
    known = {c.id for c in controls}
    missing_pos = sorted(known - set(_POS))
    extra_pos = sorted(set(_POS) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _POS mismatch: missing={missing_pos} extra={extra_pos}")
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")
    controls = [replace(c) for c in controls]
    return {
        "format": "opendj-panel-layout/1",
        "device": "xdj-rx3",
        "display_name": "2-channel all-in-one system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "XDJ_RX3_MAP",
        "sources": {
            "panel_size": "XDJ-RX3 product page, Specifications (Width 728.1 mm, Depth 469.5 mm, Height 118.4 mm)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/pioneer-xdj.ts (XDJ-RX3 MIDI Message List, 2021)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/xdj-rx3/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "SHIFT: the list gives it no wire (hardware-encoded), so it is not placed",
            "pad modes other than HOT CUE: pads carry the HOT CUE wire identity",
            "decks 3 and 4: the unit has two physical decks; the list's channel 3/4 rows have no control",
            "MSB/LSB companion CCs: TEMPO carries its MSB",
            "GUI rows (NEEDLE POSITION, TIME MODE, X-PAD, BEAT FX BANK, DECK 1/2) and hardware-only controls "
            "(SHORT CUT, USB STOP, MASTER REC, TRACK MARKER, JOG ADJUST, MIC)",
            "CH LEVEL METER, JOG DISPLAY and LOAD illumination: output only",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
