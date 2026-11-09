"""Build the DDJ-1000 practice-replica layout pair: panel.reference.json (the trace) and panel.json.

Panel layout format v1 ((private path)): every
control carries an id, a type, a rectangle in millimetres on the top panel (origin top-left,
panel = the published width x depth), a function label, a hover text and the wire identity
(channel, note|cc, number) the real unit sends.

Sources (all official, none redistributed here):
- Panel size 708.0 x 361.4 mm: DDJ-1000 Quick Start Guide (AlphaTheta DRH1656B, page 15,
  Specifications: "708 mm (width) x 73.4 mm (height) x 361.4 mm (depth)"; height not modelled).
  sha256 of that PDF is recorded in docs/controller/research/DDJ-1000-MIDI-Message-List.md.
- Wire identities: apps/webui/frontend/src/lib/rb/midi/maps/ddj-1000.ts, transcribed from the
  official "DDJ-1000 List of MIDI message" PDF v1.00 E1 (fig ids kept as `fig`).
- Positions: the top-panel figure on page 1 of that PDF (the controller artwork is a raster
  sliced into 1 px strips; the page was rendered at 600 dpi and the strips read in place).
  `_PX` below is the traced inclusive pixel box of each control on that render: the left deck,
  mixer and effect controls read by eye on gridded zoom crops, the eight pads per deck by the
  saturated-colour detector, and each right-deck control placed by translating its left twin
  by the vendor's own leader-line anchor offset (the sheet marks every control with a leader
  dot), then checked on an overlay. The plate is calibrated on the drawn body edges with
  separate x and y scales because the artwork is about 8 percent squashed vertically (the
  206 mm jog measures 215 mm wide by 217 mm tall after calibration, so both scales agree
  to within 1 percent).

panel.reference.json is the measurement of record (raw mm from px). panel.json is the same
trace normalised for the renderer (0.5 mm centres, whole-mm sizes). Scored by
scripts/skins/control_position_error.py (ADR-0137).

Run: uv run --locked python tools/deck-diagrams/scripts/build_ddj1000_panel.py
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 708.0
PANEL_D_MM = 361.4
DEVICE = "ddj-1000"
DEVICES_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
OUT_PANEL = DEVICES_DIR / "panel.json"
OUT_REFERENCE = DEVICES_DIR / "panel.reference.json"

# Calibration of the 600 dpi page-1 render (4961 x 7017 px). The body of the controller spans
# columns 364..4458 and rows 924..2845 (50 percent-dark threshold on the luma profile).
IMAGE_PX = (4961, 7017)
PANEL_PX = {"x0": 364, "x1": 4458, "y0": 924, "y1": 2845}
MM_PER_PX = {
    "x": round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5),
    "y": round(PANEL_D_MM / (PANEL_PX["y1"] - PANEL_PX["y0"] + 1), 5),
}

TRACE = {
    "source": (
        "DDJ-1000 List of MIDI message v1.00 E1, page 1 top-panel figure (raster artwork sliced into "
        "1 px strips), rendered with pdftoppm at 600 dpi (4961 x 7017 px)"
    ),
    "source_pdf_sha256": "28d68d514774daa81e603d9a8709105283f38fbc944650671971ecf57cb95d11",
    "source_image_sha256": "105b39b792808cacc8e84dc4af70eb90ef5bcb3c96e20935a9069e9062622fb8",
    "provenance": "docs/controller/research/DDJ-1000-MIDI-Message-List.md",
    "image_px": list(IMAGE_PX),
    "panel_px": PANEL_PX,
    "mm_per_px": MM_PER_PX,
    "method": (
        "Plate: body edges on the render (white page to dark body), x and y scales separate. "
        "Button: body bounding box including its lit bezel. Knob and encoder: visible cap. Pad: the "
        "coloured square. Jog: outer platter ring. Vertical faders: travel slot with cap width. "
        "Crossfader: travel slot with cap height. Left deck, mixer and effect controls read by eye on "
        "20 px gridded zoom crops; pads by a saturation threshold; right deck by translating each left "
        "control by its leader-dot anchor offset, checked on an overlay."
    ),
    "repeat_spread_mm": 1.9,
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
}

# Traced inclusive pixel boxes [x0, y0, x1, y1] on the render, keyed by the sheet's fig id.
_PX: dict[str, tuple[int, int, int, int]] = {
    "B1-L": (1560, 967, 1700, 1107),
    "B1-R": (4229, 967, 4369, 1107),
    "B2-L": (1527, 1167, 1617, 1207),
    "B2-R": (4196, 1167, 4286, 1207),
    "B3-L": (1637, 1167, 1727, 1207),
    "B3-R": (4306, 1167, 4396, 1207),
    "D1-L": (441, 2592, 581, 2732),
    "D1-R": (3111, 2592, 3251, 2732),
    "D2-L": (442, 2369, 580, 2507),
    "D2-R": (3112, 2369, 3250, 2507),
    "D3-L": (457, 1108, 1702, 2262),
    "D3-R": (3126, 1108, 4371, 2262),
    "D4-L": (1604, 2146, 1710, 2725),
    "D4-R": (4274, 2146, 4380, 2725),
    "D5-L": (1480, 2333, 1560, 2377),
    "D5-R": (4149, 2333, 4229, 2377),
    "D6-L": (424, 1300, 514, 1390),
    "D6-R": (3094, 1300, 3184, 1390),
    "D7-L": (1670, 1999, 1730, 2059),
    "D7-R": (4339, 1999, 4399, 2059),
    "D8-L": (1480, 2533, 1557, 2580),
    "D8-R": (4149, 2533, 4226, 2580),
    "D9-L": (1480, 2643, 1557, 2687),
    "D9-R": (4149, 2643, 4226, 2687),
    "D10-L": (415, 983, 521, 1089),
    "D10-R": (3085, 983, 3191, 1089),
    "D11-L": (564, 982, 670, 1088),
    "D11-R": (3234, 982, 3340, 1088),
    "D12-L": (710, 1015, 907, 1062),
    "D12-R": (3379, 1015, 3576, 1062),
    "D13-L": (1256, 1010, 1344, 1062),
    "D13-R": (3925, 1010, 4013, 1062),
    "D14-L": (1376, 1010, 1464, 1062),
    "D14-R": (4045, 1010, 4133, 1062),
    "D15-L": (426, 1170, 558, 1200),
    "D15-R": (3096, 1170, 3228, 1200),
    "D16-L": (465, 2194, 537, 2266),
    "D16-R": (3135, 2194, 3207, 2266),
    "D17-L": (552, 2194, 624, 2266),
    "D17-R": (3222, 2194, 3294, 2266),
    "D18-L": (452, 2097, 545, 2135),
    "D18-R": (3122, 2097, 3214, 2135),
    "D19-L": (426, 2006, 476, 2056),
    "D19-R": (3096, 2006, 3146, 2056),
    "F1-1": (2274, 1817, 2344, 1887),
    "F1-2": (2482, 1817, 2552, 1887),
    "F1-3": (2067, 1817, 2137, 1887),
    "F1-4": (2690, 1817, 2760, 1887),
    "F2": (1820, 1774, 1876, 1800),
    "F3": (1910, 1774, 1968, 1800),
    "F4": (1820, 1874, 1876, 1900),
    "F5": (1910, 1874, 1968, 1900),
    "F6": (2864, 2004, 2912, 2039),
    "F7": (2939, 2004, 2987, 2039),
    "F8": (2888, 2116, 2972, 2200),
    "F9": (2887, 2287, 2975, 2375),
    "F10": (2885, 2460, 2977, 2552),
    "F11": (2869, 2680, 2997, 2808),
    "M1": (2252, 2682, 2572, 2795),
    "M2-1": (2270, 2127, 2370, 2417),
    "M2-2": (2478, 2127, 2578, 2417),
    "M2-3": (2062, 2127, 2162, 2417),
    "M2-4": (2685, 2127, 2785, 2417),
    "M3-1": (2262, 1155, 2360, 1253),
    "M3-2": (2470, 1155, 2568, 1253),
    "M3-3": (2055, 1155, 2153, 1253),
    "M3-4": (2677, 1155, 2775, 1253),
    "M4-1": (2262, 1312, 2360, 1410),
    "M4-2": (2470, 1312, 2568, 1410),
    "M4-3": (2055, 1312, 2153, 1410),
    "M4-4": (2677, 1312, 2775, 1410),
    "M5-1": (2262, 1470, 2360, 1568),
    "M5-2": (2470, 1470, 2568, 1568),
    "M5-3": (2055, 1470, 2153, 1568),
    "M5-4": (2677, 1470, 2775, 1568),
    "M6-1": (2262, 1629, 2360, 1727),
    "M6-2": (2470, 1629, 2568, 1727),
    "M6-3": (2055, 1629, 2153, 1727),
    "M6-4": (2677, 1629, 2775, 1727),
    "M7-1": (2270, 2012, 2373, 2048),
    "M7-2": (2478, 2012, 2580, 2048),
    "M7-3": (2062, 2012, 2166, 2048),
    "M7-4": (2685, 2012, 2788, 2048),
    "M8": (2887, 1018, 2987, 1118),
    "M9": (2863, 1747, 3005, 1785),
    "M10": (2888, 1608, 2982, 1702),
    "M12": (1833, 2289, 1925, 2381),
    "M13": (1833, 2470, 1925, 2562),
    "M14": (1822, 1461, 1918, 1557),
    "M15": (1822, 1620, 1918, 1716),
    "M19": (1817, 2006, 1925, 2040),
    "M20": (1833, 2104, 1925, 2196),
    "P1-L": (722, 2449, 876, 2593),
    "P1-R": (3388, 2449, 3542, 2593),
    "P2-L": (907, 2449, 1061, 2593),
    "P2-R": (3573, 2449, 3726, 2593),
    "P3-L": (1092, 2449, 1246, 2593),
    "P3-R": (3757, 2449, 3912, 2593),
    "P4-L": (1277, 2449, 1431, 2593),
    "P4-R": (3943, 2449, 4097, 2593),
    "P5-L": (722, 2623, 876, 2767),
    "P5-R": (3388, 2623, 3542, 2767),
    "P6-L": (906, 2623, 1061, 2767),
    "P6-R": (3572, 2623, 3727, 2767),
    "P7-L": (1092, 2623, 1246, 2767),
    "P7-R": (3757, 2623, 3912, 2767),
    "P8-L": (1276, 2623, 1431, 2767),
    "P8-R": (3942, 2623, 4097, 2767),
    "P9-L": (727, 2342, 827, 2382),
    "P9-R": (3396, 2342, 3496, 2382),
    "P10-L": (866, 2342, 966, 2382),
    "P10-R": (3535, 2342, 3635, 2382),
    "P11-L": (1004, 2342, 1104, 2382),
    "P11-R": (3674, 2342, 3774, 2382),
    "P12-L": (1144, 2342, 1244, 2382),
    "P12-R": (3813, 2342, 3913, 2382),
    "P13-L": (1288, 2342, 1348, 2382),
    "P13-R": (3957, 2342, 4017, 2382),
    "P14-L": (1370, 2342, 1430, 2382),
    "P14-R": (4039, 2342, 4099, 2382),
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


# ----------------------------------------------------------------- geometry


def _mm_box(fig: str) -> tuple[float, float, float, float]:
    """Raw millimetres of a traced box: the reference row."""
    try:
        x0, y0, x1, y1 = _PX[fig]
    except KeyError as e:
        raise SystemExit(f"[ERROR] no traced box for fig {fig!r}") from e
    sx, sy = MM_PER_PX["x"], MM_PER_PX["y"]
    x = (x0 - PANEL_PX["x0"]) * sx
    y = (y0 - PANEL_PX["y0"]) * sy
    return round(x, 1), round(y, 1), round((x1 - x0 + 1) * sx, 1), round((y1 - y0 + 1) * sy, 1)


def _normalised(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Renderer form of the same box: centre on a 0.5 mm grid, whole-mm sizes (at least 1 mm)."""
    x, y, w, h = box
    wn, hn = max(1.0, float(round(w))), max(1.0, float(round(h)))
    cx, cy = round((x + w / 2) * 2) / 2, round((y + h / 2) * 2) / 2
    return round(cx - wn / 2, 1), round(cy - hn / 2, 1), wn, hn


# ----------------------------------------------------------------- control rows

# (fig, id tail, type, label, does, kind, number, shift kind, shift number)
DeckRow = tuple[str, str, str, str, str, str, int, str | None, int | None]
_DECK_ROWS: list[DeckRow] = [
    ("D1", "play-pause", "button", "PLAY/PAUSE", "Start or pause the deck", "note", 0x0B, "note", 0x47),
    ("D2", "cue", "button", "CUE", "Set or return to the cue point", "note", 0x0C, "note", 0x48),
    ("D3", "jog", "jog", "JOG", "Scratch or nudge the track", "cc", 0x22, "cc", 0x1F),
    ("D4", "tempo", "fader", "TEMPO", "Tempo fader", "cc", 0x00, "cc", 0x05),
    (
        "D5",
        "master-tempo",
        "button",
        "MASTER TEMPO",
        "Lock the key while the tempo changes",
        "note",
        0x1A,
        "note",
        0x60,
    ),
    ("D7", "beat-sync", "button", "BEAT SYNC", "Match tempo to the master deck", "note", 0x58, "note", 0x5C),
    ("D8", "key-sync", "button", "KEY SYNC", "Match key to the master deck", "note", 0x65, "note", 0x1C),
    ("D9", "key-reset", "button", "KEY RESET", "Return to the track's original key", "note", 0x64, "note", 0x1F),
    ("D10", "loop-in", "button", "LOOP IN", "Set the loop in point", "note", 0x10, "note", 0x4C),
    ("D11", "loop-out", "button", "LOOP OUT", "Set the loop out point", "note", 0x11, "note", 0x4D),
    (
        "D12",
        "4-beat-loop-exit",
        "button",
        "4 BEAT LOOP/EXIT",
        "Set a 4-beat loop or exit the loop",
        "note",
        0x14,
        "note",
        0x50,
    ),
    ("D13", "quantize", "button", "QUANTIZE", "Snap cue and loop points to the beat grid", "note", 0x35, "note", 0x39),
    ("D14", "slip", "button", "SLIP", "Slip mode on or off", "note", 0x40, "note", 0x17),
    ("D15", "slip-reverse", "button", "SLIP REVERSE", "Play backwards while held", "note", 0x15, "note", 0x38),
    ("D16", "search-prev", "button", "SEARCH <", "Search backwards", "note", 0x5E, "note", 0x51),
    ("D17", "search-next", "button", "SEARCH >", "Search forwards", "note", 0x5F, "note", 0x53),
    ("D18", "memory", "button", "MEMORY", "Store the cue or loop point", "note", 0x3D, "note", 0x3E),
    ("D19", "shift", "button", "SHIFT", "Hold for each control's second function", "note", 0x3F, None, None),
]

PadModeRow = tuple[str, str, str, str, int, int]
_PAD_MODE_ROWS: list[PadModeRow] = [
    ("P9", "hot-cue", "HOT CUE", "Pad mode: hot cues", 0x1B, 0x69),
    ("P10", "pad-fx1", "PAD FX1", "Pad mode: pad effects", 0x1E, 0x6B),
    ("P11", "beat-jump", "BEAT JUMP", "Pad mode: beat jump", 0x20, 0x6D),
    ("P12", "sampler", "SAMPLER", "Pad mode: sampler", 0x22, 0x6F),
    ("P13", "page-prev", "PAGE <", "Previous pad page", 0x24, 0x01),
    ("P14", "page-next", "PAGE >", "Next pad page", 0x2C, 0x09),
]

_SIDES: dict[str, int] = {"L": 1, "R": 2}


def _deck(side: str) -> list[Control]:
    deck = _SIDES[side]
    ch = deck
    pad_ch = 6 + 2 * deck
    out: list[Control] = []
    for fig, tail, typ, label, does, kind, num, skind, snum in _DECK_ROWS:
        shift = Wire(ch, skind, snum) if skind is not None and snum is not None else None
        out.append(
            _control(
                f"d{deck}-{tail}",
                f"{fig}-{side}",
                typ,
                label,
                f"{does} (deck {deck})",
                "deck",
                Wire(ch, kind, num),
                shift,
            )
        )
    # D6 DECK SELECT: the press sends note 114 on the channel of the deck being switched to.
    other = deck + 2
    out.append(
        _control(
            f"d{deck}-deck-select",
            f"D6-{side}",
            "button",
            f"DECK SELECT {deck}/{other}",
            f"Switch this deck between deck {deck} and deck {other}",
            "deck",
            Wire(other, "note", 0x72),
            Wire(7, "note", 0x7B + deck),
        )
    )
    for pad in range(8):
        out.append(
            _control(
                f"d{deck}-pad-{pad + 1}",
                f"P{pad + 1}-{side}",
                "pad",
                f"PAD {pad + 1}",
                f"Hot cue {'ABCDEFGH'[pad]} (deck {deck}, HOT CUE mode)",
                "performance",
                Wire(pad_ch, "note", pad),
                Wire(pad_ch + 1, "note", pad),
            )
        )
    for fig, tail, label, does, num, snum in _PAD_MODE_ROWS:
        out.append(
            _control(
                f"d{deck}-{tail}",
                f"{fig}-{side}",
                "button",
                label,
                f"{does} (deck {deck})",
                "performance",
                Wire(ch, "note", num),
                Wire(ch, "note", snum),
            )
        )
    out.append(
        _control(
            f"d{deck}-browse",
            f"B1-{side}",
            "encoder",
            "BROWSE",
            f"Scroll the library; press to load deck {deck}",
            "browse",
            Wire(7, "cc", 0x40),
            Wire(7, "cc", 0x64),
        )
    )
    out.append(
        _control(
            f"d{deck}-back",
            f"B2-{side}",
            "button",
            "BACK",
            "Move up one level in the library",
            "browse",
            Wire(7, "note", 0x65),
            Wire(7, "note", 0x66),
        )
    )
    out.append(
        _control(
            f"d{deck}-view",
            f"B3-{side}",
            "button",
            "VIEW",
            "Switch the library view",
            "browse",
            Wire(7, "note", 0x7A),
            Wire(7, "note", 0x68),
        )
    )
    return out


def _channel(c: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str, int, int, Wire | None]] = [
        ("M3", "knob", "TRIM", "Channel gain", "cc", c, 0x04, None),
        ("M4", "knob", "HI", "EQ high band", "cc", c, 0x07, None),
        ("M5", "knob", "MID", "EQ mid band", "cc", c, 0x0B, None),
        ("M6", "knob", "LOW", "EQ low band", "cc", c, 0x0F, None),
        ("F1", "knob", "COLOR FX", "Color effect amount", "cc", 7, 0x16 + c, None),
        ("M7", "button", "CUE", "Headphone cue for this channel", "note", c, 0x54, Wire(c, "note", 0x68)),
        ("M2", "fader", "CH FADER", "Channel volume", "cc", c, 0x13, None),
    ]
    out: list[Control] = []
    for fig, typ, label, does, kind, ch, num, shift in rows:
        tail = {
            "TRIM": "trim",
            "HI": "hi",
            "MID": "mid",
            "LOW": "low",
            "COLOR FX": "color-fx",
            "CUE": "ch-cue",
            "CH FADER": "ch-fader",
        }[label]
        out.append(
            _control(
                f"ch{c}-{tail}", f"{fig}-{c}", typ, label, f"{does} (channel {c})", "mixer", Wire(ch, kind, num), shift
            )
        )
    return out


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str, int, str, int, Wire | None]] = [
        ("M1", "crossfader", "crossfader", "CROSSFADER", "Blend the A and B sides", "mixer", 7, "cc", 0x1F, None),
        ("M8", "master", "knob", "MASTER LEVEL", "Master output level", "mixer", 7, "cc", 0x08, None),
        (
            "M9",
            "master-cue",
            "button",
            "MASTER CUE",
            "Hear the master in headphones",
            "mixer",
            7,
            "note",
            0x63,
            Wire(7, "note", 0x62),
        ),
        ("M10", "booth", "knob", "BOOTH LEVEL", "Booth output level", "mixer", 7, "cc", 0x09, None),
        (
            "M12",
            "phones-mix",
            "knob",
            "HEADPHONES MIXING",
            "Headphone cue and master blend",
            "mixer",
            7,
            "cc",
            0x0C,
            None,
        ),
        ("M13", "phones-level", "knob", "HEADPHONES LEVEL", "Headphone level", "mixer", 7, "cc", 0x0D, None),
        ("M14", "mic-eq-hi", "knob", "MIC EQ HI", "Microphone high EQ", "mixer", 7, "cc", 0x07, None),
        ("M15", "mic-eq-low", "knob", "MIC EQ LOW", "Microphone low EQ", "mixer", 7, "cc", 0x0F, None),
        (
            "M19",
            "sampler-cue",
            "button",
            "SAMPLER CUE",
            "Hear the sampler in headphones",
            "mixer",
            7,
            "note",
            0x69,
            Wire(7, "note", 0x6E),
        ),
        ("M20", "sampler-vol", "knob", "SAMPLER VOL", "Sampler level", "mixer", 7, "cc", 0x03, None),
        (
            "F2",
            "color-fx-dub-echo",
            "button",
            "DUB ECHO",
            "Select the DUB ECHO color effect",
            "effect",
            7,
            "note",
            0x00,
            Wire(7, "note", 0x08),
        ),
        (
            "F3",
            "color-fx-pitch",
            "button",
            "PITCH",
            "Select the PITCH color effect",
            "effect",
            7,
            "note",
            0x01,
            Wire(7, "note", 0x09),
        ),
        (
            "F4",
            "color-fx-noise",
            "button",
            "NOISE",
            "Select the NOISE color effect",
            "effect",
            7,
            "note",
            0x02,
            Wire(7, "note", 0x0A),
        ),
        (
            "F5",
            "color-fx-filter",
            "button",
            "FILTER",
            "Select the FILTER color effect",
            "effect",
            7,
            "note",
            0x03,
            Wire(7, "note", 0x0B),
        ),
        (
            "F6",
            "beat-prev",
            "button",
            "BEAT <",
            "Shorter effect beat",
            "effect",
            5,
            "note",
            0x4A,
            Wire(5, "note", 0x66),
        ),
        ("F7", "beat-next", "button", "BEAT >", "Longer effect beat", "effect", 5, "note", 0x4B, Wire(5, "note", 0x6B)),
        (
            "F8",
            "fx-select",
            "encoder",
            "FX SELECT",
            "Pick the beat effect (LOW CUT ECHO position)",
            "effect",
            5,
            "note",
            0x20,
            None,
        ),
        (
            "F9",
            "fx-ch-select",
            "encoder",
            "CH SELECT",
            "Pick the channel the beat effect hits (CH1 position)",
            "effect",
            5,
            "note",
            0x10,
            None,
        ),
        ("F10", "level-depth", "knob", "LEVEL/DEPTH", "Beat effect amount", "effect", 5, "cc", 0x02, None),
        (
            "F11",
            "fx-on",
            "button",
            "FX ON/OFF",
            "Beat effect on or off",
            "effect",
            5,
            "note",
            0x47,
            Wire(5, "note", 0x43),
        ),
    ]
    return [
        _control(cid, fig, typ, label, does, section, Wire(ch, kind, num), shift)
        for fig, cid, typ, label, does, section, ch, kind, num, shift in rows
    ]


def _control(
    cid: str, fig: str, typ: str, label: str, does: str, section: str, midi: Wire, shift: Wire | None
) -> Control:
    x, y, w, h = _mm_box(fig)
    return Control(cid, fig, typ, label, does, x, y, w, h, section, midi, shift)


# ----------------------------------------------------------------- documents


def _controls() -> list[Control]:
    controls = [*_deck("L"), *_deck("R"), *[c for ch in (1, 2, 3, 4) for c in _channel(ch)], *_globals()]
    ids = [c.id for c in controls]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    figs = {c.fig for c in controls}
    unused = sorted(set(_PX) - figs)
    if unused:
        raise SystemExit(f"[ERROR] traced boxes with no control: {unused}")
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {(c.x, c.y, c.w, c.h)}")
    return controls


def build_reference() -> dict[str, object]:
    rows = []
    for c in _controls():
        x0, y0, x1, y1 = _PX[c.fig]
        rows.append({"id": c.id, "px": [x0, y0, x1, y1], "x": c.x, "y": c.y, "w": c.w, "h": c.h})
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": TRACE,
        "controls": rows,
    }


def build() -> dict[str, object]:
    controls = []
    for c in _controls():
        x, y, w, h = _normalised((c.x, c.y, c.w, c.h))
        controls.append(replace(c, x=x, y=y, w=w, h=h))
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-channel performance controller",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": "DDJ1000_MAP",
        "sources": {
            "panel_size": "DDJ-1000 Quick Start Guide DRH1656B, page 15 Specifications (708 mm width x 361.4 mm depth; 73.4 mm height not modelled)",
            "wire": "apps/webui/frontend/src/lib/rb/midi/maps/ddj-1000.ts (DDJ-1000 List of MIDI message v1.00 E1)",
            "positions": (
                "measured trace of the official top-panel figure, normalised for the renderer; "
                "reference: devices/ddj-1000/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "pad modes other than HOT CUE: pads carry the HOT CUE PAGE 1 wire identity",
            "MSB/LSB companion CCs: each control carries its MSB",
            "three-position slide switches (CROSS FADER ASSIGN M11, INPUT SELECT M17, MIC OFF/ON/TALKOVER M18): no switch type in opendj-panel-layout/1, see #9402",
            "LINE/PHONO switches (M16, rear panel), level meters (M21, M22) and the jog displays: no performance input",
            "the left deck binds deck 1 and the right deck binds deck 2; decks 3 and 4 are reached with DECK SELECT on the hardware",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    reference, panel = build_reference(), build()
    DEVICES_DIR.mkdir(parents=True, exist_ok=True)
    OUT_REFERENCE.write_text(json.dumps(reference, indent=1) + "\n", encoding="utf-8")
    OUT_PANEL.write_text(json.dumps(panel, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_REFERENCE} and {OUT_PANEL} ({len(panel['controls'])} controls)")  # type: ignore[arg-type]
