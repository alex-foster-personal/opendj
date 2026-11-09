"""Build tools/deck-diagrams/devices/cdj-3000/panel.json and panel.reference.json (practice replica).

Panel layout format v1 ((private path)): one entry per
physical control on the CDJ-3000 top panel that the vendor MIDI list gives a wire, with its rectangle
in millimetres (origin top-left, panel = the published width x depth), a label, hover text and the
wire identity (channel, note|cc, number) the player sends. A CDJ-3000 is one player: the panel
carries the channel-1 wires (player number 1); players 2 to 4 are the same controls on channels 2 to 4
in CDJ3000_MAP.

Sources (all public, vendor-official, none redistributed here):
- Panel size 329 x 453 mm (W x D): CDJ-3000 Instruction Manual DRI1586A, Specifications, General
  ("Max. external dimensions 329 mm (W) x 453 mm (D) x 118 mm (H)").
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/pioneer-cdj-3000.ts, transcribed from the
  official "CDJ-3000 MIDI Message List" (E108). `fig` is that document's part number (1 to 40).
- Positions: traced from the top-panel photo on page 1 of that PDF (embedded image 1, 762 x 1014 px),
  read on a labelled 5 px grid at 5x zoom, with a second pass of pixel-profile reads (plateau walks on
  flat button bodies, saturation and brightness extents on lit caps and rings). Only numbers are
  committed; see docs/controller/research/CDJ-3000-MIDI-Message-List.md.

The pixel table `_PX` below is the measurement of record. This script writes BOTH files from it:
`panel.reference.json` carries the raw boxes converted to mm (0.1 mm), `panel.json` the same trace
normalised for the renderer (0.5 mm centres, whole-mm sizes). They are scored against each other by
scripts/skins/control_position_error.py, which is why they are never identical.

Run: uv run --locked python tools/deck-diagrams/scripts/build_cdj_3000_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PANEL_W_MM = 329.0
PANEL_D_MM = 453.0
DEVICE = "cdj-3000"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE

# Vendor image calibration: the unit spans these inclusive pixel columns (left to right body edge)
# and rows (rear face of the raised display housing, the unit's rearmost point, to the front edge).
# Check: the two scales agree to 0.2 percent, and the jog's outer rim (444 x 444 px) reads
# 220.7 x 220.3 mm, round.
IMAGE_PX = (762, 1014)
PANEL_PX = {"x0": 50, "x1": 711, "y0": 50, "y1": 962}
MM_PER_PX_X = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5)

TRACE = {
    "source": "CDJ-3000 MIDI Message List E108, page 1, embedded image 1 (top-panel photo, 762 x 1014 px)",
    "source_pdf_sha256": "44b7d91661fe4b219ab3ee05719ba7ccf4521b78aa7ee09358b4ec53de98c1aa",
    "source_image_sha256": "3d57ec5597930b60cc78e6c86ad1020946fc92dd5c823f94abe5386605a64828",
    "provenance": "docs/controller/research/CDJ-3000-MIDI-Message-List.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
    "method": (
        "Button: body bounding box (round buttons: outer edge of the button body, bezel housing "
        "excluded; CUE and PLAY/PAUSE: outer edge of the lit ring at half its saturation). Pad: pad body "
        "between its dark separator lines. Knob: visible cap. Encoder: outer knob face read as a circle, "
        "centre from its lit ring. Jog: outer platter rim (bright rim band on both axes). TEMPO: travel "
        "slot with cap width. DIRECTION: lever housing. JOG MODE: the button half of its housing, the "
        "VINYL/CDJ indicator excluded. Repeat pass: pixel-profile reads of 18 controls against the "
        "5x grid read; worst centre spread 5.8 px (PLAY/PAUSE ring, the grid read sat 5 px high)."
    ),
    "repeat_spread_mm": 2.9,
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
}


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


Box = tuple[int, int, int, int]  # inclusive x0, y0, x1, y1 on the vendor image

# ----------------------------------------------------------------- pixel trace

_PX: dict[str, Box] = {
    # left column, top to bottom
    "time-mode": (89, 277, 109, 297),
    "slip": (64, 318, 95, 334),
    "quantize": (102, 318, 133, 334),
    "hot-cue-call": (63, 376, 110, 402),
    "in-cue": (65, 420, 103, 458),
    "out": (116, 420, 154, 458),
    "reloop-exit": (133, 423, 166, 456),
    "4-beat": (74, 484, 95, 506),
    "8-beat": (125, 484, 146, 506),
    "beat-jump-rev": (70, 528, 100, 556),
    "beat-jump-fwd": (124, 528, 154, 556),
    "direction": (69, 588, 112, 631),
    "track-search-prev": (71, 668, 97, 694),
    "track-search-next": (111, 668, 137, 694),
    "search-rev": (71, 722, 97, 748),
    "search-fwd": (111, 722, 137, 748),
    "cue": (68, 779, 140, 850),
    "play-pause": (69, 871, 140, 942),
    # hot cue row (pitch 60 px) and the cue/loop memory row
    "pad-1": (149, 377, 191, 401),
    "pad-2": (209, 377, 251, 401),
    "pad-3": (269, 377, 311, 401),
    "pad-4": (329, 377, 371, 401),
    "pad-5": (389, 377, 431, 401),
    "pad-6": (449, 377, 491, 401),
    "pad-7": (509, 377, 551, 401),
    "pad-8": (569, 377, 611, 401),
    "call-prev": (465, 428, 484, 447),
    "call-next": (500, 428, 519, 447),
    "delete": (538, 426, 567, 455),
    "memory": (578, 428, 598, 447),
    # jog
    "jog": (159, 454, 602, 897),
    # right column, top to bottom
    "back": (610, 148, 637, 175),
    "tag-track": (643, 148, 675, 175),
    "browse": (606, 187, 680, 261),
    "track-filter": (610, 277, 637, 304),
    "touch-brake": (638, 374, 674, 410),
    "jog-mode": (655, 427, 688, 454),
    "beat-sync": (621, 497, 648, 527),
    "master": (660, 497, 688, 527),
    "key-sync": (637, 529, 676, 548),
    "tempo-range": (649, 590, 663, 604),
    "master-tempo": (649, 639, 663, 653),
    "tempo": (640, 693, 673, 897),
    "tempo-reset": (587, 792, 607, 812),
}


# ----------------------------------------------------------------- helpers


def _mm(box: Box) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    x = (x0 - PANEL_PX["x0"]) * MM_PER_PX_X
    y = (y0 - PANEL_PX["y0"]) * MM_PER_PX_Y
    w = (x1 - x0 + 1) * MM_PER_PX_X
    h = (y1 - y0 + 1) * MM_PER_PX_Y
    return round(x, 1), round(y, 1), round(w, 1), round(h, 1)


def _normalised(box: Box) -> tuple[float, float, float, float]:
    """Renderer form: centre on a 0.5 mm grid, whole-mm sizes (at least 1 mm)."""
    x, y, w, h = _mm(box)
    cx, cy = round((x + w / 2) * 2) / 2, round((y + h / 2) * 2) / 2
    w_n, h_n = max(1.0, float(round(w))), max(1.0, float(round(h)))
    return round(cx - w_n / 2, 1), round(cy - h_n / 2, 1), w_n, h_n


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _normalised(_PX[cid])
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _PX entry for {cid!r}") from e


# ----------------------------------------------------------------- control rows

CH = 1  # player number 1

# id, fig, type, label, does, section, kind, number
Row = tuple[str, str, str, str, str, str, str, int]

_ROWS: list[Row] = [
    (
        "time-mode",
        "13",
        "button",
        "TIME MODE/AUTO CUE",
        "Elapsed or remaining time (hold: auto cue)",
        "deck",
        "note",
        8,
    ),
    ("slip", "12", "button", "SLIP", "Slip mode on or off", "deck", "note", 7),
    ("quantize", "11", "button", "QUANTIZE", "Quantize on or off", "deck", "note", 6),
    ("hot-cue-call", "30", "button", "CALL/DELETE", "Hot cue call or delete mode", "deck", "note", 26),
    ("in-cue", "32", "button", "IN/CUE", "Set the loop in point (hold: adjust it)", "deck", "note", 35),
    ("out", "32", "button", "OUT", "Set the loop out point (hold: adjust it)", "deck", "note", 36),
    ("reloop-exit", "33", "button", "RELOOP/EXIT", "Exit or re-enter the loop", "deck", "note", 37),
    ("4-beat", "34", "button", "4 BEAT LOOP", "Four-beat loop (in a loop: halve it)", "deck", "note", 44),
    ("8-beat", "34", "button", "8 BEAT LOOP", "Eight-beat loop (in a loop: double it)", "deck", "note", 45),
    ("beat-jump-rev", "35", "button", "BEAT JUMP <", "Jump back by the beat jump size", "deck", "note", 55),
    ("beat-jump-fwd", "35", "button", "BEAT JUMP >", "Jump forward by the beat jump size", "deck", "note", 54),
    ("direction", "29", "button", "DIRECTION", "FWD, REV or SLIP REV playback lever (REV wire)", "deck", "note", 25),
    ("track-search-prev", "10", "button", "TRACK SEARCH <", "Previous track", "deck", "note", 5),
    ("track-search-next", "10", "button", "TRACK SEARCH >", "Next track", "deck", "note", 4),
    ("search-rev", "9", "button", "SEARCH <", "Fast reverse", "deck", "note", 3),
    ("search-fwd", "9", "button", "SEARCH >", "Fast forward", "deck", "note", 2),
    ("cue", "8", "button", "CUE", "Set or return to the cue point", "deck", "note", 1),
    ("play-pause", "7", "button", "PLAY/PAUSE", "Start or pause the player", "deck", "note", 0),
    ("pad-1", "31", "pad", "HOT CUE A", "Hot cue A", "performance", "note", 27),
    ("pad-2", "31", "pad", "HOT CUE B", "Hot cue B", "performance", "note", 28),
    ("pad-3", "31", "pad", "HOT CUE C", "Hot cue C", "performance", "note", 29),
    ("pad-4", "31", "pad", "HOT CUE D", "Hot cue D", "performance", "note", 30),
    ("pad-5", "31", "pad", "HOT CUE E", "Hot cue E", "performance", "note", 31),
    ("pad-6", "31", "pad", "HOT CUE F", "Hot cue F", "performance", "note", 32),
    ("pad-7", "31", "pad", "HOT CUE G", "Hot cue G", "performance", "note", 33),
    ("pad-8", "31", "pad", "HOT CUE H", "Hot cue H", "performance", "note", 34),
    ("call-prev", "19", "button", "CALL <", "Call the previous cue or loop", "deck", "note", 15),
    ("call-next", "19", "button", "CALL >", "Call the next cue or loop", "deck", "note", 14),
    ("delete", "20", "button", "DELETE", "Delete the called cue or loop", "deck", "note", 16),
    ("memory", "18", "button", "MEMORY", "Store the cue or loop", "deck", "note", 13),
    (
        "jog",
        "1",
        "jog",
        "JOG",
        "Scratch, nudge or search the track (platter top; the outer ring is CC 48)",
        "deck",
        "cc",
        16,
    ),
    ("back", "22", "button", "BACK", "Back one browse level", "browse", "note", 18),
    ("tag-track", "23", "button", "TAG TRACK/REMOVE", "Tag or untag the selected track", "browse", "note", 19),
    (
        "browse",
        "6",
        "encoder",
        "ROTARY SELECTOR",
        "Scroll the library (press loads the track, note 17)",
        "browse",
        "cc",
        79,
    ),
    ("track-filter", "24", "button", "TRACK FILTER/EDIT", "Track filter on or off", "browse", "note", 20),
    ("touch-brake", "4", "knob", "VINYL SPEED ADJ.", "Vinyl mode touch and brake speed", "deck", "cc", 30),
    ("jog-mode", "17", "button", "JOG MODE", "Vinyl or CDJ jog mode", "deck", "note", 12),
    ("beat-sync", "26", "button", "BEAT SYNC", "Beat sync to the master player", "deck", "note", 22),
    ("master", "25", "button", "MASTER", "Make this player the sync master", "deck", "note", 21),
    ("key-sync", "37", "button", "KEY SYNC", "Match the key to the master player", "deck", "note", 72),
    ("tempo-range", "15", "button", "TEMPO RANGE", "Tempo slider range (6, 10, 16, WIDE)", "deck", "note", 10),
    ("master-tempo", "16", "button", "MASTER TEMPO", "Key lock on or off", "deck", "note", 11),
    ("tempo", "3", "fader", "TEMPO", "Tempo slider", "deck", "cc", 29),
    ("tempo-reset", "14", "button", "TEMPO RESET", "Return the tempo to zero", "deck", "note", 9),
]


def controls() -> list[Control]:
    out: list[Control] = []
    for cid, fig, typ, label, does, section, kind, num in _ROWS:
        x, y, w, h = _at(cid)
        out.append(Control(cid, fig, typ, label, does, x, y, w, h, section, Wire(CH, kind, num)))
    ids = [c.id for c in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    missing = sorted(set(_PX) - set(ids))
    if missing:
        raise SystemExit(f"[ERROR] traced boxes with no control row: {missing}")
    for c in out:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel")
    return out


# ----------------------------------------------------------------- documents


def build() -> dict[str, object]:
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "Professional DJ multi player",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "CDJ3000_MAP",
        "sources": {
            "panel_size": "CDJ-3000 Instruction Manual DRI1586A, Specifications, General: 329 mm (W) x 453 mm (D) x 118 mm (H)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/pioneer-cdj-3000.ts (CDJ-3000 MIDI Message List E108)",
            "positions": (
                "measured trace of the official top-panel photo, normalised for the renderer; "
                "reference: devices/cdj-3000/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "one player: the panel carries player 1 (channel 1) wires; players 2 to 4 are channels 2 to 4 in the map",
            "no SHIFT layer: a CDJ has no shift button, so shift_midi is null everywhere",
            "touch-screen controls (Needle Position, beat loop and beat jump sizes, KEY SHIFT, KEY RESET) and the jog display",
            "controls the list gives no wire (SOURCE, BROWSE, TAG LIST, PLAYLIST, SEARCH, MENU/UTILITY, SHORTCUT, USB STOP, JOG ADJUST)",
            "the DIRECTION lever carries its REV wire (note 25); SLIP REV (note 24) is a map hint",
            "the jog carries the platter-top wire (CC 16); the outer JOG RING (CC 48) is bound in the map but not placed",
            "the ROTARY SELECTOR carries its rotation wire (CC 79); its press (note 17, load) is bound in the map but not placed",
            "the VINYL/CDJ indicator half of the JOG MODE housing and the TEMPO RESET indicator",
        ],
        "controls": [asdict(c) for c in controls()],
    }


def build_reference() -> dict[str, object]:
    rows = []
    for c in controls():
        box = _PX[c.id]
        x, y, w, h = _mm(box)
        rows.append({"id": c.id, "px": list(box), "x": x, "y": y, "w": w, "h": h})
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": dict(TRACE),
        "controls": rows,
    }


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = build()
    ref = build_reference()
    (OUT_DIR / "panel.json").write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    (OUT_DIR / "panel.reference.json").write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_DIR / 'panel.json'} and panel.reference.json ({len(panel['controls'])} controls)")  # type: ignore[arg-type]
