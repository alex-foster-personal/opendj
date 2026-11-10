"""Deterministic suggest-next explainer (issue #10399).

[if] explain sees measured terms [then] the line names the Camelot rule and steps, [else stop].
"""

from __future__ import annotations

import pytest

from apps.dj_copilot.explainer import (
    MAX_EXPLAIN_CHARS,
    RULE_LABELS,
    explain,
    join_terms,
)
from apps.shared.harmonic import CAMELOT_RULES, CLASH_RELATION, TrackFeature, key_compat

pytestmark = pytest.mark.requirement("SETPREP-10723")

# One (from_key, to_key) pair per CAMELOT_RULES id, plus a clash.
# Taken from each rule's documented example in harmonic.CAMELOT_RULES.
RULE_FIXTURES: tuple[tuple[str, str, str], ...] = (
    ("8A", "8A", "same_key"),
    ("8A", "9A", "adjacent"),
    ("8A", "8B", "relative"),
    ("8B", "9A", "diagonal"),
    ("8A", "10A", "energy_boost"),
    ("8A", "6A", "energy_drop"),
    ("8A", "3A", "jaws"),
    ("8A", "5A", "pay_attention"),
    ("8A", "9B", "wrong_way_diagonal"),
    ("8B", "5A", "parallel"),
    ("8A", "11A", "minor_third_down"),
    ("10B", "2B", "major_third"),
    ("8A", "1A", "semitone_down"),
    ("8A", "2A", "tritone"),
    ("8A", "3B", CLASH_RELATION),
)


def _tf(
    *,
    stable_id: str = "cur",
    bpm: float | None = 126.0,
    key: str | None = "8A",
    energy: int | None = 6,
) -> TrackFeature:
    return TrackFeature(stable_id, "Artist", bpm, key, energy)


def test_every_camelot_rule_has_a_label() -> None:
    """A new CAMELOT_RULES entry without an explainer label fails here."""
    for rule in CAMELOT_RULES:
        assert rule.id in RULE_LABELS, f"missing explainer label for {rule.id}"
        assert RULE_LABELS[rule.id].strip(), f"empty explainer label for {rule.id}"
    assert CLASH_RELATION in RULE_LABELS
    assert RULE_LABELS[CLASH_RELATION] == "clash"


def test_fixtures_cover_every_camelot_rule() -> None:
    covered = {rule_id for _, _, rule_id in RULE_FIXTURES}
    expected = {rule.id for rule in CAMELOT_RULES} | {CLASH_RELATION}
    assert covered == expected


def test_issue_example_line() -> None:
    line = explain(
        _tf(bpm=126.0, key="8A", energy=6),
        _tf(stable_id="nxt", bpm=128.0, key="9A", energy=7),
        {"pair_manual": 1.0},
    )
    assert line == "8A to 9A, adjacent; 126 to 128 BPM (+1.6%); energy 6 to 7; paired manually"


@pytest.mark.parametrize(("from_key", "to_key", "rule_id"), RULE_FIXTURES)
def test_each_rule_names_its_label(from_key: str, to_key: str, rule_id: str) -> None:
    assert key_compat(from_key, to_key).relation == rule_id
    line = explain(
        _tf(key=from_key),
        _tf(stable_id="nxt", key=to_key),
    )
    expected = f"{from_key} to {to_key}, {RULE_LABELS[rule_id]}"
    assert line.startswith(expected)
    assert "harmonic match" not in line
    assert "BPM locked" not in line


def test_unscored_terms_are_named_unknown() -> None:
    line = explain(
        _tf(bpm=None, key=None, energy=None),
        _tf(stable_id="nxt", bpm=128.0, key="9A", energy=7),
    )
    assert line == "key unknown; BPM unknown; energy unknown"
    assert "harmonic match" not in line
    assert "adjacent key" not in line
    assert "BPM locked" not in line
    assert "BPM in range" not in line


def test_unscored_key_keeps_measured_bpm_and_energy() -> None:
    line = explain(
        _tf(bpm=126.0, key=None, energy=6),
        _tf(stable_id="nxt", bpm=128.0, key="9A", energy=7),
    )
    assert line == "key unknown; 126 to 128 BPM (+1.6%); energy 6 to 7"


def test_pairing_omitted_when_not_measured() -> None:
    line = explain(_tf(), _tf(stable_id="nxt", key="9A", bpm=128.0, energy=7))
    assert "paired" not in line
    assert line == "8A to 9A, adjacent; 126 to 128 BPM (+1.6%); energy 6 to 7"


def test_learned_and_ai_pairing_labels() -> None:
    learned = explain(_tf(), _tf(stable_id="nxt", key="8A"), {"pair_learned": 1.0})
    assert learned.endswith("paired from history")
    ai = explain(_tf(), _tf(stable_id="nxt", key="8A"), {"pair_ai": 1.0})
    assert ai.endswith("paired by AI")


def test_manual_pairing_wins_over_weaker_sources() -> None:
    line = explain(
        _tf(),
        _tf(stable_id="nxt", key="8A"),
        {"pair_manual": 1.0, "pair_learned": 1.0, "pair_ai": 1.0},
    )
    assert line.endswith("paired manually")
    assert "paired from history" not in line
    assert "paired by AI" not in line


@pytest.mark.parametrize(("from_key", "to_key", "rule_id"), RULE_FIXTURES)
def test_fixture_rows_stay_within_120_chars(from_key: str, to_key: str, rule_id: str) -> None:
    line = explain(
        _tf(key=from_key, bpm=126.0, energy=6),
        _tf(stable_id="nxt", key=to_key, bpm=128.0, energy=7),
        {"pair_manual": 1.0},
    )
    assert len(line) <= MAX_EXPLAIN_CHARS
    # Truncation, if it ever triggers, must drop a whole term.
    for term in line.split("; "):
        assert term
        assert not term.startswith(" ")
        assert not term.endswith(" ")


def test_join_terms_never_cuts_inside_a_term() -> None:
    assert join_terms(["aa", "bb", "cc"], limit=8) == "aa; bb"
    assert join_terms(["hello world", "x"], limit=10) == "hello world"
    assert join_terms(["short", "also fine"], limit=120) == "short; also fine"
