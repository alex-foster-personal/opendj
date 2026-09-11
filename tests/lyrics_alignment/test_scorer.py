"""Acceptance tests for the lyric-onset alignment scorer.

The fixture data contains timings only. Lyrics themselves are never committed,
as required by the lyric-alignment spike spec.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.lyrics_alignment.scorer import (
    SHIP_CATASTROPHE_RATE_MAX,
    SHIP_MEDAE_S_MAX,
    SHIP_PCO_300MS_MIN,
    lyric_align_score,
)

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "lyrics_alignment" / "ship_tier.json"


def test_exact_onsets_score_perfectly() -> None:
    """If onsets agree exactly then every accuracy metric is perfect."""
    score = lyric_align_score([0.0, 1.0, 2.0], [0.0, 1.0, 2.0])

    assert score.medae_s == pytest.approx(0.0)
    assert score.pco_300ms == pytest.approx(1.0)
    assert score.catastrophe_rate == pytest.approx(0.0)
    assert score.words_scored == 3
    assert score.words_reference == 3
    assert score.words_unplaced == 0
    assert score.recall == pytest.approx(1.0)


def test_median_is_robust_to_a_single_catastrophe() -> None:
    """If one word is 2.1 seconds late then median and catastrophe stay distinct."""
    score = lyric_align_score([0.0, 1.0, 2.0, 3.0, 4.0], [0.1, 1.1, 2.1, 3.1, 6.1])

    assert score.medae_s == pytest.approx(0.1)
    assert score.pco_300ms == pytest.approx(0.8)
    assert score.catastrophe_rate == pytest.approx(0.2)


def test_threshold_boundaries_are_inclusive_except_catastrophes() -> None:
    """If error is exactly 300 ms or 2 s then spec boundary semantics hold."""
    score = lyric_align_score([0.0, 1.0, 2.0], [0.3, 0.7, 4.000000001])

    assert score.pco_300ms == pytest.approx(2 / 3)
    assert score.catastrophe_rate == pytest.approx(1 / 3)


def test_exact_two_second_error_is_not_a_catastrophe() -> None:
    """If error is exactly 2.0 s then it does not count toward catastrophe_rate."""
    score = lyric_align_score([0.0, 1.0, 2.0], [2.0, 1.0, 2.0])

    assert score.catastrophe_rate == pytest.approx(0.0)


def test_unplaced_words_are_not_silently_scored_as_accurate() -> None:
    """If an onset is absent then it reduces recall but not the accuracy denominator."""
    score = lyric_align_score([0.0, 1.0, 2.0], [0.1, None, 2.1])

    assert score.words_scored == 2
    assert score.words_unplaced == 1
    assert score.recall == pytest.approx(2 / 3)
    assert score.medae_s == pytest.approx(0.1)


def test_mismatched_word_rows_fail_loudly() -> None:
    """If words cannot be paired by index then the scorer refuses invented matches."""
    with pytest.raises(ValueError, match="same number"):
        lyric_align_score([0.0], [0.0, 1.0])


def test_lyric_align_score_rejects_non_finite_and_negative_onsets() -> None:
    with pytest.raises(ValueError, match="finite track second"):
        lyric_align_score([1.0], [float("inf")])
    with pytest.raises(ValueError, match="finite track second"):
        lyric_align_score([1.0], [float("-inf")])
    with pytest.raises(ValueError, match="finite track second"):
        lyric_align_score([-0.1], [0.0])
    with pytest.raises(ValueError, match="finite track second"):
        lyric_align_score([0.0], [-0.1])
    lyric_align_score([0.0], [0.0])


def test_ship_tier_fixture_meets_all_ratified_thresholds() -> None:
    """If the committed Ship fixture regresses then the quality gate fails."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for track in fixture["tracks"]:
        score = lyric_align_score(track["reference_onsets_s"], track["predicted_onsets_s"])
        assert score.medae_s <= SHIP_MEDAE_S_MAX, track["track_id"]
        assert score.pco_300ms >= SHIP_PCO_300MS_MIN, track["track_id"]
        assert score.catastrophe_rate <= SHIP_CATASTROPHE_RATE_MAX, track["track_id"]
