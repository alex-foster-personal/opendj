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

Two TEST-CUT cuts ((private path)) change that answer only when a `Policy` turns them
on; the default `Policy()` is the behavior above, unchanged:
  - item 3, `ci_config_by_name`: workflows and CI scripts (`BY_NAME_PATTERNS`; `ci/*` and
    shared fixtures stay FULL) stop forcing FULL and are selected like any data file (by name,
    plus the `BY_NAME_COUPLINGS` tree readers), and a deleted Python module is selected by
    its name instead of forcing FULL (a test still importing it names it).
  - item 2, `demote_weak_under`: under those test directories, a module selected ONLY by a
    weak link (`WEAK_RULES`: a named reference, a coupling, conftest reach or test-area
    data) is demoted out of the PR head's run and recorded in `Selection.demoted`. It runs
    only where the full suite runs (a FULL merge-queue draft, the nightly full-ci.yml), and
    the escape measurement reads the record to split those escapes out as weak. With the
    merge queue off (Fri 30 Oct 2026) only the nightly run covers it, so leave it unset.
"""

from __future__ import annotations

import ast
import heapq
import itertools
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from functools import cached_property, lru_cache
from pathlib import Path, PurePosixPath

from scripts.affected_tests import REPO, _imports_of, build_graph, reverse_reach
from scripts.ci_commented_list import read_commented_list

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

#: The `GLOBAL_PATTERNS` that TEST-CUT item 3 selects by name when `ci_config_by_name` is on.
#: They were 59% of full-run fallbacks ((private path), Fri 2 Oct 2026). The selector's
#: own code and its always-run list stay global even then (`SELECTOR_SOURCES`): a change that
#: breaks selection, or drops a guard from the list, must not be judged by the change itself.
#: `ci/*` and `tests/fixtures/*` stay FULL (Sat 17 Oct 2026): eleven review rounds each found
#: one more way a fixture or ci/ file reaches a test (helpers, plugins, assembled names, quote
#: styles), which says the per-rule seeding is the wrong model for them. They come back with a
#: single strong/weak edge graph (follow-up issue linked from (private path)).
BY_NAME_PATTERNS = frozenset({".github/*", "scripts/ci_*"})
SELECTOR_SOURCES = frozenset(
    {
        "scripts/ci_commented_list.py",
        "scripts/ci_test_selection.py",
        "scripts/ci_test_selection_rules.py",
        "scripts/affected_tests.py",
        "ci/pr-always-run.txt",  # `ALWAYS_RUN_NAME`: it decides which guards a head runs
    }
)

#: The weak links of (private path) item 2: they reach a test through a name, a tree,
#: a conftest or test data, not through an import of the changed code. A module is demoted
#: only when EVERY rule that selected it is one of these, so the strong ones ("import graph",
#: "always run") and any rule added later stay on the PR head without being listed here.
WEAK_RULES = ("named reference", "coupling `", "conftest reach `", "test-area data `")


@dataclass(frozen=True)
class Policy:
    """Which TEST-CUT cuts a selection applies. `Policy()` is the selector before them."""

    ci_config_by_name: bool = False
    demote_weak_under: tuple[str, ...] = ()


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
    #: When set, the needle must come with a use of one of these roots itself (`_uses_root`),
    #: in place of `readers`: a root directory's name is what most modules spell to reach
    #: one file of it.
    walk_roots: tuple[str, ...] = ()

    def fires_for(self, path: str) -> bool:
        excluded = (
            self.excluded_prefix is not None and path.startswith(self.excluded_prefix) and not path.endswith(".py")
        )
        return path.startswith(self.changed_prefix) and not excluded

    def matches(self, text: str) -> bool:
        if not any(needle in text for needle in self.needles):
            return False
        if self.walk_roots:
            return _uses_root(text, self.walk_roots)
        return self.readers is None or self.readers.search(text) is not None

    def uses_own_directory(self, code: str, here: str) -> bool:
        """Whether the module at `here` uses one of `walk_roots` by its own `__file__`
        (`Path(__file__).parent.glob("ci_*.py")` in `scripts/x.py`), spelling no root."""
        return bool(self.walk_roots) and "__file__" in code and _uses_root(code, self.walk_roots, here)


#: Calls that list a tree, as a method on the root (`root.glob(..)`) or a function taking it
#: (`os.walk(root)`, `glob.glob("scripts/*.py")`).
_WALK_CALLS = frozenset(
    {"glob", "iglob", "rglob", "iterdir", "walk", "fwalk", "scandir", "listdir", "iter_modules", "walk_packages"}
)
#: `git ls-files`, which lists the index. A module constant, not a literal in `_walk_operands`,
#: so the whole-tree guard's detector does not read this selector as a reader of the index.
_INDEX_LISTING = "ls-files"


#: Calls whose arguments are parts of the path they build, not a root handed on.
_PATH_BUILDERS = frozenset({"Path", "PurePath", "PurePosixPath", "join", "joinpath", "dirname"})


#: Pure in its arguments, and asked of the same sources again by every change set a caller selects over one
#: checkout (scripts/ci_main_hourly.py attributes a range commit by commit).
@lru_cache(maxsize=16384)
def _uses_root(text: str, roots: tuple[str, ...], here: str | None = None) -> bool:
    """Whether `text` walks one of `roots`, hands it to a call, or binds a name to it.

    Coarse on purpose (nightly review, Sun 18 Oct 2026): only direct spellings are read. A
    walk whose receiver or argument is the root (`(ROOT / "scripts").glob(..)`, `S.rglob(..)`
    of a local `S = ROOT / "scripts"`, `git ls-files scripts/`, a package's `__path__`, and
    with `here`, the module's own path, a directory read off its `__file__`) counts, and so
    does every other use of the root itself: a call argument (`files(Path("scripts"))`) or
    a binding another module can import (`SCRIPTS = ROOT / "scripts"`), whose importers are
    then selected. So a walk through an alias, a wrapper, a lambda or another module
    over-selects instead of being followed. `ROOT / "scripts" / "x.py"` names one file and
    is not the root. Source that does not parse counts.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    bound = _module_bindings(tree)

    def is_root(expr: ast.AST) -> bool:
        return _is_root(expr, roots, bound, set(), here)

    if any(is_root(value) for values in bound.values() for value in values):
        return True
    # Any other assignment of the root counts too, whatever its target: `SCRIPTS, OTHER =
    # Path("scripts"), None` binds it as surely, and the target is not worth unpacking.
    if any(
        isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None and is_root(node.value)
        for node in ast.walk(tree)
    ):
        return True
    for call in ast.walk(tree):
        if isinstance(call, ast.Attribute) and call.attr in _WALK_CALLS and is_root(call.value):
            return True  # a walk method taken off the root, called or not: `walk = SCRIPTS.glob`
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        handed = [] if callee in _PATH_BUILDERS else [*call.args, *(keyword.value for keyword in call.keywords)]
        if any(is_root(operand) for operand in [*_walk_operands(call), *handed]):
            return True
    return False


def _walk_operands(call: ast.Call) -> list[ast.AST]:
    """What a walk call lists: its arguments and, for `root.glob(..)`, its receiver. Empty for any other call."""
    func = call.func
    operands: list[ast.AST] = [*call.args, *(keyword.value for keyword in call.keywords)]
    callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    if callee in _WALK_CALLS:
        return [*operands, func.value] if isinstance(func, ast.Attribute) else operands
    if any(
        isinstance(sub, ast.Constant) and isinstance(sub.value, str) and _INDEX_LISTING in sub.value.split()
        for sub in ast.walk(call)
    ):
        return operands
    return []


def _container_parts(expr: ast.AST) -> list[ast.AST]:
    """What an element read from a container can be: `ROOTS[0]` is anything `ROOTS` holds,
    so a walk of it reaches a root `ROOTS = [SCRIPTS]` (or a tuple, set or dict) holds."""
    if isinstance(expr, ast.Subscript | ast.Starred):
        return [expr.value]
    if isinstance(expr, ast.List | ast.Tuple | ast.Set):
        return list(expr.elts)
    if isinstance(expr, ast.Dict):
        return list(expr.values)
    return []


def _is_root(
    expr: ast.AST,
    roots: tuple[str, ...],
    bound: dict[str, list[ast.expr]],
    seen: set[str],
    here: str | None = None,
) -> bool:
    """`expr` is a root itself, or a glob over it. The LAST path segment decides, so
    `ROOT / "scripts" / "bench"` (a subtree no top-level script is in) is not, unless it is a
    glob pattern (`SCRIPTS / "ci_*.py"`), which lists the segment before it. With `here`, a
    directory read off the module's `__file__` is the directory it names."""
    if here is not None:
        directory = _file_path(expr, here, bound, frozenset(seen))
        if directory is not None:
            return str(directory) in roots
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        # The whole string, or token-wise for a listing: `"git ls-files scripts/"`. Prose
        # that mentions a root (`"persona scripts"`) is not a path.
        words = expr.value.split()
        tokens = words if _INDEX_LISTING in words else [expr.value]
        return any(_spells_root(token, root) for token in tokens for root in roots)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        last = expr.left if _is_glob_segment(expr.right) else expr.right
        return _is_root(last, roots, bound, seen, here)
    if isinstance(expr, ast.Call) and expr.args:  # `Path(ROOT, "scripts")`, `os.path.join(..)`
        last = expr.args[-2] if len(expr.args) > 1 and _is_glob_segment(expr.args[-1]) else expr.args[-1]
        return _is_root(last, roots, bound, seen, here)
    if isinstance(expr, ast.List | ast.Tuple):  # `["git", "ls-files", "scripts/"]`
        return any(_is_root(element, roots, bound, seen, here) for element in expr.elts)
    if isinstance(expr, ast.Subscript | ast.Set | ast.Dict | ast.Starred):  # `ROOTS[0]` of `ROOTS = [S]`
        return any(_is_root(inner, roots, bound, seen, here) for inner in _container_parts(expr))
    if isinstance(expr, ast.Name) and expr.id in bound and expr.id not in seen:
        # `seen` is shared by the whole check, so each binding is read once: a name already
        # read was not a root, or `any` would have returned. A per-path set is exponential.
        seen.add(expr.id)
        return any(_is_root(value, roots, bound, seen, here) for value in bound[expr.id])
    # A package's own directory: `pkgutil.iter_modules(scripts.__path__)` lists `scripts`.
    # Any package counts, so `import scripts as s` then `s.__path__` over-selects rather
    # than being resolved.
    return isinstance(expr, ast.Attribute) and expr.attr == "__path__"


#: A file a walk root's glob must be able to list to count: walk roots are for the CI-script
#: coupling, so `scripts/*.py` lists the tree it cares about and `scripts/review_*.py` does not.
_ROOT_PROBE = "ci_probe.py"


def _spells_root(token: str, root: str) -> bool:
    """`root` itself, or a glob directly under it that can list a CI script there."""
    if token.rstrip("/") == root:
        return True
    return token.startswith(root + "/") and any(c in token for c in "*?[") and fnmatch(f"{root}/{_ROOT_PROBE}", token)


def _is_glob_segment(expr: ast.AST) -> bool:
    """A last path segment that is a pattern over one directory (`"ci_*.py"`), not a path."""
    return (
        isinstance(expr, ast.Constant)
        and isinstance(expr.value, str)
        and "/" not in expr.value
        and any(c in expr.value for c in "*?[")
    )


#: Calls that hand back the same path they are given (`Path(x)`, `x.resolve()`, `str(x)`).
_SAME_PATH = frozenset({"Path", "PurePath", "PurePosixPath", "str", "fspath", "abspath", "realpath", "normpath"})


def _file_path(
    expr: ast.AST, here: str, bound: dict[str, list[ast.expr]], seen: frozenset[str]
) -> PurePosixPath | None:
    """The repo path `expr` reads off `__file__` in the module at `here`: `Path(__file__).parent`
    and `os.path.dirname(__file__)` are its directory, `.parents[1]` the one above. None when
    `expr` is not such a path."""
    if isinstance(expr, ast.Name):
        if expr.id == "__file__":
            return PurePosixPath(here)
        if expr.id in seen:
            return None
        found = {_file_path(value, here, bound, seen | {expr.id}) for value in bound.get(expr.id, [])} - {None}
        return found.pop() if len(found) == 1 else None
    if isinstance(expr, ast.Attribute) and expr.attr == "parent":
        inner = _file_path(expr.value, here, bound, seen)
        return inner.parent if inner is not None else None
    if (
        isinstance(expr, ast.Subscript)
        and isinstance(expr.value, ast.Attribute)
        and expr.value.attr == "parents"
        and isinstance(expr.slice, ast.Constant)
        and isinstance(expr.slice.value, int)
    ):
        inner = _file_path(expr.value.value, here, bound, seen)
        if inner is None or expr.slice.value >= len(inner.parents):
            return None
        return inner.parents[expr.slice.value]
    if isinstance(expr, ast.Call):
        return _file_path_of_call(expr, here, bound, seen)
    return None


def _file_path_of_call(
    call: ast.Call, here: str, bound: dict[str, list[ast.expr]], seen: frozenset[str]
) -> PurePosixPath | None:
    """`_file_path` for a call: `x.resolve()`, `Path(x)` and `str(x)` are `x`, `os.path.dirname(x)` its parent."""
    func = call.func
    callee = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    if callee in {"resolve", "absolute"} and isinstance(func, ast.Attribute) and not call.args:
        return _file_path(func.value, here, bound, seen)
    if len(call.args) != 1 or callee not in {*_SAME_PATH, "dirname"}:
        return None
    inner = _file_path(call.args[0], here, bound, seen)
    return inner.parent if inner is not None and callee == "dirname" else inner


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
        "apps/live-core/",
        ("live-core", "live_core", "odj-live"),
        "Track Catcher live core: tests/live_core builds odj-live and checks it against librosa",
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
    Coupling(
        "scripts/native/oss_tip_audit/",
        ("oss_tip_audit", "oss_tip_native"),
        "oss audit Rust kernel: no Python imports it, yet CI runs every oss_tip_audit test through it"
        " (ODJ_OSS_AUDIT_BACKEND=auto); before this a kernel-only change selected no parity test"
        " (replayed Thu 15 Oct 2026, PR #6796)",
    ),
    Coupling(
        "apps/desktop/src-tauri/capabilities/",
        ("capabilities/",),  # not `"capabilities"`, a JSON key ten CloudSync tests spell
        "Tauri capability files: a guard checks them against the frontend's plugin calls (found Wed 14 Oct 2026"
        " by replaying the selector for the tree-read census's `reached:` excusals, #6539)",
    ),
)

#: How a test can read every file under a tree without naming one: a walk, a glob, a listing
#: or `git ls-files`, called through its module or imported by name (any alias). A
#: `BY_NAME_COUPLINGS` entry needs this AND its needle, because 186 test modules mention
#: `.github` and almost all of them read one named workflow.
TREE_READERS = re.compile(
    r"rglob\(|os\.walk\(|fwalk\(|(?<!\bast)\.walk\(|(?<![\w.])walk\(|\bls-files\b|recursive=True|\bi?glob\(|iterdir\(|scandir\("
    r"|listdir\(|iter_modules\(|walk_packages\("
    # A walker imported by name, under any alias: `from os import walk as w` then `w(...)`.
    r"|^[ \t]*from[ \t]+(?:os|glob|pathlib|pkgutil)[ \t]+import\b[^\n]*\b(?:walk|fwalk|scandir|listdir|i?glob|iter_modules|walk_packages)\b"
    # A walker taken without calling it, to call later: `g = Path.glob`, `map(os.listdir, ..)`,
    # `partial(Path.rglob, ..)`, or by name, `getattr(Path, "glob")`. Not on an import line,
    # where `.walk` is a module (`from scripts.walk import files`).
    r"|^(?![ \t]*(?:from|import)\b).*?(?<!\bast)\.(?:walk|fwalk|scandir|listdir|i?glob|rglob|iterdir|iter_modules|walk_packages)\b(?![ \t]*\(|\.)"
    r"|\bgetattr\([^)\n]*['\"](?:walk|fwalk|scandir|listdir|i?glob|rglob|iterdir)['\"]",
    re.M,
)

#: Tree readers of the paths TEST-CUT item 3 selects by name (fire only with `ci_config_by_name`,
#: since without it those paths are global anyway).
BY_NAME_COUPLINGS: tuple[Coupling, ...] = (
    Coupling(".github/", (".github",), "workflow tree: tests that walk or glob the workflows", readers=TREE_READERS),
    # `Path("scripts").glob("ci_*.py")`: a walk that reaches the `scripts` root, not
    # `tests/scripts/...` or a `scripts.x` import, which every selector test spells, and
    # not `REPO_ROOT / "scripts" / "x.py"` beside an unrelated glob, which ~60 modules do
    # (a needle-and-reader match selected 473 tests for one CI script, Sat 17 Oct 2026).
    # Across modules, pairing any spelled root with a walk elsewhere made the root conftest
    # a CI-script walker, which runs every test; only a walk of an imported root binding
    # counts (`from paths import SCRIPTS` then `SCRIPTS.glob(..)`).
    Coupling(
        "scripts/ci_",
        ('"scripts', "'scripts", "__path__"),
        "CI script tree: tests that walk or glob the scripts",
        readers=TREE_READERS,
        walk_roots=("scripts",),
    ),
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
    #: Modules a weak-link demotion took out of the run, with the weak rules that chose them.
    demoted: dict[str, list[str]] = field(default_factory=dict)
    #: Every module path in the import graph, so a caller can size the lane universe.
    graph_paths: list[str] = field(default_factory=list)
    #: How far each selected module sits from the change (TEST-BUDGET round 5): 0 for a
    #: changed module, 1 for its direct importers and test-area data, n for n import hops, a
    #: conftest's reach one past the conftest, and the fixed ranks below for the rules that
    #: carry no graph position. A hard budget keeps the nearest modules first.
    distance: dict[str, int] = field(default_factory=dict)

    def add(self, module: str, rule: str, distance: int | None = None) -> None:
        rules = self.reasons.setdefault(module, [])
        if rule not in rules:
            rules.append(rule)
        if distance is not None and distance < self.distance.get(module, distance + 1):
            self.distance[module] = distance


#: Ranks for the rules with no import-graph position, after the direct importers (1) and
#: before the far reaches of the graph: the whole-tree guards scan the tree, a named
#: reference is a string match, a coupling is a declared folder pairing.
ALWAYS_RUN_DISTANCE = 2
NAMED_REFERENCE_DISTANCE = 3
COUPLING_DISTANCE = 4


def reach_depths(
    seeds: set[str], importers: dict[str, set[str]], *, one_hop_seeds: frozenset[str] = frozenset()
) -> dict[str, int]:
    """Import hops from the nearest seed to every module that reaches one, seeds at 0.

    `one_hop_seeds` start at 1 (the importers of a deleted module: the deleted module is the
    origin, so they are its direct importers, not changed modules), unless also a seed.
    """
    depth = dict.fromkeys(seeds, 0)
    queue = sorted(seeds)
    for name in sorted(one_hop_seeds - seeds):
        depth[name] = 1
        queue.append(name)
    for name in queue:
        for importer in sorted(importers.get(name, ())):
            if importer not in depth:
                depth[importer] = depth[name] + 1
                queue.append(importer)
    return depth


@dataclass(frozen=True)
class Graph:
    """The static import graph, with `pytest_plugins` strings counted as imports."""

    importers: dict[str, set[str]]
    path_of: dict[str, str]
    name_of: dict[str, str]
    #: Modules some `pytest_plugins` loads, whose own declaration pytest reads in turn.
    plugin_modules: frozenset[str] = frozenset()

    @classmethod
    def load(cls, root: Path) -> Graph:
        importers, module_of = build_graph(root)
        edges = _plugin_edges(module_of, root)
        for plugin, conftests in edges.items():
            importers.setdefault(plugin, set()).update(conftests)
        path_of = {name: relative.as_posix() for name, relative in module_of.items()}
        plugin_modules = frozenset(plugin for plugin in edges if plugin in module_of)
        return cls(importers, path_of, {path: name for name, path in path_of.items()}, plugin_modules)

    def modules_in(self, directory: str) -> set[str]:
        return {name for name, path in self.path_of.items() if _parent(path) == directory}


def global_reason(path: str, policy: Policy | None = None) -> str | None:
    by_name = policy is not None and policy.ci_config_by_name and path not in SELECTOR_SOURCES
    for pattern, reason in GLOBAL_PATTERNS:
        if by_name and pattern in BY_NAME_PATTERNS:
            continue
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


#: How many spellings a folded string may have before it stops folding. A module or key name
#: is spelled a handful of ways; a name past this is a SQL builder (`sql += " AND ..."` in
#: twenty functions), and folding it multiplied without bound: the selector hung on PR #6233.
_FOLD_CAP = 64
#: And how long one may be: `n1 = n0 + n0; n2 = n1 + n1` doubles a string per name.
_FOLD_LEN = 1024


class _Folded(dict[str, set[str]]):
    """`_string_names`'s result, which also says whether a fold was cut at a cap: then a
    string the module composes may be missing, and a by-name scan must over-select."""

    cut = False


def _mark_cut(names: dict[str, set[str]]) -> set[str]:
    """Record on `names` that a fold past a cap was left unfolded, and fold nothing."""
    if isinstance(names, _Folded):
        names.cut = True
    return set()


def _concat(parts: list[set[str]], names: dict[str, set[str]]) -> set[str]:
    """Every string made of one value from each of `parts` in order, or nothing (does not
    fold, and marked cut on `names`) past `_FOLD_CAP` strings or `_FOLD_LEN` characters."""
    if math.prod(map(len, parts)) > _FOLD_CAP or sum(max(map(len, p), default=0) for p in parts) > _FOLD_LEN:
        return _mark_cut(names)
    return {"".join(chosen) for chosen in itertools.product(*parts)}


def _const_str(node: ast.AST, names: dict[str, set[str]]) -> set[str]:
    """Every string a constant expression can evaluate to, empty when it is not one.

    Folds what a binding key can be spelled as: a string constant, a name assigned such an
    expression anywhere in the module (`key = "pytest_plugins"`, every assignment counted),
    `+` of two such expressions, an f-string of them, `"".join` of a literal list or tuple
    of them, and `%` or `.format` of them.
    """
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return names.get(node.id, set())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concat([_const_str(node.left, names), _const_str(node.right, names)], names)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        return _format_all(_const_str(node.left, names), args, [], names, lambda fmt, a, _k: fmt % tuple(a))
    if isinstance(node, ast.JoinedStr):
        return _concat(
            [_const_str(v.value if isinstance(v, ast.FormattedValue) else v, names) for v in node.values], names
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        seps = _const_str(node.func.value, names)
        return _concat(
            [
                part
                for i, element in enumerate(node.args[0].elts)
                for part in ([seps] if i else []) + [_const_str(element, names)]
            ],
            names,
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return _format_all(
            _const_str(node.func.value, names), node.args, node.keywords, names, lambda fmt, a, k: fmt.format(*a, **k)
        )
    return set()


def _format_all(
    formats: set[str],
    args: list[ast.expr],
    keywords: list[ast.keyword],
    names: dict[str, set[str]],
    render: Callable[[str, list[str], dict[str, str]], str],
) -> set[str]:
    """Every string `%` or `.format` makes of foldable parts, empty when any part does not fold."""
    if any(keyword.arg is None for keyword in keywords) or any(isinstance(a, ast.Starred) for a in args):
        return set()
    arg_sets = [_const_str(a, names) for a in args]
    keyword_sets = {keyword.arg: _const_str(keyword.value, names) for keyword in keywords if keyword.arg}
    if not all(arg_sets) or not all(keyword_sets.values()):
        return set()
    if len(formats) * math.prod(map(len, [*arg_sets, *keyword_sets.values()])) > _FOLD_CAP:
        return _mark_cut(names)
    parts = [value for values in [*arg_sets, *keyword_sets.values()] for value in values]
    if any(_renders_too_long(fmt, parts) for fmt in formats):
        return _mark_cut(names)
    rendered: set[str] = set()
    for fmt in formats:
        for values in itertools.product(*arg_sets):
            for chosen in itertools.product(*keyword_sets.values()):
                try:
                    text = render(fmt, list(values), dict(zip(keyword_sets, chosen, strict=True)))
                except (TypeError, ValueError, KeyError, IndexError):
                    continue
                if len(text) > _FOLD_LEN:
                    return _mark_cut(names)
                rendered.add(text)
    return rendered


def _renders_too_long(fmt: str, parts: list[str]) -> bool:
    """Whether `fmt` filled with `parts` could exceed `_FOLD_LEN`, checked before rendering.

    Bounds what one render can allocate: each `{` or `%` field takes at most the longest
    part, and a width (`{:2000}`, `%2000s`, or a width handed in as a part) is refused
    outright past `_FOLD_LEN`, so `"{0}{0}".format(n)` chains and huge widths stop folding.
    """
    fields = fmt.count("{") + fmt.count("%")
    if len(fmt) + fields * max(map(len, parts), default=0) > _FOLD_LEN:
        return True
    return any(int(number) > _FOLD_LEN for text in (fmt, *parts) for number in re.findall(r"\d+", text))


#: Passes `_string_names` folds for: a chain of names this deep, written in any order, resolves.
_STRING_PASSES = 8


def _string_names(tree: ast.AST) -> _Folded:
    """Names assigned a constant string expression anywhere in the module, and their values.

    Repeated until nothing new folds, so a name built from another (`a = "pytest_"`,
    `key = a + "plugins"`) folds too, however long the chain and in whatever order it is
    written. `_STRING_PASSES` bounds the repetition, since `x += "a"` in a loop grows forever.
    """
    names = _Folded()
    wide: set[str] = set()  # names past `_FOLD_CAP`, which stay unfolded
    for _ in range(_STRING_PASSES):
        before = {key: len(values) for key, values in names.items()}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if value is None:
                    continue
                values = _const_str(value, names)
                for target in targets:
                    if isinstance(target, ast.Name) and values and target.id not in wide:
                        if isinstance(node, ast.AugAssign):
                            values = _concat([names.get(target.id, set()), values], names)
                        grown = names.setdefault(target.id, set()) | values
                        if len(grown) > _FOLD_CAP:
                            names.cut = True
                            wide.add(target.id)
                            del names[target.id]
                        else:
                            names[target.id] = grown
        if {key: len(values) for key, values in names.items()} == before:
            break
    else:
        names.cut = True  # still folding after the last pass, so a longer chain is unread
    return names


_MODULE_CALLS = frozenset({"__import__", "import_module"})
_LOADER_HINTS = _MODULE_CALLS | {"importlib", "builtins"}


def _plugins_tripwire(tree: ast.Module, read: set[int]) -> str | None:
    """What makes the module's `pytest_plugins` unreadable, or None when only literals declare it.

    A coarse tripwire, not a model of module semantics: the name used anywhere but the
    literal assignments `read` holds (a read, an alias, `+=`, `.append`, an attribute, a
    keyword, an import that binds it, a star import), code handed to `exec`/`eval`/`compile`
    that mentions it, or a module that reaches its own namespace or object: `globals()`, a
    bare `vars()` or `locals()`, `sys.modules[__name__]` or a load of `__name__`, and a
    module-scope `__getattr__`. A selector is not a defense against obfuscated tests, so
    spellings past these are not chased (every declaration in the tree was a plain literal
    list, Sun 18 Oct 2026).
    """
    for node in tree.body:
        defined = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "__getattr__"
        assigned = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__getattr__" for target in node.targets
        )
        imported = isinstance(node, ast.Import | ast.ImportFrom) and any(
            (alias.asname or alias.name) == "__getattr__" for alias in node.names
        )
        if defined or assigned or imported:
            return "defines a module `__getattr__`"
    for sub in ast.walk(tree):
        tripped = _call_tripwire(sub) if isinstance(sub, ast.Call) else _name_tripwire(sub, read)
        if tripped is not None:
            return tripped
    return None


def _name_tripwire(node: ast.AST, read: set[int]) -> str | None:
    """`_plugins_tripwire` for one node that is not a call."""
    if (isinstance(node, ast.Name) and node.id == "pytest_plugins" and id(node) not in read) or (
        isinstance(node, ast.Attribute) and node.attr == "pytest_plugins"
    ):
        return "uses `pytest_plugins` beyond one literal"
    if isinstance(node, ast.Import | ast.ImportFrom) and any(
        "pytest_plugins" in (alias.name, alias.asname) or alias.name == "*" for alias in node.names
    ):
        return "imports a name that can bind `pytest_plugins`"
    keyed = isinstance(node, ast.Subscript) and _is_registry(node.value) and _mentions_own_name(node.slice)
    merged = isinstance(node, ast.AugAssign) and _is_registry(node.target) and _mentions_own_name(node.value)
    if keyed or merged:  # `sys.modules[__name__]`, `sys.modules |= {__name__: ..}`
        return "reaches its own module object"
    return None


def _call_tripwire(call: ast.Call) -> str | None:
    """`_plugins_tripwire` for one call."""
    func = call.func
    callee = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    # `import_module(__name__)`, or any method of the registry (`sys.modules.get`, `.update`)
    # handed `__name__`.
    loads = callee in _MODULE_CALLS or _is_registry(getattr(func, "value", None))
    handed = [*call.args, *(keyword.value for keyword in call.keywords)]
    reasons = (
        (any(keyword.arg == "pytest_plugins" for keyword in call.keywords), "passes `pytest_plugins` as a keyword"),
        (callee == "globals" or (callee in {"vars", "locals"} and not call.args), "reads its own global namespace"),
        (loads and any(_mentions_own_name(arg) for arg in handed), "reaches its own module object"),
        (
            callee in _CODE_RUNNERS
            and any(
                isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "pytest_plugins" in sub.value
                for arg in call.args
                for sub in ast.walk(arg)
            ),
            "runs code that mentions `pytest_plugins`",
        ),
    )
    return next((reason for hit, reason in reasons if hit), None)


def _is_registry(expr: ast.AST | None) -> bool:
    """`sys.modules`, or any `<x>.modules`."""
    return isinstance(expr, ast.Attribute) and expr.attr == "modules"


def _mentions_own_name(expr: ast.AST) -> bool:
    return any(isinstance(sub, ast.Name) and sub.id == "__name__" for sub in ast.walk(expr))


def _pytest_plugins(tree: ast.Module, path: str) -> list[str]:
    """The plugin names a module's `pytest_plugins` declares, or UNKNOWN when they cannot be read.

    Only a module-level literal is read: `pytest_plugins = [...]`, a tuple, a bare string, or
    the annotated form. Anything `_plugins_tripwire` catches still loads its plugins at
    collection, so a guess would drop an import edge and let item 2 demote its consumer. It
    is FULL instead.
    """
    names: list[str] = []
    read: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if len(targets) != 1 or not (isinstance(targets[0], ast.Name) and targets[0].id == "pytest_plugins"):
            continue
        # pytest also accepts one plugin as a bare string.
        elements = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        if not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elements):
            raise FullSuite(f"UNKNOWN: `{path}` computes `pytest_plugins`, so which plugins it loads cannot be read")
        names += [e.value for e in elements if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        read.add(id(targets[0]))
    tripped = _plugins_tripwire(tree, read)
    if tripped is not None:
        raise FullSuite(f"UNKNOWN: `{path}` {tripped}, so which `pytest_plugins` it loads cannot be read")
    return names


def _plugin_edges(module_of: dict[str, Path], root: Path) -> dict[str, set[str]]:
    """plugin module -> modules naming it in `pytest_plugins` (a string import).

    Conftests and test modules both declare it (`tests/test_smartlists_delete.py` loads
    `tests.test_smartlists_route`), and pytest loads the plugin for either. A plugin module
    can declare its own, which pytest loads in turn, so every module reached as a plugin is
    read too, to the end of the chain. Elsewhere the name is an ordinary variable.
    """
    edges: dict[str, set[str]] = {}
    pending = [
        name
        for name, relative in module_of.items()
        if relative.name == "conftest.py" or _is_test_module(relative.as_posix())
    ]
    read: set[str] = set()
    while pending:
        name = pending.pop()
        if name in read:
            continue
        read.add(name)
        relative = module_of[name]
        tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=str(relative))
        for plugin in _pytest_plugins(tree, relative.as_posix()):
            edges.setdefault(plugin, set()).add(name)
            if plugin in module_of:
                pending.append(plugin)
    return edges


@dataclass(frozen=True)
class SourceTokens:
    """Path- and module-shaped tokens in a test's source.

    `ROOT / "docs/perf/register.md"` and `f"{ROOT}/docs/perf/register.md"` both have to
    name `docs/perf/register.md`, so every tail after a `/` is `written` too. And
    `monkeypatch.setattr("apps.x.y.func", ...)` has to name module `apps.x.y`, so every
    dotted prefix of a written token is a `prefix`.
    """

    text: str
    written: frozenset[str]
    prefixes: frozenset[str]

    @cached_property
    def written_outside_imports(self) -> frozenset[str]:
        """`written`, minus what only an import statement's module part writes.

        Lazy: only a reached `__main__` package (`Names.exact`) reads it, and computing it
        for every test file was half the source scan, measured Fri 16 Oct 2026.
        """
        outside_imports, _ = _tokens_of(_IMPORT_STATEMENT.sub("", self.text))
        return frozenset(outside_imports)

    def name_any(self, names: set[str]) -> bool:
        return not names.isdisjoint(self.written) or not names.isdisjoint(self.prefixes)

    def name_outside_imports(self, names: set[str]) -> bool:
        if not names:
            return False
        return not names.isdisjoint(self.written_outside_imports)


def _source_tokens(text: str) -> SourceTokens:
    written, prefixes = _tokens_of(text)
    return SourceTokens(text, frozenset(written), frozenset(prefixes))


@lru_cache(maxsize=8192)
def _test_source_snapshot(root_str: str, test_rel: str, mtime_ns: int) -> tuple[SourceTokens, str]:
    """Read and tokenize one test module; keyed by mtime so edits invalidate without a restart."""
    text = (Path(root_str) / test_rel).read_text(encoding="utf-8", errors="replace")
    return _source_tokens(text), text


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


def _dotted_from_path(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def deleted_modules(changed: list[str], graph: Graph, root: Path, policy: Policy) -> list[str]:
    """Changed Python paths that are gone from the tree, when item 3 selects them by name.

    Only a path absent on disk counts: a present module the graph skipped stays UNKNOWN.
    """
    if not policy.ci_config_by_name:
        return []
    return sorted(p for p in changed if p.endswith(".py") and p not in graph.name_of and not (root / p).exists())


def _refuse_unnarrowable(changed: list[str], graph: Graph, deleted: list[str], policy: Policy) -> None:
    if not changed:
        raise FullSuite("the diff is empty, which is indistinguishable from a failed diff")
    for path in changed:
        reason = global_reason(path, policy)
        if reason:
            raise FullSuite(f"global path changed: {reason}")
    unresolved = sorted(p for p in changed if p.endswith(".py") and p not in graph.name_of and p not in deleted)
    # Every import of a package child runs its `__init__.py`, an edge no name scan sees.
    packages = sorted(p for p in deleted if PurePosixPath(p).name == "__init__.py")
    if packages:
        raise FullSuite(f"UNKNOWN: deleted package initializer(s) are imported by every child: {', '.join(packages)}")
    if unresolved:
        raise FullSuite(
            "UNKNOWN: changed Python path(s) are not modules in the import graph "
            f"(deleted, renamed or skipped): {', '.join(unresolved)}"
        )


#: A package-relative module name, as `importlib.import_module(".gone", __package__)` takes it.
_RELATIVE_MODULE = re.compile(r"(\.+)([A-Za-z_][\w.]*)?")
_DOT_LITERAL = re.compile(r"""(?<![\w])[rbuf]{0,2}['"]\.""", re.I)


def _spread_call(call: ast.Call) -> tuple[list[ast.AST], dict[str, ast.AST]] | None:
    """A call's positional and keyword arguments with literal `*[...]` and `**{...}` spread out.

    `import_module(**{"name": ".gone", "package": __package__})` passes its name by keyword.
    None when an expansion is not a literal, since then which argument is which cannot be read.
    """
    args: list[ast.AST] = []
    for arg in call.args:
        if not isinstance(arg, ast.Starred):
            args.append(arg)
        elif isinstance(arg.value, (ast.List, ast.Tuple)) and not any(
            isinstance(e, ast.Starred) for e in arg.value.elts
        ):
            args.extend(arg.value.elts)
        else:
            return None
    keywords: dict[str, ast.AST] = {}
    for keyword in call.keywords:
        if keyword.arg is not None:
            keywords[keyword.arg] = keyword.value
        elif isinstance(keyword.value, ast.Dict) and all(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keyword.value.keys
        ):
            keywords.update(
                {
                    key.value: value
                    for key, value in zip(keyword.value.keys, keyword.value.values, strict=True)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
            )
        else:
            return None
    return args, keywords


_MODULE_TAIL = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\Z")
#: A printf conversion (`%s`, `%(pkg)s`), cut before a tail is read so `"%s.gone"` ends in `gone`.
_PRINTF_FIELD = re.compile(r"%(?:\(\w+\))?[-#0 +]*\d*(?:\.\d+)?[A-Za-z%]")


def _module_tails(
    node: ast.AST, names: dict[str, set[str]], bound: dict[str, list[ast.expr]], seen: set[str] | None = None
) -> set[str]:
    """What a module name may end in when its head does not fold.

    Every string the expression spells, folded or literal, contributes the dotted name it
    ends in: `f"{__package__}.gone"`, `pkg + ".gone"`, `".".join([pkg, "gone"])`,
    `"{}.gone".format(pkg)` and `"%s.gone" % pkg` all end in `gone`, whichever package the
    head turns out to be. One rule for every way to compose a string, rather than a case per
    operator (a `.format` target was missed by the per-operator version, Sat 17 Oct 2026).
    A name bound to such an expression (`TARGET = f"{pkg}.gone"`) counts too.

    `seen` is shared by the whole walk, so each name's bindings are read once. Tails are a
    union, so the path a name was reached by does not matter; a per-path set made
    `a = b + b; b = c + c; ...` exponential (it hung on PR #6233's deleted module).
    """
    seen = set() if seen is None else seen
    tails: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            spelled = {sub.value}
        elif isinstance(sub, ast.Name):
            spelled = names.get(sub.id, set())
            if sub.id not in seen:
                seen.add(sub.id)
                for value in bound.get(sub.id, []):
                    tails |= _module_tails(value, names, bound, seen)
        else:
            continue
        tails.update(
            match.group(1) for text in spelled if (match := _MODULE_TAIL.search(_PRINTF_FIELD.sub("\0", text)))
        )
    return tails


def _module_bindings(tree: ast.AST) -> dict[str, list[ast.expr]]:
    """Every value assigned to each plain name anywhere in the module."""
    bound: dict[str, list[ast.expr]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            pairs = [(node.target, node.value)]
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            # `for root in [SCRIPTS]` binds `root` to what the iterable holds; the iterable
            # stands for it, since a root check reads a container's elements.
            pairs = [(node.target, node.iter)]
        else:
            continue
        for target, value in pairs:
            if isinstance(target, ast.Name):
                bound.setdefault(target.id, []).append(value)
    return bound


def _relative_module_names(tree: ast.AST, path: str, text: str) -> tuple[set[str], set[str]]:
    """Absolute names of package-relative module strings passed to a call.

    `import_module(".gone", __package__)` writes neither `apps.core.gone` nor a path to it.
    Only a call to a loader (`_loader_aliases`) counts, so `helper(".gone", __package__)`
    stays a name; when a loader escapes into a container or call, any call might be one, so
    every call counts. A first argument that folds to a relative name resolves against a
    second argument or `package=` that folds, or else against this module's own package.
    A relative name starts with a dot and needs a package to anchor it, so only a source
    with a literal opening on a dot, and only a call with an anchor, is folded at all.

    Also returns tails (`_module_tails`): a relative name whose anchor does not read
    (`import_module(".gone", package)` in a helper) may land in any package, so it is
    matched by what it ends in, never resolved against this module's own package alone.
    """
    if not _DOT_LITERAL.search(text):
        return set(), set()
    own = _dotted_from_path(path)
    home = own if PurePosixPath(path).name == "__init__.py" else own.rpartition(".")[0]
    callers = _loader_aliases(tree)
    any_call = _loader_escapes(tree, callers)
    names_of: dict[str, set[str]] | None = None
    resolved: set[str] = set()
    tails: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if not any_call and name not in callers:
            continue
        spread = _spread_call(node)
        names_of = _string_names(tree) if names_of is None else names_of
        if spread is None:
            # `import_module(**KW)`: its arguments cannot be read, so any relative name the
            # module spells may be the one it loads, anchored here.
            spelled = {value for values in names_of.values() for value in values} | {
                const.value
                for const in ast.walk(tree)
                if isinstance(const, ast.Constant) and isinstance(const.value, str)
            }
            matches = [m for name in spelled if (m := _RELATIVE_MODULE.fullmatch(name))]
            # The keywords may hold any package, so match by the tail too.
            tails.update(m.group(2) for m in matches if m.group(2))
            bases = {home}
        else:
            args, keywords = spread
            named = [keywords["name"]] if "name" in keywords else []
            if not args and not named:
                continue
            given = [*args[1:2], *([keywords["package"]] if "package" in keywords else [])]
            first = args[0] if args else named[0]
            # A relative name needs an anchor: `import_module(".x")` with no package raises.
            if not given or (
                isinstance(first, ast.Constant) and not (isinstance(first.value, str) and first.value.startswith("."))
            ):
                continue
            matches = [m for name in _const_str(first, names_of) if (m := _RELATIVE_MODULE.fullmatch(name))]
            bases = {b for arg in given for b in _const_str(arg, names_of)}
            if not bases:
                # `import_module(".gone", package)`: the anchor does not read, so the name may
                # land in any package. Match it by its tail; never substitute this package.
                tails.update(m.group(2) for m in matches if m.group(2))
                continue
        resolved |= _anchor_relative(matches, bases)
    return resolved, tails


def _anchor_relative(matches: list[re.Match[str]], bases: set[str]) -> set[str]:
    """Each `_RELATIVE_MODULE` match resolved against each package in `bases`, as Python does."""
    resolved: set[str] = set()
    for match in matches:
        for base in bases:
            parts = base.split(".") if base else []
            up = len(match.group(1)) - 1
            if up > len(parts):
                continue
            rest = [match.group(2)] if match.group(2) else []
            resolved.add(".".join(parts[: len(parts) - up] + rest))
    return resolved


_DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _loader_aliases(tree: ast.AST) -> set[str]:
    """`_MODULE_CALLS` plus every name bound to one, through chains (`load = import_module`, `again = load`)."""
    callers = set(_MODULE_CALLS)
    while True:
        known = len(callers)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                callers.update(alias.asname for alias in node.names if alias.name in callers and alias.asname)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(
                node.value, (ast.Name, ast.Attribute)
            ):
                # `load = ...`, `load: object = ...` and `(load := ...)` all bind a name.
                bound = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if bound in callers:
                    callers.update(target.id for target in targets if isinstance(target, ast.Name))
        if len(callers) == known:
            return callers


def _loop_strings(tree: ast.AST, names_of: dict[str, set[str]]) -> dict[str, set[str]]:
    """Loop variables and the strings they take: `for name in ("apps.a", "apps.b")` binds both."""
    looped: dict[str, set[str]] = {}
    for loop in ast.walk(tree):
        if isinstance(loop, (ast.For, ast.comprehension)) and isinstance(loop.target, ast.Name):
            items = loop.iter.elts if isinstance(loop.iter, (ast.Tuple, ast.List, ast.Set)) else [loop.iter]
            for item in items:
                looped.setdefault(loop.target.id, set()).update(_const_str(item, names_of))
    return looped


def _names_a_loader_by_string(tree: ast.AST) -> bool:
    """Whether some string expression in the module can spell a `_MODULE_CALLS` name."""
    if any(isinstance(node, ast.Constant) and node.value in _MODULE_CALLS for node in ast.walk(tree)):
        return True
    names = _string_names(tree)
    if any(values & _MODULE_CALLS for values in names.values()):
        return True
    return any(
        isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)) and _const_str(node, names) & _MODULE_CALLS
        for node in ast.walk(tree)
    )


def _loader_escapes(tree: ast.AST, callers: set[str]) -> bool:
    """Whether a loader is used other than by a call or a plain `name = loader` binding.

    `load, other = importlib.import_module, None`, a loader in a dict, or one passed as an
    argument binds it where `_loader_aliases` cannot follow, so its calls cannot be read.
    A loader's name spelled as a string (`getattr(importlib, "import_module")`, folded or
    not) fetches it the same way.
    """
    if _names_a_loader_by_string(tree):
        return True
    used: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            used.add(id(node.func))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(target, ast.Name) for target in targets):
                used.add(id(node.value))
    return any(
        id(node) not in used
        and (
            (isinstance(node, ast.Name) and node.id in callers)
            or (isinstance(node, ast.Attribute) and node.attr in callers)
        )
        for node in ast.walk(tree)
    )


def _import_levels(level: ast.AST, tree: ast.AST, path: str) -> set[int]:
    """Every `level` an `__import__` call can run with.

    A constant, or a name whose every binding in the module assigns a constant, gives those
    ints. A non-int constant raises at run time, so it imports nothing. Anything else (a
    parameter, a name assigned an expression in any scope, an augmented or loop binding)
    could be any level, so every level from 0 to the module's depth counts.
    """
    every = set(range(len(PurePosixPath(path).parts) + 1))
    if isinstance(level, ast.Constant):
        return {level.value} if type(level.value) is int and level.value >= 0 else set()
    if not isinstance(level, ast.Name):
        return every
    values: set[int] = set()
    assigned: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == level.id for t in targets):
                continue
            assigned.update(id(t) for t in targets)
            value = node.value
            if not isinstance(value, ast.Constant):
                return every
            if type(value.value) is int and value.value >= 0:
                values.add(value.value)
    for node in ast.walk(tree):
        # Any other binding of the name: a parameter, a loop or `with` target, an import.
        if (
            (
                isinstance(node, ast.Name)
                and node.id == level.id
                and isinstance(node.ctx, ast.Store)
                and id(node) not in assigned
            )
            or (isinstance(node, ast.arg) and node.arg == level.id)
            or (isinstance(node, ast.alias) and (node.asname or node.name) == level.id)
        ):
            return every
    return values if assigned else every


def _anchor_level(names: set[str], level: int, path: str) -> set[str]:
    """`names` resolved `level` packages up from the module at `path`, or empty when that leaves the tree."""
    if level == 0:
        return set(names)
    own = _dotted_from_path(path)
    parts = (own if PurePosixPath(path).name == "__init__.py" else own.rpartition(".")[0]).split(".")
    up = level - 1
    if up >= len(parts):
        return set()
    base = parts[: len(parts) - up]
    return {".".join([*base, name]) for name in names}


def _absolute_module_names(tree: ast.AST, path: str, text: str) -> tuple[set[str], bool, set[str]]:
    """Absolute module names a module imports through `import_module(...)` or `__import__(...)`,
    and whether some such call takes a name that does not fold.

    `importlib.import_module("apps.core.gone")` is an import the static graph does not see.
    Only a call to one of `_MODULE_CALLS` (by name, attribute, or a name bound to either)
    counts, and each dotted parent is imported with it, as Python does. A name handed to
    any other call (`runpy.run_module(...)`) stays a weak named reference. A target that
    does not fold (a parameter, a container, data read at run time) imports nothing named
    here, but the caller is told, since the module may be loading what it names. A target
    whose head does not fold but whose tail does (`f"{__package__}.gone"`) also returns
    that tail (`_module_tails`), matched against what a deleted module ends in.
    """
    # A loader lives in `importlib` or `builtins`, so a module naming neither, nor a loader,
    # cannot reach one (`getattr(importlib, "import_" + "module")` still names `importlib`).
    if not any(hint in text for hint in _LOADER_HINTS):
        return set(), False, set()
    callers = _loader_aliases(tree)
    names_of: dict[str, set[str]] | None = None
    looped: dict[str, set[str]] = {}
    bound: dict[str, list[ast.expr]] | None = None
    resolved: set[str] = set()
    tails: set[str] = set()
    unfolded = _loader_escapes(tree, callers)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        ) not in callers:
            continue
        spread = _spread_call(node)
        if spread is None:
            # `import_module(*names)`: which name it loads cannot be read.
            unfolded = True
            continue
        args, keywords = spread
        first = args[0] if args else keywords.get("name")
        if first is None:
            continue
        if names_of is None:
            names_of = _string_names(tree)
            looped = _loop_strings(tree, names_of)
        targets = _const_str(first, names_of) | (looped.get(first.id, set()) if isinstance(first, ast.Name) else set())
        dotted = {name for name in targets if _DOTTED.fullmatch(name)}
        unfolded |= not dotted
        # A relative target (`".gone"`) is `_relative_module_names`'s, anchored there.
        if not dotted and not any(target.startswith(".") for target in targets):
            bound = _module_bindings(tree) if bound is None else bound
            tails |= _module_tails(first, names_of, bound)
        # `__import__("gone", globals(), locals(), (), 1)` is relative: `level` dots up from here.
        level = args[4] if len(args) > 4 else keywords.get("level")
        if level is not None:
            # `LEVEL = 1` folds like a literal; a level that does not read loads nothing named here.
            dotted = {
                name for value in _import_levels(level, tree, path) for name in _anchor_level(dotted, value, path)
            }
            unfolded |= not dotted
        for name in dotted:
            parts = name.split(".")
            resolved.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return resolved, unfolded, tails


def _importers_of_deleted(deleted: list[str], graph: Graph, root: Path) -> tuple[set[str], set[str]]:
    """Modules still in the tree that import a deleted module, and those that only name it.

    The graph has no node for a deleted module, so it has no edge from `apps/a.py` to it
    either. A test that imports only `apps.a` then fails to collect through `apps.a`, and
    is found only by seeding the reach with every module that still names the deleted one.
    Imports are read from the AST (`_imports_of`), so `from apps.core import gone`, a
    relative `from .gone import`, parentheses and backslash continuations all resolve; a
    source that does not parse is UNKNOWN, since what it imports cannot be read. A module
    that only names it (`runpy.run_module("apps.core.gone")`) is returned apart, because
    that link is a name and stays weak for item 2.
    """
    if not deleted:
        return set(), set()
    names = Names()
    for path in deleted:
        names.add(path, _dotted_from_path(path))
    deleted_names = {_dotted_from_path(path) for path in deleted}
    imported: set[str] = set()
    named: set[str] = set()
    loaders: set[str] = set()
    composers: set[str] = set()
    # Modules asking for an attribute spelled like a deleted module (`from loader import
    # gone`, `loader.gone`): a loader's `__getattr__` may import it on that request.
    requests: set[str] = set()
    deleted_tails = {module.rsplit(".", 1)[-1] for module in deleted_names}
    hints = _by_name_hints(deleted)
    for name, path in graph.path_of.items():
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError as error:
            raise FullSuite(
                f"UNKNOWN: `{path}` does not parse, so whether it imports deleted module(s) "
                f"{', '.join(deleted)} cannot be read"
            ) from error
        if not deleted_tails.isdisjoint(_requested_attributes(tree)):
            requests.add(name)
        # pytest imports a `pytest_plugins` entry at collection, so naming one there is an import.
        plugins = (
            set(_pytest_plugins(tree, path))
            if PurePosixPath(path).name == "conftest.py" or _is_test_module(path) or name in graph.plugin_modules
            else set()
        )
        # `import_module("apps.core.gone")` and `import_module(".gone", __package__)` are
        # imports too: a name handed to an import call is a strong link, not a name.
        absolute, unfolded, tails = _absolute_module_names(tree, path, text)
        # `exec("import apps.core.gone")` runs code the import graph cannot read either, and
        # `exec(f"import {__package__}.gone")` is matched by the name it ends in.
        if _executes_code(tree, text):
            unfolded = True
            tails |= _executed_tails(tree)
        if unfolded:
            loaders.add(name)
        relative, relative_tails = _relative_module_names(tree, path, text)
        dynamic = relative | absolute
        tails |= relative_tails
        if not names.any_form.isdisjoint(_imports_of(root / path, root) | plugins | dynamic) or any(
            module == tail or module.endswith("." + tail) for module in deleted_names for tail in tails
        ):
            imported.add(name)
        elif names.named_by(_source_tokens(text)):
            # It names the deleted module and hands a name we cannot read to a loader
            # (`for name in NAMES: import_module(name)`), so it may be importing it.
            (imported if unfolded else named).add(name)
        elif (composed := _composed_strings(text, path, hints)) is None or names.named_by(_source_tokens(composed)):
            # The same, with the name composed (`LOADERS["py"]("apps.core" + ".gone")`); a
            # module that only composes it (`TARGET = "apps.core" + ".gone"`) can hand it on.
            (imported if unfolded else composers).add(name)
        elif not (unfolded or relative) and any(
            module == tail or module.endswith("." + tail)
            for module in deleted_names
            for tail in _relative_strings(tree)
        ):
            # A name relative to a package it imports nothing with here (`load(".gone",
            # "apps.core")` through a helper) can be handed on too; one this module anchors
            # itself was resolved above.
            composers.add(name)
    # A generic helper (`def load(name): return import_module(name)`) names nothing itself;
    # a module that imports it, directly or through a re-export, and names the deleted
    # module may hand it that name.
    # Every loader, and every module that imports one at any depth: a test holding
    # `import_module` itself can hand it a name it imports from elsewhere.
    reach = loaders | _importer_closure(loaders, graph)
    # The name and the loader can also meet in a third module: `target.py` holds
    # `TARGET = "apps.core.gone"`, and a test imports it and the helper, then runs
    # `load(TARGET)`. Every module that imports a namer, at any depth, may hand it on.
    carries = _importer_closure(named | composers, graph)
    # A caller may compose the name it hands the helper (`load("apps.core" + ".gone")`).
    joined = (named & reach) | (composers & reach) | (reach & carries) | (requests & reach)
    # A composer no loader meets is still a name for a module (`TARGET = "apps.core" + ".gone"`
    # handed to `runpy.run_module`): kept as a weak named reference, like a literal one.
    return imported | joined, (named | composers) - joined


def _requested_attributes(tree: ast.AST) -> set[str]:
    """Every attribute the source asks of another object: `from x import a` and `x.a`."""
    return {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def _importer_closure(seeds: set[str], graph: Graph) -> set[str]:
    """Every module that imports one of `seeds`, at any depth (not the seeds themselves)."""
    reach: set[str] = set()
    frontier = set(seeds)
    while frontier:
        frontier = {i for module in frontier for i in graph.importers.get(module, set())} - reach
        reach |= frontier
    return reach


#: A module name relative to a package handed to an import call (`".gone"`, `"..core.gone"`).
_RELATIVE_NAME = re.compile(r"\.+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")


def _relative_strings(tree: ast.AST) -> set[str]:
    """What every relative module name the source spells ends in: `".gone"` is `gone`. Its
    anchor is whatever package the caller hands with it, so only the tail is matched."""
    return {
        match.group(1)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        if (match := _RELATIVE_NAME.fullmatch(node.value))
    }


def _executed_tails(tree: ast.Module) -> set[str]:
    """What each call's arguments may import when the module runs source text: `exec(f"import
    {__package__}.gone")` ends in `gone`. Every call counts, since a runner can be aliased."""
    names, bound = _string_names(tree), _module_bindings(tree)
    return {
        tail
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for arg in node.args
        for tail in _module_tails(arg, names, bound)
    }


def _executes_code(tree: ast.AST, text: str) -> bool:
    """Whether the module can run source text through builtin `exec`, `eval` or `compile`.

    Any reference to one counts, not only a call: `run = exec` then `run(...)`, or
    `getattr(builtins, "exec")`, reach it all the same. A method that shares the name
    (`re.compile(...)`) does not count; `builtins.exec` does.
    """
    if not any(word in text for word in (*_CODE_RUNNERS, "builtins")):
        return False
    if "builtins" in text and _names_a_runner_by_string(tree):
        return True
    # `import builtins as b` makes `b.exec` the runner; `from builtins import exec as run`
    # binds it under any name, so importing it counts.
    modules = {"builtins", "__builtins__"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "builtins" and alias.asname
    }
    return any(
        (isinstance(node, ast.Name) and node.id in _CODE_RUNNERS)
        or (
            isinstance(node, ast.ImportFrom)
            and node.module == "builtins"
            and any(alias.name in _CODE_RUNNERS for alias in node.names)
        )
        or (
            isinstance(node, ast.Attribute)
            and node.attr in _CODE_RUNNERS
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        )
        or (isinstance(node, ast.Constant) and node.value in _CODE_RUNNERS)
        for node in ast.walk(tree)
    )


_CODE_RUNNERS = frozenset({"exec", "eval", "compile"})


def _names_a_runner_by_string(tree: ast.AST) -> bool:
    """`getattr(builtins, "ex" + "ec")` or `__builtins__[name]`: a runner looked up by a
    composed string, which never spells the runner in the source."""
    names = _string_names(tree)  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            keys = node.args[1:2]
        elif isinstance(node, ast.Subscript):
            keys = [node.slice]
        else:
            continue
        if any(not _CODE_RUNNERS.isdisjoint(_const_str(key, names)) for key in keys):
            return True
    return False


def _composed_strings(text: str, path: str, hints: set[str]) -> str | None:
    """Every constant string the module composes, one per line, for a by-name scan.

    `NAME = "new" + ".yml"` then `(WORKFLOWS / NAME).read_text()` writes neither `new.yml`
    nor the full path, so the raw token scan misses it. `_const_str` folds `+`, f-strings,
    `%`, `.format` and `"".join`, through names assigned anywhere in the module. A composed
    name realistically keeps a stem or an extension as a literal (`f"{stem}.yml"`), so only
    a module whose text holds one of `hints` is parsed: folding every module costs ~45 s.
    None when a fold was cut at `_FOLD_CAP` or `_FOLD_LEN`: the name may be among what was
    left unfolded, so the caller counts the module as naming it (over-selects).
    """
    if not any(hint in text for hint in hints):
        return ""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return ""
    names_of = _string_names(tree)
    found = {value for values in names_of.values() for value in values}
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
            found |= _const_str(node, names_of)
    if names_of.cut:
        return None
    return "\n".join(repr(value) for value in sorted(found))


def _by_name_hints(paths: list[str]) -> set[str]:
    """A changed path's stem, and its extension unless that is `py` (in every module)."""
    hints: set[str] = set()
    for path in paths:
        pure = PurePosixPath(path)
        hints.add(pure.stem)
        if pure.suffix and pure.suffix != ".py":
            hints.add(pure.suffix[1:])
    return hints


def _select_through_named_helpers(
    changed: list[str],
    couplings: list[Coupling],
    graph: Graph,
    universe: list[str],
    root: Path,
    selection: Selection,
) -> None:
    """Item 3: a changed workflow or CI script a helper or conftest names or walks selects its tests.

    A workflow only a helper globs (`scripts/runner_toolset_scan.py`) is walked by no test,
    so a scan of test modules alone selects none of its consumers. Every module that names
    a changed `BY_NAME_PATTERNS` path, or matches a fired `BY_NAME_COUPLINGS` tree reader,
    seeds the reach instead. The link is still a name or a tree walk, so the rule stays
    weak for item 2. Only those paths: item 3 leaves every other change set as it found it
    (a data file the root conftest names made 35 of 83 replayed heads FULL, Sat 17 Oct 2026).
    """
    names = Names()
    by_name = [path for path in changed if any(fnmatch(path, pattern) for pattern in BY_NAME_PATTERNS)]
    for path in by_name:
        # A helper can name a CI script as a path (`TARGET = "scripts/ci_helper.py"`) or a
        # module (`runpy.run_module("scripts.ci_x")`) without importing it.
        names.add(path, graph.name_of.get(path))
    hints = _by_name_hints(by_name)
    tree_readers = [c for c in couplings if c in BY_NAME_COUPLINGS]
    if not names.any_form and not tree_readers:
        return
    named: set[str] = set()
    walking: set[str] = set()
    # A module can name the tree and hand it to a generic helper that does the walking
    # (`files(ROOT / ".github/workflows")` over `root.glob("*.yml")`), so neither file
    # matches alone. Such a module walks the tree when it imports, at any depth, a reader.
    pointing: set[str] = set()
    reading: set[str] = set()
    rooted = [c for c in tree_readers if c.walk_roots]
    for name, path in graph.path_of.items():
        # Test modules are seeds too: a test that names or walks the file may itself be
        # imported by another test (tests/test_codex_followups_d.py imports test_conformance).
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        composed = _composed_strings(text, path, hints) if names.any_form else ""
        if names.any_form and (composed is None or names.named_by(_source_tokens(text + "\n" + composed))):
            named.add(name)
            continue
        code = _code_of(text)
        if any(coupling.matches(text) for coupling in tree_readers) and any(
            coupling.matches(code) for coupling in tree_readers
        ):
            walking.add(name)
            continue
        # A module can walk its own directory by `__file__` (`Path(__file__).parent.glob(..)`
        # in `scripts/x.py`) without spelling the root at all.
        if any(coupling.uses_own_directory(code, path) for coupling in rooted):
            walking.add(name)
            continue
        for coupling in tree_readers:
            # A root name is too common a needle to pair with any walk elsewhere: pairing it
            # made the root conftest a CI-script walker, which runs every test.
            if coupling.walk_roots:
                continue
            if any(needle in code for needle in coupling.needles):
                pointing.add(name)
            if coupling.readers is not None and coupling.readers.search(code):
                reading.add(name)
    # Wherever the two meet: a module naming the tree that imports a reader, a reader that
    # imports the tree's path (`from paths import ROOT`, `ROOT.glob`), or a third module that
    # imports both and hands one to the other (`files(ROOT)`).
    walking |= reverse_reach(pointing, graph.importers) & reverse_reach(reading, graph.importers)
    _select_through_helpers(
        named, "named reference", graph, universe, selection,
        through_conftests=True, distance=NAMED_REFERENCE_DISTANCE,
    )  # fmt: skip
    # A conftest that walks the workflows, or imports a helper that does, runs at collection
    # for every test under it, so those tests are selected too. Shared fixtures are not
    # item 3 (they stay FULL until #7228), so the fixture resolver never reaches this.
    _select_through_helpers(
        walking, "coupling `by name`", graph, universe, selection,
        through_conftests=True, distance=COUPLING_DISTANCE,
    )  # fmt: skip


@lru_cache(maxsize=16384)  # pure, like _uses_root: one parse per source however many change sets ask
def _code_of(text: str) -> str:
    """`text` without comments or docstrings, so prose mentioning `.github` is not a walk.

    Only helpers are read this way: the root `tests/conftest.py` cites a workflow in a
    docstring and rglobs test files, which as raw text reads as a workflow walker that
    configures every test. Source that does not parse is returned whole (it can only
    over-select).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(tree)


def _select_through_helpers(
    helpers: set[str],
    link: str,
    graph: Graph,
    universe: list[str],
    selection: Selection,
    *,
    through_conftests: bool,
    distance: int,
) -> None:
    """Tests importing a helper, and with `through_conftests` tests under a conftest that imports one.

    `distance` ranks them for a hard cap: a name is a string match (NAMED_REFERENCE_DISTANCE), a
    tree walk a declared coupling (COUPLING_DISTANCE); the fit refuses a module with no distance.
    """
    universe_set = set(universe)
    for name in sorted(reverse_reach(helpers, graph.importers)):
        path = graph.path_of[name]
        if PurePosixPath(path).name == "conftest.py":
            if not through_conftests:
                continue
            directory = _parent(path)
            if directory in (".", "tests"):
                raise FullSuite(f"a changed file is named by `{path}`, which configures every test")
            for test in universe:
                if _under(test, directory):
                    selection.add(test, f"{link} via conftest `{path}`", distance)
        elif path in universe_set:
            selection.add(path, f"{link} via helper", distance)


def _seed_modules(changed: list[str], graph: Graph) -> set[str]:
    """Changed modules, every module of a changed package, and package data's neighbors."""
    seeds: set[str] = set()
    for path in changed:
        if path.endswith(".py") and path not in graph.name_of:
            continue  # a deleted module (`deleted_modules`): it is selected by name only
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


def _reach_through_packages(
    seeds: set[str], graph: Graph, *, one_hop_seeds: frozenset[str] = frozenset()
) -> dict[str, int]:
    """`reach_depths`, where a reached package initializer also reaches its package's modules.

    Importing `apps.core.other` runs `apps/core/__init__.py` first, so a module reached
    through an initializer (one that imports a changed or deleted module) breaks every
    importer of any module in that package, though none of them imports the initializer by
    name. `_seed_modules` already expands a CHANGED initializer this way; this expands one
    the reach arrives at, repeated until no depth improves. A package module counts one hop
    past its initializer, even when another path reached it first by a longer route.
    """
    starts = dict.fromkeys(seeds, 0)
    starts.update(dict.fromkeys(one_hop_seeds - seeds, 1))  # a deleted module's direct importers
    while True:
        reached = _reach_from(starts, graph.importers)
        packages = {
            name: depth for name, depth in reached.items() if PurePosixPath(graph.path_of[name]).name == "__init__.py"
        }
        improved = False
        for module in graph.path_of:
            through = [depth + 1 for package, depth in packages.items() if module.startswith(package + ".")]
            if through and min(through) < reached.get(module, min(through) + 1):
                starts[module] = min(through)
                improved = True
        if not improved:
            return reached


def _reach_from(starts: dict[str, int], importers: dict[str, set[str]]) -> dict[str, int]:
    """`reach_depths` from seeds that each start at their own depth: the shortest hops to each module."""
    depth: dict[str, int] = {}
    heap = [(hops, name) for name, hops in starts.items()]
    heapq.heapify(heap)
    while heap:
        hops, name = heapq.heappop(heap)
        if name in depth:
            continue
        depth[name] = hops
        for importer in importers.get(name, ()):
            if importer not in depth:
                heapq.heappush(heap, (hops + 1, importer))
    return depth


def _select_test_areas(changed: list[str], universe: list[str], selection: Selection) -> None:
    """Data under tests/<area>/ selects that area; a file directly in tests/ is left to names."""
    for path in changed:
        parts = PurePosixPath(path).parts
        if parts[:1] == ("tests",) and len(parts) >= 3 and not path.endswith(".py"):
            area = "/".join(parts[:2])
            for test in universe:
                if _under(test, area):
                    selection.add(test, f"test-area data `{path}`", 1)


def _select_by_conftest(reached: dict[str, int], graph: Graph, universe: list[str], selection: Selection) -> None:
    for name in sorted(reached):
        path = graph.path_of[name]
        if PurePosixPath(path).name != "conftest.py":
            continue
        directory = _parent(path)
        if directory in (".", "tests"):
            raise FullSuite(f"changed code is imported by `{path}`, which configures every test")
        for test in universe:
            if _under(test, directory):
                selection.add(test, f"conftest reach `{path}`", reached[name] + 1)


def _select_by_source(
    names: Names,
    couplings: list[Coupling],
    universe: list[str],
    root: Path,
    selection: Selection,
) -> None:
    root_str = str(root)
    for test in universe:
        stat = (root / test).stat()
        tokens, text = _test_source_snapshot(root_str, test, stat.st_mtime_ns)
        if names.named_by(tokens):
            selection.add(test, "named reference", NAMED_REFERENCE_DISTANCE)
        for coupling in couplings:
            if coupling.matches(text):
                selection.add(test, f"coupling `{coupling.changed_prefix}`", COUPLING_DISTANCE)


#: Whole-tree guard test modules a narrowed PR head always runs, whatever it changed
#: (CICD-SPEED L00). Read by `always_run_modules`; why each entry is there is in the file.
ALWAYS_RUN_NAME = "ci/pr-always-run.txt"


def always_run_modules(root: Path) -> list[str]:
    """The test module paths `ci/pr-always-run.txt` lists; `#` starts a comment.

    A missing list is FullSuite, never an empty list: the guards it names are the tests
    that a narrowed head is otherwise most likely to skip and break main with.
    """
    try:
        return read_commented_list(root / ALWAYS_RUN_NAME)
    except FileNotFoundError as missing:
        raise FullSuite(
            f"the always-run list `{ALWAYS_RUN_NAME}` is missing, so the whole-tree guards "
            "cannot be added to a selection"
        ) from missing


def _collected_always_run(universe: list[str], root: Path) -> list[str]:
    """Every always-run entry, each checked against the lane; an absent one is FullSuite, never skipped."""
    collected = set(universe)
    modules = always_run_modules(root)
    for module in modules:
        if module not in collected:
            raise FullSuite(
                f"always-run entry `{module}` is not a test module the lane collects "
                f"(renamed, deleted or lane-ignored); fix `{ALWAYS_RUN_NAME}`"
            )
    return modules


def _select_always_run(universe: list[str], root: Path, selection: Selection) -> list[str]:
    """Whole-tree guards scan the tree, not a changed module, so no link reaches them."""
    modules = _collected_always_run(universe, root)
    for module in modules:
        selection.add(module, "always run (whole-tree guard)", ALWAYS_RUN_DISTANCE)
    return modules


#: Whole-tree BOOKKEEPING guards a Python or test-layout change most often breaks, ranked WITH the
#: change (distance 0, the rank of a `CI-Tests:` line) instead of at the always-run list's 2. Main
#: run 9a7f78f300 (Wed 4 Nov 2026 11:06Z) went red on three of their tests after #5858 (scoped
#: tests/autodj in the justfile, left it in KNOWN_UNSCOPED) and #8325 (a marked test without an
#: [if]/[then] line) merged green: both heads ranked this list at 2 and the hard cap
#: (ci/test-budget.yml) cut #8325 from distance 2 and #5858 from distance 1. The same trio was
#: red on #8322, #8304 and #7107 that morning. Whole modules: 0.4 s, 6.9 s and 20.8 s locally.
TREE_BOOKKEEPING_GUARDS: tuple[str, ...] = (
    "(private path)",
    "(private path)",
    "(private path)",
)
TREE_BOOKKEEPING_DISTANCE = 0
#: What those guards read: Python under these trees, plus the gate's `pytest_scope` (justfile),
#: the requirement ids markers must resolve to, and the pytest and scope config.
TREE_BOOKKEEPING_PY_ROOTS: tuple[str, ...] = ("tests/", "scripts/", "apps/")
TREE_BOOKKEEPING_FILES = frozenset(
    {"justfile", "ci/test-scopes.yml", "pytest.ini", "pyproject.toml", "reqs.json", ".planning/REQUIREMENTS.md"}
)
#: Per-id requirement fragments (issue #10541) feed the same reqs.json the markers resolve to.
TREE_BOOKKEEPING_PREFIXES: tuple[str, ...] = ("(private path)/",)
TREE_BOOKKEEPING_REASON = "tree bookkeeping guard (Python change)"


def touches_tree_bookkeeping(changed: list[str]) -> bool:
    """True when a changed path is an input of the tree bookkeeping guards."""
    return any(
        path in TREE_BOOKKEEPING_FILES
        or path.startswith(TREE_BOOKKEEPING_PREFIXES)
        or (path.endswith(".py") and path.startswith(TREE_BOOKKEEPING_PY_ROOTS))
        for path in changed
    )


def _select_tree_bookkeeping(changed: list[str], always_run: list[str], selection: Selection) -> None:
    """Rank the bookkeeping guards at distance 0 for a change that feeds them.

    Only guards the always-run list also names are pinned (it has already checked each against
    the lane), so a tree whose list omits them, a synthetic test tree included, pins nothing;
    (private path) holds the real list to all three.
    """
    if not touches_tree_bookkeeping(changed):
        return
    for module in TREE_BOOKKEEPING_GUARDS:
        if module in always_run:
            selection.add(module, TREE_BOOKKEEPING_REASON, TREE_BOOKKEEPING_DISTANCE)


#: Guards that read ONE committed artifact rather than importing what they check, ranked at distance
#: 0 when a change touches that artifact's inputs. No import link reaches them (one parses a markdown
#: file, the other dumps the schema in a subprocess), so before this a change that broke them selected
#: neither. Main run b011e2ca95 (Wed 4 Nov 2026) went red on both: #3807 wrote "Tue 23 Sep 2026" (a
#: Wednesday) into .planning/REQUIREMENTS.md, and #8314 added a 409 to the cloudsync machine-rename
#: route without regenerating apps/webui/openapi.json (that diff ran only in the push-only `contracts`
#: job). Measured locally: 1.3 s and about 6 s per module.
@dataclass(frozen=True)
class InputPinnedGuard:
    """A guard module pinned at distance 0 when a changed path is one of its inputs."""

    module: str
    reason: str
    files: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()

    def fires_for(self, path: str) -> bool:
        return path in self.files or path.startswith(self.prefixes)


REQUIREMENTS_WEEKDAY_GUARD = "(private path)"
OPENAPI_CURRENCY_GUARD = "tests/quality/test_openapi_is_current.py"
INPUT_PINNED_DISTANCE = 0
WORKFLOW_INPUTS: tuple[str, ...] = (".github/workflows/", "ci/")
WORKFLOW_READER_GUARDS: tuple[str, ...] = (
    "(private path)",
    "(private path)",
    "(private path)",
    "(private path)",
    "(private path)",
    "tests/scripts/test_trunk_queue.py",
)
TREE_READ_CENSUS_GUARD = "(private path)"
INPUT_PINNED_GUARDS: tuple[InputPinnedGuard, ...] = (
    # The checksum scans only .planning/REQUIREMENTS.md (scripts/requirements_weekday_checksum.py).
    InputPinnedGuard(
        REQUIREMENTS_WEEKDAY_GUARD,
        "weekday checksum guard (REQUIREMENTS.md change)",
        files=frozenset({".planning/REQUIREMENTS.md", "scripts/requirements_weekday_checksum.py"}),
    ),
    # The engine app's routes and wiring live under these trees; the two generated artifacts are
    # the committed contract and its TypeScript client, regenerated together.
    InputPinnedGuard(
        OPENAPI_CURRENCY_GUARD,
        "openapi currency guard (route or contract change)",
        files=frozenset({"apps/webui/openapi.json", "apps/webui/frontend/src/lib/api-types.ts"}),
        prefixes=("apps/webui/server/", "apps/engine_core/"),
    ),
    # Workflow readers: each parses .github/workflows/*.yml or a ci/ data file directly, so a
    # workflow edit reaches them by name at best, and the cap or the weak-link demotion dropped
    # them. #8633 made the shard matrix `fromJSON(...)` and left the ledger guard red on main
    # while main's own selections skipped them (#8727; test_trunk_queue.py stayed red past #8781).
    # ci/ is in the prefix set because ci/test-scopes.yml, ci/pr-always-run.txt and
    # ci/trunk-merge-queue.json are their other inputs.
    *(
        InputPinnedGuard(module, "workflow reader guard (.github/workflows or ci/ change)", prefixes=WORKFLOW_INPUTS)
        for module in WORKFLOW_READER_GUARDS
    ),
    # The census runs nested pytest sessions under the root conftest, so a new conftest plugin
    # (#8630's leak gate) changes what those sessions report.
    InputPinnedGuard(
        TREE_READ_CENSUS_GUARD,
        "tree-read census guard (conftest, test support or census change)",
        files=frozenset({"conftest.py", "tests/conftest.py", "scripts/pytest_tree_read_census.py"}),
        prefixes=("tests/support/", *WORKFLOW_INPUTS),
    ),
)


#: Registry guards that must classify every module in the tree, ranked at distance 0 when a change ADDS a
#: `.py` file under these roots (a rename counts: `--no-renames` lists its new path as added). A modified
#: module is already in both registries; a new one is in neither until someone adds it, and no import
#: link reaches either guard. Main went red twice on Fri 6 Nov 2026 after #9262 added
#: apps/analysis/native_tables_sql.py: the sync-drift DDL classification and the derived carry-path list
#: both missed it, and the PR head ranked both modules at the always-run list's 2 behind the cap.
NEW_MODULE_ROOTS: tuple[str, ...] = ("apps/", "scripts/")
NEW_MODULE_REGISTRY_GUARDS: tuple[InputPinnedGuard, ...] = (
    InputPinnedGuard(
        "(private path)",
        "new-module registry guard (sync-drift DDL classification)",
        prefixes=NEW_MODULE_ROOTS,
    ),
    InputPinnedGuard(
        "tests/scripts/test_ci_plan_carry_paths.py",
        "new-module registry guard (derived helper_carried_paths)",
        prefixes=NEW_MODULE_ROOTS,
    ),
)


def new_module_guards_for(added: list[str]) -> list[InputPinnedGuard]:
    """The registry guards a set of ADDED paths feeds: any new `.py` file under `NEW_MODULE_ROOTS`."""
    modules = [path for path in added if path.endswith(".py")]
    return [guard for guard in NEW_MODULE_REGISTRY_GUARDS if any(guard.fires_for(path) for path in modules)]


def input_pinned_guards_for(changed: list[str]) -> list[InputPinnedGuard]:
    """The input-pinned guards a change set feeds."""
    return [guard for guard in INPUT_PINNED_GUARDS if any(guard.fires_for(path) for path in changed)]


def _select_input_pinned(
    changed: list[str],
    universe: list[str],
    selection: Selection,
    added: list[str] | None = None,
    pinned: tuple[tuple[str, str], ...] = (),
) -> None:
    """Rank each guard a change feeds at distance 0, the new-module registry guards for `added` paths
    and the caller's `pinned` (module, reason) pairs.

    Only a guard the lane collects is pinned, so a synthetic test tree without it pins nothing;
    (private path) holds the real lane to both.
    """
    collected = set(universe)
    guards = [(g.module, g.reason) for g in input_pinned_guards_for(changed) + new_module_guards_for(added or [])]
    for module, reason in [*guards, *pinned]:
        if module in collected:
            selection.add(module, reason, INPUT_PINNED_DISTANCE)


#: The module ci.yml dumps the OpenAPI contract from. A response model or route anywhere it imports
#: moves apps/webui/openapi.json: #11727's drift was a route in apps/sets/api.py, outside the
#: currency guard's prefixes, so the head that added it never ran the guard (Tue 10 Nov 2026).
OPENAPI_APP_MODULE = "apps.engine_core.app"
OPENAPI_REACH_REASON = "openapi currency guard (the engine app imports the change)"


def _select_contract_reach(reached: dict[str, int], universe: list[str], selection: Selection) -> None:
    """Rank the openapi currency guard at distance 0 when the engine app imports a changed module."""
    if OPENAPI_APP_MODULE in reached and OPENAPI_CURRENCY_GUARD in set(universe):
        selection.add(OPENAPI_CURRENCY_GUARD, OPENAPI_REACH_REASON, INPUT_PINNED_DISTANCE)


def _demote_weak_links(selection: Selection, policy: Policy) -> None:
    """Item 2: move modules chosen only by weak rules, under the demoted dirs, out of the run."""
    for module, rules in list(selection.reasons.items()):
        if not any(_under(module, directory) for directory in policy.demote_weak_under):
            continue
        if not all(rule.startswith(WEAK_RULES) for rule in rules):
            continue
        selection.demoted[module] = selection.reasons.pop(module)


def select_tests(
    changed: list[str],
    ignores: list[str],
    root: Path = REPO,
    policy: Policy | None = None,
    graph: Graph | None = None,
    added: list[str] | None = None,
    pinned: tuple[tuple[str, str], ...] = (),
) -> Selection:
    """The test modules a change set reaches, or FullSuite when it cannot be narrowed.

    `added` is the subset of `changed` the change adds (`git diff --diff-filter=A`); it pins the
    new-module registry guards. None is a caller that cannot tell, and pins none of them.

    `pinned` is (module, reason) guards the caller found the change feeds by reading the diff base,
    which this module cannot (scripts/ci_derived_inputs.py, issue #11975); each ranks at distance 0.

    `graph` is `Graph.load(root)` loaded once by a caller that selects many change sets over one
    checkout (scripts/ci_main_hourly.py attributes each commit of a range); selection never mutates it.
    """
    policy = policy or Policy()
    graph = graph or Graph.load(root)
    deleted = deleted_modules(changed, graph, root, policy)
    _refuse_unnarrowable(changed, graph, deleted, policy)
    universe = lane_universe(list(graph.path_of.values()), ignores, root)
    selection = Selection(modules=[], graph_paths=list(graph.path_of.values()))
    _select_test_areas(changed, universe, selection)
    importers_of_deleted, namers_of_deleted = _importers_of_deleted(deleted, graph, root)
    reached = _reach_through_packages(
        _seed_modules(changed, graph), graph, one_hop_seeds=frozenset(importers_of_deleted)
    )
    _select_by_conftest(reached, graph, universe, selection)
    universe_set = set(universe)
    for name, depth in reached.items():
        if graph.path_of[name] in universe_set:
            selection.add(graph.path_of[name], "import graph", depth)
    names = Names()
    for path in changed:
        names.add(path, graph.name_of.get(path) or (_dotted_from_path(path) if path in deleted else None))
    for name in reached:
        names.add(graph.path_of[name], name)
    by_name = BY_NAME_COUPLINGS if policy.ci_config_by_name else ()
    available = COUPLINGS + by_name
    couplings = [c for c in available if any(c.fires_for(p) for p in changed)]
    _select_by_source(names, couplings, universe, root, selection)
    _select_through_helpers(
        namers_of_deleted, "named reference", graph, universe, selection,
        through_conftests=True, distance=NAMED_REFERENCE_DISTANCE,
    )  # fmt: skip
    if policy.ci_config_by_name:
        _select_through_named_helpers(changed, couplings, graph, universe, root, selection)
    always_run = _select_always_run(universe, root, selection)
    _select_tree_bookkeeping(changed, always_run, selection)
    _select_input_pinned(changed, universe, selection, added, pinned)
    _select_contract_reach(reached, universe, selection)
    _demote_weak_links(selection, policy)
    selection.modules = sorted(selection.reasons)
    return selection


#: Ranks inside a capped full-suite fallback (TEST-BUDGET round 5): the whole-tree guards are the
#: tests written for exactly this case, the tests under a changed conftest or test area are its
#: nearest neighbours, and the rest of the lane is taken cheapest first until the cap is spent.
CAPPED_GUARD_DISTANCE = 0
CAPPED_NEIGHBOUR_DISTANCE = 1
CAPPED_REST_DISTANCE = 5


def _changed_test_neighbourhoods(changed: list[str], universe: set[str]) -> list[str]:
    """Directories whose tests a changed path under tests/ reaches (distance 1).

    A changed test module the lane collects is not a neighbourhood: it ranks at distance 0 itself.
    Any other changed file (a conftest, a helper) reaches its own directory, a conftest.py even
    when that is `tests/` (every test); non-Python data under `tests/<area>/` reaches the whole
    area, the same first-two-components rule `_select_test_areas` applies on the narrowed path.
    """
    near: set[str] = set()
    for path in changed:
        pure = PurePosixPath(path)
        if pure.parts[:1] != ("tests",) or path in universe:
            continue
        parent = _parent(path)
        if parent != "." and (parent != "tests" or pure.name == "conftest.py"):
            near.add(parent)
        if len(pure.parts) >= 3 and not path.endswith(".py"):
            near.add("/".join(pure.parts[:2]))
    return sorted(near)


def capped_full_selection(
    changed: list[str],
    ignores: list[str],
    root: Path = REPO,
    added: list[str] | None = None,
    pinned: tuple[tuple[str, str], ...] = (),
) -> Selection:
    """The whole lane, ranked for a hard cap, when `select_tests` could not narrow the change.

    Only a hard budget calls this: the nightly full suite and the trunk checkpoint still run
    everything, and the checkpoint reverts what a capped run let through.
    """
    graph = Graph.load(root)
    universe = lane_universe(list(graph.path_of.values()), ignores, root)
    selection = Selection(modules=[], graph_paths=list(graph.path_of.values()))
    neighbourhoods = _changed_test_neighbourhoods(changed, set(universe))
    changed_set = set(changed)
    for test in universe:
        rule = "capped full suite: rest of the lane, cheapest first"
        distance = CAPPED_REST_DISTANCE
        near = next((d for d in neighbourhoods if _under(test, d)), None)
        if test in changed_set:
            rule, distance = "capped full suite: the changed test itself", CAPPED_GUARD_DISTANCE
        elif near:
            rule, distance = f"capped full suite: under changed `{near}`", CAPPED_NEIGHBOUR_DISTANCE
        selection.add(test, rule, distance)
    always_run = _collected_always_run(universe, root)
    for module in always_run:
        selection.add(module, "capped full suite: whole-tree guard", CAPPED_GUARD_DISTANCE)
    _select_tree_bookkeeping(changed, always_run, selection)
    _select_input_pinned(changed, universe, selection, added, pinned)
    selection.modules = sorted(selection.reasons)
    return selection
