"""Tests for the lighting A/B Bradley-Terry scorer.

[if] A/B session results are scored [then] adoption follows the spec 07 section 5 rule, [else stop].
"""

from __future__ import annotations

import pytest

from apps.lighting.ab_score import choose_pairwise, choose_triad, fit_bradley_terry

pytestmark = [pytest.mark.requirement("LIGHT-14478")]

DEFAULT = "placeholder"


def test_fit_is_deterministic_and_orders_by_wins() -> None:
    """[if] the same pairs are fitted twice in any order [then] scores are identical and rank by wins, [else stop]."""
    pairs = [("warm", "cool")] * 5 + [("cool", "warm")] * 1 + [("warm", "vivid")] * 4 + [("vivid", "cool")] * 3
    first = fit_bradley_terry(pairs)
    second = fit_bradley_terry(list(reversed(pairs)))
    assert first == second
    assert first["warm"] > first["vivid"] > first["cool"]


def test_adopts_only_top_score_with_60_percent_pair_wins() -> None:
    """[if] the top variant wins >=60% of its pairs [then] it is adopted, below 60% the default stays, [else stop]."""
    strong = [("punchy", "gentle")] * 7 + [("gentle", "punchy")] * 3
    assert choose_pairwise(strong, DEFAULT) == "punchy"
    weak = [("punchy", "gentle")] * 11 + [("gentle", "punchy")] * 9
    assert fit_bradley_terry(weak)["punchy"] > fit_bradley_terry(weak)["gentle"]
    assert choose_pairwise(weak, DEFAULT) == DEFAULT


def test_la3_triad_needs_7_of_12() -> None:
    """[if] an LA3 variant is chosen in 7 of 12 triads [then] it is adopted, with 6 of 12 the default stays, [else stop]."""
    seven = ["blend"] * 7 + ["upfader"] * 3 + ["hard"] * 2
    six = ["blend"] * 6 + ["upfader"] * 4 + ["hard"] * 2
    assert choose_triad(seven, DEFAULT) == "blend"
    assert choose_triad(six, DEFAULT) == DEFAULT


def test_no_clear_winner_returns_placeholder_default() -> None:
    """[if] results tie or are empty [then] the placeholder default is returned, [else stop]."""
    tie = [("a", "b"), ("b", "a")] * 5
    assert choose_pairwise(tie, DEFAULT) == DEFAULT
    assert choose_pairwise([], DEFAULT) == DEFAULT
    assert choose_triad([], DEFAULT) == DEFAULT
