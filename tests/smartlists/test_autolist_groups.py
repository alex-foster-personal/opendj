"""Autolist group compiler tests (issue #2066).

[if] autolist selections span genre, rating, and bpm [then] groups union and intersect, [else stop].
"""

from __future__ import annotations

import pytest

from apps.smartlists.autolist_groups import (
    GENRE_UNSPECIFIED_ID,
    RATING_UNRATED_ID,
    AutolistSelectionError,
    bpm_bucket_key,
    rating_bucket_key,
    selection_to_rule,
)
from apps.smartlists.evaluator import compile_rule, evaluate

pytestmark = pytest.mark.requirement("SMART-05")


@pytest.fixture
def seeded_library(fixture_library):
    fixture_library.add_many(
        [
            {"stable_id": "a", "fields": {"genre": "House", "bpm": 125, "rating": 5}},
            {"stable_id": "b", "fields": {"genre": "House", "bpm": 118, "rating": 3}},
            {"stable_id": "c", "fields": {"genre": "Techno", "bpm": 128, "rating": 5}},
            {"stable_id": "d", "fields": {"genre": "House", "bpm": 124, "rating": 2}},
            {"stable_id": "e", "fields": {"genre": "House", "bpm": 126, "rating": 5, "energy": 9}},
        ]
    )
    return fixture_library


def test_genre_bucket_house_via_selection_to_rule(seeded_library, state_conn) -> None:
    """#2066 genre-bucket regression: House autolist tag via selection_to_rule."""
    rule = selection_to_rule({"genre": ["House"], "rating": [], "bpm": []})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert set(result) == {"a", "b", "d", "e"}


def test_within_group_union(seeded_library, state_conn) -> None:
    rule = selection_to_rule({"genre": ["House", "Techno"], "rating": [], "bpm": []})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert set(result) == {"a", "b", "c", "d", "e"}


def test_across_group_intersection(seeded_library, state_conn) -> None:
    rule = selection_to_rule({"genre": ["House"], "rating": ["5"], "bpm": []})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert set(result) == {"a", "e"}


def test_bpm_window_120_129(seeded_library, state_conn) -> None:
    seeded_library.add_many(
        [
            {"stable_id": "in1", "fields": {"genre": "X", "bpm": 125, "rating": 1}},
            {"stable_id": "in2", "fields": {"genre": "X", "bpm": 128, "rating": 1}},
            {"stable_id": "in3", "fields": {"genre": "X", "bpm": 124, "rating": 1}},
            {"stable_id": "in4", "fields": {"genre": "X", "bpm": 126, "rating": 1}},
            {"stable_id": "out1", "fields": {"genre": "X", "bpm": 118, "rating": 1}},
        ]
    )
    rule = selection_to_rule({"genre": [], "rating": [], "bpm": ["120-129"]})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert set(result) >= {"in1", "in2", "in3", "in4"}
    assert "out1" not in set(result)
    assert bpm_bucket_key(130) == "130-139"


def test_unrated_includes_zero_and_missing(fixture_library, state_conn) -> None:
    fixture_library.add_many(
        [
            {"stable_id": "z0", "fields": {"genre": "X", "bpm": 120, "rating": 0}},
            {"stable_id": "zm", "fields": {"genre": "X", "bpm": 120}},
        ]
    )
    rule = selection_to_rule({"genre": [], "rating": [RATING_UNRATED_ID], "bpm": []})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert "z0" in result
    assert "zm" in result


def test_empty_selection_is_none() -> None:
    assert selection_to_rule({"genre": [], "rating": [], "bpm": []}) is None


def test_missing_op_compiles_to_is_null() -> None:
    from apps.analysis.selection import Selection

    rule = {"field": "genre", "op": "missing", "value": None}
    sql, _params = compile_rule(rule, selection=Selection.all_rbx())
    assert "IS NULL" in sql


def test_rating_bucket_key() -> None:
    assert rating_bucket_key(None) == RATING_UNRATED_ID
    assert rating_bucket_key(0) == RATING_UNRATED_ID
    assert rating_bucket_key(5) == "5"


def test_rating_non_numeric_bucket_raises() -> None:
    with pytest.raises(AutolistSelectionError) as exc_info:
        selection_to_rule({"genre": [], "rating": ["advtest"], "bpm": []})
    exc = exc_info.value
    assert exc.group == "rating"
    assert exc.field == "selection.rating"


def test_bpm_unknown_bucket_raises() -> None:
    with pytest.raises(AutolistSelectionError) as exc_info:
        selection_to_rule({"genre": [], "rating": [], "bpm": ["advtest"]})
    exc = exc_info.value
    assert exc.group == "bpm"
    assert exc.field == "selection.bpm"


def test_rating_numeric_string_still_compiles() -> None:
    rule = selection_to_rule({"genre": [], "rating": ["4"], "bpm": []})
    assert rule is not None
    assert rule == {"field": "rating", "op": "=", "value": 4}


def test_genre_unspecified_bucket(seeded_library, state_conn) -> None:
    seeded_library.add_many(
        [
            {"stable_id": "nog", "fields": {"bpm": 120, "rating": 1}},
        ]
    )
    rule = selection_to_rule({"genre": [GENRE_UNSPECIFIED_ID], "rating": [], "bpm": []})
    assert rule is not None
    result = evaluate(rule, state_conn)
    assert "nog" in result
    assert "a" not in result
