from __future__ import annotations

from pathlib import Path

Px = tuple[int, int, int, int]  # inclusive pixel box x0, y0, x1, y1

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
