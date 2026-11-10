"""Build tools/deck-diagrams/devices/omnis-duo/panel.json, the OMNIS-DUO practice-replica panel layout.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the device's top panel (origin
top-left, panel = the published width x depth), a function label, a hover text and the wire
identity (channel, note|cc, number) the real unit sends.

Sources:
- Panel size 500 x 307 mm: OMNIS-DUO specifications, "Maximum dimensions (W x D x H)
  500 x 307 x 71 mm" (alphatheta.com product information).
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/alphatheta-omnis-duo.ts, itself
  transcribed from the official "OMNIS-DUO MIDI Message List" PDF E1 (fig numbers kept as `fig`).
- Positions: devices/omnis-duo/panel.reference.json, the measured trace of the top-panel photo on
  page 1 of that PDF (sha256 eb4c914d644be58fd3fd91555c1b5a5db027e8c70e534f9a6233ef375cd4b06a),
  normalised here for the renderer: row and column mates whose centres sit within ALIGN_MM are
  snapped onto one line, centres rounded to 0.5 mm, sizes to whole mm. Scored against the
  reference by scripts/skins/control_position_error.py (ADR-0137).

Run: uv run --locked python tools/deck-diagrams/scripts/build_omnis_duo_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PANEL_W_MM = 500.0
PANEL_D_MM = 307.0
ALIGN_MM = 1.5
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / "omnis-duo"
REFERENCE = DEVICE_DIR / "panel.reference.json"
OUT = DEVICE_DIR / "panel.json"

DECKS = (1, 2)
MIXER_CH = 5
BROWSER_CH = 1
BLUETOOTH_CH = 12
SLOTS = "ABCDEFGH"


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


Rect = tuple[float, float, float, float]

# ----------------------------------------------------------------- positions


def _pad_channel(deck: int) -> int:
    return 5 + deck


def _half_mm(v: float) -> float:
    return round(v * 2) / 2


def _snap_axis(values: dict[str, float]) -> dict[str, float]:
    """Snap values within ALIGN_MM of a group's first value onto the group mean (row/column mates).

    The bound is the group's spread, not the gap to the previous value, so near-equal rows
    cannot chain into one line (pads row 2, the crossfader and PLAY sit 1.4 mm apart in turn)."""
    order = sorted(values, key=values.__getitem__)
    out: dict[str, float] = {}
    group: list[str] = []
    for cid in order:
        if group and values[cid] - values[group[0]] > ALIGN_MM:
            mean = sum(values[g] for g in group) / len(group)
            out.update({g: mean for g in group})
            group = []
        group.append(cid)
    if group:
        mean = sum(values[g] for g in group) / len(group)
        out.update({g: mean for g in group})
    return out


def load_positions(reference: Path = REFERENCE) -> dict[str, Rect]:
    """Measured rectangles from the reference, normalised for the renderer."""
    doc = json.loads(reference.read_text(encoding="utf-8"))
    if doc.get("measured") is not True:
        raise SystemExit(f"[ERROR] {reference} is not a measurement (measured != true)")
    raw = {c["id"]: (float(c["x"]), float(c["y"]), float(c["w"]), float(c["h"])) for c in doc["controls"]}
    cx = _snap_axis({cid: x + w / 2 for cid, (x, y, w, h) in raw.items()})
    cy = _snap_axis({cid: y + h / 2 for cid, (x, y, w, h) in raw.items()})
    pos: dict[str, Rect] = {}
    for cid, (_x, _y, w, h) in raw.items():
        w_mm, h_mm = max(1.0, float(round(w))), max(1.0, float(round(h)))
        pos[cid] = (_half_mm(cx[cid] - w_mm / 2), _half_mm(cy[cid] - h_mm / 2), w_mm, h_mm)
    return pos


# ----------------------------------------------------------------- deck strips

# id suffix, fig, type, label, does, kind, number, shifted (kind, number) or None
_DECK_ROWS: list[tuple[str, str, str, str, str, str, int, tuple[str, int] | None]] = [
    ("beat-jump-rev", "1-1", "button", "BEAT JUMP <", "Jump back by the beat jump length", "note", 45, ("note", 74)),
    ("beat-jump-fwd", "1-2", "button", "BEAT JUMP >", "Jump forward by the beat jump length", "note", 44, ("note", 73)),
    (
        "beat-loop",
        "1-3",
        "encoder",
        "BEAT LOOP",
        "Turn to pick the loop length; press for a loop of that length, SHIFT + press to reloop",
        "cc",
        46,
        None,
    ),
    ("jog", "1-4", "jog", "JOG", "Touch the platter; turning it scrubs or bends the tempo", "note", 32, ("note", 72)),
    ("shift", "1-5", "button", "SHIFT", "Hold for each control's second function", "note", 63, None),
    ("cue", "1-6", "button", "CUE", "Set or return to the cue point", "note", 1, ("note", 5)),
    ("play-pause", "1-7", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0, None),
    ("tempo", "1-16", "fader", "TEMPO", "Tempo fader", "cc", 0, None),
]


def _deck(deck: int, pos: dict[str, Rect]) -> list[Control]:
    out: list[Control] = []
    for suffix, fig, typ, label, does, kind, num, shifted in _DECK_ROWS:
        cid = f"d{deck}-{suffix}"
        x, y, w, h = pos[cid]
        shift = Wire(deck, shifted[0], shifted[1]) if shifted else None
        out.append(
            Control(cid, fig, typ, label, f"{does} (deck {deck})", x, y, w, h, "deck", Wire(deck, kind, num), shift)
        )
    for pad in range(8):
        cid = f"d{deck}-pad-{pad + 1}"
        x, y, w, h = pos[cid]
        out.append(
            Control(
                cid,
                f"1-{8 + pad}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {SLOTS[pad]} (deck {deck}); SHIFT + press deletes it",
                x,
                y,
                w,
                h,
                "performance",
                Wire(_pad_channel(deck), "note", pad),
                Wire(_pad_channel(deck), "note", 8 + pad),
            )
        )
    return out


# ----------------------------------------------------------------- mixer channels

# id suffix, fig, type, label, does, CC for channel 1, CC for channel 2
_CHANNEL_ROWS: list[tuple[str, str, str, str, str, int, int]] = [
    ("trim", "3-1", "knob", "TRIM", "Channel gain", 1, 6),
    ("hi", "3-2", "knob", "HI", "EQ high band", 2, 7),
    ("mid", "3-2", "knob", "MID", "EQ mid band", 3, 8),
    ("low", "3-2", "knob", "LOW", "EQ low band", 4, 9),
    ("color", "2-4", "knob", "COLOR", "Color effect amount (filter)", 5, 10),
    ("ch-cue", "3-3", "button", "CH CUE", "Headphone cue for this channel (HEADPHONES 1/2)", 70, 71),
    ("ch-fader", "3-7", "fader", "CH FADER", "Channel volume", 17, 18),
]


def _channel(deck: int, pos: dict[str, Rect]) -> list[Control]:
    out: list[Control] = []
    for suffix, fig, typ, label, does, cc1, cc2 in _CHANNEL_ROWS:
        cid = f"ch{deck}-{suffix}"
        x, y, w, h = pos[cid]
        num = cc1 if deck == 1 else cc2
        out.append(
            Control(cid, fig, typ, label, f"{does} (channel {deck})", x, y, w, h, "mixer", Wire(MIXER_CH, "cc", num))
        )
    return out


# ----------------------------------------------------------------- globals

# id, fig, type, label, does, section, channel, kind, number, shifted wire or None
_GLOBAL_ROWS: list[tuple[str, str, str, str, str, str, int, str, int, Wire | None]] = [
    (
        "browse",
        "4-1",
        "encoder",
        "BROWSE",
        "Scroll the library; press to open",
        "browse",
        BROWSER_CH,
        "cc",
        79,
        Wire(BROWSER_CH, "cc", 96),
    ),
    ("load-1", "1-17", "button", "LOAD 1", "Load the selected track to deck 1", "browse", 1, "note", 81, None),
    ("load-2", "1-17", "button", "LOAD 2", "Load the selected track to deck 2", "browse", 2, "note", 81, None),
    ("booth", "3-9", "knob", "BOOTH", "Booth output level", "mixer", MIXER_CH, "cc", 25, None),
    ("master", "3-10", "knob", "MASTER", "Master output level", "mixer", MIXER_CH, "cc", 24, None),
    ("phones-mix", "3-4", "knob", "CUE/MASTER", "Headphone cue and master blend", "mixer", MIXER_CH, "cc", 27, None),
    ("phones-level", "3-5", "knob", "PHONES LEVEL", "Headphone level", "mixer", MIXER_CH, "cc", 26, None),
    ("crossfader", "3-6", "crossfader", "CROSSFADER", "Blend deck 1 and deck 2", "mixer", MIXER_CH, "cc", 11, None),
    ("mic1-level", "3-11", "knob", "MIC 1", "Microphone 1 level", "mixer", MIXER_CH, "cc", 88, None),
    ("mic2-level", "3-11", "knob", "MIC 2", "Microphone 2 level", "mixer", MIXER_CH, "cc", 89, None),
    ("mic-on", "3-12", "button", "MIC ON", "Microphones on (talkover)", "mixer", MIXER_CH, "cc", 90, None),
    (
        "beat-prev",
        "2-1",
        "button",
        "BEAT <",
        "Shorter effect beat",
        "effect",
        MIXER_CH,
        "cc",
        76,
        Wire(MIXER_CH, "cc", 120),
    ),
    (
        "beat-next",
        "2-1",
        "button",
        "BEAT >",
        "Longer effect beat",
        "effect",
        MIXER_CH,
        "cc",
        77,
        Wire(MIXER_CH, "cc", 121),
    ),
    ("level-depth", "2-2", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", MIXER_CH, "cc", 91, None),
    (
        "fx-on",
        "2-3",
        "button",
        "FX ON/OFF",
        "Beat effect on or off",
        "effect",
        MIXER_CH,
        "cc",
        114,
        Wire(MIXER_CH, "cc", 52),
    ),
    ("bt-pairing", "5-1", "button", "BT ON", "Bluetooth source on; hold to pair", "mixer", BLUETOOTH_CH, "cc", 0, None),
    (
        "bt-phones-cue",
        "5-2",
        "button",
        "BT CUE",
        "Hear the Bluetooth source in headphones",
        "mixer",
        BLUETOOTH_CH,
        "cc",
        1,
        None,
    ),
    ("bt-rev", "5-5", "button", "BT <<", "Bluetooth source: previous track", "mixer", BLUETOOTH_CH, "note", 2, None),
    (
        "bt-play-pause",
        "5-3",
        "button",
        "BT PLAY/PAUSE",
        "Bluetooth source: play or pause",
        "mixer",
        BLUETOOTH_CH,
        "note",
        0,
        None,
    ),
    ("bt-fwd", "5-4", "button", "BT >>", "Bluetooth source: next track", "mixer", BLUETOOTH_CH, "note", 1, None),
]


def _globals(pos: dict[str, Rect]) -> list[Control]:
    out: list[Control] = []
    for cid, fig, typ, label, does, section, ch, kind, num, shift in _GLOBAL_ROWS:
        x, y, w, h = pos[cid]
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(ch, kind, num), shift))
    return out


# ----------------------------------------------------------------- build


def build(reference: Path = REFERENCE) -> dict[str, object]:
    pos = load_positions(reference)
    controls = [*_deck(1, pos), *_deck(2, pos), *_channel(1, pos), *_channel(2, pos), *_globals(pos)]
    known = {c.id for c in controls}
    missing_pos = sorted(known - set(pos))
    extra_pos = sorted(set(pos) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] reference/builder mismatch: missing={missing_pos} extra={extra_pos}")
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the {PANEL_W_MM} x {PANEL_D_MM} mm panel")
    return {
        "format": "opendj-panel-layout/1",
        "device": "omnis-duo",
        "display_name": "2-deck portable all-in-one",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "OMNIS_DUO_MAP",
        "sources": {
            "panel_size": "OMNIS-DUO specifications, maximum dimensions 500 x 307 x 71 mm W x D x H (alphatheta.com)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/alphatheta-omnis-duo.ts (OMNIS-DUO MIDI Message List E1)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/omnis-duo/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            (
                "touch screen rows (JOG MODE, AUTO CUE, QUANTIZE, SLIP, MASTER TEMPO, SYNC, TEMPO RANGE, "
                "CUE/LOOP CALL, MEMORY/DELETE, NEEDLE SEARCH, DECK SELECT): on-screen, not panel controls; "
                "the map still binds SYNC and hints the rest"
            ),
            (
                "jog rotation (CC 34 top, CC 33 side) and BEAT LOOP press (notes 67, 68): bound in the map; "
                "the panel jog carries the touch wire and the panel encoder carries the turn wire"
            ),
            "MSB/LSB companion CC: TEMPO carries its MSB",
            "BACK button, power button, CH LEVEL METER: no MIDI input row",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    doc = build()
    OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
