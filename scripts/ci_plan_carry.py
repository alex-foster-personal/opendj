"""What a GLOBAL pytest helper carries: the derivation behind `helper_carried` in
ci/test-scopes.yml.

Supersedes: ``helper_carried_scopes`` in ``scripts/ci_plan.py``, moved here, not copied.

Split out of ci_plan.py, which crossed the file size ceiling in round 12. This module depends
on ci_plan.py (its scopes, owner index and source closure), never the reverse: plan time
reads the committed list, and this is what regenerates it and what the guard compares it to.
"""

from __future__ import annotations

import ast
import errno
import importlib.util
import posixpath
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PureWindowsPath

from scripts.ci_plan import (
    REPO,
    Config,
    Scope,
    _consumed_only_by_placed_tests,
    _consumed_scopes,
    _helper_importers,
    _owner_index,
    _source_reachability,
    _test_owner_index,
    matches,
)
from scripts.ci_plan_sources import (
    PATH_ARGUMENT,
    PATH_WRAPPERS,
    SAME_PATH_METHODS,
    _argument,
    _called_name,
    _constant_segment,
    _directory_of,
    _imported_names,
    _join_parts,
    _read_source,
    _single_bindings,
    is_test_module,
    joined_paths,
    mentioned_strings,
    pytest_inputs,
)

# A reference to one of these may run code the import reader cannot name. `re.compile` and `json.load`
# load none. `find_spec` runs no module it finds, but imports a dotted name's parent package first.
_CODE_LOADERS = frozenset(
    {
        "import_module",
        "__import__",
        "run_path",
        "run_module",
        "spec_from_file_location",
        "SourceFileLoader",
        "exec_module",
        "load_module",
        "entry_points",
        "find_spec",
    }
)
_BUILTIN_RUNNERS = frozenset({"exec", "eval", "compile"})
_BY_NAME = frozenset({"import_module", "__import__", "find_spec"})  # load a module by its dotted name


def helper_carried_scopes(config: Config, root: Path = REPO) -> frozenset[str]:
    """Scopes whose change must run EVERYTHING, because a GLOBAL pytest helper imports them.

    A helper matched by `always` is not a test and does not run; it is imported by suites
    across the tree, including scope-owned suites that are not always run. Its imports are
    therefore a dependency of those suites, and its consumers cannot be resolved from its own
    text, so the attribution is the widest honest one.

    Closed over the source graph for the same reason `observed_dependents` is: a helper that
    imports scope A also depends on everything A imports.

    Round 12 narrows ONE case, read off the consumers rather than assumed: a helper that is
    not a `conftest.py` and is imported ONLY by test modules that run in every plan carries
    nothing, because every plan already runs the suites that exercise what it imports. Two
    root helpers (`tests/quality_gate_jscpd.py`, `tests/ci_cost_guard_workflow_reader.py`)
    consumed by three root test modules were carrying the `scripts` scope, whose closure is
    the whole tree. A helper nobody imports, or one imported by a `conftest.py` or another
    helper, keeps the widest attribution.

    Round 19 widens that to every helper whose importers are all PLACED test modules, owned by
    a scope or always run: `observed_dependents` gives it an edge to each importer's owner
    instead, so a change to what it imports runs those suites rather than everything.
    """
    reaches = _source_reachability(config, root)
    carried: set[str] = set()
    for relative, file in _global_helpers(config, root):
        for scope in _consumed_scopes(_read_source(file, relative), relative, config):
            carried |= reaches[scope.name]
    return frozenset(carried)


def helper_carried_paths(config: Config, root: Path = REPO) -> frozenset[str]:
    """The files of carried scopes a global helper can reach: the derivation behind
    `helper_carried_paths` in ci/test-scopes.yml. Round 22.

    The module closure of the global helpers' imports, package `__init__`s on the way included,
    kept to scope-owned files, tests included. Where the imports cannot tell what is reached, the entry is the
    scope's source prefix instead: a scope a helper or any closure module MENTIONS (it may run or read
    any file there), and the scope of a closure module that loads code by a name it cannot show,
    which round 19 carried whole as well. Such a load in an unowned module, a global helper or a
    closure module no scope claims, carries every carried scope whole; a load whose names are all
    literals is followed like an import. Only `helper_carried` scopes get entries, since `plan` consults no other:
    a scope a closure module only mentions stays as round 19 left it (on this tree, prose).
    """
    owner_of = _owner_index(config)  # tests too: a scope-owned helper a global one imports is reached
    whole: set[str] = set()
    todo: list[str] = []
    for relative, file in _global_helpers(config, root):
        text = _read_source(file, relative)
        whole.update(_mentioned_scopes(config, text, relative))
        todo.extend(f for name in _imported_names(text, relative) for f in _module_files(name, root))
        todo.extend(_loaded_files(text, relative, owner_of, whole, config, root))
    reached: set[str] = set()
    while todo:
        relative = todo.pop()
        if relative in reached:
            continue
        reached.add(relative)
        text = _read_source(root / relative, relative)
        todo.extend(_loaded_files(text, relative, owner_of, whole, config, root))
        whole.update(_mentioned_scopes(config, text, relative))  # a closure module may run what it spells
        todo.extend(f for name in _imported_names(text, relative) for f in _module_files(name, root))
    carried = set(config.helper_carried)  # `plan` consults no other scope's entries
    files = {f for f in reached if (scope := owner_of(f)) is not None and scope.name in carried - whole}
    prefixes = {
        prefix for scope in config.scopes if scope.name in whole & carried for prefix in scope.sources + scope.tests
    }
    return frozenset(files | prefixes)


# ----- helpers -----
def _loaded_files(
    text: str, relative: str, owner_of: Callable[[str], Scope | None], whole: set[str], config: Config, root: Path
) -> list[str]:
    """The files a module's loaders run, followed like imports when every loaded name is a literal.
    Otherwise the module's own scope is carried whole, as round 19 carried it, and an unowned
    module (a global helper, or a closure module no scope claims) carries every carried scope
    whole: it may run any of their code (Sol on #7495). A path loader's target is followed too, and
    one that names no repository file carries every carried scope whole (`_path_runs`)."""
    if not _loads_code_by_name(text, relative):
        return []
    ran = _path_runs(text, relative, root)
    if ran is None:
        whole.update(config.helper_carried)
    loaded = _resolved_loads(text, relative)
    followed = [f for name in sorted(loaded or ()) for f in _module_files(name, root)]
    if loaded is not None and not ran:
        return followed
    scope = owner_of(relative)
    whole.update({scope.name} if scope is not None else set(config.helper_carried))
    return [*(ran or []), *followed]  # a literal name load beside a path load is followed too (Sentry on #8139)


def _path_runs(text: str, where: str, root: Path) -> list[str] | None:
    """The repository files a module's path loaders run (`run_path`, `spec_from_file_location`,
    `SourceFileLoader`), or None when one target names none: computed, absolute, outside the
    checkout, missing, or a loader handed on or imported under another name. Such a load may run
    any carried scope's code (Sol on #7495)."""
    tree = ast.parse(text, where)
    calls = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
    bindings = _single_bindings(tree)
    ran: list[str] = []
    for node in _loader_references(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name in PATH_ARGUMENT and alias.asname for alias in node.names):
                return None  # `from runpy import run_path as go`: called by a name the reader does not follow
            continue
        loader = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
        if loader not in PATH_ARGUMENT:
            continue
        call = calls.get(id(node))
        target = None if call is None else _argument(call, *PATH_ARGUMENT[loader])
        files = [] if target is None else _files_run_at(_path_text(target, where, bindings), root)
        if not files:
            return None
        ran.extend(files)
    return ran


def _path_text(node: ast.expr, where: str, bindings: Mapping[str, ast.expr]) -> str | None:
    """The path a loader argument spells, read through wrappers that keep it (`str(p)`,
    `os.fspath(p)`, `p.resolve()`): a literal or a string build of literals, or a join of literals
    onto a head `narrow` resolves as it resolves a walk (`REPO / "apps" / "b" / "tool.py"` for
    `REPO = Path(__file__).parents[2]`). None for anything else."""
    while True:
        if isinstance(node, ast.Call) and len(node.args) == 1 and _called_name(node.func) in PATH_WRAPPERS:
            node = node.args[0]
        elif (
            isinstance(node, ast.Call)
            and not node.args
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SAME_PATH_METHODS
        ):
            node = node.func.value
        else:
            break
    if (whole := _constant_segment(node)) is not None:
        return whole
    parts = _join_parts(node)
    if not parts:
        return _directory_of(node, where, bindings, 0)  # a name bound once to a path, or `__file__`
    head = _directory_of(parts[0], where, bindings, 0)
    rest = [segment for part in parts[1:] if (segment := _constant_segment(part))]
    return posixpath.join(head, *rest) if head is not None and len(rest) == len(parts) - 1 else None


def _files_run_at(path: str | None, root: Path) -> list[str]:
    """The repository file a path loader runs at `path`: the `.py` file, or a directory's
    `__main__.py`; none for a path outside the checkout, with a part that is not one plain
    component, or naming no such file."""
    normal = posixpath.normpath(path) if path else ""
    parts = normal.split("/")
    if not normal or parts[0] in ("", ".", "..") or not all(map(_is_plain_component, parts)):
        return []
    if normal.endswith(".py") and _is_file(root / normal):
        return [normal]
    main = f"{normal}/__main__.py"
    return [main] if _is_file(root / main) else []


def _mentioned_scopes(config: Config, text: str, relative: str) -> set[str]:
    """Scopes a module spells, in a literal or a constant join (`REPO / "apps" / "front"`), as
    `narrow` reads it (Sol on #7495)."""
    literals = mentioned_strings(text, relative) | joined_paths(text, relative)
    return {s.name for s in config.scopes if any(n in lit for n in s.mentions for lit in literals)}


def _global_helpers(config: Config, root: Path) -> Iterator[tuple[str, Path]]:
    """The pytest inputs whose imports carry: always matched, unplaced, not a test module, and
    not consumed only by placed tests (rounds 12 and 19)."""
    owner_of = _test_owner_index(config)
    inputs = {
        (file.relative_to(root) if file.is_absolute() else file).as_posix(): file
        for file in pytest_inputs(root / "tests")
    }
    importers = _helper_importers(inputs, root)
    for relative, file in inputs.items():
        if owner_of(relative) is not None or not matches(relative, config.always):
            continue
        if is_test_module(relative):
            continue
        if _consumed_only_by_placed_tests(relative, importers[relative], owner_of, config):
            continue
        yield relative, file


def _module_files(name: str, root: Path) -> list[str]:
    """The repository files importing `name` runs: every package `__init__` on the way, and the
    module itself. A third-party or standard library name matches none, and so does a name with a
    part that is not one plain path component under POSIX and Windows rules alike (`a..b`,
    `/tmp/probe`, `a\\b`, `C:probe`): its parts would build a path outside the checkout or a
    different module's, on some host (Sol on #7495). Any other part counts, an identifier or not:
    `import_module("pkg.beat-grid")` loads `pkg/beat-grid.py`."""
    parts = name.split(".")
    if not all(_is_plain_component(part) for part in parts):
        return []
    found: list[str] = []
    for depth in range(1, len(parts) + 1):
        base = Path(*parts[:depth])
        found.extend(c.as_posix() for c in (base / "__init__.py", base.with_suffix(".py")) if _is_file(root / c))
    return found


def _is_file(path: Path) -> bool:
    """`path.is_file()`, with a name too long for the filesystem read as no file: Python 3.11 raises
    for one (errno ENAMETOOLONG) where any other missing file reads False, and no file carries it."""
    try:
        return path.is_file()
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        return False


def _is_plain_component(part: str) -> bool:
    """One path component under Windows rules, which split on `/` as well as `\\`: not empty, no
    separator, no drive."""
    path = PureWindowsPath(part)
    return path.parts == (part,) and not path.drive


def _loads_code_by_name(text: str, where: str) -> bool:
    """Whether the module references a loader that runs code it names at run time. A reference,
    not a call, so `run = exec` and `from importlib import import_module as load` count too."""
    return bool(_loader_references(ast.parse(text, where)))


def _loader_references(tree: ast.AST) -> list[ast.AST]:
    builtins_names = {"builtins", "__builtins__"} | {
        a.asname or a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for a in node.names
        if a.name == "builtins"
    }
    return [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and (node.id in _CODE_LOADERS or node.id in _BUILTIN_RUNNERS))
        or (isinstance(node, ast.Attribute) and node.attr in _CODE_LOADERS)
        or (  # `builtins.exec`, `b.eval` for `import builtins as b`; never `re.compile`
            isinstance(node, ast.Attribute)
            and node.attr in _BUILTIN_RUNNERS
            and isinstance(node.value, ast.Name)
            and node.value.id in builtins_names
        )
        or (
            isinstance(node, ast.ImportFrom)
            and any(
                a.name in _CODE_LOADERS or (node.module == "builtins" and a.name in _BUILTIN_RUNNERS)
                for a in node.names
            )
        )
    ]


def _resolved_loads(text: str, where: str) -> frozenset[str] | None:
    """The module names a file's name loaders load (path loaders are `_path_runs`'s), or None when
    one cannot be shown. Each must be `import_module(...)` or `__import__(...)` called with a
    literal name, or with a parameter of a private function the file only calls, each time with a
    literal (`_can_import("soundfile")`).
    A caller of that private function in another file is the stated floor, as round 19's is."""
    tree = ast.parse(text, where)
    calls = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
    enclosing = _enclosing_functions(tree)
    names: set[str] = set()
    for node in _loader_references(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.asname for alias in node.names):
                return None  # `import_module as load`: calls by a name the reader does not follow
            continue
        if (node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)) in PATH_ARGUMENT:
            continue  # a path loader's target is `_path_runs`'s
        call = calls.get(id(node))
        loaded = None if call is None else _loaded_names(call, enclosing.get(id(call)), tree)
        if loaded is None:
            return None
        names |= loaded
    return frozenset(names)


def _enclosing_functions(tree: ast.AST) -> dict[int, ast.FunctionDef | ast.AsyncFunctionDef]:
    """The innermost function around each node, by node id; a module-level node has none."""
    return {
        id(inner): function
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        for inner in ast.walk(function)
    }  # `ast.walk` reaches an outer function first, so the innermost one wins


def _loaded_names(
    call: ast.Call, function: ast.FunctionDef | ast.AsyncFunctionDef | None, tree: ast.AST
) -> set[str] | None:
    """The absolute names one loader call loads, or None unless its whole target is literal: a
    literal name, plus either a literal package that a relative name resolves against
    (`import_module(".worker", "apps.raw")`) or `__import__`'s literal `fromlist`, each entry of
    which may be a submodule it loads. `__import__`'s globals and level leave the target unshown.
    `find_spec` loads only a dotted name's parent package: it imports that to find the name."""
    keywords = {k.arg: k.value for k in call.keywords}
    by_import = _called_name(call.func) == "__import__"
    listed = _literal_strings(keywords.pop("fromlist")) if by_import and "fromlist" in keywords else set()
    packages = [*call.args[1:], *keywords.values()]
    if (
        _called_name(call.func) not in _BY_NAME
        or not call.args
        or listed is None
        or len(packages) > (0 if listed else 1)
    ):
        return None
    loaded = _literal_arguments(call.args[0], function, tree)
    if loaded is None or "*" in listed:
        return None  # `fromlist=["*"]` loads the submodules a package's `__all__` lists
    if packages:
        package = packages[0]
        if not (isinstance(package, ast.Constant) and isinstance(package.value, str)):
            return None
        try:
            loaded = {importlib.util.resolve_name(name, package.value) for name in loaded}
        except ImportError:
            return None  # beyond the top-level package: the load fails, so nothing shows what it runs
    if any(name.startswith(".") for name in loaded):
        return None  # relative to no package
    if _called_name(call.func) == "find_spec":  # imports `apps.b` to find `apps.b.mod` (Sol on #7495)
        return {name.rpartition(".")[0] for name in loaded if "." in name}
    return loaded | {f"{name}.{entry}" for name in loaded for entry in listed}


def _literal_strings(node: ast.expr) -> set[str] | None:
    """The strings of a literal list or tuple of strings, else None."""
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return None


def _literal_arguments(
    argument: ast.expr, function: ast.FunctionDef | ast.AsyncFunctionDef | None, tree: ast.AST
) -> set[str] | None:
    """The literal names `argument` takes: itself, or what each call of its private function passes."""
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return {argument.value}
    if not (isinstance(argument, ast.Name) and function is not None and function.name.startswith("_")):
        return None
    parameters = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
    rebound = any(
        isinstance(n, ast.Name) and n.id == argument.id and isinstance(n.ctx, ast.Store) for n in ast.walk(function)
    )
    if argument.id not in parameters or rebound:
        return None
    position = parameters.index(argument.id)
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == function.name]
    callers = [
        c
        for c in ast.walk(tree)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == function.name
    ]
    passed = [c.args[position] for c in callers if len(c.args) > position]
    if not callers or len(uses) != len(callers) or len(passed) != len(callers):
        return None  # never called here, handed on (`map(_can_import, ...)`), or passed by keyword
    if not all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in passed):
        return None
    return {v.value for v in passed if isinstance(v, ast.Constant) and isinstance(v.value, str)}
