"""Rule-AST schema + validator (SMART-01).

Hand-rolled validator (no ``jsonschema`` dep); small surface, explicit
errors with a dotted path to the offending node.
"""
from __future__ import annotations

from typing import Any


class SmartlistRuleError(ValueError):
    """Raised when a rule fails schema validation.

    Message includes a dotted path (``root.children[1].value``).
    """


FIELD_TYPES: dict[str, str] = {
    "genre": "string",
    "bpm": "number",
    "key": "string",
    "rating": "number",
    "energy": "number",
    "custom_tags": "list",
    "color_tag": "string",
    "added_date": "date",
    "last_played": "date",
    "paired_with": "string",
}
ALLOWED_FIELDS: tuple[str, ...] = tuple(FIELD_TYPES.keys())

_NUMERIC_OPS: tuple[str, ...] = ("=", "!=", "<", "<=", ">", ">=", "between")
_STRING_OPS: tuple[str, ...] = ("=", "!=", "contains", "in")
_DATE_OPS: tuple[str, ...] = ("=", "<", "<=", ">", ">=", "between")
_LIST_OPS: tuple[str, ...] = ("contains", "in")

ALLOWED_OPS_BY_FIELD: dict[str, tuple[str, ...]] = {
    "genre": _STRING_OPS,
    "bpm": _NUMERIC_OPS,
    "key": _STRING_OPS,
    "rating": _NUMERIC_OPS,
    "energy": _NUMERIC_OPS,
    "custom_tags": _LIST_OPS,
    "color_tag": _STRING_OPS,
    "added_date": _DATE_OPS,
    "last_played": _DATE_OPS,
    "paired_with": ("=", "!=", "in"),
}

LOGICAL_OPS: tuple[str, ...] = ("and", "or", "not")


def _err(path: str, msg: str) -> SmartlistRuleError:
    return SmartlistRuleError(f"{path}: {msg}")


def _is_relative_date(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value.keys()) == {"$relative"}
        and isinstance(value["$relative"], str)
    )


def _validate_predicate(node: dict, path: str) -> None:
    field = node.get("field")
    op = node.get("op")
    if "value" not in node:
        raise _err(path, "predicate missing 'value'")
    value = node["value"]
    if field not in ALLOWED_FIELDS:
        raise _err(f"{path}.field",
                   f"unknown field {field!r}; expected one of {ALLOWED_FIELDS}")
    allowed_ops = ALLOWED_OPS_BY_FIELD[field]
    if op not in allowed_ops:
        raise _err(f"{path}.op",
                   f"op {op!r} not allowed for field {field!r}; "
                   f"expected one of {allowed_ops}")
    if op == "between":
        if not (isinstance(value, list) and len(value) == 2):
            raise _err(f"{path}.value", "between op expects [lo, hi] list")
    elif op == "in":
        if not isinstance(value, list):
            raise _err(f"{path}.value", "in op expects a list of values")
    elif op == "contains":
        if not isinstance(value, str):
            raise _err(f"{path}.value",
                       "contains op expects a string value")
    else:
        ftype = FIELD_TYPES[field]
        if ftype == "number" and not isinstance(value, (int, float)):
            raise _err(f"{path}.value",
                       f"field {field!r} expects a number")
        if ftype == "string" and not isinstance(value, str):
            raise _err(f"{path}.value",
                       f"field {field!r} expects a string")
        if ftype == "date" and not (
            isinstance(value, str) or _is_relative_date(value)
        ):
            raise _err(f"{path}.value",
                       "date fields accept an ISO-8601 string or a "
                       "{'$relative': '-Nd'} object")


def _validate_node(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise _err(path, f"expected object, got {type(node).__name__}")
    if "op" in node and node["op"] in LOGICAL_OPS:
        op = node["op"]
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise _err(f"{path}.children",
                       f"logical op {op!r} requires a non-empty 'children' list")
        if op == "not" and len(children) != 1:
            raise _err(f"{path}.children",
                       "'not' requires exactly one child")
        for i, child in enumerate(children):
            _validate_node(child, f"{path}.children[{i}]")
        return
    if "field" not in node or "op" not in node:
        raise _err(path,
                   "node is neither a logical op ({and,or,not} with children) "
                   "nor a predicate ({field, op, value})")
    _validate_predicate(node, path)


def validate_rule(rule: Any) -> None:
    """Raise :class:`SmartlistRuleError` if ``rule`` is malformed."""
    _validate_node(rule, "root")


__all__ = [
    "ALLOWED_FIELDS",
    "ALLOWED_OPS_BY_FIELD",
    "FIELD_TYPES",
    "LOGICAL_OPS",
    "SmartlistRuleError",
    "validate_rule",
]
