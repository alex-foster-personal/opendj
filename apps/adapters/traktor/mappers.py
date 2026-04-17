"""Field mappers: Traktor NML <-> open-dj.

Keep the mapping table and the encode/decode helpers here so the adapter in
``adapter.py`` stays a thin orchestrator. Each mapper is pure (input in,
output out; no I/O, no Element mutation) so it is trivially unit-testable.

Canonical mappings (bidirectional where noted):

    open-dj field       | NML source
    --------------------|----------------------------------------
    bpm                 | TEMPO @BPM
    key_camelot         | MUSICAL_KEY @VALUE (0..23 integer)
    rating              | INFO @RANKING (0/51/102/153/204/255)
    duration_ms         | INFO @PLAYTIME (seconds, float) * 1000
    color_rgb           | INFO @COLOR (1..16 palette index; we store the
                          "canonical" RGB for each palette slot and the
                          original index in extensions.x_traktor_color_idx)
    cue_points          | CUE_V2[] children; type enum mapped via CUE_TYPE_MAP.

The Camelot table below converts the 0..23 integer Traktor uses for
``MUSICAL_KEY @VALUE`` (open-key notation) into the Camelot wheel code
(e.g. "8A", "11B"). Reference: Mixed In Key cheatsheet.
"""

from __future__ import annotations

from typing import Literal

# ------------------------------------------------------------- Camelot map

# Traktor stores key as 0..23 (open-key). The pairing is:
#   0  -> "1d" major  (= 8B Camelot)
#   1  -> "2d" major  (= 9B)
#   ...
# We publish the Camelot (B for major, A for minor) code directly.
_CAMELOT_LOOKUP: tuple[str, ...] = (
    "8B",  "3B",  "10B", "5B",  "12B", "7B",
    "2B",  "9B",  "4B",  "11B", "6B",  "1B",
    "5A",  "12A", "7A",  "2A",  "9A",  "4A",
    "11A", "6A",  "1A",  "8A",  "3A",  "10A",
)

_CAMELOT_REVERSE: dict[str, int] = {v: i for i, v in enumerate(_CAMELOT_LOOKUP)}


def traktor_key_to_camelot(value: int | str | None) -> str | None:
    """Convert Traktor ``MUSICAL_KEY @VALUE`` to Camelot code."""
    if value is None:
        return None
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(_CAMELOT_LOOKUP):
        return _CAMELOT_LOOKUP[idx]
    return None


def camelot_to_traktor_key(camelot: str | None) -> int | None:
    """Convert Camelot code back to Traktor ``MUSICAL_KEY @VALUE``."""
    if not camelot:
        return None
    return _CAMELOT_REVERSE.get(camelot.upper())


# ---------------------------------------------------------------- rating

# Traktor stores rating as 0/51/102/153/204/255 for 0/1/2/3/4/5 stars.
_RATING_FORWARD: tuple[int, ...] = (0, 51, 102, 153, 204, 255)


def traktor_rating_to_stars(value: int | str | None) -> int | None:
    """Convert Traktor 0..255 rating to open-dj 0..5."""
    if value is None or value == "":
        return None
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    # Find the nearest slot.
    stars = max(0, min(5, round(raw / 51.0)))
    return stars


def stars_to_traktor_rating(stars: int | None) -> int | None:
    """Convert open-dj 0..5 rating back to Traktor 0..255 scale."""
    if stars is None:
        return None
    return _RATING_FORWARD[max(0, min(5, int(stars)))]


# -------------------------------------------------------------- cue map

# Traktor @TYPE enum on <CUE_V2>. Maps to open-dj cue type enum.
_CUE_TYPE_MAP: dict[int, Literal["hot", "fade_in", "fade_out", "load", "grid", "loop"]] = {
    0: "hot",
    1: "fade_in",
    2: "fade_out",
    3: "load",
    4: "grid",
    5: "loop",
}
_CUE_TYPE_REVERSE: dict[str, int] = {v: k for k, v in _CUE_TYPE_MAP.items()}


def traktor_cue_type_to_opendj(value: int | str | None):
    if value is None:
        return "hot"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return "hot"
    return _CUE_TYPE_MAP.get(n, "hot")


def opendj_cue_type_to_traktor(cue_type: str) -> int:
    return _CUE_TYPE_REVERSE.get(cue_type, 0)


# ------------------------------------------------------ cue colour palette

# Traktor does not carry a user-editable colour field on cues; the UI paints
# each TYPE in a fixed swatch. When we READ Traktor -> open-dj we synthesise
# ``color_rgb`` from the type enum. When we WRITE open-dj -> Traktor, user
# colours are dropped with a warning (adapter.py handles that).
_CUE_TYPE_COLOUR: dict[str, int] = {
    "hot": 0x2C6FB3,       # blue
    "loop": 0x33B25F,      # green
    "grid": 0xFFFFFF,      # white
    "load": 0xF2C94C,      # yellow
    "fade_in": 0xEB8C3A,   # orange
    "fade_out": 0xEB8C3A,
}


def cue_color_from_type(cue_type: str) -> int:
    return _CUE_TYPE_COLOUR.get(cue_type, 0x2C6FB3)
