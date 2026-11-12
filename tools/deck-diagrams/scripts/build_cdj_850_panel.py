"""Build tools/deck-diagrams/devices/cdj-850/panel.json and panel.reference.json (practice replica).

Panel layout format v1 ((private path)): one entry per
physical control on the CDJ-850 top panel that the vendor MIDI list gives a wire, in millimetres,
with wire identities from apps/webui/frontend/src/lib/rb/midi/maps/cdj-850.ts. One player: channel 1.

Sources (public manuals, no vendor art committed):
- Panel size 305 x 364.4 mm (W x D): CDJ-850 Operating Instructions DRB1646A, Specifications.
- Wire chart: same manual, List of MIDI Messages (printed p26 to p27); transcribed in cdj-850.ts.
- Positions: traced from the Part names and functions drawing (printed p12), read on a 200 dpi render
  of that page; a second pass of local box reads on the flat button bodies refined centres.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

PANEL_W_MM = 305.0
PANEL_D_MM = 364.4
DEVICE = "cdj-850"
OUT_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE

# Vendor drawing calibration (200 dpi render of manual page 12, control panel crop).
IMAGE_PX = (1440, 1060)
PANEL_PX = {"x0": 109, "x1": 919, "y0": 109, "y1": 979}
MM_PER_PX_X = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
MM_PER_PX_Y = round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5)

TRACE = {
    "source": "CDJ-850 Operating Instructions DRB1646A, Part names and functions (printed p12)",
    "source_pdf_sha256": "926a684d440dbc803aa0d088164848b121ac41ffdbe4178190b6a0712c31438a",
    "source_image_sha256": "9a88687e8b5468a8586763599dd684a50d380ee46fa102f7da27687c47b951b6",
    "provenance": "200 dpi render of printed p12, control-panel crop; clean-room trace",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX_X, "y": MM_PER_PX_Y},
    "method": (
        "Button: outer edge of the button body on the line drawing. Jog: outer platter rim. "
        "Tempo: travel slot with cap width. Encoder: outer knob face. Repeat pass: local "
        "threshold reads on 18 controls; worst centre spread 3.2 px (PLAY/PAUSE ring)."
    ),
    "repeat_spread_mm": 1.6,
    "issue": "#10891",
    "date": "Thu 12 Nov 2026",
}


@dataclass(frozen=True)
class Wire:
    ch: int
    kind: str
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


Box = tuple[int, int, int, int]

_PX: dict[str, Box] = {
    "play-pause": (160, 835, 265, 935),
    "cue": (160, 698, 265, 798),
    "search-rev": (132, 608, 168, 642),
    "search-fwd": (210, 608, 246, 642),
    "track-search-prev": (132, 558, 168, 592),
    "track-search-next": (210, 558, 246, 592),
    "direction-rev": (188, 528, 242, 568),
    "reloop-exit": (378, 488, 438, 548),
    "loop-out": (308, 488, 368, 548),
    "loop-in": (248, 488, 308, 548),
    "beat-select": (132, 498, 178, 538),
    "beat-loop-1": (132, 338, 178, 378),
    "beat-loop-2": (132, 378, 178, 418),
    "beat-loop-4": (132, 418, 178, 458),
    "beat-loop-8": (132, 458, 178, 498),
    "time-mode": (308, 232, 378, 278),
    "browse": (438, 132, 528, 168),
    "tag-list": (538, 132, 618, 168),
    "info": (633, 132, 708, 168),
    "menu": (735, 132, 815, 168),
    "back": (808, 232, 868, 272),
    "tag-track": (808, 288, 868, 328),
    "browse-encoder": (678, 292, 768, 382),
    "disc-eject": (832, 168, 888, 212),
    "call-prev": (552, 488, 608, 548),
    "call-next": (612, 488, 668, 548),
    "delete": (672, 488, 728, 548),
    "memory": (732, 488, 788, 548),
    "vinyl-speed": (808, 548, 858, 608),
    "jog-mode": (808, 618, 868, 658),
    "tempo-range": (828, 678, 878, 712),
    "master-tempo": (828, 728, 878, 762),
    "tempo": (772, 372, 832, 918),
    "jog": (248, 548, 748, 902),
}


def _mm(box: Box) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = box
    x = (x0 - PANEL_PX["x0"]) * MM_PER_PX_X
    y = (y0 - PANEL_PX["y0"]) * MM_PER_PX_Y
    w = (x1 - x0 + 1) * MM_PER_PX_X
    h = (y1 - y0 + 1) * MM_PER_PX_Y
    return round(x, 1), round(y, 1), round(w, 1), round(h, 1)


def _normalised(box: Box) -> tuple[float, float, float, float]:
    x, y, w, h = _mm(box)
    cx = round((x + w / 2) * 2) / 2
    cy = round((y + h / 2) * 2) / 2
    w_n, h_n = max(1.0, float(round(w))), max(1.0, float(round(h)))
    return round(cx - w_n / 2, 1), round(cy - h_n / 2, 1), w_n, h_n


def _at(cid: str) -> tuple[float, float, float, float]:
    try:
        return _normalised(_PX[cid])
    except KeyError as e:
        raise SystemExit(f"[ERROR] missing _PX entry for {cid!r}") from e


CH = 1

Row = tuple[str, str, str, str, str, str, str, int]

_ROWS: list[Row] = [
    ("play-pause", "1", "button", "PLAY/PAUSE", "Start or pause playback", "deck", "note", 0),
    ("cue", "2", "button", "CUE", "Set or return to the cue point", "deck", "note", 1),
    ("search-fwd", "3", "button", "SEARCH >", "Fast forward", "deck", "note", 2),
    ("search-rev", "3", "button", "SEARCH <", "Fast reverse", "deck", "note", 3),
    ("track-search-next", "4", "button", "TRACK SEARCH >", "Next track", "deck", "note", 4),
    ("track-search-prev", "4", "button", "TRACK SEARCH <", "Previous track", "deck", "note", 5),
    ("direction-rev", "5", "button", "DIRECTION REV", "Reverse playback direction", "deck", "note", 33),
    ("reloop-exit", "6", "button", "RELOOP/EXIT", "Exit or re-enter the loop", "deck", "note", 8),
    ("loop-out", "7", "button", "LOOP OUT", "Set the loop out point (hold: adjust)", "deck", "note", 7),
    ("loop-in", "8", "button", "LOOP IN/CUE", "Set the loop in point (hold: adjust)", "deck", "note", 6),
    ("beat-select", "9", "button", "BEAT SELECT", "Beat grid for auto loops", "deck", "note", 45),
    ("beat-loop-1", "10", "button", "AUTO BEAT LOOP 1", "One-beat auto loop", "deck", "note", 40),
    ("beat-loop-2", "10", "button", "AUTO BEAT LOOP 2", "Two-beat auto loop", "deck", "note", 41),
    ("beat-loop-4", "10", "button", "AUTO BEAT LOOP 4", "Four-beat auto loop", "deck", "note", 42),
    ("beat-loop-8", "10", "button", "AUTO BEAT LOOP 8", "Eight-beat auto loop", "deck", "note", 43),
    (
        "time-mode",
        "17",
        "button",
        "TIME MODE/AUTO CUE",
        "Elapsed or remaining time (hold: auto cue)",
        "deck",
        "note",
        14,
    ),
    ("browse", "18", "button", "BROWSE", "Enter the browse screen", "browse", "note", 54),
    ("tag-list", "19", "button", "TAG LIST", "Open the tag list", "browse", "note", 55),
    ("info", "20", "button", "INFO", "Show track information", "browse", "note", 56),
    ("menu", "21", "button", "MENU/UTILITY", "Utility and settings", "browse", "note", 63),
    ("back", "22", "button", "BACK", "Back one browse level", "browse", "note", 50),
    ("tag-track", "23", "button", "TAG TRACK/REMOVE", "Tag or untag the selected track", "browse", "note", 48),
    (
        "browse-encoder",
        "24",
        "encoder",
        "SELECT PUSH",
        "Scroll the library (press loads the track, note 51)",
        "browse",
        "cc",
        79,
    ),
    ("disc-eject", "27", "button", "DISC EJECT", "Eject the disc", "deck", "note", 47),
    ("call-prev", "28", "button", "CALL <", "Call the previous cue or loop", "deck", "note", 12),
    ("call-next", "28", "button", "CALL >", "Call the next cue or loop", "deck", "note", 11),
    ("delete", "29", "button", "DELETE", "Delete the called cue or loop", "deck", "note", 13),
    ("memory", "30", "button", "MEMORY", "Store the cue or loop", "deck", "note", 10),
    ("vinyl-speed", "31", "knob", "VINYL SPEED ADJ.", "Vinyl mode touch and brake speed", "deck", "cc", 30),
    ("jog-mode", "32", "button", "JOG MODE VINYL", "Vinyl or CDJ jog mode", "deck", "note", 18),
    ("tempo-range", "33", "button", "TEMPO RANGE", "Tempo slider range (6, 10, 16, WIDE)", "deck", "note", 16),
    ("master-tempo", "34", "button", "MASTER TEMPO", "Key lock on or off", "deck", "note", 17),
    ("tempo", "35", "fader", "TEMPO", "Tempo slider", "deck", "cc", 29),
    (
        "jog",
        "36",
        "jog",
        "JOG",
        "Scratch, nudge or search the track (platter top; outer ring is CC 48)",
        "deck",
        "cc",
        16,
    ),
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


def build() -> dict[str, object]:
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "Professional DJ media player",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "CDJ850_MAP",
        "sources": {
            "panel_size": (
                "CDJ-850 Operating Instructions DRB1646A, Specifications: 305 mm (W) x 364.4 mm (D) x 105.5 mm (H)"
            ),
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/cdj-850.ts (List of MIDI Messages, p26 to p27)",
            "positions": (
                "measured trace of the Part names drawing (printed p12); "
                "reference: devices/cdj-850/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "one player: channel 1 wires; players 2 to 4 are channels 2 to 4 in CDJ850_MAP",
            "no SHIFT layer on this player",
            "SOURCE, USB STOP, PC/DISC/USB source selectors and the LCD (no chart wire)",
            "SELECT PUSH press (note 51, browse load) and jog outer ring (CC 48) are bound but not placed",
            "power, USB port flap, standby and link indicators",
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
    print(f"[OK] wrote {OUT_DIR / 'panel.json'} and panel.reference.json ({len(panel['controls'])} controls)")
