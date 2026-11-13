"""Smartlist top-N rule with an explicit sort field (issue #12318, spec LIBM-85).

[if] a top_n node names a positive n and an allowed sort field [then] the evaluator returns that bounded set, [else stop].
"""

from __future__ import annotations

import pytest

from apps.shared.smartlists import SmartlistRuleError, referenced_fields, validate_rule
from apps.smartlists.evaluator import evaluate
from apps.webui.server.routes.smartlists import summarize_rule

pytestmark = pytest.mark.requirement("LIBM-14235")


@pytest.fixture
def dated_library(fixture_library):
    fixture_library.add_many(
        [
            {"stable_id": "a", "created_at": "2026-01-01T00:00:00+00:00", "fields": {"genre": "House", "rating": 5}},
            {"stable_id": "b", "created_at": "2026-02-01T00:00:00+00:00", "fields": {"genre": "House", "rating": 3}},
            {"stable_id": "c", "created_at": "2026-03-01T00:00:00+00:00", "fields": {"genre": "Techno", "rating": 5}},
            {"stable_id": "d", "created_at": "2026-04-01T00:00:00+00:00", "fields": {"genre": "House", "rating": 2}},
            {"stable_id": "e", "created_at": "2026-05-01T00:00:00+00:00", "fields": {"genre": "House", "rating": 4}},
        ]
    )
    return fixture_library


def test_newest_three_tracks(dated_library, state_conn) -> None:
    """[if] a smartlist asks for the newest three tracks [then] it returns exactly the three latest added, [else stop]."""
    rule = {"op": "top_n", "n": 3, "sort_field": "added_date"}
    validate_rule(rule)
    assert evaluate(rule, state_conn) == ["e", "d", "c"]


def test_highest_rated_two_tracks_breaks_ties_newest_first(dated_library, state_conn) -> None:
    """[if] a smartlist asks for the highest-rated two tracks [then] the two 5-star tracks return, tie newest first, [else stop]."""
    rule = {"op": "top_n", "n": 2, "sort_field": "rating"}
    assert evaluate(rule, state_conn, order_by="added_date desc") == ["c", "a"]
    assert sorted(evaluate(rule, state_conn, order_by="random")) == ["a", "c"]


def test_top_n_applies_to_its_child_rule_only(dated_library, state_conn) -> None:
    """[if] top_n wraps a genre rule [then] the limit picks from matching tracks only, [else stop]."""
    rule = {
        "op": "top_n",
        "n": 2,
        "sort_field": "added_date",
        "children": [{"field": "genre", "op": "=", "value": "House"}],
    }
    assert evaluate(rule, state_conn) == ["e", "d"]


@pytest.mark.parametrize("n", [0, -1, 2.5, True, "3", None])
def test_top_n_rejects_zero_negative_or_non_integer_n(n) -> None:
    """[if] a top-n value is zero, negative or not a whole number [then] validation raises SmartlistRuleError, [else stop]."""
    with pytest.raises(SmartlistRuleError, match=r"root\.n"):
        validate_rule({"op": "top_n", "n": n, "sort_field": "added_date"})


@pytest.mark.parametrize("sort_field", ["bpm", "title", "random", None])
def test_top_n_rejects_unsupported_sort_field(sort_field) -> None:
    """[if] a top-n payload names an unsupported sort field [then] validation raises SmartlistRuleError, [else stop]."""
    with pytest.raises(SmartlistRuleError, match=r"root\.sort_field"):
        validate_rule({"op": "top_n", "n": 3, "sort_field": sort_field})


@pytest.mark.parametrize(
    "extra",
    [{"direction": "asc"}, {"value": 3}, {"field": "rating"}, {"order_by": "bpm asc"}],
)
def test_top_n_refuses_clauses_it_cannot_honour(extra) -> None:
    """[if] a top_n node carries a key it does not support [then] validation fails loudly, not drops it, [else stop]."""
    with pytest.raises(SmartlistRuleError, match="top_n does not support"):
        validate_rule({"op": "top_n", "n": 3, "sort_field": "rating", **extra})


def test_top_n_takes_at_most_one_child() -> None:
    """[if] a top_n node has zero or two children [then] validation raises SmartlistRuleError, [else stop]."""
    child = {"field": "genre", "op": "=", "value": "House"}
    for children in ([], [child, child], child):
        with pytest.raises(SmartlistRuleError, match=r"root\.children"):
            validate_rule({"op": "top_n", "n": 3, "sort_field": "rating", "children": children})


def test_top_n_sort_field_counts_as_referenced() -> None:
    """[if] a top_n rule sorts by rating [then] rating is a referenced field so a rating change re-evaluates it, [else stop]."""
    rule = {"op": "top_n", "n": 3, "sort_field": "rating", "children": [{"field": "genre", "op": "=", "value": "x"}]}
    assert referenced_fields(rule) == {"genre", "rating"}


def test_top_n_rule_saves_through_the_repo(smartlists_repo) -> None:
    """[if] a top_n rule is saved [then] the repo stores it with its sort field referenced, [else stop]."""
    row = smartlists_repo.create("Newest 3", {"op": "top_n", "n": 3, "sort_field": "added_date"})
    assert row.rule["op"] == "top_n"
    assert "added_date" in row.referenced_fields


def test_top_n_rule_has_a_readable_summary() -> None:
    """[if] the smartlist list summarises a top_n rule [then] it reads TOP n BY field, not a server error, [else stop]."""
    child = {"field": "genre", "op": "=", "value": "House"}
    assert summarize_rule({"op": "top_n", "n": 3, "sort_field": "added_date"}) == "TOP 3 BY added_date"
    rule = {"op": "top_n", "n": 2, "sort_field": "rating", "children": [child]}
    assert summarize_rule(rule) == 'TOP 2 BY rating OF genre = "House"'
