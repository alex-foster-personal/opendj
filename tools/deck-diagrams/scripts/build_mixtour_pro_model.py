"""Part of `build_mixtour_pro.py`, split out with no behavior change: model."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "devices" / "reloop-mixtour-pro"
PLATE = "source/plate-top.png"
PLATE_W, PLATE_H = 1010, 1743  # px of source/plate-top.png

DEVICE = "Reloop MIXTOUR PRO"
VENDOR = "Reloop"

# ---------------------------------------------------------------- channels

# [MM] page 1 "MIDI Channel" table. Letters are the map's own key.
CHANNEL_KEY = {
    "N": "Deck transport + mixer (deck 1..4 = MIDI ch 1..4, hex 00..03)",
    "P": "Deck other functions: pads, mode, load (deck 1..4 = ch 5..8, hex 04..07)",
    "E": "FX unit per deck (deck 1..4 = ch 9..12, hex 08..0B)",
    "G": "Global (ch 16, hex 0F)",
}


def chan(letter: str, deck: int = 1) -> int:
    """1-based MIDI channel for a channel-key letter and a deck number."""
    if letter == "G":
        return 16
    base = {"N": 0, "P": 4, "E": 8}[letter]
    return base + deck


LAYERS = ["base", "shift", "mode", "shift_mode"]
LAYER_TITLE = {
    "base": "BASE",
    "shift": "SHIFT",
    "mode": "MODE",
    "shift_mode": "SHIFT + MODE",
}
LAYER_SUFFIX = {"base": "", "shift": "_shift", "mode": "_mode", "shift_mode": "_shift_mode"}

# ---------------------------------------------------------------- LED tables

# [MM] page 2. RGB pads: value = 64*full + 16*R + 4*G + B, each of R,G,B in 0..3
# (0%, 33%, 66%, 100%). 0..63 = dimmed (25%) bank, 64..127 = full (100%).
# Transport bi-LEDs: 0 off, 1 red dim, 2 blue dim, 3 red+blue (purple) dim,
# 7D red, 7E blue, 7F purple. [HW] confirmed the bi-LED values with amidi.
BILED = [
    (0x00, "off"),
    (0x01, "red, dimmed 25%"),
    (0x02, "blue, dimmed 25%"),
    (0x03, "red + blue (purple), dimmed 25%"),
    (0x7D, "red, full"),
    (0x7E, "blue, full"),
    (0x7F, "red + blue (purple), full"),
]

# ---------------------------------------------------------------- data model


@dataclass
class M:
    """One wire address. letter is the [MM] channel key; code is the data1 byte."""

    letter: str
    code: int
    type: str = "note"  # note | cc
    led: int | None = None  # LED echo address (same note) when the map lists one
    values_in: str = "OFF = 00, ON = 7F"
    values_out: str = ""
    direction: str = "in"  # in | out | both
    src: str = "MM"
    # Which deck's channel this address is on. None = derive: G letter -> global,
    # a deck-scoped control -> side. A global control with a deck-channel layer
    # must say so explicitly (modifier_side / active_decks) or the build fails.
    scope: str | None = None


@dataclass
class F:
    """What a layer does on this control."""

    text: str
    src: str
    midi: M | None = None


@dataclass
class C:
    fig: str
    num: str  # [IM] designation number
    label: str
    kind: str  # schema enum: button pad knob encoder fader jog switch meter other
    section: str
    geo: list[tuple[int, int, int, int]]  # px boxes; 1 = single, 2 = left/right
    layers: dict[str, F]
    led: str  # LED feedback prose
    notes: str = ""
    deck_scoped: bool = True  # False = global (single physical control, no deck)
    # Physical placement: "both" = mirrored left/right pair (two geo boxes),
    # "left" / "right" = one control on that side, "center" = one global control.
    side: str = "both"
    extra: dict = field(default_factory=dict)


SCOPES = ("global", "side", "modifier_side", "active_decks", "pad_bank")


def scope_of(c: C, m: M) -> str:
    """Resolve the deck scope of one address, failing loud on an ambiguous global control."""
    if m.scope:
        assert m.scope in SCOPES, (c.fig, m.scope)
        return m.scope
    if m.letter == "G":
        return "global"
    if c.deck_scoped:
        return "side"
    raise AssertionError(f"{c.fig}: layer on channel {m.letter} of a global control needs an explicit scope")


def box(x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    return (x, y, w, h)


def lr(xl: int, xr: int, y: int, w: int, h: int) -> list[tuple[int, int, int, int]]:
    return [box(xl, y, w, h), box(xr, y, w, h)]


NO_LED = "No LED on this control."
BILED_TXT = (
    "Bi-color red/blue LED under the cap, driven by the software on the SAME note the "
    "button sends, separately per layer: base note, SHIFT note, MODE note and SHIFT+MODE "
    "note each address their own LED bank; the firmware decides which bank is visible "
    "while a modifier is held [HW]. Values: 00 off, 01 red dim, 02 blue dim, 03 purple dim, "
    "7D red, 7E blue, 7F purple [MM][HW]. Meaning of the colors is djay's choice "
    "(typically blue = idle/available, red = active, purple = MODE overlay)."
)

# Pad-mode behavior keyed by the label printed under the pad. The pad NUMBER that
# selects a mode is derived from PAD_ORDER below (never listed here twice).
PAD_MODE_INFO = {
    # mode: (how it behaves, SHIFT + pad, source)
    "HOT CUE": (
        "Pads trigger the 8 cue points; each pad lights in that cue's color.",
        "Deletes the cue point on that pad.",
        "IM",
    ),
    "AUTO LOOP": (
        "Each pad is a fixed loop length; press to start the loop, press again to release it.",
        "Not documented in [IM]; SHIFT+pad still sends its own note (P 1C..23).",
        "IM",
    ),
    "BOUNCE LOOP": (
        "Momentary loop roll: the loop runs while the pad is held, length varies per pad.",
        "Not documented in [IM]; SHIFT+pad still sends its own note (P 1C..23).",
        "IM",
    ),
    "SAMPLER": (
        "Pads trigger the corresponding sample in the sample bank.",
        "Not documented in [IM]; SHIFT+pad still sends its own note (P 1C..23).",
        "IM",
    ),
    "PITCH CUE": (
        "Pads set the musical key of the track (pitch play from a cue point).",
        "Switches between cue points used as the pitch-play source.",
        "IM",
    ),
    "SAVED LOOPS": (
        "A bank of up to 8 saved loops per track for instant recall.",
        "Triggers the saved loop instantly (manual wording).",
        "IM",
    ),
    "INSTANT FX": (
        "Each pad is an instant effect, active while pressed.",
        "Not documented in [IM]; SHIFT+pad still sends its own note (P 1C..23).",
        "IM",
    ),
    "NEURAL MIX": (
        "Pads control the Neural Mix stem elements (vocals, harmonic, drums per deck).",
        "Not documented in [IM]; SHIFT+pad still sends its own note (P 1C..23).",
        "IM",
    ),
}

# Plate silkscreen order, row by row, left column then right column [LBL]:
#   HOT CUE | AUTO LOOP / BOUNCE LOOP | SAMPLER / PITCH CUE | SAVED LOOPS / INSTANT FX | NEURAL MIX
# [HW] scratchpad captured the MODE+pad mode-select notes as P 00 Hot Cue, 01 Bounce
# Loop, 02 Pitch Cue, 03 Instant FX, 04 Auto Loop, 05 Sampler, 06 Saved Loops,
# 07 Neural Mix, i.e. column-major: pads 1-4 are the LEFT column top to bottom and
# pads 5-8 the RIGHT column. The base pad notes P 14..1B are assumed to follow the
# same physical order (ASSUMED: [MM] numbers the pads without a picture).
PAD_ORDER = [  # (pad number, column, row, mode label)
    (1, 0, 0, "HOT CUE"),
    (2, 0, 1, "BOUNCE LOOP"),
    (3, 0, 2, "PITCH CUE"),
    (4, 0, 3, "INSTANT FX"),
    (5, 1, 0, "AUTO LOOP"),
    (6, 1, 1, "SAMPLER"),
    (7, 1, 2, "SAVED LOOPS"),
    (8, 1, 3, "NEURAL MIX"),
]
PAD_MODE_BY_LABEL = {label: (n, label) + PAD_MODE_INFO[label] for n, _c, _r, label in PAD_ORDER}
# (pad number, mode, how, shift, source), in pad-number order, derived from PAD_ORDER
PAD_MODES = [PAD_MODE_BY_LABEL[label] for _n, _c, _r, label in PAD_ORDER]
assert set(PAD_MODE_INFO) == {m[1] for m in PAD_MODES}, "pad-mode info and plate order disagree"


def pad_controls() -> list[C]:
    out = []
    col_x = [430, 540]
    row_y = [557, 630, 703, 777]
    for n, col, row, mode in PAD_ORDER:
        base_note = 0x14 + (n - 1)
        shift_note = 0x1C + (n - 1)
        mode_note = 0x00 + (n - 1)
        pm = PAD_MODE_BY_LABEL[mode]
        bank = (
            "All 8 pad notes exist on EVERY deck's P channel [HW]; the firmware sends a pad on the "
            "channel of the deck the pad bank is assigned to: unsplit, the deck of the side whose MODE "
            "picked the mode; with SPLIT on, pads 1-4 on the left deck and 5-8 on the right deck [IM]. "
            "Pad numbering inside a split bank is unmeasured (ASSUMED unchanged)."
        )
        out.append(
            C(
                fig=f"pad_{n}",
                num="7",
                label=f"PAD {n}",
                kind="pad",
                section="pads",
                side="left" if col == 0 else "right",
                geo=[box(col_x[col], row_y[row], 75, 51)],
                layers={
                    "base": F(
                        f"Performance pad {n} in the current pad mode (see pad-mode table). {bank}",
                        "IM",
                        M("P", base_note, led=base_note, values_out="refer to LED map", scope="pad_bank"),
                    ),
                    "shift": F(
                        f"SHIFT + pad {n}: mode-dependent second function. Hot Cue: delete cue {n}. "
                        f"Pitch Cue: switch cue point. Saved Loops: trigger saved loop {n} [IM]. "
                        f"Sends a DIFFERENT note than the base pad and lights a separate LED bank [HW].",
                        "IM",
                        M("P", shift_note, led=shift_note, values_out="refer to LED map", scope="pad_bank"),
                    ),
                    "mode": F(
                        f"MODE + pad {n}: select pad mode {mode} (label printed under the pad) [IM][LBL]. "
                        f"Mode-select notes hardware-captured as column-major, on the P channel of the deck "
                        f"whose MODE is held [HW].",
                        "HW",
                        M("P", mode_note, led=mode_note, values_out="refer to LED map", scope="modifier_side"),
                    ),
                },
                led=(
                    "RGB pad. Software sets the color by note velocity on the same note the pad "
                    "sends: value = 64*full + 16*R + 4*G + B with R,G,B in 0..3 (0/33/66/100%); "
                    "0..63 is the dimmed 25% bank, 64..127 the full 100% bank [MM]. The SHIFT "
                    "bank (note 1C..23) and the MODE bank (note 00..07) are separate LEDs: the "
                    "firmware shows the SHIFT bank only while SHIFT is held and the MODE bank only "
                    "while MODE is held; writing to the base note has no effect on them [HW]. In "
                    "Hot Cue mode the pad shows the cue's color; unset pads are off."
                ),
                notes=f"Mode label under the pad: {mode}. {pm[2]}",
                extra={"pad_mode_label": mode},
            )
        )
    return out


# Physical items with no MIDI address at all. Drawn as gray outlines, kept out of
# layout.json on purpose so the checksum stays an honest MIDI double-entry.
HARDWARE = [
    (
        "11a",
        "FILTER ON/OFF LED",
        [box(262, 742, 14, 14), box(770, 742, 14, 14)],
        "Lit whenever the FILTER knob is off center, dark at 12 o'clock. Hardware-driven, no MIDI address [MM].",
    ),
    (
        "26",
        "HEADPHONES OUT",
        [box(698, 1660, 54, 54)],
        "3.5 mm TRS stereo headphone jack on the front edge; 4 +/- 1 dBu at 32 ohm [IM].",
    ),
    (
        "27",
        "MASTER OUT (RCA)",
        [box(378, 58, 100, 60)],
        "Unbalanced RCA L/R main output on the rear, 18 +/- 1 dBu [IM]. Follows the MONO/STEREO switch.",
    ),
    (
        "28",
        "USB-C PORT (device / charging icon)",
        [box(528, 88, 48, 30)],
        "USB-C with the tablet-and-bolt icon: the playback device (iPad, iPhone, Android, Mac, PC) connects here and is "
        "charged through it, up to 45 W power delivery [IM]. Also the bus-power input when no supply is attached [IM].",
    ),
    (
        "29",
        "USB-C PORT (20 W supply icon)",
        [box(603, 88, 48, 30)],
        "USB-C with the 20 W plug icon: power supply input, 5-20 V, minimum 20 W [IM]. The manual's prose numbers these "
        "two ports inconsistently against its own designation list; the icons are the reliable guide.",
    ),
    ("30", "POWER", [box(720, 72, 48, 48)], "Power on / off push button on the rear [IM]."),
    ("31", "KENSINGTON LOCK", [box(48, 372, 36, 50)], "Security slot on the left side panel [IM]."),
]

SOURCES = [
    (
        "IM",
        "Reloop Mixtour Pro operation manual REV 1.0, 246959_Reloop_IM.pdf, English pp. 3-7 "
        "(reloop.com/media/catalog/product/pdf/2/4/6/246959_Reloop_IM.pdf)",
    ),
    (
        "LBL",
        "Silkscreen labels on the device (secondary functions printed under each control; photographed Mon 21 Sep 2026)",
    ),
    (
        "LL",
        "Reloop 'Laidback Luke Signature Features' v1.0, Reloop-LAIDBACK_LUKE_SIGNATURE_FEATURES_V1.pdf "
        "(target of reloop.com/reloop-mixtour-pro-mapping)",
    ),
    (
        "MM",
        "Manufacturer MIDI map 'RELOOP MIXTOUR PRO MIDI MAP' + 'LED colour coding map', midimap_MIXTOURPRO.pdf, "
        "as redistributed in github.com/sayak-brm/ReloopMixxxtourPro (not hosted on reloop.com)",
    ),
    (
        "HW",
        "Hardware-verified corrections, MAPPING_NOTES.md and scratchpad.txt in github.com/sayak-brm/ReloopMixxxtourPro, "
        "commit 45ce7cda (Mon 24 Aug 2026), captured with mixxx --controller-debug and amidi on a real unit",
    ),
    ("FORUM", "community.algoriddim.com/t/reloop-mixtour-pro-full-midi-map/38993, post by Slak_Jaw"),
]


# Short labels drawn inside the hit target on a non-base layer (the card carries the full text).
SHORT: dict[tuple[str, str], str] = {
    ("browse_turn", "shift"): "SHIFT SCROLL",
    ("browse_turn", "mode"): "SEEK / LOOP OUT",
    ("browse_press", "shift"): "BACK",
    ("load", "shift"): "LIBRARY < >",
    ("load", "mode"): "DECK 3 / 4",
    ("split", "shift"): "NOT DOCUMENTED",
    ("mode", "shift"): "NOT DOCUMENTED",
    ("neural_mix", "shift"): "VIEW TOGGLE",
    ("channel_fader", "shift"): "FADER START",
    ("headphone_cue", "shift"): "CFX / CFX TEMPO",
    ("fx_on", "shift"): "BACKSPIN",
    ("fx_dry_wet", "mode"): "DECK FX PARAM",
    ("param_prev", "shift"): "NOT DOCUMENTED",
    ("param_prev", "mode"): "FX SELECT <",
    ("param_next", "shift"): "NOT DOCUMENTED",
    ("param_next", "mode"): "FX SELECT >",
    ("in_out_ex", "shift"): "AUTO LOOP",
    ("in_out_ex", "mode"): "LOOP HALF",
    ("in_out_ex", "shift_mode"): "LOOP IN / MOVE",
    ("sync", "shift"): "KEY MATCH",
    ("sync", "mode"): "LOOP DOUBLE",
    ("sync", "shift_mode"): "LOOP OUT / MOVE",
    ("cue", "shift"): "PITCH BEND -",
    ("cue", "mode"): "KEY -",
    ("cue", "shift_mode"): "TEMPO -0.1",
    ("play_pause", "shift"): "PITCH BEND +",
    ("play_pause", "mode"): "KEY +",
    ("play_pause", "shift_mode"): "TEMPO +0.1",
    ("crossfader", "shift"): "FADER START",
}


def short_label(c: C, layer: str) -> str:
    if layer == "base":
        return c.label
    if c.fig.startswith("pad_"):
        return "SHIFT FN" if layer == "shift" else str(c.extra.get("pad_mode_label", ""))
    return SHORT.get((c.fig, layer), c.label)


# ---------------------------------------------------------------- emitters


def placements(c: C) -> list[tuple[str, tuple[int, int, int, int]]]:
    """(side, box) per hit target. Side is explicit on the control, never inferred from box count."""
    if c.side == "both":
        assert len(c.geo) == 2, (c.fig, "mirrored control needs two boxes")
        return [("l", c.geo[0]), ("r", c.geo[1])]
    assert len(c.geo) == 1, (c.fig, "single-side control needs one box")
    return [({"left": "l", "right": "r", "center": ""}[c.side], c.geo[0])]


def side_deck(side: str) -> int:
    """Deck 1 or 2 for a physical side (before the 3/4 toggle)."""
    return 2 if side == "r" else 1


def pct(b: tuple[int, int, int, int]) -> dict:
    x, y, w, h = b
    return {
        "x": round(x / PLATE_W * 100, 2),
        "y": round(y / PLATE_H * 100, 2),
        "w": round(w / PLATE_W * 100, 2),
        "h": round(h / PLATE_H * 100, 2),
    }


def hx(n: int) -> str:
    return f"{n:02X}"


def midi_rows(cs: list[C]) -> list[dict]:
    rows = []
    for c in cs:
        for layer in LAYERS:
            f = c.layers.get(layer)
            if not f or not f.midi:
                continue
            m = f.midi
            name = c.fig + LAYER_SUFFIX[layer]
            row = {
                "name": name,
                "section": c.section,
                "type": m.type,
                "ch": chan(m.letter),
                "code": m.code,
                "hex": hx(m.code),
                "ch_key": m.letter,
                "layer": layer,
                "direction": m.direction,
                "scope": scope_of(c, m),
                "values_in": m.values_in,
                "values_out": m.values_out,
                "led_note": m.led,
                "designation": c.num,
                "notes": f"{LAYER_TITLE[layer]} of {c.label} [{c.fig}]" if layer != "base" else f"{c.label} [{c.fig}]",
                "function": f.text,
                "source": f.src,
            }
            rows.append(row)
    return rows


def build_midi(cs: list[C]) -> dict:
    return {
        "source": "midimap_MIXTOURPRO.pdf (manufacturer MIDI map, 2 pages) + hardware corrections; see sources",
        "archived": "(private path) (local only, gitignored)",
        "device": DEVICE,
        "vendor": VENDOR,
        "deck_channels": {
            "convention": "1-based MIDI channels. Rows below are deck 1 (N = ch 1, P = ch 5, E = ch 9) and global (G = ch 16). "
            "Deck d uses N = d, P = 4 + d, E = 8 + d. The right-hand side of the unit is deck 2 (or 4 after MODE+LOAD), "
            "the left-hand side deck 1 (or 3).",
            "keys": CHANNEL_KEY,
        },
        "led_coding": {
            "rgb_pads": "value = 64*full + 16*R + 4*G + B; R,G,B in 0..3 = 0/33/66/100%; 0..63 dimmed 25%, 64..127 full",
            "transport_biled": [{"value": hx(v), "meaning": t} for v, t in BILED],
            "single_led": "OFF = 00, DIM = 01, ON = 7F (NEURAL MIX, HEADPHONE CUE); DIM = 00, ON = 7F (MODE, SPLIT); "
            "FX ON: OFF = 00, DIM = 01, ON = 02, FLASH = 7F; VU: CC 1F value 00..06 segments",
        },
        "sources": [{"tag": t, "ref": r} for t, r in SOURCES],
        "controls": midi_rows(cs),
    }


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


# ---------------------------------------------------------------- HTML: shared


def page_data(cs: list[C]) -> dict:
    """Everything deck.html's JS needs, serialized once."""
    out = {
        "layers": LAYERS,
        "layerTitle": LAYER_TITLE,
        "controls": [],
        "hardware": [],
        "padModes": PAD_MODES,
        "biled": [[hx(v), t] for v, t in BILED],
        "sources": SOURCES,
        "channelKey": CHANNEL_KEY,
    }
    for c in cs:
        layers = {}
        for k, f in c.layers.items():
            m = f.midi
            layers[k] = {
                "text": f.text,
                "src": f.src,
                "short": short_label(c, k),
                "midi": None
                if not m
                else {
                    "letter": m.letter,
                    "code": m.code,
                    "hex": hx(m.code),
                    "type": m.type,
                    "scope": scope_of(c, m),
                    "led": None if m.led is None else hx(m.led),
                    "in": m.values_in,
                    "out": m.values_out,
                    "dir": m.direction,
                },
            }
        out["controls"].append(
            {
                "fig": c.fig,
                "num": c.num,
                "label": c.label,
                "kind": c.kind,
                "section": c.section,
                "deckScoped": c.deck_scoped,
                "side": c.side,
                "geo": [pct(b) for b in c.geo],
                "layers": layers,
                "led": c.led,
                "notes": c.notes,
                "padMode": c.extra.get("pad_mode_label"),
            }
        )
    for num, label, boxes, text in HARDWARE:
        out["hardware"].append({"num": num, "label": label, "geo": [pct(b) for b in boxes], "text": text})
    return out
