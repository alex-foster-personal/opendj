"""#1515 LYR-01: real JamendoLyrics timings through the ratified scorer.

The synthetic Ship-tier fixture (``ship_tier.json``, ``test_scorer.py``) proves
the scorer's contract. This one proves what the aligner actually does: the
round3a best-of predictions, scored index-aligned against ground-truth word
onsets, with the measured values pinned as a RATCHET.

The corpus does NOT meet Ship tier and this file says so out loud
(:func:`test_corpus_verdict_is_recorded_as_measured`). medae_s and pco_300ms
clear their thresholds; catastrophe_rate is 5.2% against a 3.0% ceiling, so
these numbers are the honest baseline a later alignment round has to beat, not
a pass.

Timings only. No lyric text, no song titles: the alignment spike spec forbids
committing lyrics, and every ``track_id`` is an opaque digest.

Regression lines:
- if the scorer's arithmetic drifts then per-track measured values stop matching -- broken
- if a later round regresses these songs then the ratchet fails -- broken
- if the corpus figures are quietly restated as a Ship pass then broken
- if the fixture ever carries lyric text or a song title then broken
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

FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "lyrics_alignment" / "jamendo_round3a_measured.json"
)
RATCHET_TOLERANCE = 1e-9

# The measured LYR-01 corpus row, Wed 9 Sep 2026: round3a best-of predictions
# over 79/79 JamendoLyrics songs, 21,580 reference words, none unplaced.
CORPUS_MEDAE_S = 0.038095238
CORPUS_PCO_300MS = 0.914226135
CORPUS_CATASTROPHE_RATE = 0.051992586
CORPUS_WORDS = 21580


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_every_track_reproduces_its_measured_score(fixture: dict) -> None:
    """If the scorer's arithmetic drifts then these stop matching."""
    for track in fixture["tracks"]:
        score = lyric_align_score(track["reference_onsets_s"], track["predicted_onsets_s"])
        measured = track["measured"]
        assert score.medae_s == pytest.approx(measured["medae_s"], abs=RATCHET_TOLERANCE), (
            track["track_id"]
        )
        assert score.pco_300ms == pytest.approx(measured["pco_300ms"], abs=RATCHET_TOLERANCE), (
            track["track_id"]
        )
        assert score.catastrophe_rate == pytest.approx(
            measured["catastrophe_rate"], abs=RATCHET_TOLERANCE
        ), track["track_id"]
        assert score.words_reference == measured["words_reference"], track["track_id"]


def test_forced_alignment_places_every_reference_word(fixture: dict) -> None:
    """If a word goes unplaced then recall drops and the denominators change."""
    for track in fixture["tracks"]:
        score = lyric_align_score(track["reference_onsets_s"], track["predicted_onsets_s"])
        assert score.words_unplaced == 0, track["track_id"]
        assert score.recall == pytest.approx(1.0), track["track_id"]


def test_sample_pooled_is_the_ratchet(fixture: dict) -> None:
    """If a later alignment round regresses these songs then this fails.

    Pooled over the sampled songs only. It is a regression pin, never a corpus
    estimate: the fixture's own warning says so, and the sample can meet Ship
    tier while the corpus does not.
    """
    reference = [t for track in fixture["tracks"] for t in track["reference_onsets_s"]]
    predicted = [t for track in fixture["tracks"] for t in track["predicted_onsets_s"]]
    pooled = lyric_align_score(reference, predicted)
    recorded = fixture["sample_pooled"]

    assert pooled.words_reference == recorded["words_reference"]
    assert pooled.medae_s <= recorded["medae_s"] + RATCHET_TOLERANCE
    assert pooled.pco_300ms >= recorded["pco_300ms"] - RATCHET_TOLERANCE
    assert pooled.catastrophe_rate <= recorded["catastrophe_rate"] + RATCHET_TOLERANCE
    assert "NOT an estimate of the corpus" in recorded["warning"]


def test_corpus_verdict_is_recorded_as_measured(fixture: dict) -> None:
    """If the LYR-01 corpus row is restated as a Ship pass then broken.

    Two of three thresholds clear; catastrophe_rate does not. Reporting this
    as measured rather than as a pass is the whole point of D13.5.
    """
    corpus = fixture["corpus_measured"]

    assert corpus["words_reference"] == CORPUS_WORDS
    assert corpus["words_unplaced"] == 0
    assert corpus["medae_s"] == pytest.approx(CORPUS_MEDAE_S, abs=1e-6)
    assert corpus["pco_300ms"] == pytest.approx(CORPUS_PCO_300MS, abs=1e-6)
    assert corpus["catastrophe_rate"] == pytest.approx(CORPUS_CATASTROPHE_RATE, abs=1e-6)

    assert corpus["medae_s"] <= SHIP_MEDAE_S_MAX, "medae cleared Ship at measurement time"
    assert corpus["pco_300ms"] >= SHIP_PCO_300MS_MIN, "pco cleared Ship at measurement time"
    assert corpus["catastrophe_rate"] > SHIP_CATASTROPHE_RATE_MAX, (
        "catastrophe_rate did NOT clear Ship; if a round fixes it, restate the "
        "recorded corpus row rather than deleting this assertion"
    )
    assert corpus["meets_ship_tier"] is False


def test_fixture_carries_timings_only(fixture: dict) -> None:
    """If lyric text or a title ever lands in the fixture then broken."""
    allowed = {"track_id", "measured", "reference_onsets_s", "predicted_onsets_s"}
    for track in fixture["tracks"]:
        assert set(track) == allowed, track["track_id"]
        assert track["track_id"].startswith("jl-"), "track ids must stay opaque digests"
        assert all(
            isinstance(t, (int, float)) for t in track["reference_onsets_s"]
        ), track["track_id"]
