"""The scope import graph the planner reads, its cycles, and what the global helpers carry.

Supersedes: nothing. A new read-only report; the numbers it prints had no runnable home.

The analysis behind finding 4 of `(private path)`, committed in round 12 of
the SMARTEST-CI counter so the numbers in `(private path)` can be re-run rather than
trusted. Read-only; prints a report. Same readers as `scripts/ci_plan.py`, so it cannot
disagree with the plan.

    uv run python -m scripts.ci_plan_layering
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from scripts.ci_plan import (
    REPO,
    Config,
    _consumed_scopes,
    _owner_index,
    _test_owner_index,
    matches,
    read_config,
)
from scripts.ci_plan_carry import helper_carried_scopes
from scripts.ci_plan_sources import _read_source, imported_modules, is_test_module, pytest_inputs

Edge = tuple[str, str]


def scope_edges(config: Config, root: Path = REPO) -> dict[Edge, list[str]]:
    """(importing scope, imported scope) -> the import sites, as `path -> module`."""
    owner = _owner_index(config, sources_only=True)
    edges: dict[Edge, list[str]] = defaultdict(list)
    for scope in config.scopes:
        for pattern in scope.sources:
            base = root / pattern.rstrip("/")
            if not base.is_dir():
                continue
            for file in sorted(base.rglob("*.py")):
                where = file.relative_to(root).as_posix()
                if owner(where) is not scope:
                    continue
                for module in imported_modules(_read_source(file, where), where):
                    other = owner(module.replace(".", "/") + "/")
                    if other is not None and other.name != scope.name:
                        edges[(scope.name, other.name)].append(f"{where} -> {module}")
    return dict(edges)


def strongly_connected(nodes: Iterable[str], edges: Iterable[Edge]) -> list[list[str]]:
    """Tarjan. Every component of size > 1 is an import cycle."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for other in adjacency[node]:
            if other not in index:
                visit(other)
                low[node] = min(low[node], low[other])
            elif other in on_stack:
                low[node] = min(low[node], index[other])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.append(popped)
                if popped == node:
                    break
            components.append(sorted(component))

    for node in nodes:
        if node not in index:
            visit(node)
    return sorted(components, key=len, reverse=True)


def helper_direct_scopes(config: Config, root: Path = REPO) -> dict[str, list[str]]:
    """Global helper -> the scopes it consumes directly. The closure is what it carries."""
    owner_of = _test_owner_index(config)
    found: dict[str, list[str]] = {}
    for file in pytest_inputs(root / "tests"):
        relative = file.relative_to(root).as_posix()
        if owner_of(relative) is not None or not matches(relative, config.always):
            continue
        if is_test_module(relative):
            continue
        names = [s.name for s in _consumed_scopes(_read_source(file, relative), relative, config)]
        if names:
            found[relative] = names
    return found


def closure(start: Iterable[str], edges: Iterable[Edge]) -> frozenset[str]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        adjacency[a].add(b)
    seen = set(start)
    todo = list(seen)
    while todo:
        node = todo.pop()
        for other in adjacency[node]:
            if other not in seen:
                seen.add(other)
                todo.append(other)
    return frozenset(seen)


def main() -> int:
    config = read_config()
    edges = scope_edges(config)
    names = [s.name for s in config.scopes]
    sites = sum(len(v) for v in edges.values())
    print(f"scopes {len(names)}  edges {len(edges)}  import sites {sites}")
    largest = strongly_connected(names, edges)[0]
    inside = [e for e in edges if e[0] in largest and e[1] in largest]
    print(f"largest cycle {len(largest)} scopes, {len(inside)} edges: {', '.join(largest)}")
    for edge in sorted(inside, key=lambda e: (len(edges[e]), e)):
        print(f"  {len(edges[edge]):3d} sites  {edge[0]} -> {edge[1]}")
    print("global helpers, direct scopes and closure:")
    for helper, direct in helper_direct_scopes(config).items():
        print(f"  {helper}: {direct} -> {len(closure(direct, edges))} scopes")
    carried = helper_carried_scopes(config)
    print(f"helper_carried after the consumer rule: {len(carried)} scopes")
    print("  " + ", ".join(sorted(carried)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
