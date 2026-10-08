"""The selection rules of the PR-head test selector (DEVOPS-20).

Split from scripts/ci_test_selection.py, which decides WHETHER a run selects (event, kill
switch, fallbacks) and records the result; this module decides WHAT a change set selects.
Decision record: (private path).

What is selected, all of it a superset of the static lower bound:
  - import graph: test modules that import a changed module, transitively. A changed
    package `__init__.py` seeds every module in that package (Python executes it on any
    import beneath it), and a `pytest_plugins` string in a conftest counts as an import.
  - conftest reach: a conftest that imports changed code selects every test beneath it.
  - named references: test modules whose source names a changed path, its basename, or
    the dotted name, path or basename of any module the change reaches. This is what
    catches data files read by name and modules a test spawns as a subprocess. The
    package of a reached `__main__.py` counts only written out in full (`python -m pkg`),
    never as the dotted prefix of a `pkg.submodule` mention.
  - package data: a non-Python file inside a Python package (outside tests/) counts as a
    change to the modules beside it.
  - test-area data: a non-Python file under `tests/<area>/` selects that whole area.
  - `COUPLINGS`: the small explicit map of tree-level couplings no import or name shows.
    An entry can hand a subtree to a narrower entry that also requires a way to read the
    tree unnamed (the frontend's `tests/` tree).
  - always run: the whole-tree guard modules `ci/pr-always-run.txt` lists, on every
    narrowed head, because they scan the tree rather than import what changed.

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
#:
#: `.test_durations` is NOT here (Fri 2 Oct 2026, TEST-CUT round 1, (private path)):
#: the ledger only weighs shards and sets the fast-tier ceiling, it changes no test's
#: behavior, and as a trigger it sent 18 PR heads in 27 h to the full lane (63 shard
#: runner-min each). A ledger edit is now an ordinary data file: the tests that name it
#: (the fast-tier and selector suites) are selected by named reference.
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
    ("tests/fixtures/*", "shared test fixtures"),
)

#: Basenames too generic to stand for one file when a test names them.
GENERIC_BASENAMES = frozenset({"__init__.py", "__main__.py"})


@dataclass(frozen=True)
class Coupling:
    """A changed tree that tests read or run without importing or naming one file of it.

    `excluded_prefix` hands a subtree's NON-Python files to a narrower entry. A Python file
    stays with the broad entry: repo-wide Python-source scanners read every `.py` file
    through library code no test source shows (Thu 8 Oct 2026, an audit hook caught
    tests/scripts/test_ci_plan_helpers.py opening frontend `tests/e2e/support/*.py` via
    scripts.ci_plan_carry). `readers`, when set, is a second condition a needle match must
    also meet: the source must show a way to reach the tree without naming the changed
    file (a walk, a directory join, a tool run inside it).
    """

    changed_prefix: str
    needles: tuple[str, ...]
    why: str
    excluded_prefix: str | None = None
    readers: re.Pattern[str] | None = None

    def fires_for(self, path: str) -> bool:
        excluded = (
            self.excluded_prefix is not None and path.startswith(self.excluded_prefix) and not path.endswith(".py")
        )
        return path.startswith(self.changed_prefix) and not excluded

    def matches(self, text: str) -> bool:
        if not any(needle in text for needle in self.needles):
            return False
        return self.readers is None or self.readers.search(text) is not None


FRONTEND = "apps/webui/frontend/"
FRONTEND_TESTS = FRONTEND + "tests/"
FRONTEND_NEEDLES = ("webui/frontend", '"frontend"', "'frontend'")

#: How a Python test that mentions the frontend can read a file under its `tests/` tree
#: WITHOUT naming that file (naming it is the named-reference rule's job). Measured Thu 8
#: Oct 2026 against every frontend-mentioning test module (Sel-0 d): a path literal or a
#: pathlib join into that tree, a recursive walk or `git ls-files` (which also sees it), a
#: directory listing, or a tool run rooted in the frontend (`cwd=FRONTEND`, `--dir`, `-C`).
FRONTEND_TESTS_READERS = re.compile(
    r"frontend/tests\b"
    r"|(?i:frontend\w*)[\"']?\s*\)?\s*/\s*[\"']tests\b"
    r"|rglob\(|os\.walk\(|\bls-files\b|recursive=True|glob\([^)\n]*\*\*"
    r"|iterdir\(|scandir\(|listdir\("
    r"|cwd\s*=\s*(?:str\()?\w*(?i:frontend)|[\"'](?:--dir|-C|--prefix)[\"']"
)


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
        FRONTEND,
        FRONTEND_NEEDLES,
        "frontend tree: Python contract tests read or scan its sources, configs and scripts",
        excluded_prefix=FRONTEND_TESTS,
    ),
    Coupling(
        FRONTEND_TESTS,
        FRONTEND_NEEDLES,
        "frontend tests tree: only tests that walk, join into or run a tool in it read it "
        "unnamed (a change there no longer selects all ~100 frontend contract tests, Sel-0 d)",
        readers=FRONTEND_TESTS_READERS,
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
#: The module part of an import statement. Importing a package runs its `__init__.py`, never
#: its `__main__.py`, so a package named here is not a `python -m` run.
_IMPORT_STATEMENT = re.compile(r"^[ \t]*(?:from[ \t]+[\w.]+[ \t]+import\b|import[ \t]+[\w.]+)", re.M)


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
        if _is_test_module(path) and (root / path).is_file() and not any(_under(path, ignore) for ignore in ignores)
    )


def _pytest_plugins(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in node.targets
        ):
            elements = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else []
            names += [e.value for e in elements if isinstance(e, ast.Constant) and isinstance(e.value, str)]
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


@dataclass(frozen=True)
class SourceTokens:
    """Path- and module-shaped tokens in a test's source.

    `ROOT / "docs/perf/register.md"` and `f"{ROOT}/docs/perf/register.md"` both have to
    name `docs/perf/register.md`, so every tail after a `/` is `written` too. And
    `monkeypatch.setattr("apps.x.y.func", ...)` has to name module `apps.x.y`, so every
    dotted prefix of a written token is a `prefix`.
    """

    written: frozenset[str]
    prefixes: frozenset[str]
    #: `written`, minus what only an import statement's module part writes.
    written_outside_imports: frozenset[str]

    def name_any(self, names: set[str]) -> bool:
        return not names.isdisjoint(self.written) or not names.isdisjoint(self.prefixes)

    def name_outside_imports(self, names: set[str]) -> bool:
        return not names.isdisjoint(self.written_outside_imports)


def _source_tokens(text: str) -> SourceTokens:
    written, prefixes = _tokens_of(text)
    outside_imports, _ = _tokens_of(_IMPORT_STATEMENT.sub("", text))
    return SourceTokens(frozenset(written), frozenset(prefixes), frozenset(outside_imports))


def _tokens_of(text: str) -> tuple[set[str], set[str]]:
    written: set[str] = set()
    prefixes: set[str] = set()
    for raw in _TOKEN.findall(text):
        token = (raw[2:] if raw.startswith("./") else raw).rstrip("./")
        if not token:
            continue
        written.add(token)
        if "/" in token:
            written.update(token[i + 1 :] for i, char in enumerate(token) if char == "/")
        elif "." in token:
            parts = token.split(".")
            prefixes.update(".".join(parts[:k]) for k in range(1, len(parts)))
    return written, prefixes


@dataclass
class Names:
    """What the change set's reach is called in test source.

    `any_form` matches as written, as a path tail or as a dotted prefix. `exact` is the
    package of a reached `__main__.py`, which `python -m <package>` runs, and it matches
    only written out in full outside an import statement. As a dotted prefix it is every
    `<package>.<submodule>` mention, and in `from <package> import x` it is an import:
    neither runs `__main__`, and the import graph already follows both (Sel-0 a: on a
    server change `apps.webui.server` and `apps.engine_core` matched about 160 test
    modules that only import from the server).
    """

    any_form: set[str] = field(default_factory=set)
    exact: set[str] = field(default_factory=set)

    def add(self, path: str, dotted: str | None) -> None:
        self.any_form.add(path)
        basename = PurePosixPath(path).name
        if basename not in GENERIC_BASENAMES:
            self.any_form.add(basename)
        if dotted:
            self.any_form.add(dotted)
            if basename == "__main__.py":
                self.exact.add(dotted.removesuffix(".__main__"))

    def named_by(self, tokens: SourceTokens) -> bool:
        return tokens.name_any(self.any_form) or tokens.name_outside_imports(self.exact)


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


def _select_by_conftest(reached: set[str], graph: Graph, universe: list[str], selection: Selection) -> None:
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
    names: Names,
    couplings: list[Coupling],
    universe: list[str],
    root: Path,
    selection: Selection,
) -> None:
    for test in universe:
        text = (root / test).read_text(encoding="utf-8", errors="replace")
        if names.named_by(_source_tokens(text)):
            selection.add(test, "named reference")
        for coupling in couplings:
            if coupling.matches(text):
                selection.add(test, f"coupling `{coupling.changed_prefix}`")


#: Whole-tree guard test modules a narrowed PR head always runs, whatever it changed
#: (CICD-SPEED L00). Read by `always_run_modules`; why each entry is there is in the file.
ALWAYS_RUN_NAME = "ci/pr-always-run.txt"


def always_run_modules(root: Path) -> list[str]:
    """The test module paths `ci/pr-always-run.txt` lists; `#` starts a comment.

    A missing list is FullSuite, never an empty list: the guards it names are the tests
    that a narrowed head is otherwise most likely to skip and break main with.
    """
    try:
        text = (root / ALWAYS_RUN_NAME).read_text(encoding="utf-8")
    except FileNotFoundError as missing:
        raise FullSuite(
            f"the always-run list `{ALWAYS_RUN_NAME}` is missing, so the whole-tree guards "
            "cannot be added to a selection"
        ) from missing
    entries = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return [entry for entry in entries if entry]


def _select_always_run(universe: list[str], root: Path, selection: Selection) -> None:
    """Whole-tree guards scan the tree, not a changed module, so no link reaches them."""
    collected = set(universe)
    for module in always_run_modules(root):
        if module not in collected:
            raise FullSuite(
                f"always-run entry `{module}` is not a test module the lane collects "
                f"(renamed, deleted or lane-ignored); fix `{ALWAYS_RUN_NAME}`"
            )
        selection.add(module, "always run (whole-tree guard)")


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
    names = Names()
    for path in changed:
        names.add(path, graph.name_of.get(path))
    for name in reached:
        names.add(graph.path_of[name], name)
    couplings = [c for c in COUPLINGS if any(c.fires_for(p) for p in changed)]
    _select_by_source(names, couplings, universe, root, selection)
    _select_always_run(universe, root, selection)
    selection.modules = sorted(selection.reasons)
    return selection
