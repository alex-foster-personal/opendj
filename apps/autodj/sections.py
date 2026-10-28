"""One section vocabulary for every phrase source.

Two producers fill ``/anlz`` ``phrases``: rekordbox's PSSI tag (``kind`` +
``mood`` integers, no label) and our own All-In-One sections (a ``label``
string, ``source: "own"``; STRUCT-02). A planner should not need to know which
one it got, so both map onto :data:`LABELS`.

PSSI kinds per mood follow rekordbox's phrase analysis as decoded by Deep
Symmetry's ``crate-digger`` / ``dysentery`` notes (the PSSI section of the ANLZ
format analysis). rekordbox shows High-mood phrases as Intro, Up, Down, Chorus
and Outro; for dance music a DJ reads those as intro, build, breakdown, drop
and outro, which is the vocabulary used here. ``raw`` keeps rekordbox's own
word so nothing is hidden.

This closes the canon doc's "PSSI kinds are not mapped to intro/build/drop/
outro" gap (``auto-play-next.ts:8``, "no PSSI").
"""

from __future__ import annotations

from typing import Final

INTRO: Final = "intro"
VERSE: Final = "verse"
BUILD: Final = "build"
CHORUS: Final = "chorus"
DROP: Final = "drop"
BREAKDOWN: Final = "breakdown"
BRIDGE: Final = "bridge"
INSTRUMENTAL: Final = "instrumental"
OUTRO: Final = "outro"
SILENCE: Final = "silence"
UNKNOWN: Final = "unknown"

LABELS: Final[tuple[str, ...]] = (
    INTRO,
    VERSE,
    BUILD,
    CHORUS,
    DROP,
    BREAKDOWN,
    BRIDGE,
    INSTRUMENTAL,
    OUTRO,
    SILENCE,
    UNKNOWN,
)

#: Sections a DJ mixes OUT of: the music is winding down or thin.
MIX_OUT_LABELS: Final = frozenset({OUTRO, BREAKDOWN, BRIDGE, INSTRUMENTAL})
#: Sections a DJ mixes IN over: sparse, usually beat-led, no hook yet.
MIX_IN_LABELS: Final = frozenset({INTRO, BUILD, INSTRUMENTAL})
#: Sections a DJ lands the incoming track's energy on.
PEAK_LABELS: Final = frozenset({DROP, CHORUS})

MOOD_HIGH: Final = 1
MOOD_MID: Final = 2
MOOD_LOW: Final = 3

# (raw rekordbox word, DJ label) per kind, per mood.
_PSSI: Final[dict[int, dict[int, tuple[str, str]]]] = {
    MOOD_HIGH: {
        1: ("Intro", INTRO),
        2: ("Up", BUILD),
        3: ("Down", BREAKDOWN),
        5: ("Chorus", DROP),
        6: ("Outro", OUTRO),
    },
    MOOD_MID: {
        1: ("Intro", INTRO),
        2: ("Verse 1", VERSE),
        3: ("Verse 2", VERSE),
        4: ("Verse 3", VERSE),
        5: ("Verse 4", VERSE),
        6: ("Verse 5", VERSE),
        7: ("Verse 6", VERSE),
        8: ("Bridge", BRIDGE),
        9: ("Chorus", CHORUS),
        10: ("Outro", OUTRO),
    },
    MOOD_LOW: {
        1: ("Intro", INTRO),
        2: ("Verse 1", VERSE),
        3: ("Verse 1", VERSE),
        4: ("Verse 1", VERSE),
        5: ("Verse 2", VERSE),
        6: ("Verse 2", VERSE),
        7: ("Verse 2", VERSE),
        8: ("Bridge", BRIDGE),
        9: ("Chorus", CHORUS),
        10: ("Outro", OUTRO),
    },
}

# All-In-One's label set (apps/analysis_structure/allin1_runner.py), plus the
# DJ labels the energy rules serve in its place (apps/analysis_structure/dj_labels.py).
_OWN: Final[dict[str, str]] = {
    "buildup": BUILD,
    "drop": DROP,
    "breakdown": BREAKDOWN,
    "cooldown": OUTRO,
    "start": SILENCE,
    "end": SILENCE,
    "intro": INTRO,
    "outro": OUTRO,
    "break": BREAKDOWN,
    "bridge": BRIDGE,
    "inst": INSTRUMENTAL,
    "solo": INSTRUMENTAL,
    "verse": VERSE,
    "chorus": CHORUS,
}


def pssi_label(kind: int, mood: int) -> tuple[str, str]:
    """``(raw, label)`` for one PSSI entry; unknown pairs say ``unknown``."""
    raw_label = _PSSI.get(mood, {}).get(kind)
    if raw_label is None:
        return (f"kind {kind} mood {mood}", UNKNOWN)
    return raw_label


def own_label(label: str) -> tuple[str, str]:
    """``(raw, label)`` for one own-structure section label."""
    return (label, _OWN.get(label.strip().lower(), UNKNOWN))


def phrase_label(phrase: dict[str, object]) -> tuple[str, str]:
    """The label of one ``/anlz`` phrase row, whichever producer wrote it."""
    label = phrase.get("label")
    if isinstance(label, str) and label:
        raw, mapped = own_label(label)
        model = phrase.get("model_label")  # the model's label, kept when a DJ label replaced it
        if mapped == UNKNOWN and isinstance(model, str) and model:
            return (raw, own_label(model)[1])
        return (raw, mapped)
    kind, mood = phrase.get("kind"), phrase.get("mood")
    if isinstance(kind, int) and isinstance(mood, int):
        return pssi_label(kind, mood)
    return ("", UNKNOWN)
