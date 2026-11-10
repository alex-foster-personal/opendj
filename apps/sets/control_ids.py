"""Stable control ids for set recording v2 (spec: (private path) via PR #10726).

The engine (`apps/audio-engine/src/engine.rs`) and page each log v2 rows for
controls they own. Mixer ids captured on the engine path are listed in
`ENGINE_MIXER_CONTROL_IDS`; the page must not emit v2 rows for those ids when
the command reaches the engine (SETREC-05).
"""

from __future__ import annotations

from typing import Final

#: All mixer control ids the rust engine records (SETREC-05).
ENGINE_MIXER_CONTROL_IDS: Final[frozenset[str]] = frozenset(
    {
        "deck.1.trim",
        "deck.2.trim",
        "deck.3.trim",
        "deck.4.trim",
        "deck.1.auto_gain",
        "deck.2.auto_gain",
        "deck.3.auto_gain",
        "deck.4.auto_gain",
        "deck.1.eq.low",
        "deck.1.eq.mid",
        "deck.1.eq.high",
        "deck.2.eq.low",
        "deck.2.eq.mid",
        "deck.2.eq.high",
        "deck.3.eq.low",
        "deck.3.eq.mid",
        "deck.3.eq.high",
        "deck.4.eq.low",
        "deck.4.eq.mid",
        "deck.4.eq.high",
        "deck.1.filter",
        "deck.2.filter",
        "deck.3.filter",
        "deck.4.filter",
        "deck.1.fader",
        "deck.2.fader",
        "deck.3.fader",
        "deck.4.fader",
        "deck.1.assign",
        "deck.2.assign",
        "deck.3.assign",
        "deck.4.assign",
        "mixer.crossfader",
        "mixer.master_volume",
        "mixer.master_mute",
    }
)

#: Mixer ids the performance page may log when the engine reports `unsupported`.
PAGE_MIXER_CONTROL_IDS: Final[frozenset[str]] = frozenset()

#: Scripted mixer session coverage denominator (spec section 11 family P2).
SCRIPTED_MIXER_CONTROL_IDS: Final[tuple[str, ...]] = tuple(sorted(ENGINE_MIXER_CONTROL_IDS))


def assert_engine_page_mixer_disjoint() -> None:
    """Fail when any mixer id would be logged by both engine and page."""
    overlap = ENGINE_MIXER_CONTROL_IDS & PAGE_MIXER_CONTROL_IDS
    if overlap:
        raise AssertionError(f"mixer control ids claimed by both engine and page: {sorted(overlap)}")


# ---------------------------------------------------------------------------
# Stem family (SETREC-08): page-owned via performance-ipc (Web Audio path).
# ---------------------------------------------------------------------------

STEM_PARTS: Final[tuple[str, ...]] = ("vocal", "instrumental", "drums", "bass", "other")


def stem_gain_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem gain id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.gain"


def stem_mute_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem mute id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.mute"


def stem_solo_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem solo id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.solo"


STEM_CONTINUOUS_IDS: Final[frozenset[str]] = frozenset(stem_gain_id(d, p) for d in range(1, 5) for p in STEM_PARTS)

STEM_SET_IDS: Final[frozenset[str]] = frozenset(
    {stem_gain_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
    | {stem_mute_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
    | {stem_solo_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
)

SCRIPTED_STEM_CONTROL_IDS: Final[tuple[str, ...]] = tuple(
    id_ for d, p in ((1, part) for part in STEM_PARTS) for id_ in (stem_mute_id(1, p), stem_gain_id(1, p))
) + (
    stem_solo_id(1, "vocal"),
    stem_mute_id(2, "vocal"),
    stem_gain_id(2, "instrumental"),
)
