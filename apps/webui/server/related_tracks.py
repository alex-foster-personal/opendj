"""Related Tracks scorer (LIBUX-10046): pure rules, no I/O.

Mimics rekordbox Related Tracks: every collection track except the seed
scores one point per ENABLED rule it matches; tracks scoring 0 are dropped;
best first, ties by BPM distance, then stable_id.

Rules:
  bpm    -- within ``bpm_percent`` of the seed BPM, or of half / double it (always on)
  key    -- same or adjacent Camelot key
  genre  -- same genre (casefolded)
  color  -- same rekordbox ColorID (0 = no colour, never matches)
  rating -- candidate rating equal or higher than the seed's (unrated seed never matches)
  mytag  -- at least one shared tag
  artist -- any credited artist in common

Defaults (the #9753 set): bpm 6 percent, key on, genre on, everything else off.

Acceptance:
  [if] bpm_percent=4 and the candidate is 5 percent off [then ⛔️ bpm in matched]
  [if] seed rated 0 and rating enabled [then ⛔️ rating in matched]
  [if] the seed itself is a candidate [then ⛔️ it is ranked]
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from apps.shared.harmonic import camelot_rules_allow

from .backend import Track
from .track_features import camelot_or_none

RULE_NAMES: tuple[str, ...] = ("bpm", "key", "genre", "color", "rating", "mytag", "artist")
BPM_PERCENT_MIN = 1
BPM_PERCENT_MAX = 20
_KEY_RULES = ("same_key", "adjacent")
_BPM_MULTIPLIERS = (0.5, 1.0, 2.0)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|;|\bfeat\.?|\bft\.?)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class RelatedCriteria:
    bpm_percent: int = 6
    key: bool = True
    genre: bool = True
    color: bool = False
    rating: bool = False
    mytag: bool = False
    artist: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> RelatedCriteria:
        return cls(**{k: raw[k] for k in asdict(DEFAULT_RELATED_CRITERIA) if k in raw})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_RELATED_CRITERIA = RelatedCriteria()


@dataclass(frozen=True)
class RelatedCandidate:
    stable_id: str
    bpm: float | None
    camelot: str | None
    genre: str | None
    rating: int | None
    tags: frozenset[str]
    artists: frozenset[str]
    color_id: int | None


def _norm(text: str | None) -> str | None:
    folded = (text or "").strip().casefold()
    return folded or None


def credited_artists(artist: str | None) -> frozenset[str]:
    if not artist:
        return frozenset()
    return frozenset(p.strip().casefold() for p in _ARTIST_SPLIT.split(artist) if p.strip())


def candidate_from_track(track: Track, color_id: int | None = None) -> RelatedCandidate:
    return RelatedCandidate(
        stable_id=track.stable_id,
        bpm=track.bpm,
        camelot=camelot_or_none(track.key),
        genre=_norm(track.genre),
        rating=track.rating,
        tags=frozenset(t for t in (_norm(tag) for tag in track.tags) if t),
        artists=credited_artists(track.artist),
        color_id=color_id,
    )


# ----------------------------------------------------------------- rules


def bpm_distance_pct(seed_bpm: float | None, cand_bpm: float | None) -> float | None:
    """Percent off the nearest of seed x0.5, x1, x2; None when either BPM is unknown."""
    if seed_bpm is None or cand_bpm is None or seed_bpm <= 0 or cand_bpm <= 0:
        return None
    return min(abs(cand_bpm - seed_bpm * m) / (seed_bpm * m) * 100 for m in _BPM_MULTIPLIERS)


def _match_bpm(seed: RelatedCandidate, cand: RelatedCandidate, pct: int) -> bool:
    distance = bpm_distance_pct(seed.bpm, cand.bpm)
    return distance is not None and distance <= pct


def _match_key(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return bool(seed.camelot and cand.camelot and camelot_rules_allow(seed.camelot, cand.camelot, _KEY_RULES))


def _match_genre(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return seed.genre is not None and seed.genre == cand.genre


def _match_color(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return bool(seed.color_id) and seed.color_id == cand.color_id


def _match_rating(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return bool(seed.rating) and cand.rating is not None and cand.rating >= (seed.rating or 0)


def _match_mytag(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return bool(seed.tags & cand.tags)


def _match_artist(seed: RelatedCandidate, cand: RelatedCandidate) -> bool:
    return bool(seed.artists & cand.artists)


def matched_rules(seed: RelatedCandidate, cand: RelatedCandidate, criteria: RelatedCriteria) -> tuple[str, ...]:
    checks = {
        "bpm": _match_bpm(seed, cand, criteria.bpm_percent),
        "key": criteria.key and _match_key(seed, cand),
        "genre": criteria.genre and _match_genre(seed, cand),
        "color": criteria.color and _match_color(seed, cand),
        "rating": criteria.rating and _match_rating(seed, cand),
        "mytag": criteria.mytag and _match_mytag(seed, cand),
        "artist": criteria.artist and _match_artist(seed, cand),
    }
    return tuple(name for name in RULE_NAMES if checks[name])


# ----------------------------------------------------------------- ranking


def rank_related(
    seed: RelatedCandidate, candidates: Iterable[RelatedCandidate], criteria: RelatedCriteria
) -> list[tuple[RelatedCandidate, tuple[str, ...]]]:
    scored = [
        (cand, matched)
        for cand in candidates
        if cand.stable_id != seed.stable_id and (matched := matched_rules(seed, cand, criteria))
    ]

    def _order(item: tuple[RelatedCandidate, tuple[str, ...]]) -> tuple[int, float, str]:
        cand, matched = item
        distance = bpm_distance_pct(seed.bpm, cand.bpm)
        return (-len(matched), float("inf") if distance is None else distance, cand.stable_id)

    return sorted(scored, key=_order)
