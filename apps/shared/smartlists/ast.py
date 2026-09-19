"""Rule-AST walker helpers."""
from __future__ import annotations

from collections.abc import Iterator

from .schema import LOGICAL_OPS


def is_logical(node: dict) -> bool:
    return isinstance(node, dict) and node.get("op") in LOGICAL_OPS


def is_predicate(node: dict) -> bool:
    return (
        isinstance(node, dict)
        and "field" in node
        and "op" in node
        and not is_logical(node)
    )


def walk(rule: dict) -> Iterator[dict]:
    yield rule
    if is_logical(rule):
        for child in rule.get("children", []):
            yield from walk(child)


def referenced_fields(rule: dict) -> set[str]:
    fields: set[str] = set()
    for node in walk(rule):
        if is_predicate(node):
            fields.add(node["field"])
    return fields


__all__ = ["is_logical", "is_predicate", "referenced_fields", "walk"]
