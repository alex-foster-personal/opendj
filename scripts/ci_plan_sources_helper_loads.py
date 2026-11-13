"""pytest helper loads the CI planner cannot see through imports alone."""

from __future__ import annotations

import ast
import importlib.util
import posixpath
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeGuard

from scripts.ci_plan_sources_ast import _type_checking_only
from scripts.ci_plan_sources_literals import mentioned_strings
from scripts.ci_plan_sources_loader_graph import (
    LOADERS,
    NAME_LOADERS,
    PATH_ARGUMENT,
    PREPARES_ONLY,
    RUNS_SOURCE,
    _argument,
    _called_name,
    _computed_verdict,
    _dotted,
    _is_relative_load,
    _is_runner_reference,
    _loaded_module,
    _loader_imports,
    _plugin_names,
    _proven_loaders,
    _referenced_loader,
    _resolve_loader,
    _spelled_strings,
    _string_value,
)
from scripts.ci_plan_sources_read import PlanError, imported_test_helpers


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
