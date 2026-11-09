"""Section resolver: ``/anlz`` phrases onto one lighting section vocabulary (LIGHT-03 in the spec).

Two producers fill ``/api/v1/tracks/{id}/anlz`` ``phrases``: rekordbox's PSSI tag (``kind`` and
``mood`` integers, no label) and our own sections (a ``label`` string such as intro, buildup, drop,
breakdown, cooldown, outro; ``apps/analysis_structure/dj_labels.py:65``). A look chooser should not
care which one it got, so both map onto :class:`LightSection`.

The PSSI kind table mirrors ``apps/autodj/sections.py:61`` (``_PSSI``) rather than importing it:
outside code reaches a package only through its contract (``.importlinter``
``module-public-surface``). Unlike autodj, lighting keeps rekordbox's own word: a PSSI Chorus is
``chorus`` here, never relabelled ``drop``; only our own sections say ``drop``. Track mood mimics
rekordbox HIGH / MID / LOW. An unknown kind, mood or label resolves to ``unknown``, never a guess.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final


class LightSection(StrEnum):
    INTRO = "intro"
    VERSE = "verse"
    BUILD = "build"
    CHORUS = "chorus"
    DROP = "drop"
    BREAKDOWN = "breakdown"
    BRIDGE = "bridge"
    OUTRO = "outro"
    UNKNOWN = "unknown"


class TrackMood(StrEnum):
    HIGH = "high"
    MID = "mid"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SectionSpan:
    start_ms: float
    end_ms: float
    section: LightSection
    mood: TrackMood
    raw: str


@dataclass(frozen=True)
class SectionChange:
    at_ms: float
    section: LightSection
    previous: LightSection
    mood: TrackMood


_S = LightSection
_MOODS: Final[dict[int, TrackMood]] = {1: TrackMood.HIGH, 2: TrackMood.MID, 3: TrackMood.LOW}

# (rekordbox word, section) per kind, per PSSI mood; mirrors apps/autodj/sections.py:61.
_PSSI: Final[dict[TrackMood, dict[int, tuple[str, LightSection]]]] = {
    TrackMood.HIGH: {
        1: ("Intro", _S.INTRO),
        2: ("Up", _S.BUILD),
        3: ("Down", _S.BREAKDOWN),
        5: ("Chorus", _S.CHORUS),
        6: ("Outro", _S.OUTRO),
    },
    TrackMood.MID: {
        1: ("Intro", _S.INTRO),
        2: ("Verse 1", _S.VERSE),
        3: ("Verse 2", _S.VERSE),
        4: ("Verse 3", _S.VERSE),
        5: ("Verse 4", _S.VERSE),
        6: ("Verse 5", _S.VERSE),
        7: ("Verse 6", _S.VERSE),
        8: ("Bridge", _S.BRIDGE),
        9: ("Chorus", _S.CHORUS),
        10: ("Outro", _S.OUTRO),
    },
    TrackMood.LOW: {
        1: ("Intro", _S.INTRO),
        2: ("Verse 1", _S.VERSE),
        3: ("Verse 1", _S.VERSE),
        4: ("Verse 1", _S.VERSE),
        5: ("Verse 2", _S.VERSE),
        6: ("Verse 2", _S.VERSE),
        7: ("Verse 2", _S.VERSE),
        8: ("Bridge", _S.BRIDGE),
        9: ("Chorus", _S.CHORUS),
        10: ("Outro", _S.OUTRO),
    },
}

# Own-section labels (dj_labels.DJ_LABELS plus the All-In-One set); mirrors apps/autodj/sections.py:97.
_OWN: Final[dict[str, LightSection]] = {
    "intro": _S.INTRO,
    "buildup": _S.BUILD,
    "drop": _S.DROP,
    "breakdown": _S.BREAKDOWN,
    "break": _S.BREAKDOWN,
    "cooldown": _S.OUTRO,
    "outro": _S.OUTRO,
    "bridge": _S.BRIDGE,
    "verse": _S.VERSE,
    "chorus": _S.CHORUS,
}


# ----------------------------------------------------------------------------- helpers


def _resolve_phrase(phrase: Mapping[str, Any]) -> tuple[str, LightSection, TrackMood]:
    label = phrase.get("label")
    if isinstance(label, str) and label:
        return (label, _OWN.get(label.strip().lower(), _S.UNKNOWN), TrackMood.UNKNOWN)
    kind, mood_code = phrase.get("kind"), phrase.get("mood")
    if isinstance(kind, int) and isinstance(mood_code, int):
        mood = track_mood(mood_code)
        word, section = _PSSI.get(mood, {}).get(kind, (f"kind {kind} mood {mood_code}", _S.UNKNOWN))
        return (word, section, mood)
    return ("", _S.UNKNOWN, TrackMood.UNKNOWN)


def _ms(seconds: Any) -> float:
    return round(float(seconds) * 1000.0, 3)


# ----------------------------------------------------------------------------- public api


def track_mood(pssi_mood: int | None) -> TrackMood:
    """rekordbox PSSI mood 1 / 2 / 3 as HIGH / MID / LOW; anything else is unknown."""
    if pssi_mood is None:
        return TrackMood.UNKNOWN
    return _MOODS.get(pssi_mood, TrackMood.UNKNOWN)


def resolve_sections(anlz_payload: Mapping[str, Any], duration_s: float | None = None) -> list[SectionSpan]:
    """Spans in time order. No phrases gives one ``unknown`` span over the track (to ``duration_s``, else open-ended)."""
    phrases = anlz_payload.get("phrases") or []
    if not phrases:
        end_ms = _ms(duration_s) if duration_s is not None else math.inf
        return [SectionSpan(0.0, end_ms, _S.UNKNOWN, TrackMood.UNKNOWN, "")]
    spans = []
    for phrase in phrases:
        raw, section, mood = _resolve_phrase(phrase)
        spans.append(SectionSpan(_ms(phrase["start_s"]), _ms(phrase["end_s"]), section, mood, raw))
    return sorted(spans, key=lambda span: span.start_ms)


def section_events(spans: Sequence[SectionSpan], from_ms: float, to_ms: float) -> list[SectionChange]:
    """One change per span boundary inside the half-open window ``(from_ms, to_ms]``."""
    return [
        SectionChange(at_ms=span.start_ms, section=span.section, previous=prior.section, mood=span.mood)
        for prior, span in pairwise(spans)
        if from_ms < span.start_ms <= to_ms
    ]
