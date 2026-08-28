"""SMART-01 -- additional rule AST edge cases and walker semantics.

Complements ``test_schema.py`` with a second file covering the
``is_logical`` / ``is_predicate`` / ``walk`` helpers exported from
``apps.shared.smartlists.ast`` and a handful of deep-nesting cases
that exercise error-path reporting.
"""
from __future__ import annotations

import pytest

from apps.shared.smartlists import (
    SmartlistRuleError,
    is_logical,
    is_predicate,
    referenced_fields,
    validate_rule,
    walk,
)

pytestmark = pytest.mark.requirement("SMART-01")


def test_is_logical_matches_and_or_not_nodes() -> None:
    assert is_logical({"op": "and", "children": []}) is True
    assert is_logical({"op": "or", "children": []}) is True
    assert is_logical({"op": "not", "children": []}) is True


def test_is_logical_rejects_predicate_and_non_dict() -> None:
    assert is_logical({"field": "bpm", "op": "=", "value": 128}) is False
    assert is_logical("not a dict") is False  # type: ignore[arg-type]
    assert is_logical({}) is False


def test_is_predicate_requires_field_and_op_and_not_logical() -> None:
    assert is_predicate({"field": "bpm", "op": ">", "value": 120}) is True
    # Logical nodes are never predicates even if they somehow carry a field.
    assert is_predicate({"op": "and", "children": []}) is False
    # Missing op.
    assert is_predicate({"field": "bpm", "value": 120}) is False
    # Missing field.
    assert is_predicate({"op": ">", "value": 120}) is False


def test_walk_visits_nested_children_depth_first() -> None:
    rule = {
        "op": "and",
        "children": [
            {"field": "bpm", "op": "=", "value": 128},
            {
                "op": "or",
                "children": [
                    {"field": "genre", "op": "=", "value": "House"},
                    {"field": "rating", "op": ">=", "value": 4},
                ],
            },
        ],
    }
    nodes = list(walk(rule))
    # root + and(root) child1 + or + 2 or-children = 5 nodes total.
    assert len(nodes) == 5
    # The root comes first, then the AND's first child, then the nested OR.
    assert nodes[0] is rule
    assert nodes[1]["field"] == "bpm"
    assert nodes[2]["op"] == "or"


def test_referenced_fields_from_deeply_nested_not_tree() -> None:
    rule = {
        "op": "not",
        "children": [
            {
                "op": "and",
                "children": [
                    {"field": "genre", "op": "=", "value": "Tech House"},
                    {"field": "bpm", "op": "between", "value": [124, 130]},
                ],
            },
        ],
    }
    validate_rule(rule)
    assert referenced_fields(rule) == {"genre", "bpm"}


def test_validate_rule_rejects_unknown_logical_op() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"op": "xor", "children": [
            {"field": "bpm", "op": "=", "value": 120},
            {"field": "bpm", "op": "=", "value": 130},
        ]})


def test_validate_rule_reports_path_through_nested_or() -> None:
    rule = {
        "op": "or",
        "children": [
            {"field": "bpm", "op": "=", "value": 120},
            {
                "op": "and",
                "children": [
                    {"field": "genre", "op": "=", "value": "House"},
                    {"field": "bpm", "op": "between", "value": "not-a-list"},
                ],
            },
        ],
    }
    with pytest.raises(SmartlistRuleError) as excinfo:
        validate_rule(rule)
    # The failing predicate is at children[1].children[1].
    msg = str(excinfo.value)
    assert "children[1]" in msg


def test_validate_rule_rejects_non_dict_root() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule([])  # type: ignore[arg-type]
    with pytest.raises(SmartlistRuleError):
        validate_rule("root")  # type: ignore[arg-type]
