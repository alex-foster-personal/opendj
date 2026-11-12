"""Explicit hybrid ranking for collection search (issue #11146, parent #298).

Ranking policy (best band wins; never concatenate source lists):

1. Strong metadata title and/or artist matches (FTS over indexed title/artist).
2. High-confidence lyric line matches (lyric FTS hit on the committed index).
3. Soft metadata matches (genre, tags, comments, notes) and fuzzy-only lyric hits.
4. AI-suggested titles resolved to a library ``stable_id``.
5. AI suggestions labelled external (not library rows).

Within a band, ties break on play count (desc), recency (``last_played_at`` then
``modified_at``, desc), then BPM distance and Camelot key distance to an
optional loaded-deck reference when those fields exist, then ``stable_id`` asc
for stability.

Unresolved AI suggestions (no library id and no external label) are dropped and
never appear as library rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Literal

from apps.shared.harmonic import CamelotKey, camelot_distance
from apps.webui.server.track_features import camelot_or_none

SearchProvenance = Literal["metadata", "lyrics", "ai_library", "ai_external"]


class RankBand(IntEnum):
    """Lower numeric value ranks higher in the merged list."""

    METADATA_STRONG = 0
    LYRIC_HIGH = 1
    SOFT = 2
    AI_LIBRARY = 3
    AI_EXTERNAL = 4


@dataclass(frozen=True, slots=True)
class DeckReference:
    """Optional BPM/key anchor from a loaded deck for proximity tie-breaks."""

    bpm: float | None = None
    key: str | None = None


@dataclass(frozen=True, slots=True)
class TieBreakFields:
    play_count: int = 0
    last_played_at: str | None = None
    modified_at: str | None = None
    bpm: float | None = None
    key: str | None = None


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    stable_id: str
    match_context: str
    provenance: SearchProvenance
    band: RankBand
    tie: TieBreakFields = TieBreakFields()


@dataclass(frozen=True, slots=True)
class AiSuggestionInput:
    """Optional AI shortlist row before library resolution."""

    title_guess: str
    stable_id: str | None = None
    external_label: str | None = None


def _query_terms(query: str) -> list[str]:
    cleaned = query.strip().replace('"', "").replace("\x00", "")
    return [term for term in cleaned.split() if term]


def metadata_band(query: str, title: str, artist: str) -> RankBand:
    """Classify a metadata FTS hit from indexed title/artist text."""
    terms = _query_terms(query)
    if not terms:
        return RankBand.SOFT
    title_l = (title or "").lower()
    artist_l = (artist or "").lower()

    def term_hits_title_or_artist(term: str) -> bool:
        needle = term.lower()
        return needle in title_l or needle in artist_l

    if all(term_hits_title_or_artist(term) for term in terms):
        return RankBand.METADATA_STRONG
    return RankBand.SOFT


def lyric_band(*, fts_hit: bool) -> RankBand:
    return RankBand.LYRIC_HIGH if fts_hit else RankBand.SOFT


def ai_candidates_from_suggestions(
    suggestions: list[AiSuggestionInput],
) -> list[HybridCandidate]:
    """Turn AI suggestions into rankable rows; drop unresolved hallucinations."""
    out: list[HybridCandidate] = []
    for index, suggestion in enumerate(suggestions):
        if suggestion.stable_id:
            out.append(
                HybridCandidate(
                    stable_id=suggestion.stable_id,
                    match_context=suggestion.title_guess,
                    provenance="ai_library",
                    band=RankBand.AI_LIBRARY,
                )
            )
            continue
        if suggestion.external_label:
            out.append(
                HybridCandidate(
                    stable_id=f"external:{index}:{suggestion.external_label}",
                    match_context=suggestion.external_label,
                    provenance="ai_external",
                    band=RankBand.AI_EXTERNAL,
                )
            )
    return out


def _parse_iso_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def _recency_score(tie: TieBreakFields) -> float:
    last = _parse_iso_timestamp(tie.last_played_at)
    modified = _parse_iso_timestamp(tie.modified_at)
    return max(last, modified)


def _bpm_distance(bpm: float | None, deck: DeckReference | None) -> float:
    if deck is None or deck.bpm is None or bpm is None:
        return float("inf")
    return abs(float(bpm) - float(deck.bpm))


def _key_distance(key: str | None, deck: DeckReference | None) -> int:
    if deck is None or deck.key is None or not key:
        return 999
    ref_s = camelot_or_none(deck.key)
    cand_s = camelot_or_none(key)
    if ref_s is None or cand_s is None:
        return 999
    try:
        k1 = CamelotKey.from_string(ref_s)
        k2 = CamelotKey.from_string(cand_s)
    except ValueError:
        return 999
    return camelot_distance(k1, k2)


def _sort_key(candidate: HybridCandidate, deck: DeckReference | None) -> tuple:
    tie = candidate.tie
    return (
        int(candidate.band),
        -int(tie.play_count),
        -_recency_score(tie),
        _bpm_distance(tie.bpm, deck),
        _key_distance(tie.key, deck),
        candidate.stable_id,
    )


def _better_candidate(left: HybridCandidate, right: HybridCandidate, deck: DeckReference | None) -> HybridCandidate:
    return left if _sort_key(left, deck) <= _sort_key(right, deck) else right


def merge_hybrid_candidates(
    candidates: list[HybridCandidate],
    *,
    deck: DeckReference | None = None,
) -> list[HybridCandidate]:
    """Dedupe by ``stable_id``, keep the best band and tie-break, then sort."""
    by_id: dict[str, HybridCandidate] = {}
    for candidate in candidates:
        existing = by_id.get(candidate.stable_id)
        if existing is None:
            by_id[candidate.stable_id] = candidate
            continue
        by_id[candidate.stable_id] = _better_candidate(existing, candidate, deck)
    merged = list(by_id.values())
    merged.sort(key=lambda c: _sort_key(c, deck))
    return merged


__all__ = [
    "AiSuggestionInput",
    "DeckReference",
    "HybridCandidate",
    "RankBand",
    "SearchProvenance",
    "TieBreakFields",
    "ai_candidates_from_suggestions",
    "lyric_band",
    "merge_hybrid_candidates",
    "metadata_band",
]
