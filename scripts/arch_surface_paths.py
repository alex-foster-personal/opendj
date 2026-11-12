"""Part of `arch_surface.py`, split out with no behavior change: paths."""

from __future__ import annotations

import ast
import functools
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

CONTRACT = "contract"
HTTP = "http"
CONTAINER = "apps"
DEFAULT_ALLOWED_EXTERNAL = frozenset({"pydantic", "typing_extensions", "annotated_types"})
# What a call that loads a module by a name no reader can resolve imports; it is never pure.
DYNAMIC = "<dynamic import>"
# Every call that loads a module by name or path: importlib's, the builtin's, a spec's, runpy's and pkgutil's.
_LOADERS = frozenset(
    {"import_module", "__import__", "exec_module", "load_module", "run_module", "run_path", "resolve_name"}
)
# pydoc's loader, whose name is common enough to mean something else (``apps.reconcile.locate``), so it
# is a loader only in a module that can hold pydoc (`_pydoc_bindings`, which also reads its other
# entry points). ``pydoc.locate("apps.x.impl.run")`` imports ``apps.x.impl`` to find ``run``.
_PYDOC_LOADERS = frozenset({"locate"})
# The keyword each loader takes its target by: ``import_module(name=...)``, ``run_module(mod_name=...)``.
_TARGETS = frozenset({"name", "mod_name", "path_name", "path", "location"})
# The standard library's module-loading machinery: a pure scope that holds one can reach a loader
# by reflection (``getattr(importlib, "import_module")``) that no reader can follow, so it is refused.
_LOADER_MODULES = frozenset({"importlib", "runpy", "pkgutil", "zipimport", "imp", "builtins", "pydoc"})
# Builtins that run text as code; ``exec("import x")`` loads what no import statement shows.
_EVALUATORS = frozenset({"exec", "eval", "compile"})


@dataclass(frozen=True, order=True)
class Violation:
    rule: str  # "deep-import", "impure-contract", "dynamic-load", "stale-dynamic-loader" or "unread-module"
    importer: str
    imported: str

    def describe(self) -> str:
        if self.rule == "dynamic-load":
            return (
                f"{self.importer}: loads a module by a name no gate can read; name the module in"
                " source, or list this module under dynamic_loaders"
            )
        if self.rule == "stale-dynamic-loader":
            return f"{self.importer}: listed under dynamic_loaders but loads nothing by a runtime name; unlist it"
        if self.rule == "unread-module":
            return (
                f"{self.importer}: held only as {self.imported}, which no gate can read for its imports;"
                " ship it as source, or keep the build output out of the tree (.gitignore)"
            )
        if self.rule == "deep-import":
            return f"{self.importer} -> {self.imported}: import its {CONTRACT} instead"
        if self.imported == DYNAMIC:
            return f"{self.importer}: loads a module by a name no gate can read; a {CONTRACT} imports statically"
        return (
            f"{self.importer} -> {self.imported}: a {CONTRACT} and its package root import only"
            " stdlib, pydantic and enforced modules' contracts"
        )


def package_of(module: str, container: str = CONTAINER) -> str | None:
    """`apps.X.y.z` -> `X`; None for anything outside the container."""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != container:
        return None
    return parts[1]


def _within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def is_contract(module: str, container: str = CONTAINER) -> bool:
    parts = module.split(".")
    return len(parts) >= 3 and parts[0] == container and parts[2] == CONTRACT


def _pure_import_ok(
    importer: str, imported: str, allowed_external: frozenset[str], container: str, enforced: frozenset[str]
) -> bool:
    top = imported.split(".", maxsplit=1)[0]
    if top != container:
        if top in _LOADER_MODULES:
            return False
        return top in sys.stdlib_module_names or top in allowed_external or top == "__future__"
    if is_contract(imported, container):
        # Importing apps.Y.contract runs apps.Y's root first, and only an enforced root is
        # scanned for purity: an unenforced one could pull an implementation in behind it.
        return package_of(imported, container) in enforced
    # A root reading a name from itself (``from . import x``, x no module) imports nothing new.
    # Nothing else in the container, the scope's own package root included: the declaration
    # gate reads only what a contract module defines, so a root's function or value re-exported
    # through the contract would cross the boundary unread (Sol, PR #6182).
    return imported == importer


def _is_root(importer: str, importer_pkg: str | None, delivery: frozenset[str], roots: frozenset[str]) -> bool:
    """A delivery layer, a named composition root outside the container, or a package's ``__main__`` entry point.

    Only a ``__main__`` inside the container: one elsewhere is under no other rule here either.
    """
    return (
        importer_pkg in delivery
        or importer.split(".", maxsplit=1)[0] in roots
        or (importer_pkg is not None and importer.rsplit(".", maxsplit=1)[-1] == "__main__")
    )


def surface_violations(
    imports: Iterable[tuple[str, str]],
    enforced: Iterable[str],
    delivery: Iterable[str],
    allowed_external: Iterable[str] = DEFAULT_ALLOWED_EXTERNAL,
    container: str = CONTAINER,
    dynamic_loaders: Iterable[str] = (),
    *,
    composition_roots: Iterable[str],
) -> list[Violation]:
    """Every direct import that breaks the surface or purity rule, sorted.

    ``composition_roots`` names the top-level trees outside the container that are entry
    points (CLIs, farms, benches) and so may import a module's ``http``; it has no default,
    so every caller states the allowance it is checking.

    ``imports`` is (importer, imported) module pairs; external packages may appear
    squashed to their top-level name, as grimp reports them. A load by a name no reader
    can resolve (`DYNAMIC`) could load any implementation unseen, so it fails closed: it is
    refused in every importer the graph holds, inside the container or out (a script loads an
    implementation as surely as an app does), except in a module ``dynamic_loaders`` lists (a pure
    scope refuses it as impure either way), and a listed module that no longer loads that
    way is refused as stale, so the list only shrinks.
    """
    enforced_set = frozenset(enforced)
    delivery_set = frozenset(delivery)
    externals = frozenset(allowed_external)
    listed = frozenset(dynamic_loaders)
    roots = frozenset(composition_roots)
    found: set[Violation] = set()
    loading: set[str] = set()
    for importer, imported in imports:
        importer_pkg = package_of(importer, container)
        imported_pkg = package_of(imported, container)
        pure_scope = importer_pkg in enforced_set and (
            is_contract(importer, container) or importer == f"{container}.{importer_pkg}"
        )
        if imported == DYNAMIC:  # every scanned importer, in the container or not
            loading.add(importer)
            if importer not in listed and not pure_scope:
                found.add(Violation("dynamic-load", importer, DYNAMIC))

        if imported_pkg in enforced_set and importer_pkg != imported_pkg:
            base = f"{container}.{imported_pkg}"
            ok = _within(imported, f"{base}.{CONTRACT}") or (
                _is_root(importer, importer_pkg, delivery_set, roots) and _within(imported, f"{base}.{HTTP}")
            )
            if not ok:
                found.add(Violation("deep-import", importer, imported))

        if pure_scope and not _pure_import_ok(importer, imported, externals, container, enforced_set):
            found.add(Violation("impure-contract", importer, imported))
    found.update(Violation("stale-dynamic-loader", module, DYNAMIC) for module in listed - loading)
    return sorted(found)


def _from_base(module: str, is_package: bool, level: int, name: str | None) -> str:
    """The absolute module a ``from ... import`` names, relative levels resolved."""
    if level == 0:
        return name or ""
    parts = module.split(".") if is_package else module.split(".")[:-1]
    parts = parts[: len(parts) - (level - 1)]
    return ".".join([*parts, name] if name else parts)


def _callee(call: ast.Call) -> ast.expr:
    callee = call.func
    while isinstance(callee, ast.NamedExpr):  # ``(f := getattr(...))(name)`` calls its value
        callee = callee.value
    return callee


def _reaches_module_table(tree: ast.AST) -> bool:
    """Whether the module imports ``sys`` and names its module table (``sys.modules``,
    ``getattr(sys, "modules")``, ``from sys import modules``), which holds every loaded module."""
    imports_sys = any(isinstance(node, ast.alias) and node.name == "sys" for node in _nodes(tree))
    module_table = (
        (isinstance(node, ast.Attribute) and node.attr == "modules")
        or (isinstance(node, ast.Constant) and node.value == "modules")
        or (isinstance(node, ast.alias) and node.name == "modules")
        for node in _nodes(tree)
    )
    return imports_sys and any(module_table)


def _root_name(node: ast.AST) -> str:
    """The name an attribute chain starts from: ``importlib`` for ``importlib.util.__dict__``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


_BUILTINS = frozenset({"builtins", "__builtins__"})


def _reaches_builtins(tree: ast.AST) -> bool:
    """Whether the module can hold the builtins module under some name: it names it (``import
    builtins``, ``__import__("builtins")``, ``__builtins__``, ``f.__globals__["__builtins__"]``) or
    reaches the module table that holds it (``sys.modules``). Which name ends up holding it
    (``b = __import__("builtins")``, ``m = sys.modules[k]``) is more than any reader can follow,
    so every attribute call named like an evaluator in such a module is read as one. A load by a
    name no reader can resolve is DYNAMIC already."""
    for node in _nodes(tree):
        if isinstance(node, ast.Name) and node.id in _BUILTINS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _BUILTINS:
            return True
        if isinstance(node, ast.Constant) and node.value in _BUILTINS:
            return True
        if isinstance(node, ast.alias) and node.name.split(".")[0] in _BUILTINS:
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _BUILTINS:
            return True
    return _reaches_module_table(tree)


def _evaluator_names(tree: ast.AST) -> frozenset[str]:
    """Each alias ``from builtins import exec as run`` binds (`_runs_text` adds `_EVALUATORS`)."""
    evaluators: set[str] = set()
    for node in _nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "builtins":
            evaluators.update(a.asname for a in node.names if a.asname and a.name in _EVALUATORS)
    return frozenset(evaluators)


def _tested_only(tree: ast.AST) -> frozenset[int]:
    """Loader names written as text that can only be tested against, never looked up: an
    operand of ``==`` or ``!=``, the elements of a set display that is itself the right side of ``in`` or ``not in``, or that is
    bound once (bare or through ``frozenset``/``set``) to a name read nowhere but there. A gate's
    own table of loaders (`_LOADERS`) loads nothing; the same text handed anywhere else might."""
    bound = Counter(_bound_names(tree))
    tests = {
        id(right)
        for node in _nodes(tree)
        if isinstance(node, ast.Compare)
        for op, right in zip(node.ops, node.comparators, strict=True)
        if isinstance(op, ast.In | ast.NotIn)
    }
    reads: dict[str, list[ast.Name]] = {}
    for node in _nodes(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            reads.setdefault(node.id, []).append(node)
    # ``alias.name == "resolve_name"`` only tests the text too.
    compared = {
        id(side)
        for node in _nodes(tree)
        if isinstance(node, ast.Compare)
        for op, left, right in zip(node.ops, [node.left, *node.comparators], node.comparators, strict=False)
        if isinstance(op, ast.Eq | ast.NotEq)
        for side in (left, right)
        if isinstance(side, ast.Constant)
    }
    displays = [node for node in _nodes(tree) if isinstance(node, ast.Set) and id(node) in tests]
    for node in _nodes(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name, value = node.targets[0].id, node.value
        if isinstance(value, ast.Call) and _called_name(value.func) in {"frozenset", "set"}:
            built = isinstance(value.func, ast.Name) and not bound[value.func.id] and not value.keywords
            value = value.args[0] if built and len(value.args) == 1 else value
        if isinstance(value, ast.Set) and bound[name] == 1 and all(id(r) in tests for r in reads.get(name, ())):
            displays.append(value)
    return frozenset(compared | {id(e) for d in displays for e in d.elts if isinstance(e, ast.Constant)})


def _name_resolvers(tree: ast.AST) -> frozenset[int]:
    """Callees that are ``importlib.util.resolve_name``, which only turns a relative name absolute
    and imports nothing, unlike ``pkgutil.resolve_name``: ``importlib.util.resolve_name`` with
    ``importlib`` bound only by plain ``import importlib[.x]``, or a name bound only by
    ``from importlib.util import resolve_name``."""
    bound = Counter(_bound_names(tree))
    aliases = [
        (node, alias) for node in _nodes(tree) if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names
    ]
    plain = sum(
        isinstance(node, ast.Import) and alias.asname is None and alias.name.split(".")[0] == "importlib"
        for node, alias in aliases
    )
    named = {
        alias.asname or alias.name
        for node, alias in aliases
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "importlib.util"
        and alias.name == "resolve_name"
    }
    return frozenset(
        id(node)
        for node in _nodes(tree)
        if (
            isinstance(node, ast.Attribute)
            and ast.unparse(node) == "importlib.util.resolve_name"
            and bound["importlib"] == plain > 0
        )
        or (isinstance(node, ast.Name) and node.id in named and bound[node.id] == 1)
    )


def _loads_entry_point(call: ast.Call, modules: frozenset[str]) -> bool:
    """``x.load()``, as an entry point is loaded: no arguments, and not read straight off a name an
    import of another module binds (``json.load(fh)`` takes one anyway)."""
    callee = _callee(call)
    off_module = isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name) and callee.value.id in modules
    bare = not call.args and not call.keywords
    return isinstance(callee, ast.Attribute) and callee.attr == "load" and bare and not off_module


def _holds_metadata(tree: ast.AST) -> bool:
    """Whether the module can hold ``importlib.metadata`` (or its ``importlib_metadata`` backport),
    whose entry points import the ``module:attr`` text they carry on ``.load()``
    (``EntryPoint(name, value, group).load()``, ``entry_points(group=g)[0].load()``): it imports
    it, or names it as text. That text is runtime data, so such a load is never read."""
    tested = _tested_only(tree)
    texts = (
        n.value
        for n in _nodes(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in tested
    )
    imported = (
        [a.name for a in n.names]
        if isinstance(n, ast.Import)
        else [f"{n.module}.{a.name}" for a in n.names]
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
        else []
        for n in _nodes(tree)
    )
    names = [*texts, *(name for each in imported for name in each)]
    return any(name.startswith(("importlib.metadata", "importlib_metadata")) for name in names)


def _pydoc_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str], bool]:
    """How the module can hold pydoc, every one of whose entry points resolves a dotted text by
    importing it (``pydoc.locate``, ``pydoc.render_doc("apps.x.impl.run")``, ``pydoc.resolve``): the
    names ``import pydoc`` binds, the names ``from pydoc import ...`` binds, and whether it names
    pydoc as text (``__import__("pydoc")``), where which name ends up holding it is unknown. A text
    only tested against (`_tested_only`) names nothing."""
    tested = _tested_only(tree)
    imports = [n for n in _nodes(tree) if isinstance(n, ast.Import | ast.ImportFrom)]
    bound = {a.asname or a.name for n in imports if isinstance(n, ast.Import) for a in n.names if a.name == "pydoc"}
    members = {
        a.asname or a.name
        for n in imports
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == "pydoc"
        for a in n.names
    }
    texted = any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.split(".")[0] == "pydoc"
        and id(n) not in tested
        for n in _nodes(tree)
    )  # fmt: skip
    return frozenset(bound), frozenset(members), texted


def _is_loader(name: str | None, aliases: frozenset[str]) -> bool:
    """A loader by its own name or by one the module binds it to (`_loader_names`)."""
    return name in _LOADERS or name in aliases


def _loader_names(tree: ast.AST) -> frozenset[str]:
    """Every name the module binds a loader to (`_is_loader` adds `_LOADERS` themselves), so that
    ``from importlib import import_module as load``, ``load = importlib.import_module`` or
    ``again = load`` still reads ``again("x")`` as a load."""
    pydoc, members, texted = _pydoc_bindings(tree)
    homed = bool(pydoc or members or texted) or _reaches_module_table(tree)
    names = set(members)
    for node in _nodes(tree):
        # ``locate`` read where pydoc can be held: tested by name, never listed
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
        names.update([name] if homed and name in _PYDOC_LOADERS else [])
        # any entry point read off pydoc, or, where pydoc is taken by its text, off anything called
        rooted = isinstance(node, ast.Attribute) and _root_name(node) in pydoc
        names.update([name] if rooted else [])
        if texted and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname for a in node.names if a.asname and a.name in _LOADERS)
    # ``(load,) = (import_module,)`` binds by position as ``load = import_module`` does.
    bindings = [
        pair
        for node in _nodes(tree)
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr) and node.value is not None
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        for pair in _unpacked(target, node.value)
    ]
    grown = True
    while grown:  # to a fixed point: `again = load` binds a loader as surely as `load = import_module`
        before = len(names)
        names.update(name for name, value in bindings if _is_loader(_called_name(value), frozenset(names)))
        grown = len(names) > before
    return frozenset(names)


# The import machinery, every callable of which is read as a loader (`_machinery_calls`) except
# these, which never import a module by a name they are handed: they read installed metadata,
# build specs, loaders and paths without running them, or list without importing. Each is the
# qualified name an import binds; a ``.load()``, ``exec_module`` or ``load_module`` on what they
# return is read where it is called.
# The interpreter's own interactive loop (`_INTERPRETERS`) is read the same way, its every call a
# `DYNAMIC` load: ``code.InteractiveInterpreter().runsource(text)`` runs text as code, and the
# interpreter it builds is handed on as a value no reader follows (Codex, PR #6182).
_INTERPRETERS = frozenset({"code", "codeop"})
# A pickle stream names the modules its objects come from, and unpickling imports each one
# (``pickle.loads(payload)`` can load ``apps.x.impl``): every call into a pickle reader is
# `DYNAMIC` too, whatever it is handed, since even literal bytes name what no reader parses
# (Codex, PR #6182). Writing a pickle imports nothing (`_NON_LOADING`).
_DESERIALIZERS = frozenset({"pickle", "_pickle", "cPickle", "shelve", "dill", "cloudpickle", "joblib"})
_MACHINERY = frozenset(
    {"importlib", "importlib_metadata", "pkgutil", "runpy", "zipimport", "imp", *_INTERPRETERS, *_DESERIALIZERS}
)
_NON_LOADING = frozenset(
    {
        "importlib.invalidate_caches", "importlib.resources.as_file", "importlib.util.resolve_name",
        "importlib.util.decode_source", "importlib.util.source_hash", "importlib.util.cache_from_source",
        "importlib.util.source_from_cache", "importlib.util.spec_from_loader",
        "importlib.util.spec_from_file_location", "importlib.util.module_from_spec",
        "pkgutil.iter_modules", "pkgutil.extend_path", "pkgutil.get_importer", "zipimport.zipimporter",
        *(f"{lib}.{name}" for lib in ("pickle", "_pickle", "cPickle", "dill", "cloudpickle")
          for name in ("dump", "dumps", "Pickler", "PickleError", "PicklingError", "UnpicklingError",
                       "PickleBuffer", "HIGHEST_PROTOCOL", "DEFAULT_PROTOCOL")),
        "joblib.dump", "joblib.Parallel", "joblib.delayed", "joblib.cpu_count",
    }
)  # fmt: skip
# Whole namespaces that only read metadata or define types: entry points load on ``.load()``.
_NON_LOADING_WITHIN = ("importlib.metadata.", "importlib_metadata.", "importlib.machinery.", "importlib.abc.")


@functools.lru_cache(maxsize=4)  # each reader walks one module's tree many times, then moves on
def _nodes(tree: ast.AST) -> tuple[ast.AST, ...]:
    """Every node of ``tree``, as ``ast.walk`` yields them, walked once per tree."""
    return tuple(ast.walk(tree))


def _bound_names(tree: ast.AST) -> Iterator[str]:
    for node in _nodes(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            yield node.id
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, ast.alias):
            yield (node.asname or node.name).split(".")[0]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.name
        elif isinstance(node, ast.Global | ast.Nonlocal):
            yield from node.names  # rebound somewhere else, so its text is not known here


_Displays = Mapping[str, Sequence[ast.Tuple | ast.List | ast.Dict]]


def _attribute_key(node: ast.Attribute) -> str:
    """The binding key for ``<any object>.name``; no plain name starts with a dot, so none collides."""
    return f".{node.attr}"


def _unpacked(
    target: ast.expr, value: ast.expr, containers: _Displays = MappingProxyType({})
) -> Iterator[tuple[str, ast.expr]]:
    """(name, value) for each name ``target = value`` binds: the whole value for a name, and for
    ``(a, b) = ("x", "y")`` each item its position takes, the value a display written out or a name
    bound to one. A starred target takes one item when the lengths match, so the rest still line up,
    and binds a list, never text; a starred value lines nothing up."""
    if isinstance(target, ast.Name):
        yield target.id, value
        return
    if isinstance(target, ast.Attribute):
        # ``self.target = "apps.x.impl"``: keyed by the attribute's name on whatever object holds it.
        yield _attribute_key(target), value
        return
    if not isinstance(target, ast.Tuple | ast.List):
        return
    held = [value] if isinstance(value, ast.Tuple | ast.List) else containers.get(getattr(value, "id", ""), ())
    for display in held:
        if isinstance(display, ast.Dict) or any(isinstance(item, ast.Starred) for item in display.elts):
            continue
        if len(display.elts) == len(target.elts):
            for each, item in zip(target.elts, display.elts, strict=True):
                yield from _unpacked(each, item, containers)


def _called_name(func: ast.expr | None) -> str | None:
    if isinstance(func, ast.NamedExpr):  # ``(load := __import__)("x")`` calls what it binds
        return _called_name(func.value)
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None
