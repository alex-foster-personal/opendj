"""Canonical simple-genre grouping (LIBUX-06).

.planning/REQUIREMENTS.md's LIBUX-06 entry calls out that the repo already
has an ad hoc "simple genre" grouping and asks that it be looked up and
recorded once as the definition, rather than reinvented per feature. The
only existing implementation is a hover-color helper on the frontend,
``apps/webui/frontend/src/lib/rb/genre-color.ts`` (its ``FAMILY`` table,
introduced in ``3dca5498a``). This module is that same 16-family table,
ported 1:1 (same match order, same regex intent, same colors) so the wheel's
branching structure and the library-row hover glow describe the same
grouping. There is no runtime path between a Python module and a SvelteKit
one, so this is a deliberate parallel port, not an import -- keep both lists
in sync by hand if either changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GenreFamily:
    name: str
    pattern: re.Pattern[str]
    color: str


def _family(name: str, pattern: str, color: str) -> GenreFamily:
    return GenreFamily(name, re.compile(pattern, re.IGNORECASE), color)


# Mirrors genre-color.ts FAMILY, in the same match order (first match wins).
SIMPLE_GENRE_FAMILIES: list[GenreFamily] = [
    _family("drum-and-bass", r"\b(drum\s*[&n]\s*bass|dnb|jungle|neurofunk|rollers?)\b", "#5ee07a"),
    _family("breakbeat-garage", r"\b(breakbeat|breaks|ukg|garage|2[- ]?step)\b", "#f0c14a"),
    _family("techno", r"\b(techno|industrial|schranz|peak\s*time)\b", "#5ec8ff"),
    _family("tech-house", r"\b(tech\s*house)\b", "#4ad0c8"),
    _family("deep-house", r"\b(deep\s*house|organic|melodic\s*house)\b", "#7aa8ff"),
    _family("house", r"\b(house|afro\s*house|jackin)\b", "#ff9a4a"),
    _family("trance", r"\b(trance|psytrance|progressive)\b", "#c084fc"),
    _family("hardstyle", r"\b(hardstyle|hardcore|gabber|frenchcore)\b", "#ff5a5a"),
    _family("dubstep", r"\b(dubstep|bass\s*music|riddim|tearout)\b", "#a78bfa"),
    _family("hip-hop", r"\b(hip[- ]?hop|rap|trap|grime|drill)\b", "#f472b6"),
    _family("disco-funk-soul", r"\b(disco|nu[- ]?disco|boogie|funk|soul)\b", "#fb923c"),
    _family("ambient-downtempo", r"\b(ambient|downtempo|chill|lo[- ]?fi)\b", "#94a3b8"),
    _family("electro-edm", r"\b(electro|edm|big\s*room|festival)\b", "#38bdf8"),
    _family("reggae", r"\b(reggae|dancehall|dub)\b", "#86efac"),
    _family("jazz-latin-world", r"\b(jazz|latin|afrobeat|world)\b", "#fbbf24"),
    _family("pop-rock-indie", r"\b(pop|rock|indie|metal)\b", "#e2e8f0"),
]


def simple_genre_family(tag: str | None) -> tuple[str, str] | None:
    """(family_name, color) for a raw genre tag, or None when no family matches.

    None also covers a missing/blank tag -- callers bucket those tracks as
    unclassified rather than guessing a family.
    """
    if tag is None:
        return None
    stripped = tag.strip()
    if stripped == "":
        return None
    for family in SIMPLE_GENRE_FAMILIES:
        if family.pattern.search(stripped):
            return (family.name, family.color)
    return None


__all__ = ["SIMPLE_GENRE_FAMILIES", "GenreFamily", "simple_genre_family"]

