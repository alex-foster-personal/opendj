"""[if] hybrid rank policy changes [then] band order tests fail, [else stop]."""

from __future__ import annotations

import pytest

from apps.webui.server.hybrid_rank import (
    AiSuggestionInput,
    DeckReference,
    HybridCandidate,
    RankBand,
    TieBreakFields,
    ai_candidates_from_suggestions,
    lyric_band,
    merge_hybrid_candidates,
    metadata_band,
)

pytestmark = pytest.mark.requirement("SEARCH-NAV-13535")


def _cand(
    stable_id: str,
    *,
    band: RankBand,
    provenance: str = "metadata",
    play_count: int = 0,
    context: str = "",
) -> HybridCandidate:
    return HybridCandidate(
        stable_id=stable_id,
        match_context=context or stable_id,
        provenance=provenance,  # type: ignore[arg-type]
        band=band,
        tie=TieBreakFields(play_count=play_count),
    )


def test_metadata_strong_outranks_lyric_and_soft() -> None:
    """[if] strong title metadata and lyric-only compete [then] metadata ranks first, [else stop]."""
    ranked = merge_hybrid_candidates(
        [
            _cand("lyric", band=RankBand.LYRIC_HIGH, provenance="lyrics"),
            _cand("meta", band=RankBand.METADATA_STRONG, provenance="metadata"),
            _cand("soft", band=RankBand.SOFT, provenance="metadata"),
        ]
    )
    assert [c.stable_id for c in ranked] == ["meta", "lyric", "soft"]


def test_lyric_high_outranks_soft_fuzzy() -> None:
    """[if] lyric line and soft fuzzy compete [then] lyric ranks above soft, [else stop]."""
    ranked = merge_hybrid_candidates(
        [
            _cand("soft", band=RankBand.SOFT, provenance="metadata"),
            _cand("lyric", band=RankBand.LYRIC_HIGH, provenance="lyrics"),
        ]
    )
    assert [c.stable_id for c in ranked] == ["lyric", "soft"]


def test_unresolved_ai_suggestion_is_dropped() -> None:
    """[if] AI title has no library or external label [then] it is omitted, [else stop]."""
    accepted = ai_candidates_from_suggestions([AiSuggestionInput(title_guess="phantom track")])
    assert accepted == []


def test_resolved_ai_cannot_outrank_metadata_strong() -> None:
    """[if] library metadata strong and AI library compete [then] metadata wins, [else stop]."""
    ai = ai_candidates_from_suggestions([AiSuggestionInput(title_guess="AI pick", stable_id="ai1")])
    ranked = merge_hybrid_candidates(
        [_cand("hard", band=RankBand.METADATA_STRONG), *ai],
    )
    assert ranked[0].stable_id == "hard"


def test_tie_break_prefers_higher_play_count() -> None:
    """[if] same band ties [then] higher play_count ranks first, [else stop]."""
    ranked = merge_hybrid_candidates(
        [
            _cand("low", band=RankBand.SOFT, play_count=1),
            _cand("high", band=RankBand.SOFT, play_count=50),
        ]
    )
    assert [c.stable_id for c in ranked] == ["high", "low"]


def test_metadata_band_classifies_title_artist_terms() -> None:
    assert metadata_band("lantern", "Lantern Orchard", "Mira Valen") == RankBand.METADATA_STRONG
    assert metadata_band("synthwave", "Velvet Static", "The Lowlands") == RankBand.SOFT


def test_lyric_band_splits_fts_and_fuzzy() -> None:
    assert lyric_band(fts_hit=True) == RankBand.LYRIC_HIGH
    assert lyric_band(fts_hit=False) == RankBand.SOFT


def test_deck_bpm_proximity_breaks_ties() -> None:
    deck = DeckReference(bpm=128.0)
    ranked = merge_hybrid_candidates(
        [
            HybridCandidate(
                stable_id="far",
                match_context="",
                provenance="metadata",
                band=RankBand.SOFT,
                tie=TieBreakFields(bpm=140.0),
            ),
            HybridCandidate(
                stable_id="near",
                match_context="",
                provenance="metadata",
                band=RankBand.SOFT,
                tie=TieBreakFields(bpm=129.0),
            ),
        ],
        deck=deck,
    )
    assert ranked[0].stable_id == "near"
