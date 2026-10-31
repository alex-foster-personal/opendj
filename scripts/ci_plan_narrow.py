"""Round 23: narrow a SCOPED plan's DEPENDENT scopes to the test modules that can reach the change.

`Scope.dependents` is scope-granular: a scope is a dependent when ANY of its test modules imports
the changed scope, and the plan then ran ALL of its test modules. `narrow` keeps the changed scopes'
tests and the always-run tests whole, and from each dependent scope only the modules whose static
reach holds a changed `.py` file, or spells a mention needle of a changed non-Python file, or runs
code of the scope that owns a changed non-Python file (which may read it by a path no literal
spells). The whole-tree guards `ci/pr-always-run.txt` lists are never narrowed away.

Reach is read off the tree: a module's imports, those of the conftests and package initializers
pytest runs before it (rounds 14 and 15), and the repository modules their string literals name
(`-m apps.x.cli` with a package's `__main__.py`, `"apps/x/job.py"`), closed transitively. A module the index cannot read, or
never read, is kept. A non-Python change no needle matches keeps its scope's
dependents whole. Full triggers, unclaimed paths and the cap all answer FULL before this runs, and a
FULL plan is returned unchanged.

Depends on ci_plan.py; ci_plan.py never imports it, so `plan` stays pure and scope-granular, and
`python -m scripts.ci_plan` keeps printing the scope-granular plan this one is measured against.

Usage:
    python -m scripts.ci_plan_narrow --base <ref> [--head <ref>]
"""

from __future__ import annotations

import argparse
import ast
import posixpath
import re
import sys
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from scripts.ci_plan import (
    CONFIG_PATH,
    REPO,
    Change,
    Config,
    Plan,
    Verdict,
    _owner_index,
    matches,
    plan,
    read_changes,
    read_config,
)
from scripts.ci_plan_carry import (
    _BY_NAME,
    _enclosing_functions,
    _is_file,
    _is_plain_component,
    _literal_arguments,
    _loaded_names,
    _module_files,
)
from scripts.ci_plan_sources import (
    PlanError,
    _called_name,
    _directory_of,
    _imported_names,
    _read_source,
    _relative_base,
    _simple_bindings,
    _single_bindings,
    is_test_module,
    joined_paths,
    mentioned_strings,
    pytest_inputs,
)
from scripts.ci_test_selection_rules import FullSuite, always_run_modules

_WALKS = frozenset({"rglob", "glob", "iglob", "walk", "iterdir", "scandir", "listdir"})
_ANY_WALKER = "*"  # a walker name from a star import of a module still being read: any call may walk
_MODULE_WALKS = frozenset({"os", "glob"})  # `os.walk(top)`, `glob.glob(pattern)`: the target is an argument
_TARGET_ARGS = frozenset({"top", "path", "pathname"})
# Walks read by hand that the resolver cannot place and no test outcome depends on, each named by
# its file and the function holding it. Every other walk in the file is indexed as usual; a guard
# test holds each entry to exactly one walk call there, unresolved.
DECLARED_WALKS: Mapping[str, str] = {
    # `pytest_ignore_collect` reads the test modules pytest is already collecting, for their
    # `rb_parity` marker: it decides collection, never an outcome, and a changed test module is
    # selected by its own reach. Undeclared, every module would walk the root through it.
    "tests/conftest.py": "_contains_rb_parity_marker",
}
_PYTHON_GLOB = re.compile(r"(?:^|/)[^/]*[*?[][^/]*\.py$")  # `*.py`, `**/test_*.py`; never a bare `*`


@dataclass(frozen=True)
class Reach:
    files: frozenset[str]  # every repository file the module can run
    literals: frozenset[str]  # every string literal those files spell
    walked: frozenset[str]  # every directory those files walk, "." for the repository root


def narrow(
    scoped: Plan,
    changes: tuple[Change, ...],
    config: Config,
    modules: tuple[str, ...],
    index: Mapping[str, Reach | None],
    guards: frozenset[str],
) -> Plan:
    """`scoped` with each dependent scope's tests cut to the modules that can reach `changes`."""
    if scoped.verdict is not Verdict.SCOPED:
        return scoped
    owner_of = _owner_index(config)
    by_name = {s.name: s for s in config.scopes}
    paths = [c.path for c in changes]
    whole = {o.name for p in paths if (o := owner_of(p)) is not None}
    needles: set[str] = set()
    data_owners: set[str] = set()  # sources and tests of a scope whose non-Python file changed
    for path in paths:
        scope = owner_of(path)
        if path.endswith(".py") or scope is None:
            continue
        found = [n for n in scope.mentions if n in path]
        if found:
            needles.update(found)
            data_owners.update((*scope.sources, *scope.tests))
        elif not found:
            whole.update(scope.dependents)
    dependents = [name for name in scoped.scopes if name not in whole]
    if not dependents:
        return scoped
    kept_prefixes = set(config.always) | {t for name in scoped.scopes if name in whole for t in by_name[name].tests}
    changed = frozenset(p for p in paths if p.endswith(".py"))
    holding = frozenset(d.as_posix() for p in changed for d in PurePosixPath(p).parents)  # "." is the root
    owner_sources = tuple(data_owners)

    @cache  # reaches share most files: one glob match per file, not per (module, file)
    def owned_by_data_scope(path: str) -> bool:
        return matches(path, owner_sources)

    contributing: set[str] = set()
    candidates = 0
    picked: set[str] = set()
    for module in modules:
        owners = [name for name in dependents if matches(module, by_name[name].tests)]
        if not owners or matches(module, tuple(kept_prefixes)):
            continue
        candidates += 1
        if module in guards or _can_reach(
            index.get(module), changed, holding, needles, owned_by_data_scope
        ):  # missing: None, kept
            picked.add(module)
            contributing.update(owners)
    return Plan(
        Verdict.SCOPED,
        tuple(name for name in scoped.scopes if name in whole or name in contributing),
        tuple(sorted(kept_prefixes | picked)),
        f"{scoped.reason}; dependents narrowed to {len(picked)} of {candidates} test modules",
    )


def read_reach_index(root: Path, modules: tuple[str, ...]) -> dict[str, Reach | None]:
    """Each module's `Reach` read off the tree under `root`; None where any file on the way cannot
    be parsed, since no imports found and could not look must not be the same value."""
    reader = _Reader(root)
    return {module: reader.reach(module) for module in modules}


# ----- helpers -----


def _can_reach(
    reach: Reach | None,
    changed: frozenset[str],
    holding: frozenset[str],
    needles: set[str],
    owned_by_data_scope: Callable[[str], bool],
) -> bool:
    """`holding` is every directory above a changed `.py` file: a scanner names the directory it
    walks (`REPO / "apps"`, then `rglob("*.py")`), never the file."""
    if reach is None:
        return True
    if reach.files & changed or reach.walked & holding or any(owned_by_data_scope(f) for f in reach.files):
        return True
    return any(needle in literal for literal in reach.literals for needle in needles)


def _program_imports(literal: str, where: str) -> set[str]:
    """The imports of a literal that is itself a Python program (`[sys.executable, "-c", BOOT]`,
    `exec(BOOT)`), dedented as `textwrap.dedent` callers write it. Text that does not parse is no
    program (an error message that says "cannot import"), so it adds nothing."""
    try:
        return _imported_names(textwrap.dedent(literal), where)
    except PlanError:
        return set()


def _walks_read_python(spelled: frozenset[str]) -> bool:
    """Whether a file's walks can read Python files: it spells a `.py` pattern or suffix, or
    `git ls-files`. A walk that reads no Python file walks data (fixtures, a cache, a USB drive),
    so its directories do not count."""
    stripped = {s.strip() for s in spelled}
    return _lists_tracked_files(spelled) or ".py" in stripped or any(_PYTHON_GLOB.search(s) for s in stripped)


def _loaded_modules(text: str, where: str) -> frozenset[str] | None:
    """The modules a file's `import_module` and `__import__` calls load, resolved as the carry
    resolves them, so `import_module(".worker", "apps.raw")` loads `apps.raw.worker`. None when a
    call takes more than a computed name and its target still cannot be shown (a computed package,
    a level, `fromlist=["*"]`), or when a loader goes by another name or is referenced without
    being called (`import_module as load`, `load = importlib.import_module`), since the reader
    cannot see what that loads. A lone computed name, `import_module(name)`, adds nothing: the floor."""
    if not any(name in text for name in _BY_NAME):
        return frozenset()
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    enclosing = _enclosing_functions(tree)
    calls = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name in _BY_NAME and a.asname for a in node.names):
            return None
        if not (isinstance(node, ast.Name) and node.id in _BY_NAME) and not (
            isinstance(node, ast.Attribute) and node.attr in _BY_NAME
        ):
            continue
        call = calls.get(id(node))
        if call is None:
            return None
        function = enclosing.get(id(call))
        loaded = _loaded_names(call, function, tree)
        if loaded is not None:
            names |= loaded
        elif not (
            len(call.args) == 1 and not call.keywords and _literal_arguments(call.args[0], function, tree) is None
        ):
            return None
    return frozenset(names)


def _walked_directories(text: str, where: str, imported: frozenset[str]) -> frozenset[str]:
    """The repository directories a file's walks read (`rglob`, `os.walk`, `iterdir` and the like;
    `ast.walk` walks a syntax tree), each from its own call: the receiver of `target.rglob(...)`,
    the first argument of `os.walk(target)`, or the fixed head of `glob.glob("apps/**/*.py")`. A
    target that cannot be resolved (a parameter, a loop variable, a name bound twice) may be any
    directory, so it is the root. `imported` holds the walker names the file takes from repository
    modules (`_Reader._imported_walkers`); `_ANY_WALKER` among them walks the root."""
    if _ANY_WALKER in imported:
        return frozenset({"."})  # a star import still being read: any name may be a walker
    if not any(name in text for name in _WALKS | imported):
        return frozenset()  # a call needs its name in the text, so most files skip the parse
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    bindings = _single_bindings(tree)
    calls = _walk_calls(tree, imported)
    exempt = {id(call) for found in _functions_named(tree, DECLARED_WALKS.get(where), calls) for call in found}
    return frozenset(_walk_target(call, where, bindings) or "." for call in calls if id(call) not in exempt)


def declared_walk_targets(text: str, where: str, function: str) -> list[str | None]:
    """What each walk call in `function` resolves to: the guard on a `DECLARED_WALKS` entry."""
    tree = ast.parse(text, where)
    bindings = _single_bindings(tree)
    found = _functions_named(tree, function, _walk_calls(tree, frozenset()))
    return [_walk_target(call, where, bindings) for calls in found for call in calls]


def _walk_calls(tree: ast.AST, imported: frozenset[str]) -> list[ast.Call]:
    """The directory walk calls under `tree`, by any name a walk goes by; `ast.walk` excluded."""
    walkers = _walker_names(tree, imported)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _called_name(node.func) in walkers
        and not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ast"
        )
    ]


def _functions_named(tree: ast.AST, name: str | None, calls: list[ast.Call]) -> list[list[ast.Call]]:
    """The walk `calls` inside each function of the file called `name`."""
    named = (f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == name)
    inside = [{id(node) for node in ast.walk(function)} for function in named]
    return [[call for call in calls if id(call) in nodes] for nodes in inside]


def _lists_tracked_files(spelled: frozenset[str]) -> bool:
    """`git ls-files`, as an argument (`["git", "ls-files"]`) or inside a command string."""
    return any("ls-files" in literal for literal in spelled)


def _walker_names(tree: ast.AST, imported: frozenset[str]) -> set[str]:
    """The names a directory walk goes by, closed over the file's simple bindings:
    `from os import walk as scan` or `scan = os.walk`, and the `imported` walker names."""
    bindings = _simple_bindings(tree)
    walkers = (
        set(_WALKS)
        | imported
        | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module != "ast"
            for alias in node.names
            if alias.name in _WALKS and alias.asname
        }
    )
    while True:
        known = len(walkers)
        walkers |= {
            name
            for name, value in bindings
            if isinstance(value, (ast.Name, ast.Attribute)) and _called_name(value) in walkers
        }
        if len(walkers) == known:
            return walkers


def _walk_target(call: ast.Call, where: str, bindings: Mapping[str, ast.expr]) -> str | None:
    """The directory one walk call reads, or None when its target cannot be resolved."""
    func = call.func
    if isinstance(func, ast.Name) or (
        isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in _MODULE_WALKS
    ):
        if any(keyword.arg == "root_dir" for keyword in call.keywords):
            return None
        target = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg in _TARGET_ARGS), None)
        return "." if target is None else _directory_of(target, where, bindings, 0)  # `os.listdir()`: the cwd
    if isinstance(func, ast.Attribute):
        return _directory_of(func.value, where, bindings, 0)
    return None


class _Reader:
    """Per-file imports, literals and literal-named roots, each read once."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.read: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]] | None] = {}
        self.modules: dict[str, list[str]] = {}  # the files each module-shaped literal names
        self.exports: dict[str, frozenset[str] | None] = {}  # each file's walker names; None while read
        self.top = {p.stem if p.suffix == ".py" else p.name for p in root.iterdir()}  # where a module name can start

    def reach(self, module: str) -> Reach | None:
        files: set[str] = set()
        literals: set[str] = set()
        walked: set[str] = set()
        todo = [module, *self._collected_first(module)]
        while todo:
            relative = todo.pop()
            if relative in files:
                continue
            files.add(relative)
            edges = self._edges(relative)
            if edges is None:
                return None
            following, spelled, walks = edges
            literals |= spelled
            walked |= walks
            todo.extend(following - files)
        return Reach(frozenset(files), frozenset(literals), frozenset(walked))

    def _edges(self, relative: str) -> tuple[frozenset[str], frozenset[str], frozenset[str]] | None:
        if relative not in self.read:
            try:
                text = _read_source(self.root / relative, relative)
                names = _imported_names(text, relative)
                spelled = frozenset(mentioned_strings(text, relative) | joined_paths(text, relative))
                walks: frozenset[str] = frozenset()
                if _walks_read_python(spelled):
                    walks = _walked_directories(text, relative, self._imported_walkers(text, relative))
                    # `git ls-files` walks the root too: without a path it lists the whole tree, and
                    # unordered literals cannot show whether a path follows
                    walks |= {"."} if _lists_tracked_files(spelled) else frozenset()
                loads = _loaded_modules(text, relative)
            except PlanError:
                self.read[relative] = None
                return None
            if loads is None:
                self.read[relative] = None
                return None
            names |= loads | {
                n for literal in spelled if "import" in literal for n in _program_imports(literal, relative)
            }
            following = {f for name in names for f in _module_files(name, self.root)}
            following |= {f for literal in spelled for f in self._named(literal.strip())}
            self.read[relative] = (
                frozenset(following),
                spelled,
                walks,
            )
        return self.read[relative]

    def _imported_walkers(self, text: str, where: str) -> frozenset[str]:
        """The walker names `where` takes from repository modules (Sol on #7495, twice): a name
        imported from a module that binds it as a walker (`from tests.web.helper import scan`,
        through re-exports), and the walker names of an imported module, which its attribute calls
        reach (`helper.scan`, `h.scan`). A name imported from a module still being read (an import
        cycle) counts as a walker. A star import takes every walker the module binds, and one from a
        module still being read is `_ANY_WALKER` (Sol on #7495). Read only for a file whose walks could
        count (`_walks_read_python`)."""
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            raise PlanError(f"cannot parse {where}: {exc}") from None
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = ".".join(filter(None, [_relative_base(where, node.level) if node.level else "", node.module]))
                exports = self._exports(module) if module else frozenset()
                for alias in node.names:
                    if alias.name == "*":  # every walker the module binds; unknown while it is read
                        found |= {_ANY_WALKER} if exports is None else exports
                        continue
                    if exports is None or alias.name in exports:
                        found.add(alias.asname or alias.name)
                    found |= self._exports(f"{module}.{alias.name}") or frozenset()
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    found |= self._exports(alias.name) or frozenset()
        return frozenset(found)

    def _exports(self, module: str) -> frozenset[str] | None:
        """The walker names a repository module binds (none for any other module), or None while
        one of its files is still being read."""
        if module.partition(".")[0] not in self.top:
            return frozenset()  # `typing`, `os`: no repository file, so no lookup
        path = module.replace(".", "/")
        names: set[str] = set()
        for relative in (f for f in _module_files(module, self.root) if f in {f"{path}.py", f"{path}/__init__.py"}):
            if relative not in self.exports:
                self.exports[relative] = None
                text = _read_source(self.root / relative, relative)
                imported = self._imported_walkers(text, relative)  # raises PlanError if it does not parse
                self.exports[relative] = frozenset(_walker_names(ast.parse(text), imported))
            found = self.exports[relative]
            if found is None:
                return None
            names |= found
        return frozenset(names)

    def _named(self, literal: str) -> list[str]:
        """The repository files a literal names as a root, bounded by the files that exist and not
        by its spelling (Sol on #7495): the module `python -m` runs for a dotted name, and a `.py`
        path inside the checkout. Both readings count, since `worker.py` may be either. A directory
        or any other literal is a mention, never a root."""
        return [*self._named_module(literal), *self._named_path(literal)]

    def _named_module(self, literal: str) -> list[str]:
        """`tools.worker` and `tools.beat-grid`: each part one plain path component, as carry reads
        `import_module`, an identifier or not; and `tools/pkg/__main__.py` for a package."""
        parts = literal.split(".")
        if parts[0] not in self.top or not all(_is_plain_component(part) for part in parts):
            return []
        if literal not in self.modules:
            main = f"{'/'.join(parts)}/__main__.py"  # what `-m` runs for a package
            self.modules[literal] = [
                *_module_files(literal, self.root),
                *([main] if _is_file(self.root / main) else []),
            ]
        return self.modules[literal]

    def _named_path(self, literal: str) -> list[str]:
        """A `.py` path, normalized (`./x`, `tests/../x`), inside the checkout (`../x` and `/x` name
        nothing) and one plain component per part under POSIX and Windows rules alike, so plans do
        not depend on the host; spaces and length are no bar."""
        path = posixpath.normpath(literal)
        parts = path.split("/")
        inside = path.endswith(".py") and parts[0] not in ("", "..") and all(map(_is_plain_component, parts))
        return [path] if inside and _is_file(self.root / path) else []

    def _collected_first(self, module: str) -> list[str]:
        """What pytest runs before the module: each ancestor `conftest.py` and package `__init__.py`.
        POSIX on every OS: relative imports resolve by splitting on `/`."""
        candidates = (
            (parent / name).as_posix()
            for parent in PurePosixPath(module).parents
            for name in ("conftest.py", "__init__.py")
        )
        return [c for c in candidates if (self.root / c).is_file()]


def test_modules(root: Path = REPO) -> tuple[str, ...]:
    """Every test module pytest collects under `tests/`, relative to `root`."""
    found = (f.relative_to(root).as_posix() if f.is_absolute() else f.as_posix() for f in pytest_inputs(root / "tests"))
    return tuple(sorted(m for m in found if is_test_module(m)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        changes = read_changes(args.base, args.head)
        modules = test_modules()
        scoped = plan(changes, config)
        result = narrow(
            scoped,
            changes,
            config,
            modules,
            read_reach_index(REPO, modules) if scoped.verdict is Verdict.SCOPED else {},
            frozenset(always_run_modules(REPO)),
        )
    except (PlanError, FullSuite) as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    selected = (
        len(modules) if result.verdict is Verdict.FULL else sum(1 for m in modules if matches(m, result.test_paths))
    )
    print(f"verdict={result.verdict}")
    print(f"reason={result.reason}")
    print(f"scopes={','.join(result.scopes)}")
    print(f"modules={selected} of {len(modules)}")
    print(f"tests={' '.join(result.test_paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
