"""Build the Denon DJ PRIME 4+ practice-replica layout pair (ADR-0137, epic #9366).

Writes tools/deck-diagrams/devices/denon-prime4-plus/panel.reference.json (the measurement of
record) and panel.json (the same trace normalised for the renderer). The pixel table `_PX`
below is the single source of position truth.

Sources (public, official, cited in docs/controller/research/DENON-PRIME-4-PLUS-sources.md):
- Panel size 728.47 x 496.82 mm (W x D; H 103.63 mm not modelled): "User Guide - PRIME 4,
  PRIME 4+, PRIME 2, PRIME GO, PRIME GO+, SC LIVE 4, SC LIVE 2" v5.3.0, Technical
  Specifications, PRIME 4 / PRIME 4+, Dimensions (page 101). The same guide states that every
  PRIME 4 feature applies to PRIME 4+ unless noted (page 6).
- Positions: the "Top Panel" figure of that guide (page 6, a vector drawing). The page was
  rendered once with pdftoppm 26.08.0 at 400 dpi (3307 x 4678 px, sha256 in TRACE) and the
  figure's drawing paths were read in that pixel frame with PyMuPDF, so each `_PX` row is a
  path rectangle of the figure, not an eyeballed box. Neither the PDF nor the render enters
  the repo. The 143 numbered callout discs that the guide overlays on the figure were excluded
  by their fill and 77 px size before reading.
- Control names and numbers (`fig`) are the figure's callout numbers (pages 6 to 10).
- Wire identities: Denon DJ publishes NO MIDI message list for the PRIME 4+ (checked at
  denondj.com/downloads, Sun 8 Nov 2026; the LC6000, MCX8000 and legacy lists are other
  products). Every control therefore carries a PLACEHOLDER wire on channel 16 that exists only
  so the layout joins to the hint table of DENON_PRIME4_PLUS_MAP; nothing is transcribed from
  the hardware, no binding exists, and the map is not registered for hardware.

Calibration is ISOTROPIC: one scale from the published 728.47 mm width over the plate outline's
2584 px (x 360..2943); every round control is round in the figure (knob rings 53.0 x 52.3 px,
pads 77.8 x 76.8, jog bezel 569.2 x 561.6, all within 1.5 percent), so no separate y scale is
fitted. The plate outline spans 1504 px = 424.0 mm of the published 496.82 mm depth. The
remaining 72.8 mm is the sloped front face (the figure draws its headphone jack and knob below
the plate line) and the rear connector edge, neither of which carries a top-panel control; the
trace puts y 0 at the plate's rear edge, so if part of that remainder lies behind the plate,
every control shifts equally in y, which control_position_error cannot see.

Rectangle rule (TRACE["method"]): button = its filled body path (CUE and PLAY, which are
outlined, from their four corner arcs); pad = the filled pad face; knob and encoder = the knob
drawing's outermost ring (the scale ring, 53.0 px for the 13 mm class, 63.6 px for SWEEP FX
and MASTER, 70.5 px for BROWSE); jog = the outermost bezel ring; vertical fader = the slot
rails' travel with the cap's width; crossfader = the slot rails' length with the cap's height;
crossfader assign = the slide switch surround. Deck 2 is deck 1 translated by +1730.2 px: for
35 of 38 deck controls an identical path exists at that offset (spread 0.6 px); the three
composite boxes (PITCH, CUE, PLAY) were checked by their corner arcs.

Normalisation (panel.json only): centres snapped to 0.5 mm, sizes to whole mm, and row or
column mates (same control type, centres within 1.5 mm) placed on one line.

Run: uv run --locked python tools/deck-diagrams/scripts/build_denon_prime4_plus_panel.py
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PANEL_W_MM = 728.47
PANEL_D_MM = 496.82
DEVICE = "denon-prime4-plus"
DEVICE_MAP = "DENON_PRIME4_PLUS_MAP"
DEVICE_DIR = Path(__file__).resolve().parents[1] / "devices" / DEVICE
PANEL_OUT = DEVICE_DIR / "panel.json"
REFERENCE_OUT = DEVICE_DIR / "panel.reference.json"

IMAGE_PX = (3307, 4678)
PANEL_PX = {"x0": 360, "x1": 2943, "y0": 1177, "y1": 2680}
MM_PER_PX = round(PANEL_W_MM / (PANEL_PX["x1"] - PANEL_PX["x0"] + 1), 5)
SOURCE_PDF_SHA256 = "a16b93d9cb97df95546e032435ee24e7fcd9520bbf88ca076e5d98b023a45402"
SOURCE_IMAGE_SHA256 = (
    "1f1a44b399fc3d69cad91cd527a6efe9e41cc34d5d0a8986a3f0ee2b5d1a6146"  # pdftoppm -r 400 -png -f 6 -l 6
)
ALIGN_TOL_MM = 1.5
DECK_SHIFT_PX = 1730.2

PLACEHOLDER_CH = 16  # every wire below is a placeholder on this channel; see the module docstring


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


# ----------------------------------------------------------------- the trace (pixels)
# Inclusive [x0, y0, x1, y1] in the 400 dpi render of page 6; see the module docstring.

# Left deck (deck 1).
_PX_DECK_1: dict[str, list[int]] = {
    "d1-auto-loop": [433, 1700, 485, 1751],
    "d1-loop-in": [411, 1799, 445, 1818],
    "d1-loop-out": [474, 1799, 507, 1818],
    "d1-slip": [552, 1707, 589, 1744],
    "d1-censor": [400, 1873, 456, 1894],
    "d1-deck-1": [413, 1964, 444, 1995],
    "d1-deck-3": [413, 2036, 444, 2067],
    "d1-vinyl": [1008, 1707, 1046, 1744],
    "d1-key-lock": [1120, 1715, 1157, 1737],
    "d1-jog": [502, 1735, 1070, 2296],
    "d1-pitch": [1109, 1823, 1168, 2208],
    "d1-pitch-bend-minus": [1098, 2274, 1122, 2298],
    "d1-pitch-bend-plus": [1155, 2274, 1179, 2298],
    "d1-track-prev": [403, 2187, 442, 2226],
    "d1-track-next": [479, 2187, 518, 2226],
    "d1-beat-jump-back": [403, 2269, 442, 2308],
    "d1-beat-jump-fwd": [479, 2269, 518, 2308],
    "d1-sync": [413, 2373, 509, 2395],
    "d1-cue": [402, 2435, 519, 2533],
    "d1-play": [402, 2543, 519, 2641],
    "d1-hot-cue": [581, 2372, 662, 2395],
    "d1-loop": [691, 2372, 771, 2395],
    "d1-roll": [801, 2372, 881, 2395],
    "d1-slicer": [910, 2372, 990, 2395],
    "d1-shift": [1063, 2373, 1159, 2395],
    "d1-edit-grid": [1063, 2465, 1159, 2486],
    "d1-grid-prev": [1062, 2539, 1098, 2561],
    "d1-grid-next": [1124, 2539, 1161, 2561],
    "d1-param-prev": [1062, 2609, 1098, 2631],
    "d1-param-next": [1124, 2609, 1161, 2631],
    "d1-pad-1": [583, 2446, 660, 2522],
    "d1-pad-2": [693, 2446, 769, 2522],
    "d1-pad-3": [802, 2446, 879, 2522],
    "d1-pad-4": [912, 2446, 989, 2522],
    "d1-pad-5": [583, 2554, 660, 2630],
    "d1-pad-6": [693, 2554, 769, 2630],
    "d1-pad-7": [802, 2554, 879, 2630],
    "d1-pad-8": [912, 2554, 989, 2630],
}

# Right deck (deck 2): each box is the deck 1 path found again at +1730.2 px (PITCH, CUE and PLAY
# are composites, translated and checked by their corner arcs and rails).
_PX_DECK_2: dict[str, list[int]] = {
    "d2-auto-loop": [2164, 1700, 2216, 1751],
    "d2-loop-in": [2142, 1799, 2176, 1818],
    "d2-loop-out": [2205, 1799, 2238, 1818],
    "d2-slip": [2282, 1707, 2319, 1744],
    "d2-censor": [2130, 1873, 2186, 1894],
    "d2-deck-2": [2143, 1964, 2174, 1995],
    "d2-deck-4": [2143, 2036, 2174, 2067],
    "d2-vinyl": [2739, 1707, 2776, 1744],
    "d2-key-lock": [2850, 1715, 2887, 1737],
    "d2-jog": [2233, 1736, 2800, 2295],
    "d2-pitch": [2839, 1823, 2898, 2208],
    "d2-pitch-bend-minus": [2829, 2274, 2852, 2298],
    "d2-pitch-bend-plus": [2885, 2274, 2909, 2298],
    "d2-track-prev": [2133, 2187, 2173, 2226],
    "d2-track-next": [2209, 2187, 2249, 2226],
    "d2-beat-jump-back": [2133, 2269, 2173, 2308],
    "d2-beat-jump-fwd": [2209, 2269, 2249, 2308],
    "d2-sync": [2143, 2373, 2239, 2395],
    "d2-cue": [2132, 2435, 2250, 2533],
    "d2-play": [2132, 2543, 2250, 2641],
    "d2-hot-cue": [2312, 2372, 2392, 2395],
    "d2-loop": [2421, 2372, 2502, 2395],
    "d2-roll": [2531, 2372, 2611, 2395],
    "d2-slicer": [2640, 2372, 2721, 2395],
    "d2-shift": [2793, 2373, 2890, 2395],
    "d2-edit-grid": [2793, 2465, 2890, 2486],
    "d2-grid-prev": [2792, 2539, 2828, 2561],
    "d2-grid-next": [2854, 2539, 2891, 2561],
    "d2-param-prev": [2792, 2609, 2828, 2631],
    "d2-param-next": [2854, 2609, 2891, 2631],
    "d2-pad-1": [2313, 2446, 2390, 2522],
    "d2-pad-2": [2423, 2446, 2500, 2522],
    "d2-pad-3": [2533, 2446, 2609, 2522],
    "d2-pad-4": [2642, 2446, 2719, 2522],
    "d2-pad-5": [2313, 2554, 2390, 2630],
    "d2-pad-6": [2423, 2554, 2500, 2630],
    "d2-pad-7": [2533, 2554, 2609, 2630],
    "d2-pad-8": [2642, 2554, 2719, 2630],
}

# Mixer strips in their physical order, left to right: CH3, CH1, CH2, CH4.
_PX_MIXER: dict[str, list[int]] = {
    "ch3-fx-1": [1248, 1659, 1284, 1681],
    "ch3-fx-2": [1311, 1659, 1347, 1681],
    "ch3-level": [1271, 1713, 1323, 1764],
    "ch3-high": [1271, 1820, 1323, 1872],
    "ch3-mid": [1271, 1930, 1323, 1981],
    "ch3-low": [1271, 2039, 1323, 2090],
    "ch3-sweep-fx": [1266, 2140, 1329, 2202],
    "ch3-cue": [1270, 2248, 1325, 2268],
    "ch3-fader": [1264, 2308, 1330, 2490],
    "ch3-xf-assign": [1276, 2521, 1319, 2549],
    "ch1-fx-1": [1425, 1659, 1461, 1681],
    "ch1-fx-2": [1487, 1659, 1524, 1681],
    "ch1-level": [1448, 1713, 1500, 1764],
    "ch1-high": [1448, 1820, 1500, 1872],
    "ch1-mid": [1448, 1930, 1500, 1981],
    "ch1-low": [1448, 2039, 1500, 2090],
    "ch1-sweep-fx": [1443, 2140, 1506, 2202],
    "ch1-cue": [1446, 2248, 1502, 2268],
    "ch1-fader": [1442, 2308, 1507, 2490],
    "ch1-xf-assign": [1453, 2521, 1496, 2549],
    "ch2-fx-1": [1778, 1659, 1815, 1681],
    "ch2-fx-2": [1841, 1659, 1877, 1681],
    "ch2-level": [1802, 1713, 1854, 1764],
    "ch2-high": [1802, 1820, 1854, 1872],
    "ch2-mid": [1802, 1930, 1854, 1981],
    "ch2-low": [1802, 2039, 1854, 2090],
    "ch2-sweep-fx": [1796, 2140, 1859, 2202],
    "ch2-cue": [1800, 2248, 1856, 2268],
    "ch2-fader": [1795, 2308, 1860, 2490],
    "ch2-xf-assign": [1806, 2521, 1849, 2549],
    "ch4-fx-1": [1955, 1659, 1992, 1681],
    "ch4-fx-2": [2018, 1659, 2054, 1681],
    "ch4-level": [1978, 1713, 2031, 1764],
    "ch4-high": [1978, 1820, 2031, 1872],
    "ch4-mid": [1978, 1930, 2031, 1981],
    "ch4-low": [1978, 2039, 2031, 2090],
    "ch4-sweep-fx": [1973, 2140, 2036, 2202],
    "ch4-cue": [1977, 2248, 2033, 2268],
    "ch4-fader": [1971, 2308, 2038, 2490],
    "ch4-xf-assign": [1983, 2521, 2026, 2549],
}

# Browse, phones, sweep, crossfader, master, zone, booth, eject, mic and FX sections.
_PX_GLOBAL: dict[str, list[int]] = {
    "browse": [1616, 1694, 1686, 1762],
    "back": [1565, 1659, 1639, 1716],
    "fwd": [1663, 1659, 1737, 1716],
    "load-1": [1565, 1740, 1639, 1797],
    "load-2": [1663, 1740, 1737, 1797],
    "view": [1633, 1830, 1669, 1852],
    "phones-mix": [1625, 2166, 1677, 2217],
    "phones-level": [1625, 2264, 1677, 2316],
    "split-cue": [1623, 2365, 1679, 2385],
    "sweep-filter": [1602, 2447, 1638, 2469],
    "sweep-echo": [1664, 2447, 1701, 2469],
    "sweep-wash": [1602, 2503, 1638, 2525],
    "sweep-noise": [1664, 2503, 1701, 2525],
    "crossfader": [1552, 2575, 1749, 2642],
    "master-level": [2143, 1274, 2205, 1336],
    "zone-assign": [2274, 1236, 2298, 1259],
    "zone-level": [2351, 1222, 2403, 1273],
    "zone-low": [2468, 1222, 2520, 1273],
    "zone-high": [2585, 1222, 2637, 1273],
    "booth-level": [2351, 1330, 2404, 1381],
    "booth-low": [2468, 1330, 2520, 1381],
    "booth-high": [2585, 1330, 2637, 1381],
    "eject": [2819, 1348, 2852, 1380],
    "mic1-on": [532, 1232, 556, 1255],
    "mic1-level": [615, 1218, 667, 1269],
    "mic1-low": [730, 1218, 782, 1269],
    "mic1-mid": [845, 1218, 897, 1269],
    "mic1-high": [960, 1218, 1012, 1269],
    "mic1-echo-on": [1071, 1232, 1095, 1255],
    "talkover": [418, 1292, 474, 1312],
    "mic2-on": [532, 1349, 556, 1372],
    "mic2-level": [615, 1335, 667, 1386],
    "mic2-low": [730, 1335, 782, 1386],
    "mic2-high": [845, 1335, 897, 1386],
    "echo-level": [960, 1335, 1012, 1386],
    "mic2-echo-on": [1071, 1349, 1095, 1372],
    "fx1-select": [498, 1491, 550, 1543],
    "fx1-param": [673, 1491, 725, 1543],
    "fx1-freq": [847, 1491, 899, 1543],
    "fx1-wet-dry": [1021, 1491, 1073, 1543],
    "fx1-on": [496, 1578, 552, 1599],
    "fx1-param-btn": [671, 1578, 727, 1599],
    "fx1-reset": [845, 1578, 901, 1599],
    "fx1-beats-prev": [998, 1577, 1034, 1599],
    "fx1-beats-next": [1061, 1577, 1097, 1599],
    "fx2-select": [2228, 1491, 2280, 1543],
    "fx2-param": [2403, 1491, 2455, 1543],
    "fx2-freq": [2577, 1491, 2629, 1543],
    "fx2-wet-dry": [2752, 1491, 2804, 1543],
    "fx2-on": [2227, 1578, 2283, 1599],
    "fx2-param-btn": [2401, 1578, 2457, 1599],
    "fx2-reset": [2575, 1578, 2631, 1599],
    "fx2-beats-prev": [2728, 1577, 2765, 1599],
    "fx2-beats-next": [2791, 1577, 2827, 1599],
}

_PX: dict[str, list[int]] = {**_PX_DECK_1, **_PX_DECK_2, **_PX_MIXER, **_PX_GLOBAL}

# Second pass, read independently on the RASTER render by flood-filling each control's dark
# body from a seed on its left edge (vector data used only to skip controls under a callout
# disc). Largest centre difference against `_PX` is 1.5 px = 0.42 mm, recorded as
# repeat_spread_mm 0.5.
_PX_REPEAT: dict[str, list[int]] = {
    "d1-pad-1": [577, 2440, 666, 2527],
    "d1-pad-4": [906, 2440, 994, 2527],
    "d1-pad-5": [577, 2548, 666, 2636],
    "d1-pad-8": [906, 2548, 994, 2636],
    "d2-pad-1": [2308, 2440, 2396, 2528],
    "d2-pad-4": [2636, 2440, 2725, 2528],
    "d2-pad-5": [2308, 2548, 2396, 2636],
    "d2-pad-8": [2636, 2548, 2725, 2636],
    "d1-grid-prev": [1058, 2535, 1101, 2565],
    "d2-grid-prev": [2788, 2535, 2832, 2565],
    "d1-pitch-bend-plus": [1152, 2271, 1179, 2301],
}

TRACE: dict[str, object] = {
    "source": (
        "User Guide - PRIME 4, PRIME 4+, PRIME 2, PRIME GO, PRIME GO+, SC LIVE 4, SC LIVE 2 (v5.3.0), "
        "page 6 Top Panel figure (vector drawing), rendered with pdftoppm 26.08.0 at 400 dpi (3307 x 4678 px)"
    ),
    "source_url": (
        "https://cdn.inmusicbrands.com/engine/53/PRIME%204%2C%20PRIME%204%2B%2C%20PRIME%202%2C%20PRIME%20GO"
        "%2C%20PRIME%20GO%2B%2C%20SC%20LIVE%204%2C%20SC%20LIVE%202%20-%20User%20Guide%20-%20v5.3.0.pdf"
    ),
    "source_pdf_sha256": SOURCE_PDF_SHA256,
    "source_image_sha256": SOURCE_IMAGE_SHA256,
    "provenance": "docs/controller/research/DENON-PRIME-4-PLUS-sources.md",
    "image_px": list(IMAGE_PX),
    "panel_px": dict(PANEL_PX),
    "mm_per_px": {"x": MM_PER_PX, "y": MM_PER_PX},
    "calibration": (
        "isotropic: one scale from the published 728.47 mm width over the plate outline's 2584 px; every "
        "round control is round in the figure (knob rings 53.0 x 52.3 px, pads 77.8 x 76.8, jog bezel "
        "569.2 x 561.6), so no separate y scale is fitted. The plate outline is 1504 px = 424.0 mm of the "
        "published 496.82 mm depth; the remaining 72.8 mm is the sloped front face (drawn below the plate "
        "line with its headphone jack and knob) and the rear connector edge, neither carrying a top-panel "
        "control. y 0 is the plate's rear edge: a remainder behind it would shift every control equally."
    ),
    "method": (
        "Boxes are the figure's own vector path rectangles, read with PyMuPDF in the 400 dpi pixel frame "
        "after excluding the 143 callout discs (black fill, 77 px). Button: filled body path (CUE and PLAY: "
        "from their corner arcs). Pad: filled pad face. Knob and encoder: outermost ring of the knob drawing. "
        "Jog: outermost bezel ring. Vertical fader: slot rails' travel with the cap width. Crossfader: slot "
        "rails' length with the cap height. Crossfader assign: slide switch surround. Deck 2 = deck 1 + "
        "1730.2 px, verified path by path."
    ),
    "repeat_pass": _PX_REPEAT,
    "repeat_spread_mm": 0.5,
    "midi": (
        "no published MIDI message list (Denon DJ downloads page, Sun 8 Nov 2026); wires are placeholders "
        "on channel 16 that only join the layout to the map's hints"
    ),
    "issue": "#9366",
    "date": "Sun 8 Nov 2026",
}


# ----------------------------------------------------------------- helpers


def _mm_box(px: list[int]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = px
    s = MM_PER_PX
    return (
        round((x0 - PANEL_PX["x0"]) * s, 1),
        round((y0 - PANEL_PX["y0"]) * s, 1),
        round((x1 - x0 + 1) * s, 1),
        round((y1 - y0 + 1) * s, 1),
    )


# ----------------------------------------------------------------- controls (no wires yet)

# (id suffix, fig, type, label, does, section)
_DECK_ROWS: list[tuple[str, str, str, str, str, str]] = [
    (
        "auto-loop",
        "45",
        "encoder",
        "AUTO LOOP",
        "Turn to set the auto loop size, press to start or stop it; with SHIFT, move the loop",
        "deck",
    ),
    ("loop-in", "46", "button", "LOOP IN", "Set the loop in point", "deck"),
    ("loop-out", "46", "button", "LOOP OUT", "Set the loop out point", "deck"),
    ("slip", "37", "button", "SLIP", "Toggle slip mode", "deck"),
    ("censor", "38", "button", "CENSOR", "Hold to play reversed and snap back; with SHIFT, reverse playback", "deck"),
    ("vinyl", "30", "button", "VINYL", "Toggle vinyl mode on the platter", "deck"),
    ("key-lock", "31", "button", "KEY LOCK", "Toggle key lock; hold for key sync; with SHIFT, reset the key", "deck"),
    ("jog", "22", "jog", "PLATTER", "Scratch or nudge the track (platter display in the centre)", "deck"),
    ("pitch", "32", "fader", "PITCH", "Tempo fader", "deck"),
    (
        "pitch-bend-minus",
        "33",
        "button",
        "PITCH BEND -",
        "Hold to slow the track; with SHIFT, set the pitch range",
        "deck",
    ),
    (
        "pitch-bend-plus",
        "33",
        "button",
        "PITCH BEND +",
        "Hold to speed the track; with SHIFT, set the pitch range",
        "deck",
    ),
    ("track-prev", "25", "button", "TRACK SKIP <", "Previous track; with SHIFT, search backward", "deck"),
    ("track-next", "25", "button", "TRACK SKIP >", "Next track; with SHIFT, search forward", "deck"),
    (
        "beat-jump-back",
        "26",
        "button",
        "BEAT JUMP <",
        "Jump back by the beat jump size; with SHIFT, change the size",
        "deck",
    ),
    (
        "beat-jump-fwd",
        "26",
        "button",
        "BEAT JUMP >",
        "Jump forward by the beat jump size; with SHIFT, change the size",
        "deck",
    ),
    ("sync", "27", "button", "SYNC", "Toggle sync", "deck"),
    ("cue", "28", "button", "CUE", "Return to the cue point, or set it while paused", "deck"),
    ("play", "29", "button", "PLAY/PAUSE", "Start or pause the deck; with SHIFT, stutter play", "deck"),
    ("hot-cue", "40", "button", "HOT CUE", "Pad mode: hot cues; press again for stems", "performance"),
    ("loop", "41", "button", "LOOP", "Pad mode: manual loop; press again for auto loop", "performance"),
    ("roll", "42", "button", "ROLL", "Pad mode: roll; with SHIFT, sampler", "performance"),
    ("slicer", "43", "button", "SLICER", "Pad mode: slicer; press again for slicer loop", "performance"),
    ("shift", "34", "button", "SHIFT", "Hold for each control's second function", "deck"),
    ("edit-grid", "35", "button", "EDIT GRID", "Hold to edit the beat grid; with SHIFT, reset it", "deck"),
    ("grid-prev", "36", "button", "GRID <", "Move the downbeat marker back; with SHIFT, halve the tempo", "deck"),
    ("grid-next", "36", "button", "GRID >", "Move the downbeat marker forward; with SHIFT, double the tempo", "deck"),
    ("param-prev", "44", "button", "PARAMETER <", "Pad-mode parameter down", "performance"),
    ("param-next", "44", "button", "PARAMETER >", "Pad-mode parameter up", "performance"),
]

_DECK_SELECT = {1: ("deck-1", "deck-3", "DECK 1", "DECK 3"), 2: ("deck-2", "deck-4", "DECK 2", "DECK 4")}


def _deck(deck: int) -> list[Control]:
    out: list[Control] = []
    a, b, la, lb = _DECK_SELECT[deck]
    out.append(
        Control(
            f"d{deck}-{a}",
            "24",
            "button",
            la,
            f"Give this side {la.lower()} (deck {deck} side)",
            0,
            0,
            0,
            0,
            "deck",
            _NO_WIRE,
        )
    )
    out.append(
        Control(
            f"d{deck}-{b}",
            "24",
            "button",
            lb,
            f"Give this side {lb.lower()}; with SHIFT, toggle 2- or 4-deck mode (deck {deck} side)",
            0,
            0,
            0,
            0,
            "deck",
            _NO_WIRE,
        )
    )
    for suffix, fig, typ, label, does, section in _DECK_ROWS:
        out.append(
            Control(f"d{deck}-{suffix}", fig, typ, label, f"{does} (deck {deck})", 0, 0, 0, 0, section, _NO_WIRE)
        )
    out.extend(
        Control(
            f"d{deck}-pad-{pad}",
            "39",
            "pad",
            f"PAD {pad}",
            f"Performance pad {pad}; its function follows the pad mode (deck {deck})",
            0,
            0,
            0,
            0,
            "performance",
            _NO_WIRE,
        )
        for pad in range(1, 9)
    )
    return out


def _channel(ch: int) -> list[Control]:
    rows: list[tuple[str, str, str, str, str]] = [
        ("fx-1", "8", "button", "FX 1", "Route this channel to the FX 1 module"),
        ("fx-2", "8", "button", "FX 2", "Route this channel to the FX 2 module"),
        ("level", "9", "knob", "LEVEL", "Channel gain (pre-fader, pre-EQ)"),
        ("high", "11", "knob", "HIGH", "EQ high band"),
        ("mid", "11", "knob", "MID", "EQ mid band"),
        ("low", "11", "knob", "LOW", "EQ low band"),
        ("sweep-fx", "12", "knob", "SWEEP FX", "Amount of the selected sweep effect"),
        ("cue", "13", "button", "CUE", "Headphone cue for this channel"),
        ("fader", "14", "fader", "CH FADER", "Channel volume"),
        ("xf-assign", "15", "button", "CROSSFADER ASSIGN", "Slide switch: L, Thru or R"),
    ]
    return [
        Control(f"ch{ch}-{s}", fig, typ, label, f"{does} (channel {ch})", 0, 0, 0, 0, "mixer", _NO_WIRE)
        for s, fig, typ, label, does in rows
    ]


def _globals() -> list[Control]:
    rows: list[tuple[str, str, str, str, str, str]] = [
        (
            "browse",
            "2",
            "encoder",
            "BROWSE",
            "Turn to scroll the library, press to select or load; with SHIFT, send to Prepare",
            "browse",
        ),
        ("back", "3", "button", "BACK", "Previous window in the library", "browse"),
        ("fwd", "4", "button", "FWD", "Next window in the library; with SHIFT, toggle quantize", "browse"),
        (
            "load-1",
            "5",
            "button",
            "LOAD <",
            "Load the selected track to deck 1 or 3; double-press to instant double",
            "browse",
        ),
        (
            "load-2",
            "6",
            "button",
            "LOAD >",
            "Load the selected track to deck 2 or 4; double-press to instant double",
            "browse",
        ),
        ("view", "7", "button", "VIEW", "Cycle library and performance views; hold for the control centre", "browse"),
        ("phones-mix", "18", "knob", "PHONES MIX", "Headphone mix between cue and master", "mixer"),
        ("phones-level", "19", "knob", "PHONES LEVEL", "Headphone volume", "mixer"),
        ("split-cue", "20", "button", "SPLIT CUE", "Split cue and master across the headphone channels", "mixer"),
        ("sweep-filter", "21", "button", "FILTER", "Sweep FX: filter", "effect"),
        ("sweep-echo", "21", "button", "ECHO", "Sweep FX: echo", "effect"),
        ("sweep-wash", "21", "button", "WASH OUT", "Sweep FX: wash out", "effect"),
        ("sweep-noise", "21", "button", "NOISE", "Sweep FX: noise", "effect"),
        ("crossfader", "16", "crossfader", "CROSSFADER", "Blend the channels assigned left and right", "mixer"),
        ("master-level", "61", "knob", "MASTER LEVEL", "Master output level", "mixer"),
        ("zone-assign", "62", "button", "ZONE ASSIGN", "Send deck 4 to the zone outputs as a zone playlist", "mixer"),
        ("zone-level", "63", "knob", "ZONE LEVEL", "Zone output level", "mixer"),
        ("zone-low", "64", "knob", "ZONE LOW", "Zone EQ low band", "mixer"),
        ("zone-high", "64", "knob", "ZONE HIGH", "Zone EQ high band", "mixer"),
        ("booth-level", "65", "knob", "BOOTH LEVEL", "Booth output level", "mixer"),
        ("booth-low", "66", "knob", "BOOTH LOW", "Booth EQ low band", "mixer"),
        ("booth-high", "66", "knob", "BOOTH HIGH", "Booth EQ high band", "mixer"),
        ("eject", "69", "button", "EJECT", "Open the source menu to eject media", "browse"),
        ("mic1-on", "55", "button", "MIC 1 ON/OFF", "Microphone 1 on or off", "mixer"),
        ("mic1-level", "56", "knob", "MIC 1 LEVEL", "Microphone 1 level", "mixer"),
        ("mic1-low", "57", "knob", "MIC 1 LOW", "Microphone 1 EQ low", "mixer"),
        ("mic1-mid", "57", "knob", "MIC 1 MID", "Microphone 1 EQ mid", "mixer"),
        ("mic1-high", "57", "knob", "MIC 1 HIGH", "Microphone 1 EQ high", "mixer"),
        ("mic1-echo-on", "58", "button", "MIC 1 ECHO", "Echo on microphone 1 on or off", "mixer"),
        ("talkover", "60", "button", "TALKOVER", "Duck the main mix while speaking", "mixer"),
        ("mic2-on", "55", "button", "MIC 2 ON/OFF", "Microphone 2 on or off", "mixer"),
        ("mic2-level", "56", "knob", "MIC 2 LEVEL", "Microphone 2 level", "mixer"),
        ("mic2-low", "57", "knob", "MIC 2 LOW", "Microphone 2 EQ low", "mixer"),
        ("mic2-high", "57", "knob", "MIC 2 HIGH", "Microphone 2 EQ high", "mixer"),
        ("echo-level", "59", "knob", "ECHO LEVEL", "Microphone echo amount", "mixer"),
        ("mic2-echo-on", "58", "button", "MIC 2 ECHO", "Echo on microphone 2 on or off", "mixer"),
    ]
    out = [
        Control(cid, fig, typ, label, does, 0, 0, 0, 0, section, _NO_WIRE)
        for cid, fig, typ, label, does, section in rows
    ]
    for n in (1, 2):
        out += [
            Control(
                f"fx{n}-select",
                "48",
                "knob",
                "FX SELECT",
                f"Select the active effect (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-param",
                "50",
                "knob",
                "FX PARAMETER",
                f"Effect parameter (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-freq",
                "52",
                "knob",
                "FX FREQUENCY",
                f"Frequency band the effect hits (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-wet-dry",
                "53",
                "knob",
                "FX WET/DRY",
                f"Effect wet/dry mix (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(f"fx{n}-on", "47", "button", "FX ON", f"Effect on or off (FX {n})", 0, 0, 0, 0, "effect", _NO_WIRE),
            Control(
                f"fx{n}-param-btn",
                "49",
                "button",
                "PARAM",
                f"Reset the effect parameter to its default (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-reset",
                "51",
                "button",
                "RESET",
                f"Reset the FX frequency to centre; with SHIFT, reset the effect (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-beats-prev",
                "54",
                "button",
                "BEATS <",
                f"Shorter effect beat (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
            Control(
                f"fx{n}-beats-next",
                "54",
                "button",
                "BEATS >",
                f"Longer effect beat (FX {n})",
                0,
                0,
                0,
                0,
                "effect",
                _NO_WIRE,
            ),
        ]
    return out


_NO_WIRE = Wire(PLACEHOLDER_CH, "note", 0)  # replaced by _assign_placeholder_wires
_NOTE_TYPES = {"button", "pad"}


def _assign_placeholder_wires(controls: list[Control]) -> list[Control]:
    """Sequential placeholder wires on channel 16: notes for buttons and pads, CCs for the rest.
    The order is the layout order, so the map's hint table is reproducible from this file."""
    note = cc = 0
    out: list[Control] = []
    for c in controls:
        if c.type in _NOTE_TYPES:
            out.append(replace(c, midi=Wire(PLACEHOLDER_CH, "note", note)))
            note += 1
        else:
            out.append(replace(c, midi=Wire(PLACEHOLDER_CH, "cc", cc)))
            cc += 1
    if note > 128 or cc > 128:
        raise SystemExit(f"[ERROR] placeholder wires overflow channel 16: notes {note}, ccs {cc}")
    return out


# ----------------------------------------------------------------- geometry


def _reference_rows(order: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cid in order:
        x, y, w, h = _mm_box(_PX[cid])
        rows.append({"id": cid, "px": list(_PX[cid]), "x": x, "y": y, "w": w, "h": h})
    return rows


def _aligned_centres(controls: list[Control], axis: int) -> dict[str, float]:
    """Row/column mates (same type, centres within ALIGN_TOL_MM) share one snapped centre."""
    centres: dict[str, float] = {}
    for c in controls:
        x, y, w, h = _mm_box(_PX[c.id])
        centres[c.id] = (x + w / 2) if axis == 0 else (y + h / 2)
    by_type: dict[str, list[Control]] = {}
    for c in controls:
        by_type.setdefault(c.type, []).append(c)
    out: dict[str, float] = {}
    for members in by_type.values():
        ordered = sorted(members, key=lambda c: centres[c.id])
        group: list[Control] = []
        for c in ordered:
            if group and centres[c.id] - centres[group[-1].id] > ALIGN_TOL_MM:
                _snap_group(group, centres, out)
                group = []
            group.append(c)
        if group:
            _snap_group(group, centres, out)
    return out


def _snap_group(group: list[Control], centres: dict[str, float], out: dict[str, float]) -> None:
    mean = math.fsum(centres[c.id] for c in group) / len(group)  # fsum: identical on every Python version
    snapped = round(mean * 2) / 2
    for c in group:
        out[c.id] = snapped


def _normalised(controls: list[Control]) -> list[Control]:
    cx = _aligned_centres(controls, 0)
    cy = _aligned_centres(controls, 1)
    out: list[Control] = []
    for c in controls:
        _, _, w_raw, h_raw = _mm_box(_PX[c.id])
        w, h = float(round(w_raw)), float(round(h_raw))
        x = round(cx[c.id] - w / 2, 1)
        y = round(cy[c.id] - h / 2, 1)
        out.append(replace(c, x=x, y=y, w=w, h=h))
    return out


# ----------------------------------------------------------------- build


def _controls() -> list[Control]:
    base = [*_deck(1), *_deck(2), *_channel(3), *_channel(1), *_channel(2), *_channel(4), *_globals()]
    ids = [c.id for c in base]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"[ERROR] duplicate control ids: {dupes}")
    known = set(ids)
    missing_pos = sorted(known - set(_PX))
    extra_pos = sorted(set(_PX) - known)
    if missing_pos or extra_pos:
        raise SystemExit(f"[ERROR] _PX mismatch: missing={missing_pos} extra={extra_pos}")
    for cid in _PX_REPEAT:
        if cid not in known:
            raise SystemExit(f"[ERROR] repeat pass names an unknown control: {cid}")
    return _assign_placeholder_wires(base)


def build_reference() -> dict[str, object]:
    controls = _controls()
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "units": "mm",
        "measured": True,
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "trace": TRACE,
        "controls": _reference_rows([c.id for c in controls]),
    }


def build() -> dict[str, object]:
    controls = _normalised(_controls())
    for c in controls:
        if c.x < 0 or c.y < 0 or c.x + c.w > PANEL_W_MM or c.y + c.h > PANEL_D_MM:
            raise SystemExit(f"[ERROR] {c.id} lies outside the panel: {c.x},{c.y},{c.w},{c.h}")
    return {
        "format": "opendj-panel-layout/1",
        "device": DEVICE,
        "display_name": "4-deck standalone DJ system",
        "units": "mm",
        "panel": {"w": PANEL_W_MM, "d": PANEL_D_MM},
        "device_map": DEVICE_MAP,
        "sources": {
            "panel_size": (
                "User Guide - PRIME 4, PRIME 4+, PRIME 2, PRIME GO, PRIME GO+, SC LIVE 4, SC LIVE 2 v5.3.0, "
                "Technical Specifications, PRIME 4 / PRIME 4+, Dimensions (728.47 x 496.82 x 103.63 mm)"
            ),
            "wire": (
                "PLACEHOLDER wires on channel 16 (apps/webui/frontend/src/lib/rb/midi/maps/denon-prime4-plus.ts hints): "
                "Denon DJ publishes no MIDI message list for the PRIME 4+"
            ),
            "positions": (
                "measured trace of the official top-panel figure (vector paths of the user guide, page 6), "
                f"normalised for the renderer; reference: devices/{DEVICE}/panel.reference.json (ADR-0137)"
            ),
        },
        "out_of_scope": [
            "every wire identity: no published MIDI list, so every control is a map hint (inert) until one exists",
            "touchscreen, platter displays, FX OLED displays, channel and master level meters: displays, not controls",
            "SD card slot and USB 1/2 ports: media slots on the top panel, not controls",
            "front-panel and rear-panel controls (mic inputs, headphone jacks, zone and booth outputs)",
            "pad modes other than the mode button pressed: pads carry one placeholder wire each",
        ],
        "controls": [asdict(c) for c in controls],
    }


if __name__ == "__main__":
    ref = build_reference()
    doc = build()
    REFERENCE_OUT.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_OUT.write_text(json.dumps(ref, indent=1) + "\n", encoding="utf-8")
    PANEL_OUT.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"[OK] wrote {REFERENCE_OUT} and {PANEL_OUT} ({len(doc['controls'])} controls)")  # type: ignore[arg-type]
