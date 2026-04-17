"""Rekordbox cue colour palette.

Rekordbox stores a cue's ``Color`` as an integer that indexes into a
fixed 8-slot palette (0 means "no colour"). pyrekordbox does not expose
the palette directly, so we hardcode it here with the standard Rekordbox
XML RGB values.
"""
from __future__ import annotations

RB_COLOR_PALETTE: dict[int, tuple[int, int, int] | None] = {
    0: None,
    1: (255, 0, 0),        # red
    2: (255, 140, 0),      # orange
    3: (255, 255, 0),      # yellow
    4: (0, 255, 0),        # green
    5: (0, 200, 255),      # cyan
    6: (0, 0, 255),        # blue
    7: (255, 0, 255),      # magenta
    8: (255, 255, 255),    # white
}


def color_index_to_rgb(idx: int | None) -> tuple[int, int, int] | None:
    if idx is None:
        return None
    return RB_COLOR_PALETTE.get(int(idx))


def rgb_to_color_index(rgb: tuple[int, int, int] | None) -> int:
    if rgb is None:
        return 0
    best_idx = 0
    best_dist = float("inf")
    for idx, pal in RB_COLOR_PALETTE.items():
        if pal is None:
            continue
        dist = sum((a - b) ** 2 for a, b in zip(rgb, pal))
        if dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


__all__ = ["RB_COLOR_PALETTE", "color_index_to_rgb", "rgb_to_color_index"]
