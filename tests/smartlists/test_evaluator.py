"""SMART-02 -- evaluator compile + evaluate tests."""
from __future__ import annotations

import pytest

from apps.analysis.selection import Selection
from apps.shared.smartlists import SmartlistRuleError
from apps.smartlists.evaluator import EvaluatorError, compile_rule, evaluate

# These compile-only tests state the selection explicitly: compile_rule has
# no default, so a rule can never be compiled against an unstated source.
RBX = Selection.all_rbx()

pytestmark = pytest.mark.requirement("SMART-02")


@pytest.fixture
def seeded_library(fixture_library):
    fixture_library.add_many([
        {"stable_id": "a", "fields": {"genre": "House", "bpm": 125, "rating": 5}},
        {"stable_id": "b", "fields": {"genre": "House", "bpm": 118, "rating": 3}},
        {"stable_id": "c", "fields": {"genre": "Techno", "bpm": 128, "rating": 5}},
        {"stable_id": "d", "fields": {"genre": "House", "bpm": 124, "rating": 2}},
        {"stable_id": "e", "fields": {"genre": "House", "bpm": 126, "rating": 5, "energy": 9}},
    ])
    return fixture_library


def test_compile_and_tree() -> None:
    rule = {
        "op": "and",
        "children": [
            {"field": "genre", "op": "=", "value": "House"},
            {"field": "bpm", "op": "between", "value": [120, 128]},
        ],
    }
    sql, params = compile_rule(rule, selection=RBX)
    assert " AND " in sql
    assert params == ["House", 120, 128]


def test_compile_is_deterministic() -> None:
    rule = {"field": "bpm", "op": ">=", "value": 128}
    s1, p1 = compile_rule(rule, selection=RBX)
    s2, p2 = compile_rule(rule, selection=RBX)
    assert s1 == s2 and p1 == p2


def test_compile_paired_with_equality() -> None:
    sql, params = compile_rule(
        {"field": "paired_with", "op": "=", "value": "anchor-id"}
    , selection=RBX)
    assert "SELECT to_stable_id FROM pairings" in sql
    assert params == ["anchor-id", "anchor-id"]


def test_compile_paired_with_in_list() -> None:
    sql, params = compile_rule(
        {"field": "paired_with", "op": "in", "value": ["a", "b", "c"]}
    , selection=RBX)
    assert "IN (?, ?, ?)" in sql
    assert params == ["a", "b", "c", "a", "b", "c"]


def test_compile_contains_string_uses_like() -> None:
    sql, params = compile_rule(
        {"field": "genre", "op": "contains", "value": "House"}
    , selection=RBX)
    assert "LIKE ?" in sql
    assert params == ["%House%"]


def test_compile_contains_list_uses_json_each() -> None:
    sql, params = compile_rule(
        {"field": "custom_tags", "op": "contains", "value": "peak"}
    , selection=RBX)
    assert "json_each" in sql
    assert params == ["peak"]


def test_simple_genre_filter(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "=", "value": "House"}, state_conn,
    )
    assert set(result) == {"a", "b", "d", "e"}


def test_and_composition(seeded_library, state_conn) -> None:
    result = evaluate(
        {
            "op": "and",
            "children": [
                {"field": "genre", "op": "=", "value": "House"},
                {"field": "bpm", "op": "between", "value": [120, 128]},
                {"field": "rating", "op": ">=", "value": 4},
            ],
        },
        state_conn,
    )
    assert set(result) == {"a", "e"}


def test_or_composition(seeded_library, state_conn) -> None:
    result = evaluate(
        {
            "op": "or",
            "children": [
                {"field": "genre", "op": "=", "value": "Techno"},
                {"field": "rating", "op": "=", "value": 2},
            ],
        },
        state_conn,
    )
    assert set(result) == {"c", "d"}


def test_not_composition(seeded_library, state_conn) -> None:
    result = evaluate(
        {
            "op": "not",
            "children": [
                {"field": "genre", "op": "=", "value": "House"},
            ],
        },
        state_conn,
    )
    assert set(result) == {"c"}


def test_between(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "bpm", "op": "between", "value": [124, 126]},
        state_conn,
    )
    assert set(result) == {"a", "d", "e"}


def test_in_list(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "in", "value": ["House", "Techno"]},
        state_conn,
    )
    assert set(result) == {"a", "b", "c", "d", "e"}


def test_empty_in_list_matches_nothing(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "in", "value": []},
        state_conn,
    )
    assert result == []


def test_custom_tags_contains(fixture_library, state_conn) -> None:
    fixture_library.add_track("x")
    fixture_library.add_field("x", "custom_tags", ["peak", "afro"])
    fixture_library.add_track("y")
    fixture_library.add_field("y", "custom_tags", ["warmup"])
    result = evaluate(
        {"field": "custom_tags", "op": "contains", "value": "peak"},
        state_conn,
    )
    assert result == ["x"]


def test_order_by_bpm_desc(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "=", "value": "House"},
        state_conn, order_by="bpm desc",
    )
    assert result == ["e", "a", "d", "b"]


def test_order_by_bpm_asc(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "=", "value": "House"},
        state_conn, order_by="bpm asc",
    )
    assert result == ["b", "d", "a", "e"]


def test_order_by_rejects_unknown(seeded_library, state_conn) -> None:
    with pytest.raises(EvaluatorError):
        evaluate(
            {"field": "genre", "op": "=", "value": "House"},
            state_conn, order_by="title asc",
        )


def test_limit_clamps_result(seeded_library, state_conn) -> None:
    result = evaluate(
        {"field": "genre", "op": "=", "value": "House"},
        state_conn, order_by="bpm desc", limit=2,
    )
    assert len(result) == 2


def test_sql_injection_probe_is_parameterised(
    fixture_library, state_conn,
) -> None:
    fixture_library.add_track("mal")
    fixture_library.add_field("mal", "genre", "'; DROP TABLE tracks; --")
    result = evaluate(
        {
            "field": "genre", "op": "=",
            "value": "'; DROP TABLE tracks; --",
        },
        state_conn,
    )
    assert result == ["mal"]
    rows = state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
    assert rows[0] >= 1


def test_paired_with_equality_joins_pairings(
    fixture_library, state_conn, pairings_repo_slm,
) -> None:
    fixture_library.add_track("anchor")
    fixture_library.add_track("b_paired")
    fixture_library.add_track("c_paired")
    fixture_library.add_track("d_unrelated")
    pairings_repo_slm.add("anchor", "b_paired", direction="into")
    pairings_repo_slm.add("anchor", "c_paired", direction="either")
    pairings_repo_slm.add("other", "d_unrelated", direction="into")
    result = evaluate(
        {"field": "paired_with", "op": "=", "value": "anchor"},
        state_conn,
    )
    assert set(result) == {"b_paired", "c_paired"}


def test_paired_with_ignores_out_of(
    fixture_library, state_conn, pairings_repo_slm,
) -> None:
    fixture_library.add_track("anchor")
    fixture_library.add_track("neighbour")
    pairings_repo_slm.add("anchor", "neighbour", direction="out_of")
    result = evaluate(
        {"field": "paired_with", "op": "=", "value": "anchor"},
        state_conn,
    )
    assert result == []


def test_paired_with_reads_reverse_stored_edges(
    fixture_library, state_conn, pairings_repo_slm,
) -> None:
    """If anchor's pairing is stored as (x, anchor, out_of|either) then x matches, else stop."""
    for sid in ("anchor", "b_rev", "c_rev", "d_into_anchor"):
        fixture_library.add_track(sid)
    pairings_repo_slm.add("b_rev", "anchor", direction="out_of")
    pairings_repo_slm.add("c_rev", "anchor", direction="either")
    pairings_repo_slm.add("d_into_anchor", "anchor", direction="into")
    for rule, expected in (
        ({"op": "=", "value": "anchor"}, {"b_rev", "c_rev"}),
        ({"op": "in", "value": ["anchor"]}, {"b_rev", "c_rev"}),
        ({"op": "!=", "value": "anchor"}, {"anchor", "d_into_anchor"}),
    ):
        result = evaluate({"field": "paired_with", **rule}, state_conn)
        assert set(result) == expected, rule


def test_evaluate_validates_by_default(state_conn) -> None:
    with pytest.raises(SmartlistRuleError):
        evaluate({"field": "mystery", "op": "=", "value": "x"}, state_conn)


def test_relative_date_compiles(fixture_library, state_conn) -> None:
    fixture_library.add_track("recent")
    result = evaluate(
        {
            "field": "added_date", "op": ">",
            "value": {"$relative": "-90d"},
        },
        state_conn,
    )
    assert "recent" in result
