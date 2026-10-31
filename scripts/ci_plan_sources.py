"""Reading Python sources and the package imports they name, for scripts/ci_plan.py.

Split out of ci_plan.py, which crossed the file size ceiling. The division is real rather
than clerical: everything here answers "what does this FILE say", from bytes on disk up to
the set of packages a module imports, and knows nothing about scopes, verdicts or plans.
ci_plan.py owns that half and imports this one, never the reverse.

Every read here fails LOUD. A source this module cannot decode or parse is a dependency the
planner cannot see, and a planner that cannot see a dependency narrows around it.
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import ntpath
import posixpath
import re
import string
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard


class PlanError(Exception):
    """The plan could not be MEASURED. Never rendered as a verdict."""


_TOP_PACKAGES = ("apps", "scripts", "ops")


def _read_source(path: Path, where: str) -> str:
    """Decode strictly. A lenient decode deletes the bytes it cannot read and hands back a
    SHORTER file, so an import can vanish and the derivation gets quietly smaller while both
    it and the guard checking it agree on the same corrupted text. Same class as the parse
    failure below, and it was left behind when that one was fixed. Sol's P1 on #3339."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError(f"cannot decode {where} as utf-8: {exc}") from None
    except OSError as exc:
        raise PlanError(f"cannot read {where}: {exc}") from None


def is_test_module(path: str) -> bool:
    """pytest's own two shapes. Discovery and the completeness invariant have to agree on
    this: they used different predicates, so a `foo_test.py` could satisfy the ownership
    check while never being scanned for the imports that put its suite in a scoped plan.
    Sol's P1 on #3339."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def test_modules(root: Path) -> list[Path]:
    """Every module pytest will COLLECT under `root`, by the one predicate above.

    Collection is the question the completeness invariant asks. It is NOT the question the
    dependency derivation asks; see `pytest_inputs`."""
    return sorted(p for p in root.rglob("*.py") if is_test_module(p.as_posix()))


def pytest_inputs(root: Path) -> list[Path]:
    """Every Python file under `root`, collected or not.

    `conftest.py` runs automatically and fixture and helper modules are imported by the tests
    that use them, so their imports are the suite's dependencies just as much as a test
    module's. Feeding only collected modules to the derivation loses an edge whenever a helper
    reaches a scope its own tests never name: 24 support modules in this repository import a
    scope other than the one that owns them. Sol's P1 on #3339."""
    return sorted(root.rglob("*.py"))


def _relative_base(where: str, level: int) -> str:
    """The absolute package a `from ..x import y` in `where` refers to.

    Discarding relative imports outright is under-selection with extra steps: `from ..
    engine_core import X` in an `apps/open_dj/` module is a cross-scope edge, and dropping it
    leaves that suite out of a SCOPED plan when engine_core changes. Sol's P1 on #3339.

    A level that climbs past the repository root cannot be resolved, and unresolvable is not
    the same as absent, so it RAISES. Today's tree never goes deeper than level 2 and no
    relative import crosses a scope, which is why this was invisible rather than harmless.
    """
    parts = where.removesuffix(".py").split("/")[:-1]
    if level - 1 > len(parts):
        raise PlanError(f"relative import in {where} climbs past the repository root")
    kept = parts[: len(parts) - (level - 1)]
    return ".".join(kept)


def imported_packages(text: str, where: str) -> set[str]:
    """The `apps.x` / `scripts.x` / `ops.x` packages a module imports: `imported_modules`
    cut to two components. Kept for callers that ask about packages; the planner's owner
    lookups use the full names, because a nested scope (`apps/webui/frontend/`) is invisible
    at two components (Codex on #3780)."""
    return {".".join(module.split(".")[:2]) for module in imported_modules(text, where)}


_TYPING_MODULES = ("typing", "typing_extensions")


def _names_bound_otherwise(tree: ast.AST) -> set[str]:
    """Every name bound by anything but the typing sentinel and module themselves (`from typing
    import TYPE_CHECKING [as name]`, `import typing [as name]`): an assignment or loop target, a
    parameter, a match capture, a def or class, any other import, `from typing import cast as gate`
    too (Sol on #7121); a write through the module namespace (`_namespace_writes`, `"*"` for any
    name), and an attribute set by name (`typing.TYPE_CHECKING = True`, `setattr(module,
    "TYPE_CHECKING", True)`, Sol on #7121)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        bound.update(_namespace_writes(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.attr)
        elif isinstance(node, ast.Call) and _called_name(node.func) in {"setattr", "delattr"} and len(node.args) > 1:
            bound.add(_string_value(node.args[1]) or "*")
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, ast.ExceptHandler | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            bound.update(
                alias.asname or alias.name
                for alias in node.names
                if node.module not in _TYPING_MODULES or alias.name != "TYPE_CHECKING"
            )
        elif isinstance(node, ast.Import):
            bound.update(
                alias.asname or alias.name.partition(".")[0]
                for alias in node.names
                if alias.name not in _TYPING_MODULES
            )
    return bound


def _type_checking_only(tree: ast.AST) -> set[int]:
    """The ids of every node under an `if TYPE_CHECKING:` body. Round 12.

    An import that only exists for an annotation is not something a test can fail through:
    `TYPE_CHECKING` is False at runtime, the name is never bound, and the caller that passes
    the real object in is the module carrying the runtime edge (its own imports are read).
    import-linter has the same knob, `exclude_type_checking_imports`. Two of the seven edges
    holding the scope cycle together (`cloud` and `adapters` to `engine_core`) were this shape.

    Only the typing sentinel qualifies: a bare name must be imported from `typing` (or
    `typing_extensions`) in this module, and an attribute must hang off one of those modules
    or an alias of them. `if feature.TYPE_CHECKING:` and a local `TYPE_CHECKING = True` are
    runtime flags whose imports execute, so they stay edges (Codex on #3830). Scope is not
    tracked: a name bound any other way anywhere in the module (assignment, loop target,
    parameter, def, another import) stops being the sentinel everywhere, which errs toward
    keeping an edge (Sol on #3830).
    """
    sentinel_names: set[str] = set()
    typing_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _TYPING_MODULES:
            sentinel_names.update(alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING")
        elif isinstance(node, ast.Import):
            typing_aliases.update(alias.asname or alias.name for alias in node.names if alias.name in _TYPING_MODULES)
    rebound = _names_bound_otherwise(tree)
    if "*" in rebound:
        return set()  # `globals().update(flags)` may rebind the sentinel: every block may run
    both = sentinel_names & typing_aliases  # `import typing as gate` beside `TYPE_CHECKING as gate`: neither holds
    sentinel_names -= rebound | both
    # `typing.TYPE_CHECKING = True` rebinds the attribute every alias reads
    typing_aliases = set() if "TYPE_CHECKING" in rebound else typing_aliases - rebound - both
    hidden: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_flag = (isinstance(test, ast.Name) and test.id in sentinel_names) or (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id in typing_aliases
        )
        if is_flag:
            hidden.update(id(inner) for stmt in node.body for inner in ast.walk(stmt))
    return hidden


def _imported_names(text: str, where: str) -> set[str]:
    """Every dotted name a module imports, in FULL, outside `if TYPE_CHECKING:` blocks."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    hidden = _type_checking_only(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in hidden:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _relative_base(where, node.level) if node.level else ""
            module = f"{base}.{node.module}" if base and node.module else (base or node.module)
            if not module:
                continue
            # `from apps import engine_core` names the package in the ALIAS, not the module.
            found.update(f"{module}.{alias.name}" for alias in node.names)
            found.add(module)
    return {part for part in found if "." in part}


def imported_modules(text: str, where: str, top_packages: tuple[str, ...] = _TOP_PACKAGES) -> set[str]:
    """Every dotted module name under `top_packages` (default `apps`, `scripts`, `ops`) a
    module imports, read with the AST and returned in FULL. The seam selector also passes
    `tests`, because a test helper is reached through `tests.` imports.

    A regular expression missed `from apps import engine_core` outright and saw only the
    first name in `import apps.foo, apps.bar`. Sol's P1 on #3339, and the reason it was
    invisible: the generated `dependents` and the guard that checks it used the SAME matcher,
    so the guard agreed with a derivation that was wrong in exactly the same way. An equality
    between two runs of one broken instrument proves nothing.

    A file that does not parse RAISES rather than returning an empty set: no imports found
    and could not look are the same value, and the second one must never narrow a plan.
    """
    return {p for p in _imported_names(text, where) if p.split(".")[0] in top_packages}


def imported_test_helpers(text: str, where: str) -> set[str]:
    """The `tests.*` modules a pytest input imports, in full. Round 12.

    The other direction of the same reader: who CONSUMES a support module. A global helper
    imported only by test modules that run in every plan carries no scope, because the scopes
    it imports are exercised in every plan already; `helper_carried_scopes` asks this to tell
    that helper apart from one a scope-owned suite or another helper reaches.
    """
    return {p for p in _imported_names(text, where) if p.split(".")[0].casefold() == "tests"}  # `Tests` too


@dataclass(frozen=True)
class HelperLoads:
    """The `tests.*` modules a pytest input loads without an import statement. Round 19."""

    by_call: frozenset[str]  # `importlib.import_module` / `__import__` with a literal name: its caller's
    by_plugins: frozenset[str]  # named in `pytest_plugins`: loaded for the whole session, no suite's
    unresolved: bool  # a load whose name the reader cannot resolve, so it may be any helper
    path_targets: frozenset[str]  # file names a computed path load may reach: the ones the module spells
    lookalike: frozenset[str]  # named by a call that only looks like a loader: never placed, only widened


def loaded_test_helpers(text: str, where: str) -> HelperLoads:
    """Read with the AST: only a real loading construct counts, never a string that merely equals a
    helper's name (test data, an assertion), which would place a helper nothing loads.

    A computed `pytest_plugins` entry is unresolved, and so is any use of that list besides an
    assignment and a literal `append`/`extend`/`insert`, and a relative load whose package is not a
    literal. So is a computed load in a module that spells `tests`, `tests/` or a `tests.` name, which
    it may be built from; one with no such literal (`_can_import(module_name)` for optional packages)
    is not. A computed PATH load (`spec_from_file_location(name, GATE)`) may reach only the files the
    module names, so it marks just those (`path_targets`); a module loading `scripts/merge_gate.py`
    while it holds `"tests/test_foo.py"` as data unplaces nothing. A name that reaches the
    loader with no `tests` literal in the module (read from a file, the environment) is the stated
    floor, as a path a test never spells is for `mentioned_strings`.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    found: dict[str, set[str | None]] = defaultdict(set)  # verdict -> the modules it names
    dead = _type_checking_only(tree)  # never runs; other dead code reads as live, the stated floor
    loaders, modules, assigned_two = _proven_loaders(tree, dead)
    every_loader, every_module, _, conflicting = _loader_imports(ast.walk(tree))  # proven or not: a rebound `run` ran
    conflicting |= assigned_two
    resolved = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    resolved |= {id(node.value) for node in ast.walk(tree) if _is_followed_alias(node, loaders)}
    resolved |= {id(node.value) for node in ast.walk(tree) if _is_followed_alias(node, modules)}
    transferred = _bound_values(tree)
    composed, stored = _composed_tests_names(tree), _stored_tests_names(tree)
    for node in ast.walk(tree):
        if id(node) in dead:
            continue
        if isinstance(node, ast.Call):
            if (dotted := _dotted(node.func)) is not None and dotted.partition(".")[0] in conflicting:
                found["unresolved"].add(None)  # a name imported as two loaders: either may run
            ever_bound = (every_loader, every_module)
            for verdict, module in _read_loader_call(node, loaders, modules, ever_bound, (composed, stored)):
                found[verdict].add(module)
        elif (handed := _referenced_loader(node, loaders)) is not None and id(node) not in resolved:
            # handed on as a value (`map(import_module, names)`): it loads names unseen, and a path loader
            # (`map(runpy.run_path, paths)`) reaches the files the module spells, as `run_path(GATE)` does (Sol on #7121)
            found["computed_path" if handed in PATH_ARGUMENT else "computed"].add(None)
        elif _is_runner_reference(node, every_loader | loaders, every_module | modules) and id(node) not in resolved:
            found["unresolved"].add(None)  # `map(exec, sources)`: it runs code the reader never sees
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in every_module | modules
            and id(node) in transferred - resolved
        ):
            # A loader module bound by a form the alias table does not follow (`b: object = builtins`,
            # `(b := builtins)`, `for b in [builtins]`, `b = a` over an unproven `a`) may call a loader
            # unseen (Sol on #7121). A module handed to a call (`run(builtins)`) is the stated floor:
            # every real case is `monkeypatch.setattr(builtins, ...)` or `hasattr(pytest, ...)`, and
            # reading those as unresolved carried 18 more scopes.
            found["unresolved"].add(None)
        elif isinstance(node, ast.Name) and node.id in conflicting and id(node) not in resolved:
            found["unresolved"].add(None)  # `holder.run = run` over two imported loaders: either is handed on
    found["plugin"] = _plugin_names(tree, dead)
    literals = mentioned_strings(text, where)
    spells_tests = any(s.casefold() in {"tests", "tests/"} or s.casefold().startswith("tests.") for s in literals)
    computed = bool(found["computed"] or found["computed_path"]) and spells_tests
    return HelperLoads(
        _tests_names(found["proven"]),
        _tests_names(found["plugin"]),
        None in found["plugin"] or bool(found["relative"] or found["unresolved"]) or computed,
        frozenset(
            posixpath.basename(s.replace("\\", "/")).casefold()
            for s in literals | _spelled_strings(tree, literals)
            if s
        )
        if found["computed_path"]
        else frozenset(),
        _tests_names(found["lookalike"]),
    )


def _is_followed_alias(node: ast.AST, loaders: Mapping[str, str]) -> TypeGuard[ast.Assign]:
    """An assignment `_proven_loaders` followed: every target a plain name it proved. Its value is
    the alias, not a loader handed on; `holder.load = importlib.import_module`, or a name also bound
    to something else, is a transfer the reader cannot follow, so its value stays a reference. So is
    a name the reader did not prove (`b = a` over `a` imported as two things, Sol on #7121). The same
    holds for loader modules (`b = builtins`)."""
    return (
        isinstance(node, ast.Assign)
        and all(isinstance(target, ast.Name) and target.id in loaders for target in node.targets)
        and (not isinstance(node.value, ast.Name) or node.value.id in loaders)
    )


def _bound_values(tree: ast.AST) -> set[int]:
    """The names a value is bound or returned from: an assignment, annotated assignment or walrus
    value, a loop or comprehension iterable, a `match` subject, a `with` item, a parameter default,
    and a returned, yielded or lambda value (Sol on #7121). In that expression, not inside a call or
    behind an attribute."""
    sources: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr | ast.Return | ast.Yield | ast.YieldFrom):
            sources.extend([node.value] if node.value is not None else [])
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            sources.append(node.iter)
        elif isinstance(node, ast.Match):
            sources.append(node.subject)
        elif isinstance(node, ast.withitem):
            sources.append(node.context_expr)
        elif isinstance(node, ast.arguments):
            sources.extend(default for default in [*node.defaults, *node.kw_defaults] if default is not None)
        elif isinstance(node, ast.Lambda):
            sources.append(node.body)
    found: set[int] = set()
    todo: list[ast.AST] = list(sources)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.Name):
            found.add(id(node))
        elif not isinstance(node, ast.Call | ast.Attribute):
            todo.extend(ast.iter_child_nodes(node))
    return found


def _tests_names(names: set[str | None]) -> frozenset[str]:
    """The `tests.*` names, `Tests.*` too: a path in another case (`Tests\\Rig.py`) runs
    `tests/rig.py` on a case-insensitive checkout."""
    return frozenset(n for n in names if n is not None and n.split(".")[0].casefold() == "tests")


def _read_loader_call(
    call: ast.Call,
    loaders: Mapping[str, str],
    modules: Mapping[str, str],
    ever_bound: tuple[Mapping[str, str], Mapping[str, str]],
    held_names: tuple[frozenset[str], Mapping[str, frozenset[str]]],
) -> list[tuple[str, str | None]]:
    """What one call loads, as (verdict, module) pairs: `proven` through a proven binding with
    literal arguments; `relative` against a package only the running module knows; `computed` or
    `computed_path` when its arguments are not literals; `lookalike` for a call that only shares a
    loader's name (its own `import_module`, a method), and for every `tests.*` dotted name handed to
    any other call, directly or inside an argument (`pluginmanager.import_plugin("tests.rig")`,
    `load_plugins(["tests.rig"])`, Sol on #7121): a loader nobody listed may load it. `held_names`
    holds the names bound to a built `tests` name (`_composed_tests_names`) and the literal ones each
    name holds (`_stored_tests_names`), so a stored name reads like its value.
    A source runner is read through `ever_bound` too, every binding proven or not: its own `def
    exec`, or `from builtins import exec as run` with `run` rebound later, ran before the rebinding,
    and what its source loads only widens (Sol on #7121)."""
    if (loader := _resolve_loader(call.func, loaders, modules)) is not None:
        if loader in RUNS_SOURCE:
            return _read_executed_source(call)
        if _is_relative_load(call, loader):
            return [("relative", None)]
        if (module := _loaded_module(call, loader)) is not None:
            return _with_submodules(call, loader, "lookalike" if loader in PREPARES_ONLY else "proven", module)
        return [(_computed_verdict(call, loader), None)]
    if _resolve_loader(call.func, *ever_bound) in RUNS_SOURCE:
        return _read_executed_source(call)
    if (name := _called_name(call.func)) in NAME_LOADERS:
        module = _loaded_module(call, name)  # type: ignore[arg-type]
        if module is None:  # computed: a path one reaches the files the module names (Sol on #7121)
            return [(_computed_verdict(call, name), None)]  # type: ignore[arg-type]
        return _with_submodules(call, name, "lookalike", module)  # type: ignore[arg-type]
    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    named = [node for argument in arguments for node in ast.walk(argument) if isinstance(node, ast.expr)]
    composed_names, stored = held_names
    built = any(
        _is_tests_fragment(node) or (isinstance(node, ast.Name) and node.id in composed_names) for node in named
    )
    composed: list[tuple[str, str | None]] = [("computed", None)] if built else []
    literal = [("lookalike", dotted) for node in named if (dotted := _dotted_tests_name(node)) is not None]
    held = [("lookalike", d) for node in named if isinstance(node, ast.Name) for d in sorted(stored.get(node.id, ()))]
    return composed + literal + held


def _composed_tests_names(tree: ast.AST) -> frozenset[str]:
    """The names bound to a `tests` name built at run time (`plugin = ".".join(("tests", name))`), or
    to a value holding one or another such name, closed over aliases: handed to a call nobody listed,
    one reads like the built name (Sol on #7121). A name bound twice counts if either binding does."""
    bindings = _name_bindings(tree)
    composed: frozenset[str] = frozenset()
    while True:
        grown = composed | {
            name.id
            for target, value in bindings
            if any(
                _is_tests_fragment(inner) or (isinstance(inner, ast.Name) and inner.id in composed)
                for inner in ast.walk(value)
                if isinstance(inner, ast.expr)
            )
            for name in ast.walk(target)
            if isinstance(name, ast.Name)
        }
        if grown == composed:
            return composed
        composed = grown


def _stored_tests_names(tree: ast.AST) -> dict[str, frozenset[str]]:
    """The literal `tests.*` names each name is bound to (`plugin = "tests.rig"`), or to a value
    holding them, closed over aliases: handed to a call nobody listed, a name reads like the literals
    it holds (Codex on #7121). A name bound twice holds every binding's literals."""
    bindings = _name_bindings(tree)
    held: dict[str, frozenset[str]] = {}
    while True:
        grown = dict(held)
        for target, value in bindings:
            found: set[str] = set()
            for inner in ast.walk(value):
                if isinstance(inner, ast.Name):
                    found |= held.get(inner.id, frozenset())
                elif isinstance(inner, ast.expr) and (dotted := _dotted_tests_name(inner)) is not None:
                    found.add(dotted)
            for name in (node for node in ast.walk(target) if isinstance(node, ast.Name)):
                grown[name.id] = grown.get(name.id, frozenset()) | found
        if grown == held:
            return held
        held = grown


def _name_bindings(tree: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """Each (target, value) a name is bound by: an assignment, annotated or augmented assignment, or
    walrus; a `for` loop or comprehension over its iterable and a `with ... as` over its context;
    each capture of a `match` case, which holds what the subject holds; and a parameter of a `def`,
    `async def` or `lambda` over its default (Sol on #7121)."""
    bindings: list[tuple[ast.expr, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign | ast.NamedExpr) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bindings.extend((target, node.value) for target in targets)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            bindings.append((node.target, node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bindings.append((node.optional_vars, node.context_expr))
        elif isinstance(node, ast.Match):
            captures = [
                name
                for case in node.cases
                for pattern in ast.walk(case.pattern)
                for name in (getattr(pattern, "name", None), getattr(pattern, "rest", None))
                if isinstance(name, str)
            ]
            bindings.extend((ast.Name(id=name, ctx=ast.Store()), node.subject) for name in captures)
        elif isinstance(node, ast.arguments):  # positional defaults fill the last positional parameters
            positional = [*node.posonlyargs, *node.args]
            paired = [
                *zip(positional[len(positional) - len(node.defaults) :], node.defaults, strict=True),
                *zip(node.kwonlyargs, node.kw_defaults, strict=True),
            ]
            bindings.extend(
                (ast.Name(id=arg.arg, ctx=ast.Store()), value) for arg, value in paired if value is not None
            )
    return bindings


def _is_tests_fragment(node: ast.expr) -> bool:
    """Part of a `tests` name built at run time and handed to a call nobody listed (Codex on #7121):
    `"tests." + name`, `f"tests.{name}"`, `"tests.{}".format(name)` or `"tests" + "." + name`, and a
    string method building one, `".".join(("tests", name))` or `"{}.{}".format("tests", name)` (Sol on
    #7121). It reads as a computed load, which keeps every helper carried in a module that spells
    `tests`. A path join (`REPO / "tests"`, `os.path.join("tests", name)`), an entry-point spec
    (`"tests.x:Probe"`) or a logger name (`"tests.engine-warning-log"`) is not one: 10 test modules
    hold those, and reading them as loads carried 37 of 53 scopes (measured Fri 23 Oct 2026)."""
    if (
        isinstance(node, ast.JoinedStr)
        or (isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)))
        or _is_string_method_call(node)
    ):
        return any(
            isinstance(inner, ast.Constant) and str(inner.value).casefold() == "tests" for inner in ast.walk(node)
        )
    text = (_string_value(node) or "").casefold()
    return text.startswith("tests.") and (text.endswith(".") or "{" in text or "%" in text)


def _is_string_method_call(node: ast.expr) -> bool:
    """`".".join(parts)`, `"{}.{}".format(...)`, or either called on `str` with the string first."""
    if not (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"join", "format"}
    ):
        return False
    receiver = node.func.value
    return _string_value(receiver) is not None or (isinstance(receiver, ast.Name) and receiver.id == "str")


def _source_value(node: ast.expr | None) -> str | None:
    """A literal that can be run as source: a string, or bytes (`exec(b"import tests.rig")`) decoded
    as Python decodes source (PEP 263: a coding declaration, else UTF-8). Bytes that do not decode
    raise PlanError, as `exec` raises on them; the caller reads that as unresolved, like text that
    does not parse (Sol on #7121)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        try:
            return importlib.util.decode_source(node.value)
        except (UnicodeDecodeError, SyntaxError) as exc:
            raise PlanError(f"executed bytes source does not decode: {exc}") from None
    return _string_value(node)


def _read_executed_source(call: ast.Call) -> list[tuple[str, str | None]]:
    """`exec("import tests.rig")` runs code the module holds as a string: its imports and loads are
    read like the module's own, but only ever widen (`lookalike`), and an unresolved load in it is
    unresolved here; unparsable is unresolved. A source built at run time is the stated floor, as a
    name read from a file is: the census and reconcile suites `exec`/`eval` built text that loads no
    helper, and counting it would carry every helper on the real tree."""
    try:
        source = _source_value(_argument(call, 0, "source"))  # `compile(source=...)` takes it by keyword too
        if source is None:
            return []
        loads = loaded_test_helpers(source, "<executed source>")
        names = imported_test_helpers(source, "<executed source>")
    except PlanError:
        return [("unresolved", None)]
    readings: list[tuple[str, str | None]] = [
        ("lookalike", name) for name in names | loads.by_call | loads.by_plugins | loads.lookalike
    ]
    return readings + ([("unresolved", None)] if loads.unresolved or loads.path_targets else [])


def _with_submodules(call: ast.Call, loader: str, verdict: str, module: str) -> list[tuple[str, str | None]]:
    """`__import__("tests", fromlist=["rig"])` loads `tests.rig` as well: each literal entry is a
    submodule it may load, and a computed list (or `"*"`) may load any, so it reads as computed.
    `runpy.run_module("tests.pkg")` runs `tests.pkg.__main__` when `tests.pkg` is a package (Sol on
    #7121), so that name is read too; for a plain module no input has it, and it is skipped."""
    readings: list[tuple[str, str | None]] = [(verdict, module)]
    if loader == "run_module":
        return [*readings, (verdict, f"{module}.__main__")]
    if loader != "__import__" or (given := _argument(call, 3, "fromlist")) is None:
        return readings
    entries = [_string_value(entry) for entry in given.elts] if isinstance(given, (ast.List, ast.Tuple)) else [None]
    return readings + [(verdict, f"{module}.{e}") if e not in (None, "*") else ("computed", None) for e in entries]


def _dotted_tests_name(node: ast.expr) -> str | None:
    """A literal dotted module name under `tests` (`"tests.rig"`); a path or other text is None."""
    text = _string_value(node)
    if text is None or text.split(".")[0].casefold() != "tests":
        return None
    return text if all(part.isidentifier() for part in text.split(".")) else None


PROVIDERS = {  # each loader and the module that defines it: a call counts only through that binding
    "import_module": "importlib",
    "__import__": "builtins",
    "run_module": "runpy",
    "run_path": "runpy",
    "spec_from_file_location": "importlib.util",
    "SourceFileLoader": "importlib.machinery",
    "importorskip": "pytest",
    "exec": "builtins",
    "eval": "builtins",
    "compile": "builtins",
}
RUNS_SOURCE = {"exec", "eval", "compile"}  # builtins that run (or ready to run) code held as a string
NAME_LOADERS = frozenset(PROVIDERS) - RUNS_SOURCE  # matched by name alone; `re.compile` is a pattern, not a runner
PREPARES_ONLY = {"spec_from_file_location", "SourceFileLoader"}  # build a spec or loader: the module runs on
# `exec_module`, wherever the spec goes or never, so the call only widens and never places the helper
NAME_ARGUMENT = {"run_module": "mod_name", "importorskip": "modname"}  # a name loader's keyword, if not `name`
LOADERS = frozenset(PROVIDERS)
PATH_ARGUMENT = {
    "run_path": (0, "path_name"),
    "spec_from_file_location": (1, "location"),
    "SourceFileLoader": (1, "path"),
}


def _called_name(func: ast.expr) -> str | None:
    """`import_module` for `importlib.import_module(...)` and `import_module(...)` alike."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _proven_loaders(tree: ast.Module, dead: set[int]) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """The names this module binds ONLY to a loader (mapped to it) or to a loader's module (mapped to
    the module): `from importlib import import_module as load`, `load = importlib.import_module`
    chained to a fixed point, `import importlib.util`, and the builtin `__import__`. A name bound
    any other way as well (its own `def import_module`, `importlib = None`), or only in `dead` code,
    proves nothing, nor does a name holding both a loader and a loader module (`load = builtins`
    over `from importlib import import_module as load`, Sol on #7121). Third, the names assigned more
    than one loader (`a = b; b = a` over two imported loaders): either may run, so a call through
    one is unresolved, as for imports."""
    live = [node for node in ast.walk(tree) if id(node) not in dead]
    loaders, modules, proven, conflicting = _loader_imports(live)
    held = {name: {loader} for name, loader in loaders.items()}  # every loader each name may hold; only grows
    held_modules = {name: {module} for name, module in modules.items()}  # likewise for loader modules
    while True:
        grew = False
        single = _single(held_modules)
        for node in live:
            if (
                not isinstance(node, ast.Assign)
                or (assigned := _assigned(node.value, held, held_modules, single)) is None
            ):
                continue
            table, found = assigned
            for target in (t for t in node.targets if isinstance(t, ast.Name)):
                grew |= not found <= table.setdefault(target.id, set())
                table[target.id] |= found
        if not grew:
            break  # each pass adds a (name, value) pair or stops, so `a = b; b = a` converges (Codex on #7121)
    loaders, modules = _single(held), _single(held_modules)
    for node in live:
        if isinstance(node, ast.Assign) and _assigned(node.value, held, held_modules, modules) is not None:
            proven.update(target.id for target in node.targets if isinstance(target, ast.Name))
    counts = _binding_counts(tree)
    if "*" in counts:  # `from x import *` may bind any name, a loader's too: nothing is proven
        return {}, {}, set()
    mixed = held.keys() & held_modules.keys()  # a name holding a loader and a loader module
    ambiguous = {name for name, count in counts.items() if count > proven[name]} | conflicting | mixed
    return (
        {name: loader for name, loader in loaders.items() if name not in ambiguous},
        {name: module for name, module in modules.items() if name not in ambiguous},
        {name for name, found in held.items() if len(found) > 1}
        | {name for name, found in held_modules.items() if len(found) > 1},
    )


def _single(held: Mapping[str, set[str]]) -> dict[str, str]:
    return {name: next(iter(found)) for name, found in held.items() if len(found) == 1}


def _assigned(
    value: ast.expr, held: dict[str, set[str]], held_modules: dict[str, set[str]], modules: Mapping[str, str]
) -> tuple[dict[str, set[str]], set[str]] | None:
    """What an assignment's value may hold, and the table it belongs in: loaders (`_held_loaders`),
    or the loader modules a name holds (`b = builtins`, Sol on #7121); None for anything else. A
    dotted alias (`u = importlib.util`) is not followed: its providers' loaders are matched by name
    anyway."""
    if found := _held_loaders(value, held, modules):
        return held, found
    found = set(held_modules.get(value.id, ())) if isinstance(value, ast.Name) else set()
    return (held_modules, found) if found else None


def _loader_imports(
    nodes: Iterable[ast.AST],
) -> tuple[dict[str, str], dict[str, str], Counter[str], set[str]]:
    """Loaders and loader modules bound by import statements, how often each name is so bound, and
    the names bound to more than one of them (`from builtins import exec as run` and later `from
    importlib import import_module as run`): which one a call reaches depends on the order the
    module runs in, so such a name proves nothing and a call through it is unresolved (Sol on #7121)."""
    loaders: dict[str, str] = {name: name for name in ("__import__", *RUNS_SOURCE)}  # builtins, bound unimported
    modules: dict[str, str] = {}
    proven: Counter[str] = Counter()
    identities: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(m == alias.name or m.startswith(alias.name + ".") for m in PROVIDERS.values()):
                    bound = alias.asname or alias.name.split(".")[0]
                    modules[bound] = alias.name if alias.asname else bound
                    proven[bound] += 1
                    identities[bound].add(f"module {modules[bound]}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in LOADERS and node.module == PROVIDERS[alias.name]:
                    loaders[alias.asname or alias.name] = alias.name
                    proven[alias.asname or alias.name] += 1
                    identities[alias.asname or alias.name].add(f"loader {alias.name}")
    return loaders, modules, proven, {name for name, bound in identities.items() if len(bound) > 1}


def _binding_counts(tree: ast.Module) -> dict[str, int]:
    """How many times the module binds each name: imports, assignments, `def`, `class`, parameters,
    `del`, and the forms the syntax tree holds as plain strings: `except E as name` and the match
    captures `case name`, `[*name]` and `{**name}`; a write through the module namespace
    (`globals()["name"] = ...`) counts for its literal key. A `from x import *`, a computed key or a
    mutating namespace call (`globals().update(...)`) may bind any name and counts under `"*"`."""
    counts: dict[str, int] = {}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.asname or alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names = [node.id]
        elif written := _namespace_writes(node):
            names = written
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.arg):
            names = [node.arg]
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names = [node.name]
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names = [node.rest]
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


_NAMESPACE_READS = {"get", "keys", "values", "items", "copy", "__getitem__", "__contains__"}


def _namespace_writes(node: ast.AST) -> list[str]:
    """The names one node may bind through the module namespace: the literal key of
    `globals()["name"] = ...` or its `del`, and `"*"`, any name, for a computed key or a mutating
    method (`update`, `setdefault`, `__setitem__`), whose keys are not read. A read writes nothing."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) and _is_namespace(node.value):
        return [_string_value(node.slice) or "*"]
    if isinstance(node, ast.Attribute) and node.attr not in _NAMESPACE_READS and _is_namespace(node.value):
        return ["*"]
    return []


def _is_namespace(node: ast.expr) -> bool:
    """`globals()`, `locals()` or `vars(...)`: a mapping whose writes bind names."""
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"globals", "locals", "vars"}
    )


def _held_loaders(value: ast.expr, held: Mapping[str, set[str]], modules: Mapping[str, str]) -> set[str]:
    """Every loader an assignment's value may hold: all of a name's, so an alias of a name holding
    two loaders holds both (`c = a` after `a = spec_from_file_location`, Sol on #7121), or the one
    a `<module>.<loader>` attribute is."""
    if isinstance(value, ast.Name):
        return set(held.get(value.id, ()))
    return {loader} if (loader := _resolve_loader(value, {}, modules)) is not None else set()


def _resolve_loader(func: ast.expr, loaders: Mapping[str, str], modules: Mapping[str, str]) -> str | None:
    """The loader `func` is, through a proven binding: a bound name, or `<module>.<loader>` where the
    module is the one that defines that loader. None for anything else, `client.import_module` too."""
    if isinstance(func, ast.Name):
        return loaders.get(func.id)
    if isinstance(func, ast.Attribute) and func.attr in LOADERS:
        receiver = _dotted(func.value)
        if receiver is not None:
            head, _, rest = receiver.partition(".")
            if head in modules and ".".join(filter(None, [modules[head], rest])) == PROVIDERS[func.attr]:
                return func.attr
    return None


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)) is not None:
        return f"{base}.{node.attr}"
    return None


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((given.value for given in call.keywords if given.arg == keyword), None)


def _is_relative_load(call: ast.Call, loader: str) -> bool:
    """`__import__` with a nonzero `level`, or a leading-dot `import_module` name whose `package` is
    not a literal: both resolve against a package only the running module knows."""
    if loader == "__import__":
        level = _argument(call, 4, "level")
        return level is not None and not (isinstance(level, ast.Constant) and level.value == 0)
    if loader == "import_module":
        name = _string_value(_argument(call, 0, "name"))
        return name is not None and name.startswith(".") and _string_value(_argument(call, 1, "package")) is None
    return False


def _computed_verdict(call: ast.Call, loader: str) -> str:
    """How a load with no literal module widens: a path loader's reaches the files its module names,
    a package run reaches a `__main__.py` no path names (unresolved), any other is computed."""
    if _runs_a_package(call, loader) or _is_relative_load(call, loader) or _has_an_unreadable_file_name(call, loader):
        return "unresolved"  # a relative one reached only through a call sharing a loader's name too (Sol on #7121)
    return "computed_path" if loader in PATH_ARGUMENT else "computed"


PATH_JOINS = {"Path", "PurePath", "PosixPath", "WindowsPath", "join", "joinpath"}  # their last argument ends the path
PATH_WRAPPERS = {"fspath", "str", "abspath", "normpath", "realpath", "expanduser"}  # the same path, one argument
SAME_PATH_METHODS = {"resolve", "absolute", "expanduser"}  # `Path(...).resolve()`: the same path


def _path_tail(path: ast.expr) -> ast.expr:
    """The expression a path's last piece is built from, read through joins (`HERE / "pkg"`,
    `Path("tests", "pkg")`, `os.path.join(...)`) and wrappers that keep the path (`os.fspath(p)`,
    `str(p)`, `p.resolve()`)."""
    while True:
        if isinstance(path, ast.BinOp) and isinstance(path.op, ast.Div):
            path = path.right
        elif isinstance(path, ast.Call) and path.args and _called_name(path.func) in PATH_JOINS:
            path = path.args[-1]
        elif isinstance(path, ast.Call) and len(path.args) == 1 and _called_name(path.func) in PATH_WRAPPERS:
            path = path.args[0]
        elif (
            isinstance(path, ast.Call)
            and not path.args
            and isinstance(path.func, ast.Attribute)
            and path.func.attr in SAME_PATH_METHODS
        ):
            path = path.func.value
        else:
            return path


def _has_an_unreadable_file_name(call: ast.Call, loader: str) -> bool:
    """A path whose file name is built with two or more pieces only the running module knows
    (`HERE / f"{first}{second}.py"`, Sol on #7121): no literal spells the name or fills it, so
    `path_targets` cannot hold it. One such piece is filled by each literal (`_spelled_strings`),
    and a build whose last piece holds a separator (`dirname(__file__) + "/rig.py"`) spells the
    whole name."""
    path = _argument(call, *PATH_ARGUMENT[loader]) if loader in PATH_ARGUMENT else None
    pieces = _string_pieces(_path_tail(path)) if path is not None else None
    if not pieces:
        return False
    whole = pieces[-1] is not None and bool({"/", "\\"} & set(pieces[-1]))
    return not whole and pieces.count(None) > 1


def _spelled_strings(tree: ast.Module, literals: Iterable[str]) -> set[str]:
    """Every string the module builds from literals, as Python evaluates it (`"r" + "ig.py"`), and
    every build with one piece only the running module knows, filled by each literal the module
    spells: `SCRIPTS / f"{name}.py"` beside `_load("mik_audio_resolve")` reaches that script. A
    piece no literal fills is the stated floor, as a name read from a file is."""
    spelled: set[str] = set()
    for node in ast.walk(tree):
        pieces = _string_pieces(node) if isinstance(node, ast.expr) else None
        if pieces is None or pieces.count(None) > 1:
            continue
        if None not in pieces:
            spelled.add("".join(piece for piece in pieces if piece is not None))
            continue
        hole = pieces.index(None)
        before = "".join(piece for piece in pieces[:hole] if piece is not None)
        after = "".join(piece for piece in pieces[hole + 1 :] if piece is not None)
        spelled |= {before + literal + after for literal in literals}
    return spelled


def _string_pieces(node: ast.expr) -> list[str | None] | None:
    """A string build's pieces in order, None for each one only the running module knows:
    `"r" + stem` is `["r", None]`, and `f"{stem}.py"` and `"%s.py" % stem` are `[None, ".py"]` (the
    last with an empty first piece). Any other `%` format is unreadable. None when `node` builds
    no string."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_pieces_of(node.left), *_pieces_of(node.right)]
    if isinstance(node, ast.JoinedStr):
        return [
            _string_value(part)
            if isinstance(part, ast.Constant)
            else _joined(_pieces_of(part.value))
            if isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None
            else None
            for part in node.values
        ]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and (form := _string_value(node.func.value)) is not None
    ):
        return _format_pieces(form, node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and (form := _string_value(node.left)) is not None:
        if form.count("%") == 1 and form.count("%s") == 1 and not isinstance(node.right, ast.Tuple):
            before, _, after = form.partition("%s")
            return [before, _joined(_pieces_of(node.right)), after]
        return [None, None]
    return None


def _format_pieces(form: str, call: ast.Call) -> list[str | None]:
    """`"{}{}.py".format("r", "ig")` as its pieces (Sol on #7121): an automatic or numbered field takes
    its positional argument's folded value. A named field, a keyword or starred argument, a format
    spec or conversion, or a template `str.format` would refuse, is unreadable."""
    try:
        fields = list(string.Formatter().parse(form))
    except ValueError:
        return [None, None]
    if call.keywords or any(isinstance(argument, ast.Starred) for argument in call.args):
        return [None, None]
    pieces: list[str | None] = []
    automatic = 0
    for literal, field, spec, conversion in fields:
        pieces.append(literal)
        if field is None:
            continue
        if spec or conversion or not (field == "" or field.isdigit()):
            return [None, None]
        index = automatic if field == "" else int(field)
        automatic += field == ""
        pieces.append(_joined(_pieces_of(call.args[index])) if index < len(call.args) else None)
    return pieces


def _pieces_of(node: ast.expr) -> list[str | None]:
    return _string_pieces(node) or [_string_value(node)]


def _joined(pieces: list[str | None]) -> str | None:
    return None if None in pieces else "".join(piece for piece in pieces if piece is not None)


def _runs_a_package(call: ast.Call, loader: str) -> bool:
    """`runpy.run_path("tests/support/pkg")`: a path whose last piece is known and is not a `.py`
    file names a directory or an archive, and Python runs the `__main__.py` inside it, which the
    reader cannot name from the path, so the load is unresolved (Codex on #7121). The path is read
    through its wrappers (`Path("tests/support/pkg")`, `os.fspath(...)`, `HERE / "pkg"`, Sol on
    #7121); an unreadable last piece (`run_path(GATE)`) reaches the files the module spells."""
    if loader != "run_path" or (path := _argument(call, *PATH_ARGUMENT[loader])) is None:
        return False
    last = _pieces_of(_path_tail(path))[-1]
    return last is not None and not last.casefold().endswith(".py")


def _loaded_module(call: ast.Call, loader: str) -> str | None:
    """The dotted module a loader call loads when its arguments are literals: a name (relative ones
    against a literal `package`), or a `.py` path under the repository root. None when computed."""
    if loader in PATH_ARGUMENT:
        path = _string_value(_argument(call, *PATH_ARGUMENT[loader]))
        if path is None or not path.endswith(".py") or ntpath.isabs(path) or ntpath.splitdrive(path)[0]:
            return None  # computed, absolute (`/srv/checkout/tests/rig.py`) or on a drive (`C:tests\\rig.py`, relative
            # to that drive's own working directory): reaches the files the module names
        relative = posixpath.normpath(path.replace("\\", "/"))  # `tests\rig.py` is the same file on Windows
        return None if relative.startswith("../") else relative.removesuffix(".py").replace("/", ".")
    if _is_relative_load(call, loader):
        return None  # against a package only the running module knows
    name = _string_value(_argument(call, 0, NAME_ARGUMENT.get(loader, "name")))
    if name is not None and name.startswith("."):
        try:
            return importlib.util.resolve_name(name, _string_value(_argument(call, 1, "package")))
        except ImportError:  # `..rig` beyond `tests`, or no package: the call fails if it runs (Sol on #7121)
            return None
    return name


def _referenced_loader(node: ast.AST, loaders: Mapping[str, str]) -> str | None:
    """The loader read as a value, or None: the caller skips a call's own function and an
    assignment's value, which `_proven_loaders` follows, so what is left is a loader handed on
    unseen. Matched by name, proven or not: it can only widen. Source runners are
    `_is_runner_reference`'s."""
    if not isinstance(node, ast.Name | ast.Attribute) or not isinstance(node.ctx, ast.Load):
        return None
    name = node.id if isinstance(node, ast.Name) else node.attr
    proven = loaders.get(name) if isinstance(node, ast.Name) else None
    if proven is not None and proven not in RUNS_SOURCE:
        return proven
    return name if name in NAME_LOADERS else None


def _is_runner_reference(node: ast.AST, loaders: Mapping[str, str], modules: Mapping[str, str]) -> bool:
    """`exec` / `eval` / `compile` read as a value through a binding (`exec`, `builtins.exec`, `b.exec`
    for `import builtins as b`, `run` for `from builtins import exec as run`). The caller passes every
    binding, proven or not: proof is per module, so `def helper(exec)` in one function unproved the
    builtin and hid `holder.run = exec` at module level (Sol on #7121). The bindings include the
    aliases `_proven_loaders` propagates, so `run = exec; holder.run = run` is matched too. It only widens."""
    if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(node.ctx, ast.Load):
        return False
    return _resolve_loader(node, loaders, modules) in RUNS_SOURCE


def _string_value(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


PLUGIN_MUTATIONS = {"append": 0, "extend": 0, "insert": 1}  # each list method and its new-entries argument


def _plugin_names(tree: ast.Module, dead: set[int]) -> set[str | None]:
    """Every name the module's `pytest_plugins` hands pytest, None for one the reader cannot read: a
    computed entry, or any mention of the name besides a single-target assignment and a literal
    `append`, `extend` or `insert` on it: read back, aliased (`pytest_plugins = plugins = []`),
    unpacked, imported, or set by its name (`globals()["pytest_plugins"]`, `update(pytest_plugins=...)`).
    One under `if TYPE_CHECKING:` (`dead`) never runs and hands pytest nothing (Sol on #7121)."""
    names: set[str | None] = set()
    read: set[int] = set()  # the `pytest_plugins` names read here, as a declaration or a mutation's receiver
    live = [node for node in ast.walk(tree) if id(node) not in dead]
    for node in live:
        if (declared := _plugin_declaration(node)) is not None:
            read.add(id(declared[0]))
            names |= {_string_value(entry) for entry in _list_entries(declared[1])}
        elif isinstance(node, ast.Call) and (entries := _plugin_mutation_entries(node)) is not None:
            read.add(id(node.func.value))  # type: ignore[attr-defined]
            names |= {_string_value(entry) for entry in entries}
    if any(_names_pytest_plugins(node) and id(node) not in read for node in live):
        names.add(None)
    return names


def _plugin_declaration(node: ast.AST) -> tuple[ast.Name, ast.expr] | None:
    """`pytest_plugins = [...]`, `+=` or annotated, as (target, value): one plain-name target only,
    since a second target (`pytest_plugins = plugins = []`) shares the list with a name unread here."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
        target, value = node.target, node.value
    else:
        return None
    if isinstance(target, ast.Name) and target.id == "pytest_plugins":
        return target, value
    return None


def _plugin_mutation_entries(call: ast.Call) -> list[ast.expr] | None:
    """The entries `pytest_plugins.append(x)` / `.extend([...])` / `.insert(i, x)` adds; None for any other call."""
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest_plugins"
        and (index := PLUGIN_MUTATIONS.get(func.attr)) is not None
        and len(call.args) == index + 1
        and not call.keywords
    ):
        return None
    argument = call.args[index]
    return _list_entries(argument) if func.attr == "extend" else [argument]


def _list_entries(value: ast.expr) -> list[ast.expr]:
    return value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]


IDENTIFIER_FIELDS = (  # each node that spells an identifier, and the field it spells it in
    (ast.Name, "id"),
    (ast.Attribute, "attr"),
    (ast.keyword, "arg"),
    (ast.alias, "name"),
    (ast.alias, "asname"),
    (ast.FunctionDef, "name"),
    (ast.AsyncFunctionDef, "name"),
    (ast.ClassDef, "name"),
    (ast.MatchAs, "name"),  # `case pytest_plugins:` binds the subject (Sol on #7121)
    (ast.MatchStar, "name"),  # `case [*pytest_plugins]:`
    (ast.MatchMapping, "rest"),  # `case {**pytest_plugins}:`
)


def _names_pytest_plugins(node: ast.AST) -> bool:
    """A mention of the plugin list: the identifier anywhere (a name in any context, an attribute,
    a keyword, an import, a definition), or the string as a key (`globals()["pytest_plugins"]`,
    `{"pytest_plugins": ...}`, `setattr(module, "pytest_plugins", ...)`, a namespace method,
    `globals().setdefault("pytest_plugins", ...)`), and a namespace write whose key the reader cannot
    read (Sol on #7121). The string held as data
    (`t.id == "pytest_plugins"` in a test of the root conftest) is not."""
    if any(isinstance(node, kind) and getattr(node, field) == "pytest_plugins" for kind, field in IDENTIFIER_FIELDS):
        return True
    if isinstance(node, ast.Subscript) and _string_value(node.slice) == "pytest_plugins":
        return True
    if isinstance(node, ast.Dict) and any(
        key is not None and _string_value(key) == "pytest_plugins" for key in node.keys
    ):
        return True
    if isinstance(node, ast.Call) and (
        _called_name(node.func) in {"setattr", "getattr", "delattr"}
        or (isinstance(node.func, ast.Attribute) and _is_namespace(node.func.value))
    ):
        return any(_string_value(argument) == "pytest_plugins" for argument in node.args)
    return "*" in _namespace_writes(node)  # `globals().update(names)`: it may set the list


# Path joins and the repository paths they resolve to, read by `narrow` for the paths a module spells
# and walks, and by carry for the files a path loader runs (Sol on #7495). Moved here from
# ci_plan_narrow.py, which imports ci_plan_carry.py.
_PATH_TYPES = frozenset({"Path", "PurePath", "PurePosixPath", "PosixPath"})
_PATH_JOINS = _PATH_TYPES | {"join", "joinpath"}
_PATH_SHAPED = re.compile(r"^[\w./-]{1,200}$")
_WILDCARD = re.compile(r"[*?[]")
_SAME_PATH = frozenset({"resolve", "absolute", "abspath", "realpath"})
_RESOLVE_DEPTH = 12


def _division_chain(node: ast.BinOp) -> list[ast.expr]:
    """`a / b / c` as `[a, b, c]`."""
    parts: list[ast.expr] = []
    current: ast.expr = node
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
        parts.insert(0, current.right)
        current = current.left
    return [current, *parts]


def _join_parts(node: ast.AST) -> list[ast.expr] | None:
    """The segments a path join spells, in order, a joined receiver or left side flattened:
    `a / b`, `Path(a, b)`, `os.path.join(a, b)` and `a.joinpath(b)`; None for any other node."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        head, *rest = _division_chain(node)
        return [*(_join_parts(head) or [head]), *rest]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath":
        return [*(_join_parts(node.func.value) or [node.func.value]), *node.args]
    if isinstance(node, ast.Call) and _called_name(node.func) in _PATH_JOINS:
        return list(node.args)
    return None


def _constant_segment(node: ast.expr) -> str | None:
    """A string constant, a string build of nothing but constants (`f"{'fro'}nt"`, `"fro" + "nt"`),
    or a join of nothing but those, as path text with its slashes kept (a leading one makes it
    absolute); None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value or None
    if (pieces := _string_pieces(node)) is not None:
        return _joined(pieces) or None
    values = [_constant_segment(part) for part in _join_parts(node) or ()]
    return posixpath.join(*[v for v in values if v]) if values and all(values) else None


def joined_paths(text: str, where: str) -> set[str]:
    """Paths spelled as a constant join: the trailing run of two or more string segments of
    `ROOT / "scripts" / "x.py"`, or of a `Path(...)`, `os.path.join(...)` or `.joinpath(...)` call,
    a `.joinpath` receiver included.
    A runtime segment ends the run, so `ROOT / name / "x.py"` joins nothing."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    found: set[str] = set()
    for node in ast.walk(tree):
        segments = _join_parts(node)
        if segments is None:  # any other node spells no join
            continue
        run: list[str] = []
        for segment in reversed(segments):
            value = _constant_segment(segment)
            if not value:
                break
            run.insert(0, value)
        if len(run) >= 2:
            found.add(posixpath.join(*run))  # an absolute segment restarts the path, as Python joins do
    return found


def _simple_bindings(tree: ast.AST) -> list[tuple[str, ast.expr]]:
    """Each `name = value`, `name: T = value` and `(name := value)` in the file."""
    bindings: list[tuple[str, ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bindings += [(target.id, node.value) for target in node.targets if isinstance(target, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name) and node.value:
            bindings.append((node.target.id, node.value))
    return bindings


def _single_bindings(tree: ast.AST) -> dict[str, ast.expr]:
    """The simple bindings of names bound nowhere else in the file: no second assignment, and no
    parameter, loop variable or import of the same name, any of which could rebind it."""
    stores = Counter(n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
    stores.update(n.arg for n in ast.walk(tree) if isinstance(n, ast.arg))
    stores.update(
        (alias.asname or alias.name).split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for alias in n.names
    )
    return {name: value for name, value in _simple_bindings(tree) if stores[name] == 1}


def _directory_of(node: ast.expr, where: str, bindings: Mapping[str, ast.expr], depth: int) -> str | None:
    """The repository path an expression names: a constant (`"apps"`, a glob by its fixed head), the
    file itself (`__file__`) and the paths one step from it, a constant join (`REPO / "apps"`,
    `Path("apps")`, `os.path.join(ROOT, "apps")`), or a name bound once to one of those. None
    when it cannot be resolved."""
    if depth > _RESOLVE_DEPTH:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _repository_dir(node.value)
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return where
        value = bindings.get(node.id)
        return None if value is None else _directory_of(value, where, bindings, depth + 1)
    step = _path_step(node)
    if step is not None:
        return _parent_of(_directory_of(step[0], where, bindings, depth + 1), step[1])
    parts = _join_parts(node)
    if parts is None:
        return None
    if not parts:
        return "."  # `Path()` is `Path(".")`
    head = _directory_of(parts[0], where, bindings, depth + 1)
    rest = [p.value for p in parts[1:] if isinstance(p, ast.Constant) and isinstance(p.value, str)]
    return None if head is None or len(rest) != len(parts) - 1 else _repository_dir(posixpath.join(head, *rest))


def _path_step(node: ast.expr) -> tuple[ast.expr, int] | None:
    """The path an expression steps from, and how many directories up: `p.parent` (1),
    `p.parents[k]` (k + 1), `os.path.dirname(p)` (1), `p.resolve()` or `os.path.abspath(p)` (0)."""
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return node.value, 1
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        return node.value.value, node.slice.value + 1
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _SAME_PATH:
        return (node.args[0] if node.args else node.func.value), 0
    if isinstance(node, ast.Call) and _called_name(node.func) == "dirname" and len(node.args) == 1:
        return node.args[0], 1
    return None


def _repository_dir(path: str) -> str | None:
    """`path` as a repository path cut at its first wildcard (`apps/**/*.py` is `apps`): "." for the
    root, None for a path leaving the repository."""
    head = "/".join(itertools.takewhile(lambda part: not _WILDCARD.search(part), path.strip().split("/")))
    return "." if posixpath.normpath(head or ".") == "." else _repository_path(head)


def _parent_of(path: str | None, levels: int) -> str | None:
    """`path` `levels` directories up, or None above the repository root."""
    for _ in range(levels):
        if path is None or path == ".":
            return None
        path = posixpath.dirname(path) or "."
    return path


def _repository_path(literal: str) -> str | None:
    """`literal` as a normalized repository path: `./x` and `tests/../x` name `x`. None for text
    that is not path-shaped, the root itself, or a path leaving the repository (`../x`, `/x`)."""
    text = literal.strip()
    if not _PATH_SHAPED.match(text):
        return None
    path = posixpath.normpath(text)
    return None if path in (".", "..") or path.startswith(("/", "../")) else path


def mentioned_strings(text: str, where: str) -> set[str]:
    """Every string literal a module carries in CODE, read with the AST. Round 9.

    The consumers of a scope the import reader cannot see (Svelte, TypeScript, JSON) are the
    tests that spell its path: `Path("apps/webui/frontend/dist")`, a `pnpm --dir` argument,
    `WEBUI / "frontend"`. Those are string constants. Comments and docstrings are NOT: the root
    `tests/conftest.py` says "frontend" twice in comments and consumes nothing, and reading
    raw text would have made the whole frontend helper-carried, which is FULL on every change
    and exactly the over-selection this reader exists to remove. A docstring is the first
    statement of a module, class or function, and is skipped by that position.

    Fails loud on a file that does not parse, for the reason `imported_packages` does.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    }
