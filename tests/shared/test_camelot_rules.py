"""Camelot mixing rules behind the compatible filter's checkboxes (Tue 27 Oct 2026).

The old filter allowed 0, 1 or 2 wheel steps and ignored the A/B letter, so
8A matched 9B and 7B under "±1". Every rule is now a move from the reference
key with an explicit letter relation. Regression lines:
- if a rule matches by number alone then 8A -> 9B passes "±1" again
- if 12 <-> 1 does not wrap then 12A loses 1A as a neighbour
- if the browser's rule table drifts from harmonic.py then the UI and the
  server disagree about what a ticked box means
"""

from __future__ import annotations

import pytest

from apps.shared.harmonic import (
    CAMELOT_RULE_IDS,
    DEFAULT_CAMELOT_RULE_IDS,
    camelot_rule_matches,
    camelot_rules_allow,
)
from scripts.gen_camelot_rules_ts import TS_PATH, render_camelot_rules_ts

# (rule, reference, candidates that match, candidates that must not)
CASES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("same_key", "8A", ("8A", "Am"), ("8B", "9A", "7A")),
    ("same_key", "8B", ("8B", "C"), ("8A",)),
    ("adjacent", "8A", ("7A", "9A"), ("9B", "7B", "8A", "10A", "8B")),
    ("adjacent", "12A", ("11A", "1A"), ("1B", "12B")),
    ("adjacent", "1B", ("12B", "2B"), ("12A", "2A")),
    ("relative", "8A", ("8B",), ("8A", "9B", "7B")),
    ("relative", "8B", ("8A",), ("9A", "7A")),
    ("diagonal", "8B", ("9A",), ("7A", "9B", "8A")),
    ("diagonal", "8A", ("7B",), ("9B", "7A", "8B")),
    ("diagonal", "12B", ("1A",), ("11A",)),
    ("diagonal", "1A", ("12B",), ("2B",)),
    ("energy_boost", "8A", ("10A",), ("6A", "10B", "9A")),
    ("energy_boost", "11B", ("1B",), ("1A", "9B")),
    ("energy_drop", "8A", ("6A",), ("10A", "6B")),
    ("energy_drop", "2B", ("12B",), ("4B", "12A")),
    ("jaws", "8A", ("3A",), ("3B", "1A", "8A")),
    ("jaws", "1B", ("8B",), ("6B", "8A")),
    ("pay_attention", "8A", ("5A",), ("11A", "5B")),
    ("pay_attention", "2B", ("11B",), ("5B", "11A")),
)


@pytest.mark.parametrize(("rule", "reference", "yes", "no"), CASES)
def test_each_rule_matches_its_move_and_respects_the_letter(
    rule: str, reference: str, yes: tuple[str, ...], no: tuple[str, ...]
) -> None:
    """[if] a rule is checked against its move [then] only that move, with that letter, matches."""
    for candidate in yes:
        assert camelot_rule_matches(rule, reference, candidate), f"{rule}: {reference} -> {candidate}"
    for candidate in no:
        assert not camelot_rule_matches(rule, reference, candidate), f"{rule}: {reference} -/-> {candidate}"


def test_every_rule_has_a_case() -> None:
    """[if] a rule is added [then] it needs a case here, or this fails."""
    assert {case[0] for case in CASES} == set(CAMELOT_RULE_IDS)


def test_defaults_are_same_adjacent_relative() -> None:
    """[if] nothing is chosen [then] same key, ±1 same letter and relative are on, the rest off."""
    assert DEFAULT_CAMELOT_RULE_IDS == ("same_key", "adjacent", "relative")


def test_rules_allow_is_an_or_over_enabled_rules() -> None:
    """[if] several rules are enabled [then] a candidate passes when any one matches."""
    assert camelot_rules_allow("8A", "9B", ["relative", "adjacent"]) is False
    assert camelot_rules_allow("8A", "9B", ["relative", "adjacent", "diagonal"]) is False
    assert camelot_rules_allow("8B", "9A", ["relative", "diagonal"]) is True
    assert camelot_rules_allow("8A", "8A", []) is False


def test_unknown_rule_and_bad_key_raise() -> None:
    """[if] a rule id or key is unknown [then] it raises instead of reading as "no match"."""
    with pytest.raises(ValueError, match="unknown Camelot rule"):
        camelot_rule_matches("tritone", "8A", "2A")
    with pytest.raises(ValueError):
        camelot_rule_matches("same_key", "8A", "13A")
    # if an earlier rule matches first then a later unknown id must still raise
    with pytest.raises(ValueError, match="unknown Camelot rule"):
        camelot_rules_allow("8A", "8A", ["same_key", "bogus"])


def test_browser_rule_table_is_generated_from_harmonic() -> None:
    """[if] camelot-rules.generated.ts differs from harmonic.py [then] UI and server disagree."""
    assert TS_PATH.read_text(encoding="utf-8") == render_camelot_rules_ts(), (
        "run: uv run --no-sync python -m scripts.gen_camelot_rules_ts"
    )
