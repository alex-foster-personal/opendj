"""Regression tests for apps/lyrics/asr_match.py (pure, no dataset needed).

- if match_words does not compute rates/gaps from a hand-checked diff then broken
- if normalization stops folding case or stripping edge punctuation then broken
- if internal apostrophes (French elisions) get stripped then broken
- if empty input is accepted then broken
"""

from __future__ import annotations

import pytest

from apps.lyrics.asr_match import match_words, normalize_word


def test_normalize_word() -> None:
    assert normalize_word("Fire!") == "fire", "if edge punctuation survives then broken"
    assert normalize_word("«Oui,»") == "oui", "if unicode edge punctuation survives then broken"
    assert normalize_word("l'hiver") == "l'hiver", (
        "if internal apostrophes get stripped then broken"
    )


def test_match_words_hand_checked() -> None:
    # ref: 6 tokens; asr misses 'that' (index 2..3 gap of 1) and appends 2 extras.
    ref = ["Find", "that", "fire,", "find", "that", "fire"]
    asr = ["find", "that", "fire", "find", "fire", "whoa", "whoa"]
    r = match_words(ref, asr)
    assert r.n_ref == 6 and r.n_asr == 7, "if token counts drift then broken"
    assert r.n_matched == 5, "if LCS != 5 for this fixture then broken"
    assert r.ref_match_rate == pytest.approx(5 / 6), "if ref-match != 5/6 then broken"
    assert r.asr_match_rate == pytest.approx(5 / 7), "if asr-match != 5/7 then broken"
    assert r.longest_ref_gap == 1, "if the single dropped 'that' is not gap 1 then broken"
    assert r.longest_asr_gap == 2, "if the trailing whoa-whoa is not gap 2 then broken"


def test_match_words_disjoint_streams() -> None:
    r = match_words(["uno", "dos", "tres"], ["alpha", "beta"])
    assert r.n_matched == 0, "if disjoint streams match then broken"
    assert r.longest_ref_gap == 3 and r.longest_asr_gap == 2, "if full-gap runs wrong then broken"


def test_match_words_rejects_empty() -> None:
    with pytest.raises(ValueError, match="no reference words"):
        match_words([], ["a"])
    with pytest.raises(ValueError, match="no ASR words"):
        match_words(["a"], [])
