"""The selection rules of the PR-head test selector (DEVOPS-20).

Split from scripts/ci_test_selection.py, which decides WHETHER a run selects (event, kill
switch, fallbacks) and records the result; this module decides WHAT a change set selects.
Decision record: docs/decisions/ADR-NEW-pr-heads-run-affected-tests.md.

What is selected, all of it a superset of the static lower bound:
  - import graph: test modules that import a changed module, transitively. A changed
    package `__init__.py` seeds every module in that package (Python executes it on any
    import beneath it), and a `pytest_plugins` string in a conftest counts as an import.
  - conftest reach: a conftest that imports changed code selects every test beneath it.
  - named references: test modules whose source names a changed path, its basename, or
    the dotted name, path or basename of any module the change reaches. This is what
    catches data files read by name and modules a test spawns as a subprocess.
  - package data: a non-Python file inside a Python package (outside tests/) counts as a
    change to the modules beside it.
  - test-area data: a non-Python file under `tests/<area>/` selects that whole area.
  - `COUPLINGS`: the small explicit map of tree-level couplings no import or name shows.

Raises FullSuite, never returns an empty answer for a change it cannot narrow: a global
path (`GLOBAL_PATTERNS`), an empty diff, a changed Python path the import graph cannot
resolve (the UNKNOWN of `scripts.affected_tests`), or changed code that a top-level conftest
imports.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from scripts.affected_tests import REPO, build_graph, reverse_reach

DURATIONS_NAME = ".test_durations"

#: A change to any of these runs the full suite. `*` crosses `/` (fnmatch), so `ci/*`
#: is the whole tree. The reason is what the log and the record print.
GLOBAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("conftest.py", "a conftest configures every test beneath it"),
    ("*/conftest.py", "a conftest configures every test beneath it"),
    ("pyproject.toml", "pytest and dependency configuration"),
    ("uv.lock", "the dependency lock"),
    ("pytest.ini", "pytest configuration"),
    ("setup.cfg", "pytest configuration"),
    ("tox.ini", "pytest configuration"),
    (".python-version", "the interpreter version"),
    ("requirements*.txt", "the CI test environment's dependencies"),
    ("requirements*.in", "the CI test environment's dependencies"),
    ("pylock*.toml", "the hash-pinned CI venv lock"),
    (".github/*", "workflow and runner configuration"),
    ("ci/*", "CI configuration"),
    ("scripts/ci_*", "CI scripts, this selector included"),
    ("scripts/affected_tests.py", "the import graph this selector is built on"),
    (DURATIONS_NAME, "the shard balance ledger"),
    ("tests/fixtures/*", "shared test fixtures"),
)

#: Basenames too generic to stand for one file when a test names them.
GENERIC_BASENAMES = frozenset({"__init__.py", "__main__.py"})


@dataclass(frozen=True)
class Coupling:
    """A changed tree that tests read or run without importing or naming one file of it."""

    changed_prefix: str
    needles: tuple[str, ...]
    why: str


#: Built Thu 1 Oct 2026 by grepping test modules for path literals (counts are modules
#: whose source contains a needle at that date). Each entry is pinned by a test that its
#: prefix exists and its needles still match at least one test module.
COUPLINGS: tuple[Coupling, ...] = (
    Coupling(
        "apps/audio-engine/",
        ("audio-engine", "audio_engine", "odj-audio"),
        "Rust engine: 18 modules build, spawn or read the engine and its sources",
    ),
    Coupling(
        "apps/webui/frontend/",
        ("webui/frontend", '"frontend"', "'frontend'"),
        "frontend tree: 67 Python contract tests read or scan frontend sources",
    ),
    Coupling(
        "ops/",
        ('"ops"', "ops/"),
        "fleet scripts: tests run ops/** shell scripts and scan the tree",
    ),
    Coupling(".agents/", (".agents",), "skills: tests parse skill files and their scripts"),
    Coupling(".planning/", (".planning",), "planning ledgers: requirements and debt readers"),
    Coupling("docs/", ('"docs"', "docs/"), "docs: link, ADR and register readers"),
    Coupling("specs/", ('"specs"', "specs/"), "specs: readers of the spec tree"),
    Coupling("data/", ('"data"', "data/"), "data: tests read committed data files by tree"),
    Coupling("tools/", ('"tools"', "tools/"), "tools: tests run tool scripts by tree"),
)

_TOKEN = re.compile(r"[\w.+@/-]+")


class FullSuite(Exception):
    """The change cannot be narrowed safely; the reason says why."""


@dataclass
class Selection:
    modules: list[str]
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def add(self, module: str, rule: str) -> None:
        rules = self.reasons.setdefault(module, [])
        if rule not in rules:
            rules.append(rule)


@dataclass(frozen=True)
class Graph:
    """The static import graph, with conftest `pytest_plugins` strings counted as imports."""

    importers: dict[str, set[str]]
    path_of: dict[str, str]
    name_of: dict[str, str]

    @classmethod
    def load(cls, root: Path) -> Graph:
        importers, module_of = build_graph(root)
        for plugin, conftests in _plugin_edges(module_of, root).items():
            importers.setdefault(plugin, set()).update(conftests)
        path_of = {name: str(relative) for name, relative in module_of.items()}
        return cls(importers, path_of, {path: name for name, path in path_of.items()})

    def modules_in(self, directory: str) -> set[str]:
        return {name for name, path in self.path_of.items() if _parent(path) == directory}


def global_reason(path: str) -> str | None:
    for pattern, reason in GLOBAL_PATTERNS:
        if fnmatch(path, pattern):
            return f"`{path}` matches `{pattern}` ({reason})"
    return None


def _parent(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _is_test_module(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.parts[:1] == ("tests",) and pure.name.startswith("test_") and pure.suffix == ".py"


def _under(path: str, directory: str) -> bool:
    if directory in ("", "."):
        return True
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def lane_universe(module_paths: list[str], ignores: list[str], root: Path) -> list[str]:
    """Test modules the shard lane collects: under tests/, test_*.py, present, not ignored."""
    return sorted(
        path
        for path in module_paths
        if _is_test_module(path)
        and (root / path).is_file()
        and not any(_under(path, ignore) for ignore in ignores)
    )


def _pytest_plugins(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins"
            for target in node.targets
        ):
            elements = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else []
            names += [
                e.value
                for e in elements
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return names


def _plugin_edges(module_of: dict[str, Path], root: Path) -> dict[str, set[str]]:
    """plugin module -> conftest modules naming it in `pytest_plugins` (a string import)."""
    edges: dict[str, set[str]] = {}
    for name, relative in module_of.items():
        if relative.name == "conftest.py":
            tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=str(relative))
            for plugin in _pytest_plugins(tree):
                edges.setdefault(plugin, set()).add(name)
    return edges


def _source_tokens(text: str) -> set[str]:
    """Path- and module-shaped tokens in a test's source, plus their tails and prefixes.

    `ROOT / "docs/perf/register.md"` and `f"{ROOT}/docs/perf/register.md"` both have to
    name `docs/perf/register.md`, and `monkeypatch.setattr("apps.x.y.func", ...)` has to
    name module `apps.x.y`: so every tail after a `/` and every dotted prefix counts too.
    """
    tokens: set[str] = set()
    for raw in _TOKEN.findall(text):
        token = (raw[2:] if raw.startswith("./") else raw).rstrip("./")
        if not token:
            continue
        tokens.add(token)
        if "/" in token:
            tokens.update(token[i + 1 :] for i, char in enumerate(token) if char == "/")
        elif "." in token:
            parts = token.split(".")
            tokens.update(".".join(parts[:k]) for k in range(1, len(parts)))
    return tokens


def _names_for(path: str, dotted: str | None) -> set[str]:
    names = {path}
    basename = PurePosixPath(path).name
    if basename not in GENERIC_BASENAMES:
        names.add(basename)
    if dotted:
        names.add(dotted)
    return names


def _refuse_unnarrowable(changed: list[str], graph: Graph) -> None:
    if not changed:
        raise FullSuite("the diff is empty, which is indistinguishable from a failed diff")
    for path in changed:
        reason = global_reason(path)
        if reason:
            raise FullSuite(f"global path changed: {reason}")
    unresolved = sorted(p for p in changed if p.endswith(".py") and p not in graph.name_of)
    if unresolved:
        raise FullSuite(
            "UNKNOWN: changed Python path(s) are not modules in the import graph "
            f"(deleted, renamed or skipped): {', '.join(unresolved)}"
        )


def _seed_modules(changed: list[str], graph: Graph) -> set[str]:
    """Changed modules, every module of a changed package, and package data's neighbors."""
    seeds: set[str] = set()
    for path in changed:
        if path.endswith(".py"):
            seeds.add(graph.name_of[path])
        if PurePosixPath(path).name == "__init__.py":
            package = graph.name_of[path]
            seeds.update(name for name in graph.path_of if name.startswith(package + "."))
        elif not path.endswith(".py") and not path.startswith("tests/"):
            parent = _parent(path)
            if f"{parent}/__init__.py" in graph.name_of:
                seeds |= graph.modules_in(parent)
    return seeds


def _select_test_areas(changed: list[str], universe: list[str], selection: Selection) -> None:
    """Data under tests/<area>/ selects that area; a file directly in tests/ is left to names."""
    for path in changed:
        parts = PurePosixPath(path).parts
        if parts[:1] == ("tests",) and len(parts) >= 3 and not path.endswith(".py"):
            area = "/".join(parts[:2])
            for test in universe:
                if _under(test, area):
                    selection.add(test, f"test-area data `{path}`")


def _select_by_conftest(
    reached: set[str], graph: Graph, universe: list[str], selection: Selection
) -> None:
    for name in sorted(reached):
        path = graph.path_of[name]
        if PurePosixPath(path).name != "conftest.py":
            continue
        directory = _parent(path)
        if directory in (".", "tests"):
            raise FullSuite(f"changed code is imported by `{path}`, which configures every test")
        for test in universe:
            if _under(test, directory):
                selection.add(test, f"conftest reach `{path}`")


def _select_by_source(
    names: set[str],
    couplings: list[Coupling],
    universe: list[str],
    root: Path,
    selection: Selection,
) -> None:
    for test in universe:
        text = (root / test).read_text(encoding="utf-8", errors="replace")
        if _source_tokens(text) & names:
            selection.add(test, "named reference")
        for coupling in couplings:
            if any(needle in text for needle in coupling.needles):
                selection.add(test, f"coupling `{coupling.changed_prefix}`")


def select_tests(changed: list[str], ignores: list[str], root: Path = REPO) -> Selection:
    """The test modules a change set reaches, or FullSuite when it cannot be narrowed."""
    graph = Graph.load(root)
    _refuse_unnarrowable(changed, graph)
    universe = lane_universe(list(graph.path_of.values()), ignores, root)
    selection = Selection(modules=[])
    _select_test_areas(changed, universe, selection)
    reached = reverse_reach(_seed_modules(changed, graph), graph.importers)
    _select_by_conftest(reached, graph, universe, selection)
    universe_set = set(universe)
    for name in reached:
        if graph.path_of[name] in universe_set:
            selection.add(graph.path_of[name], "import graph")
    names: set[str] = set()
    for path in changed:
        names |= _names_for(path, graph.name_of.get(path))
    for name in reached:
        names |= _names_for(graph.path_of[name], name)
    couplings = [c for c in COUPLINGS if any(p.startswith(c.changed_prefix) for p in changed)]
    _select_by_source(names, couplings, universe, root, selection)
    selection.modules = sorted(selection.reasons)
    return selection
