"""Rule-AST walker helpers."""

from __future__ import annotations

from collections.abc import Iterator

from .schema import LOGICAL_OPS, TOP_N_OP


def is_logical(node: dict) -> bool:
    return isinstance(node, dict) and node.get("op") in LOGICAL_OPS


def is_predicate(node: dict) -> bool:
    return isinstance(node, dict) and "field" in node and "op" in node and not is_logical(node)


def is_top_n(node: dict) -> bool:
    """True for a LIBM-85 top-N selection node."""
    return isinstance(node, dict) and node.get("op") == TOP_N_OP


def walk(rule: dict) -> Iterator[dict]:
    yield rule
    if is_logical(rule) or is_top_n(rule):
        for child in rule.get("children", []):
            yield from walk(child)


def referenced_fields(rule: dict) -> set[str]:
    fields: set[str] = set()
    for node in walk(rule):
        if is_predicate(node):
            fields.add(node["field"])
        elif is_top_n(node):
            # The sort field decides membership, so a change to it must re-evaluate the list.
            fields.add(node["sort_field"])
    return fields


__all__ = ["is_logical", "is_predicate", "is_top_n", "referenced_fields", "walk"]
