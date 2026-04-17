"""SMART-01 -- rule schema + validator tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.shared.smartlists import (
    SmartlistRuleError,
    referenced_fields,
    validate_rule,
)


pytestmark = pytest.mark.requirement("SMART-01")


FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "smartlists"


def test_canonical_rule_validates() -> None:
    rule = json.loads((FIXTURE_DIR / "canonical.json").read_text())
    validate_rule(rule)


def test_referenced_fields_walks_tree() -> None:
    rule = json.loads((FIXTURE_DIR / "canonical.json").read_text())
    assert referenced_fields(rule) == {"genre", "bpm", "rating", "added_date"}


def test_leaf_predicate_is_valid_root() -> None:
    rule = json.loads((FIXTURE_DIR / "high_energy.json").read_text())
    validate_rule(rule)
    assert referenced_fields(rule) == {"energy"}


def test_not_requires_single_child() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"op": "not", "children": []})
    with pytest.raises(SmartlistRuleError):
        validate_rule({
            "op": "not",
            "children": [
                {"field": "bpm", "op": ">", "value": 120},
                {"field": "bpm", "op": "<", "value": 130},
            ],
        })


def test_and_requires_children() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"op": "and"})
    with pytest.raises(SmartlistRuleError):
        validate_rule({"op": "and", "children": []})


def test_unknown_field_rejected() -> None:
    with pytest.raises(SmartlistRuleError) as excinfo:
        validate_rule({"field": "mystery", "op": "=", "value": "x"})
    assert "mystery" in str(excinfo.value)


def test_bad_op_for_field_rejected() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "bpm", "op": "contains", "value": 120})


def test_between_expects_list_of_two() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "bpm", "op": "between", "value": 120})
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "bpm", "op": "between", "value": [120]})


def test_in_expects_list() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "genre", "op": "in", "value": "House"})


def test_relative_date_accepted() -> None:
    validate_rule({
        "field": "added_date", "op": ">",
        "value": {"$relative": "-90d"},
    })


def test_number_field_rejects_string_value() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "bpm", "op": "=", "value": "fast"})


def test_roundtrip_through_json() -> None:
    rule = json.loads((FIXTURE_DIR / "canonical.json").read_text())
    again = json.loads(json.dumps(rule))
    validate_rule(again)
    assert referenced_fields(again) == referenced_fields(rule)


def test_error_path_is_reported() -> None:
    rule = {
        "op": "and",
        "children": [
            {"field": "genre", "op": "=", "value": "House"},
            {"field": "bpm", "op": "between", "value": 120},
        ],
    }
    with pytest.raises(SmartlistRuleError) as excinfo:
        validate_rule(rule)
    assert "children[1]" in str(excinfo.value)


def test_missing_value_key_rejected() -> None:
    with pytest.raises(SmartlistRuleError):
        validate_rule({"field": "bpm", "op": "="})
