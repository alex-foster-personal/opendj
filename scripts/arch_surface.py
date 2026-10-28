"""Module public-surface rules (ADR-NEW-module-contracts), as a pure function.

An enforced package ``apps.X`` is reached from outside only through ``apps.X.contract``;
the delivery layers, the composition roots outside ``apps/`` (``scripts/``) and any
``__main__`` module (a ``python -m`` entry point builds its own process) may also
import ``apps.X.http``, the module's own router and service builder. The
contract, and the package root that Python executes whenever the contract is imported,
import only the standard library, the allowed external packages and other enforced
modules' contracts (whose roots are scanned too), so a contract can never drag an implementation in behind it.

This file imports nothing outside the standard library, so the main test environment
can exercise it without import-linter. ``scripts.arch_surface_contract`` adapts it to
import-linter's custom contract API, which is what `.importlinter` runs.
"""

from __future__ import annotations

import ast
import functools
import importlib.machinery
import importlib.util
import itertools
import os
import re
import shlex
import shutil
import string
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

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


def source_imports(source: str, module: str, is_package: bool, known_modules: Iterable[str]) -> list[tuple[str, int]]:
    """Every (imported module, line) in ``source``, read from the syntax tree.

    Unlike the import graph `.importlinter` builds (``exclude_type_checking_imports``),
    this keeps imports under ``if TYPE_CHECKING:``: an annotation in a contract that names
    an implementation type is part of the contract's surface even though it never runs.
    ``from X import Y`` reads as ``X.Y`` when that is a known module, else as ``X``.
    """
    tree = ast.parse(source)
    known = frozenset(known_modules)
    found = [*_imports_in(_nodes(tree), module, is_package, known, tree), *_module_table_loads(tree, known)]
    return sorted({*found, *((DYNAMIC, line) for line in _reflective_loads(tree, _loader_names(tree)))})


def _callee(call: ast.Call) -> ast.expr:
    callee = call.func
    while isinstance(callee, ast.NamedExpr):  # ``(f := getattr(...))(name)`` calls its value
        callee = callee.value
    return callee


def _holds_loader_module_loosely(tree: ast.AST) -> bool:
    """Whether the module uses a name bound to a loader module (``import importlib``, ``import
    builtins as b``) or to anything imported from one, other than to read a named attribute off it:
    ``getattr(importlib, name)`` hands the module itself somewhere a reader cannot follow, and
    ``b.__dict__[name]`` hands out its whole namespace."""
    bound: set[str] = set()
    for node in _nodes(tree):
        if isinstance(node, ast.Import):
            bound.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if alias.name.split(".")[0] in _LOADER_MODULES
            )
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _LOADER_MODULES:
            bound.update(alias.asname or alias.name for alias in node.names)
    read = {id(node.value) for node in _nodes(tree) if isinstance(node, ast.Attribute)}
    called = {id(node.func) for node in _nodes(tree) if isinstance(node, ast.Call)}
    return (
        any(
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in bound
            and id(node) not in read | called
            for node in _nodes(tree)
        )
        or any(_root_name(node) in bound for node in _nodes(tree) if _is_namespace_read(node))
        or (bool(bound) and any(_reads_own_namespace(node) for node in _nodes(tree)))
        or _loads_loader_module(tree)
    )


def _reads_own_namespace(node: ast.AST) -> bool:
    """A read of a whole scope's namespace, which holds every name the module bound by import:
    ``globals()``, ``locals()``, a bare ``vars()``, or a frame's or function's ``f_globals``,
    ``f_locals`` or ``__globals__``. ``globals()["import" + "_module"](target)`` calls an imported
    loader that no name in the source reads."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id in {"globals", "locals"} or (node.func.id == "vars" and not node.args)
    return isinstance(node, ast.Attribute) and node.attr in {"f_globals", "f_locals", "__globals__"}


def _loads_loader_module(tree: ast.AST) -> bool:
    """Whether the module may take a loader module from somewhere other than an import statement:
    it names one as text (``m = __import__("importlib")``, ``import_module("runpy")``) or reaches the
    module table (``sys.modules``). Which name ends up holding it is more than a reader can follow,
    so such a module holds it loosely."""
    texts = (
        node.value.split(".")[0] in _LOADER_MODULES
        for node in _nodes(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    return any(texts) or _reaches_module_table(tree)


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


# The module table's reads that take one module by its key: ``sys.modules["apps.x.impl"]``,
# ``sys.modules.get(name)``; a membership test (``"x" in sys.modules``) takes none.
_TABLE_LOOKUPS = frozenset({"get", "pop", "setdefault"})


def _module_table_loads(tree: ast.Module, known: frozenset[str]) -> list[tuple[str, int]]:
    """What reads of the module table (``sys.modules``, through a name an import of ``sys``
    binds once, or ``from sys import modules``) take: a loaded module is used as surely as an
    imported one (Codex, PR #6182). A key that folds to text takes that module; any other key,
    and any other read (the table handed on, iterated, its ``.values()``), is `DYNAMIC`. A
    write or a delete takes nothing, and nor does ``sys.modules[__name__]``, the module itself.
    The table of a ``sys`` a loader returns (``__import__("sys").modules``) is read the same way,
    and ``sys`` handed on whole (``getattr(sys, "modules")``, ``s = sys``) is `DYNAMIC`."""
    nodes = list(_nodes(tree))
    bound = Counter(_bound_names(tree))
    imports = [a for n in nodes if isinstance(n, ast.Import) for a in n.names if a.name == "sys"]
    names = {a.asname or a.name for a in imports}
    tables = {
        a.asname or a.name
        for n in nodes
        if isinstance(n, ast.ImportFrom) and n.module == "sys" and n.level == 0
        for a in n.names
        if a.name == "modules"
    }
    parents = {id(child): parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    constants = _constants(tree)
    found: list[tuple[str, int]] = []

    def is_sys(node: ast.expr) -> bool:
        """``node`` is the ``sys`` module: a name an import of ``sys`` binds once, or a call
        handed the text ``sys`` (``__import__("sys")``, ``importlib.import_module("sys")``)."""
        if isinstance(node, ast.Name):
            return node.id in names and bound[node.id] == 1
        return isinstance(node, ast.Call) and any(
            _literal(arg, constants) == "sys" for arg in (*node.args, *(k.value for k in node.keywords))
        )

    for node in nodes:
        if not isinstance(node, ast.Attribute | ast.Name | ast.Call):
            continue
        parent = parents.get(id(node))
        if isinstance(node, ast.Call | ast.Name) and is_sys(node):
            # ``sys`` handed on (``getattr(sys, "modules")``, ``s = sys``, a fromlist import)
            # reaches the table by a route no reader follows; one named member does not.
            member = isinstance(parent, ast.Attribute) and parent.value is node and not parent.attr.startswith("__")
            if not member:
                found.append((DYNAMIC, node.lineno))
            continue
        if isinstance(node, ast.Call):
            continue
        table = (isinstance(node, ast.Attribute) and node.attr == "modules" and is_sys(node.value)) or (
            isinstance(node, ast.Name) and node.id in tables and bound[node.id] == 1
        )
        if table and isinstance(node.ctx, ast.Load):
            taken = _table_read(parent, parents.get(id(parent)), constants, known)
            found += [(taken, node.lineno)] if taken else []
    return found


def _table_read(
    parent: ast.AST | None, call: ast.AST | None, constants: Mapping[str, str], known: frozenset[str]
) -> str | None:
    """The module one read of the module table takes, given the node holding the table and the
    node above that: a known module, `DYNAMIC`, or None when the read takes no module."""
    key: ast.expr | None
    if isinstance(parent, ast.Subscript):
        if not isinstance(parent.ctx, ast.Load):
            return None  # ``sys.modules[name] = module`` or ``del sys.modules[name]``
        key = parent.slice
    elif isinstance(parent, ast.Compare) and all(isinstance(op, ast.In | ast.NotIn) for op in parent.ops):
        return None
    elif (
        isinstance(parent, ast.Attribute)
        and parent.attr in _TABLE_LOOKUPS
        and isinstance(call, ast.Call)
        and call.func is parent
    ):
        key = call.args[0] if call.args else None
    else:
        return DYNAMIC
    if isinstance(key, ast.Name) and key.id == "__name__":
        return None
    text = _literal(key, constants)
    if text is None:
        return DYNAMIC
    return _importable(text, known) if _importable(text, known) in known else None


def _is_namespace_read(node: ast.AST) -> bool:
    """A dunder read that hands out a namespace rather than one named member: ``b.__dict__``,
    ``importlib.util.__getattribute__``. A loader's own name (``b.__import__``) is a load already."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr.startswith("__")
        and node.attr.endswith("__")
        and node.attr not in _LOADERS
    )


def _root_name(node: ast.AST) -> str:
    """The name an attribute chain starts from: ``importlib`` for ``importlib.util.__dict__``."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def _reflective_loads(tree: ast.AST, loaders: frozenset[str], *, computed: bool = True) -> Iterator[int]:
    """Lines that may load a module through a path no reader can follow: the builtins
    namespace (``vars(__builtins__)["__import__"]``), code run from text, a loader named
    as a string (``getattr(mod, "import_module")``), a loader passed or stored rather than
    called (``load_with(__import__)``), or a call of any callee but a name or an attribute
    (``getattr(...)(name)``, ``table["load"](name)``, ``(lambda: f)()(name)``). That last kind
    counts only where ``computed`` says a loader can reach such a callee at all."""
    called = {id(_callee(node)) for node in _nodes(tree) if isinstance(node, ast.Call)}
    tested, resolvers, metadata = _tested_only(tree), _name_resolvers(tree), _holds_metadata(tree)
    homed = any(_pydoc_bindings(tree)) or _reaches_module_table(tree)
    evaluators, reaches_builtins, modules = _evaluator_names(tree), _reaches_builtins(tree), _other_modules(tree)
    for node in _nodes(tree):
        if isinstance(node, ast.Name):
            named = (_is_loader(node.id, loaders) and id(node) not in resolvers) or node.id in _EVALUATORS
            handed = named and isinstance(node.ctx, ast.Load) and id(node) not in called
            hit = node.id == "__builtins__" or handed
        elif isinstance(node, ast.Attribute):
            named = (_is_loader(node.attr, loaders) and id(node) not in resolvers) or node.attr in _EVALUATORS
            # ``load = ep.load``: an entry point's loader handed on where entry points can be held
            off_module = isinstance(node.value, ast.Name) and node.value.id in modules
            entry = metadata and node.attr == "load" and not off_module
            hit = (named or entry) and id(node) not in called
        elif isinstance(node, ast.Constant):
            # ``getattr(builtins, "exec")``: an evaluator named as text where builtins can be held
            evaluator = reaches_builtins and node.value in _EVALUATORS
            loader = node.value in _LOADERS or (homed and node.value in _PYDOC_LOADERS) or evaluator
            hit = loader and id(node) not in tested
        elif isinstance(node, ast.Call):
            unnamed = computed and not isinstance(_callee(node), ast.Name | ast.Attribute)
            entry = metadata and _loads_entry_point(node, modules)  # ``ep.load()`` loads the text ep carries
            hit = unnamed or entry or _runs_text(_callee(node), evaluators, reaches_builtins, modules)
        else:
            continue
        if hit:
            yield node.lineno
    yield from _machinery_handed(tree)


def _runs_text(callee: ast.expr, evaluators: frozenset[str], reaches_builtins: bool, modules: frozenset[str]) -> bool:
    """Whether ``callee`` is a builtin that runs text as code: by its own name or an alias
    (``exec``, ``from builtins import exec as run``), or an attribute so named in a module that can
    hold the builtins module at all (`_reaches_builtins`), unless it is read straight off a name
    only an import of another module binds (``re.compile``, ``modules``): that name holds that
    module, never builtins, so the gate does not flag every regex."""
    if isinstance(callee, ast.Name):
        return callee.id in _EVALUATORS or callee.id in evaluators
    if not (isinstance(callee, ast.Attribute) and callee.attr in _EVALUATORS and reaches_builtins):
        return False
    return not (isinstance(callee.value, ast.Name) and callee.value.id in modules)


def _other_modules(tree: ast.AST) -> frozenset[str]:
    """Names bound once, by an import of a module other than builtins (``import re``,
    ``from x import y``): each holds that module or its member, never the builtins module."""
    bound = Counter(_bound_names(tree))
    names = {
        (alias.asname or alias.name).split(".")[0]
        for node in _nodes(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        and not (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in _BUILTINS)
        for alias in node.names
        if alias.name.split(".")[0] not in _BUILTINS
    }
    return frozenset(name for name in names if bound[name] == 1)


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


def _pinned_texts(tree: ast.AST, constants: Mapping[str, str]) -> dict[str, frozenset[str]]:
    """{name: texts} for each name, in any scope, whose every binding is ``name = <text>``
    (``def f(): target = "json"``): what it holds is one of those texts and nothing else. A name
    bound any other way as well (a parameter, a loop target, ``+=``, an import) is left out."""
    sites: dict[str, list[str]] = {}
    for node in _nodes(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            text = _literal(node.value, constants)
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and isinstance(text, str):
                sites.setdefault(targets[0].id, []).append(text)
    bound = Counter(_bound_names(tree))
    return {name: frozenset(texts) for name, texts in sites.items() if len(texts) == bound[name]}


def _importable(name: str, known: frozenset[str]) -> str:
    """The module a dotted object path loads: ``pydoc.locate`` and ``pkgutil.resolve_name`` import
    the longest prefix of ``apps.x.impl.run`` that is a module, then read the rest as attributes."""
    parts = name.split(".")
    prefixes = (".".join(parts[:end]) for end in range(len(parts), 0, -1))
    return next((prefix for prefix in prefixes if prefix in known), name)


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


def _machinery_calls(tree: ast.AST) -> dict[int, str]:
    """Callees taken from the import machinery (`_MACHINERY`) that may import by a name they are
    handed: an attribute read off a name an import of it binds (``importlib.util.find_spec``,
    ``resources.files``), or a name ``from importlib.resources import files`` binds, each by its
    qualified name, unless `_NON_LOADING` says it never imports. Nearly all of the machinery
    imports, so it is read whole rather than listed a function at a time."""
    return {id(node): qualified for node, called, qualified in _machinery_refs(tree) if called}


def _machinery_handed(tree: ast.AST) -> Iterator[int]:
    """Lines that hand a loading part of the import machinery on rather than call it
    (``load = files``, ``run(importlib.resources.files)``, ``lib = importlib``): what it is later
    called with no reader can follow, so each reads as `DYNAMIC`."""
    return (node.lineno for node, called, _ in _machinery_refs(tree) if not called)


def _machinery_refs(tree: ast.AST) -> Iterator[tuple[ast.expr, bool, str]]:
    """Each whole read (``importlib.util.find_spec``, never its ``importlib.util`` inside) of a
    loading part of the import machinery, through a name only imports of it bind, and whether
    it is called right there, with its qualified name."""
    bound = Counter(_bound_names(tree))
    imported: list[tuple[str, str]] = []
    for node in _nodes(tree):
        if isinstance(node, ast.Import):
            imported += [
                (a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0])
                for a in node.names
                if a.name.split(".")[0] in _MACHINERY
            ]
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] in _MACHINERY
        ):
            imported += [(a.asname or a.name, f"{node.module}.{a.name}") for a in node.names]
    # A name every binding of which imports the same part (``import joblib`` in two functions)
    # holds that part wherever it is read; one bound to anything else as well may not.
    held = {name: qualified for name, qualified in imported}
    same = Counter(pair for pair in imported if held[pair[0]] == pair[1])
    held = {name: qualified for name, qualified in held.items() if same[(name, qualified)] == bound[name]}
    nodes = list(_nodes(tree))
    inner = {id(node.value) for node in nodes if isinstance(node, ast.Attribute)}
    called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    for node in nodes:
        if not isinstance(node, ast.Name | ast.Attribute) or id(node) in inner:
            continue
        parts: list[str] = []
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            parts.insert(0, root.attr)
            root = root.value
        if not (isinstance(root, ast.Name) and root.id in held):
            continue
        qualified = ".".join([held[root.id], *parts])
        if qualified not in _NON_LOADING and not qualified.startswith(_NON_LOADING_WITHIN):
            yield node, id(node) in called, qualified


def _machinery_loads(
    call: ast.Call, constants: Mapping[str, str], known: frozenset[str], qualified: str = ""
) -> list[str]:
    """What a `_machinery_calls` call loads: each module of the repository a text it is handed names
    (``"apps.x.impl:run"`` names ``apps.x.impl``), and `DYNAMIC` for any argument no reader can fold
    to text. A literal that is no text (a number, ``None``) names nothing. A call into the
    interactive interpreter (`_INTERPRETERS`) or a pickle reader (`_DESERIALIZERS`) is `DYNAMIC`
    whatever it is handed."""
    if qualified.partition(".")[0] in _INTERPRETERS | _DESERIALIZERS:
        return [DYNAMIC]
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return [DYNAMIC]
    found: list[str] = []
    for arg in [*call.args, *(k.value for k in call.keywords)]:
        if isinstance(arg, ast.Constant) and not isinstance(arg.value, str):
            continue
        text = _literal(arg, constants)
        if text is None:
            return [DYNAMIC]
        name = _importable(text.partition(":")[0], known)
        found.extend([name] if name in known else [])
    return found


def _pinned_loads(
    call: ast.Call, constants: Mapping[str, str], pinned: Mapping[str, frozenset[str]], known: frozenset[str]
) -> list[str]:
    """What a loader call loads, its target read through a pinned name (`_pinned_texts`) when no
    literal or constant names it: each known module the name may hold, and `DYNAMIC` for any text
    that names no module. A pinned name is read module-wide, so it may carry texts another scope
    binds; only a module that exists is reported, as the bound-text scan reports."""
    loaded = _loaded(call, constants)
    target = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg in _TARGETS), None)
    if loaded != DYNAMIC or not isinstance(target, ast.Name) or target.id not in pinned:
        return [loaded]
    resolved = {_loaded(call, {**constants, target.id: text}) for text in pinned[target.id]}
    return sorted(name for name in resolved if name == DYNAMIC or name in known)


def _constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound once, to text: ``TARGET = "apps.x.impl"`` then
    ``import_module(TARGET)`` loads what the literal would. A name bound anywhere else as
    well (reassigned, a parameter, a loop target) may hold anything, so it is left out."""
    texts: dict[str, str] = {}
    bound = Counter(_bound_names(tree))
    for stmt in tree.body:
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        text = _literal(value, texts)  # ``TARGET = "apps.x." + "impl"`` folds as the call's own text would
        if (
            len(targets) == 1
            and isinstance(targets[0], ast.Name)
            and isinstance(text, str)
            and bound[targets[0].id] == 1
        ):
            texts[targets[0].id] = text
    return texts


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


def _imports_in(
    nodes: Iterable[ast.AST],
    module: str,
    is_package: bool,
    known: frozenset[str],
    tree: ast.Module,
) -> list[tuple[str, int]]:
    """What ``nodes`` import, reading names through what ``tree`` binds them to. Every scan takes
    the whole module, so none can read a loader, a constant or a local text another misses."""
    loaders, constants, resolvers = _loader_names(tree), _constants(tree), _name_resolvers(tree)
    machinery = _machinery_calls(tree)
    pinned = _pinned_texts(tree, constants)
    containers = _bound_containers(tree)
    bound_texts = _bound_texts(tree, constants, containers)
    found: set[tuple[str, int]] = set()
    for node in (_spread(each, containers) if isinstance(each, ast.Call) else each for each in nodes):
        if isinstance(node, ast.Import):
            found.update((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(module, is_package, node.level, node.module)
            for alias in node.names:
                sub = f"{base}.{alias.name}"
                found.add((sub if sub in known else base, node.lineno))
        elif isinstance(node, ast.Call) and _called_name(node.func) in _EVALUATORS:
            # ``exec("import apps.x.impl")`` imports as surely as the statement it holds.
            code = _literal(node.args[0] if node.args else None, constants)
            found.update((imported, node.lineno) for imported, _ in _imports_in_code(code, module, is_package, known))
        elif isinstance(node, ast.Call) and (
            (loader := _is_loader(_called_name(node.func), loaders) and id(node.func) not in resolvers)
            or id(node.func) in machinery
        ):
            # A named loader reads its pins; any other call into the import machinery reads as one.
            targets = (
                (_importable(t, known) for t in _pinned_loads(node, constants, pinned, known))
                if loader
                else _machinery_loads(node, constants, known, machinery[id(node.func)])
            )
            found.update((target, node.lineno) for target in targets)
        elif isinstance(node, ast.Call):
            # ``getattr(importlib, "import_module")("apps.x.impl")`` or ``(load,) = (import_module,)``
            # then ``load("apps.x.impl")``: a loader reaches a call by more routes than any reader
            # can follow, so any call handed a module's own name is read as loading it.
            target = _loaded(node, constants)
            if target in known:
                found.add((target, node.lineno))
        if isinstance(node, ast.Call):
            # ``__import__("apps", fromlist=["x.impl"])`` imports each entry below the package it loads,
            # the package read through a name bound in any scope too (``base = "apps"``). Any callee
            # is read so, since a loader reaches a call by routes no reader follows (`_loader_names`).
            loaded = _loaded(node, constants)
            first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in _TARGETS), None)
            bases = (
                [loaded] if loaded != DYNAMIC or first is None else _texts(first, constants, bound_texts, containers)
            )
            entries = list(_fromlist(node, constants, bound_texts, containers))
            below = (f"{base}.{entry}" for base in bases for entry in entries)
            found.update((name, node.lineno) for name in below if name in known)
            # ``spec_from_file_location("alias", "apps/x/impl.py")`` names its module second, and
            # ``exec_module`` later runs it: a module named in any argument is read as loaded.
            found.update(
                (name, node.lineno)
                for name in _named_modules(node, constants, bound_texts, containers)
                if name in known or name == DYNAMIC
            )
            # ``run = exec`` then ``run("import apps.x.impl")``: code handed to any callee is read
            # for the modules it imports, as a name handed to one is.
            # ``run([sys.executable, "-c", "import apps.x.impl"])`` holds its code inside a display.
            handed = [*node.args, *(keyword.value for keyword in node.keywords)]  # compile(source=...)
            held = (each for arg in handed for each in _held_items(arg, containers))
            texts = (text for arg in held for text in _texts(arg, constants, bound_texts, containers))
            for code in (source for text in texts for source in _sources_in(text)):
                if "import" in code:
                    ran = _imports_in_code(code, module, is_package, known)
                    found.update((imported, node.lineno) for imported, _ in ran if imported in known)
    return sorted(found)


_Displays = Mapping[str, Sequence[ast.Tuple | ast.List | ast.Dict]]


def _texts(
    node: ast.expr,
    constants: Mapping[str, str],
    bound_texts: Mapping[str, Collection[str]],
    containers: _Displays = MappingProxyType({}),
    seen: frozenset[int] = frozenset(),
) -> Iterator[str]:
    """Every text ``node`` may hold: its folded value, or for a name a reader cannot pin to one
    value, each text it is bound to anywhere (``def f(): T = "apps.x.impl"``), and for an item of a
    name bound to a display, each text the item it picks holds (``TARGETS[0]``)."""
    text = _literal(node, constants)
    if text is not None:
        yield text
    elif isinstance(node, ast.Name):
        yield from sorted(bound_texts.get(node.id, ()))
    elif isinstance(node, ast.Attribute):
        # ``self.target`` holds each text an attribute of that name is set to on any object, and
        # ``C.kind`` each text a name ``kind`` is bound to (a class body binds plain names).
        yield from sorted({*bound_texts.get(_attribute_key(node), ()), *bound_texts.get(node.attr, ())})
    elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        for display in containers.get(node.value.id, ()):
            picked = None if id(display) in seen else _picked(display, node.slice, constants)
            if picked is not None:  # ``A = (A[0],)`` picks itself, so each display is read once
                yield from _texts(picked, constants, bound_texts, containers, seen | {id(display)})
    elif isinstance(node, ast.IfExp | ast.BoolOp | ast.NamedExpr):
        # ``"apps.x.impl" if on else "apps.x.contract"``, ``name or "apps.x.impl"`` and
        # ``(t := "apps.x.impl")`` evaluate to one of their parts, so each part's texts are held.
        branches = [node.body, node.orelse] if isinstance(node, ast.IfExp) else []
        operands = node.values if isinstance(node, ast.BoolOp) else []
        walrus = [node.value] if isinstance(node, ast.NamedExpr) else []
        for part in (*branches, *operands, *walrus):
            yield from _texts(part, constants, bound_texts, containers, seen)
    else:
        # ``leaf = "impl"`` then ``f"apps.x.{leaf}"``: a composed text holds each text its parts can.
        for composed in _composed(node, containers):
            yield from _combined([_texts(each, constants, bound_texts, containers, seen) for each in composed])
        yield from _filled_in(node, constants, bound_texts, containers, seen)
    if text is None:
        # ``"apps.x.contract".replace("contract", "impl")`` and ``"_apps.x.impl"[1:]``: a text
        # method or a slice computes a text from texts, so it is run on each text it is handed.
        for run, receiver, operands in _transformed(node):
            held_values: list[list[object]] = [list(_texts(receiver, constants, bound_texts, containers, seen))]
            held_values += [_values(each, constants, bound_texts, containers, seen) for each in operands]
            yield from _ran(run, held_values)


def _filled_in(
    node: ast.expr,
    constants: Mapping[str, str],
    bound_texts: Mapping[str, Collection[str]],
    containers: _Displays,
    seen: frozenset[int],
) -> Iterator[str]:
    """``"apps.x.%s" % leaf`` and ``"apps.x.{}".format(leaf)`` fill a template with its values."""
    for template, values, names in _templates(node):
        given: list[tuple[int | str, ast.expr]] = [*enumerate(values), *names.items()]
        held = {key: list(_texts(value, constants, bound_texts, containers, seen)) for key, value in given}
        for text in _texts(template, constants, bound_texts, containers, seen):
            filled = _filled(text, held, percent=isinstance(node, ast.BinOp))
            yield from () if filled is None else _combined([[""], *filled])


# The methods of ``str`` that make a text from a text and nothing else: each is run, never imitated.
_TEXT_METHODS = frozenset(
    {
        *("capitalize", "casefold", "center", "expandtabs", "ljust", "lower", "lstrip", "removeprefix"),
        *("removesuffix", "replace", "rjust", "rstrip", "strip", "swapcase", "title", "upper", "zfill"),
    }
)
_Run = Callable[..., object]


def _transformed(node: ast.expr) -> list[tuple[_Run, ast.expr, list[ast.expr]]]:
    """(run, receiver, operands) for a text method called on a text with its operands written out
    (``t.replace("a", "b")``), or a text sliced or indexed (``t[1:]``); none for anything else."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _TEXT_METHODS:
        if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
            return []  # a spread lines up with nothing a reader can see
        method, count, names = getattr(str, node.func.attr), len(node.args), [str(k.arg) for k in node.keywords]

        def call(text: str, *values: object) -> object:
            return method(text, *values[:count], **dict(zip(names, values[count:], strict=True)))

        return [(call, node.func.value, [*node.args, *(k.value for k in node.keywords)])]
    if isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Slice):
            bounds = [node.slice.lower, node.slice.upper, node.slice.step]
            given = [b for b in bounds if b is not None]

            def cut(text: str, *values: Any) -> object:
                it = iter(values)
                return text[slice(*(None if b is None else next(it) for b in bounds))]

            return [(cut, node.value, given)]
        return [(lambda text, at: text[at], node.value, [node.slice])]
    return []


def _values(
    node: ast.expr,
    constants: Mapping[str, str],
    bound_texts: Mapping[str, Collection[str]],
    containers: _Displays,
    seen: frozenset[int],
) -> list[object]:
    """Each value an operand of a text method may hold: a written-out number, or its texts."""
    negative = isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
    number = node.operand if isinstance(node, ast.UnaryOp) and negative else node
    if isinstance(number, ast.Constant) and type(number.value) is int:
        return [-number.value if negative else number.value]
    return list(_texts(node, constants, bound_texts, containers, seen))


def _ran(run: _Run, held: Sequence[Sequence[object]]) -> Iterator[str]:
    """Each text ``run`` returns for one value of every operand; nothing past ``_COMBINATIONS``."""
    count = 1
    for option in held:
        count *= len(option)
    if not 0 < count <= _COMBINATIONS:
        return
    for operands in itertools.product(*held):
        try:
            result = run(*operands)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue  # what raises at run time loads nothing
        if isinstance(result, str):
            yield result


def _templates(node: ast.expr) -> list[tuple[ast.expr, Sequence[ast.expr], dict[str, ast.expr]]]:
    """(template, positional values, named values) for ``template % values`` or
    ``template.format(*values, **named)`` written out; none for a spread, which nobody can line up."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        return [] if any(isinstance(v, ast.Starred) for v in values) else [(node.left, values, {})]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        if any(isinstance(v, ast.Starred) for v in node.args) or any(k.arg is None for k in node.keywords):
            return []
        return [(node.func.value, node.args, {k.arg: k.value for k in node.keywords if k.arg is not None})]
    return []


def _filled(template: str, held: Mapping[int | str, list[str]], *, percent: bool) -> list[list[str]] | None:
    """The options for each piece of ``template`` once filled from ``held``: a literal piece has one,
    a field the texts its value holds. None when a field formats its value (``%d``, ``{!r}``,
    ``{:>8}``), reaches into it (``{0.x}``) or names nothing held: no reader knows that text."""
    pieces: list[list[str]] = []
    if percent:
        auto = iter(range(len(held)))
        for literal, field in (
            (chunk, None) if i % 2 == 0 else ("", chunk) for i, chunk in enumerate(re.split(r"(%.?)", template))
        ):
            if field == "%%":
                pieces.append(["%"])
            elif field is not None:
                index = next(auto, None)
                if field != "%s" or index is None:
                    return None
                pieces.append(held[index])
            else:
                pieces.append([literal])
        return pieces if next(auto, None) is None else None  # a value left over raises at run time
    automatic = 0
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:  # an unclosed brace raises at run time too
        return None
    for literal, field, spec, conversion in parsed:
        pieces.append([literal])
        if field is None:
            continue
        if spec or conversion:
            return None
        key: int | str = int(field) if field.isdigit() else field
        if field == "":
            key, automatic = automatic, automatic + 1
        if key not in held:
            return None
        pieces.append(held[key])
    return pieces


def _composed(node: ast.expr, containers: _Displays) -> list[list[ast.expr]]:
    """Each [separator, *parts] a concatenation, an f-string or a ``sep.join(parts)`` puts together,
    the parts of a join read from a display or from each display a name is bound to; none for any
    other expression."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [[ast.Constant(""), node.left, node.right]]
    if isinstance(node, ast.JoinedStr):
        # A conversion (``!r``) or a format spec changes the text, so only a bare ``{B}`` composes.
        if any(isinstance(v, ast.FormattedValue) and (v.conversion != -1 or v.format_spec) for v in node.values):
            return []
        return [[ast.Constant(""), *(v.value if isinstance(v, ast.FormattedValue) else v for v in node.values)]]
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
    ):
        return []
    held = node.args[0]
    displays = [held] if isinstance(held, ast.Tuple | ast.List) else containers.get(getattr(held, "id", ""), ())
    return [
        [node.func.value, *display.elts]
        for display in displays
        if not isinstance(display, ast.Dict)  # a dict joins its keys; a spread (``*rest``) holds no text
    ]


_PASSES = 32  # a name bound through a chain longer than this reads the texts the chain reached
_COMBINATIONS = 256  # past this many, a composed text is left unread rather than enumerated


def _combined(held: Sequence[Iterable[str]]) -> Iterator[str]:
    """Each text one separator joining one text of every part makes (``held`` is the separator's
    texts, then each part's); nothing when any of them holds none."""
    options = [sorted(set(texts)) for texts in held]
    count = 1
    for option in options:
        count *= len(option)
    if 0 < count <= _COMBINATIONS:
        yield from (sep.join(parts) for sep, *parts in itertools.product(*options))


def _fromlist(
    call: ast.Call, constants: Mapping[str, str], bound_texts: Mapping[str, Collection[str]], containers: _Displays
) -> Iterator[str]:
    """Each name ``__import__``'s ``fromlist`` (its fourth argument) holds, written out or bound to a name."""
    value = call.args[3] if len(call.args) > 3 else next((k.value for k in call.keywords if k.arg == "fromlist"), None)
    if isinstance(value, ast.Tuple | ast.List):
        displays: Sequence[ast.Tuple | ast.List | ast.Dict] = [value]
    else:
        displays = containers.get(value.id, ()) if isinstance(value, ast.Name) else ()
    for display in displays:
        for item in () if isinstance(display, ast.Dict) else display.elts:
            yield from _texts(item, constants, bound_texts, containers)


def _bound_containers(tree: ast.AST) -> dict[str, list[ast.Tuple | ast.List | ast.Dict]]:
    """{name: literals} for each name bound to a tuple, list or dict display in any scope, directly
    or through another name (``ALIASED = TARGETS``) or a part an expression may evaluate to
    (``T = A if on else B``), to a fixed point: displays only come from the tree, so it ends. A
    display grows a name's list too: ``cmd += [...]``, ``cmd.extend([...])``, and an item
    ``cmd.append(x)`` or ``cmd.insert(i, x)`` adds (Codex, PR #6182)."""
    bindings: list[tuple[str, ast.expr]] = []
    for node in _nodes(tree):
        grown = _grown(node)
        if grown:
            bindings.append(grown)
            continue
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        bindings += [pair for target in targets for pair in _unpacked(target, value)]
    found: dict[str, list[ast.Tuple | ast.List | ast.Dict]] = {}
    changed = True
    while changed:
        changed = False
        for name, value in bindings:
            held = found.setdefault(name, [])
            new = [d for d in _displays_of(value, found) if all(d is not h for h in held)]
            held += new
            changed = changed or bool(new)
    return {name: held for name, held in found.items() if held}


def _grown(node: ast.AST) -> tuple[str, ast.expr] | None:
    """The name a statement grows a list of and what it adds: ``cmd += ["-m", t]`` and
    ``cmd.extend(["-m", t])`` add a display, ``cmd.append(t)`` and ``cmd.insert(i, t)`` one item,
    read as a one-item tuple placed where the call is. An insert keeps its index under
    `_INSERT_AT` (None when no reader folds it) for `_flat_sequence` to place it by."""
    if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and isinstance(node.target, ast.Name):
        return node.target.id, node.value
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args):
        return None
    owner, method = node.func.value, node.func.attr
    if not isinstance(owner, ast.Name) or method not in {"extend", "append", "insert"}:
        return None
    if method == "extend":
        return owner.id, node.args[0]
    added = ast.copy_location(ast.Tuple(elts=[node.args[-1]], ctx=ast.Load()), node)
    if method == "insert":
        index = node.args[0] if len(node.args) == 2 else None
        try:
            at = ast.literal_eval(index) if index is not None else None
        except ValueError:
            at = None
        setattr(added, _INSERT_AT, at if isinstance(at, int) else None)
    return owner.id, added


# Where `_grown` records an insert's index on the one-item tuple it reads the insert as.
_INSERT_AT = "_arch_surface_insert_at"


def _displays_of(value: ast.expr, found: _Displays) -> Iterator[ast.Tuple | ast.List | ast.Dict]:
    """Each display ``value`` may evaluate to: itself, a name's, a conditional's or walrus's
    parts', or either side of a ``+`` (``[sys.executable] + ["-m", t]``)."""
    if isinstance(value, ast.Tuple | ast.List | ast.Dict):
        yield value
    elif isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        yield from (*_displays_of(value.left, found), *_displays_of(value.right, found))
    elif isinstance(value, ast.Name):
        yield from list(found.get(value.id, ()))
    elif isinstance(value, ast.IfExp):
        yield from (*_displays_of(value.body, found), *_displays_of(value.orelse, found))
    elif isinstance(value, ast.BoolOp):
        yield from (d for each in value.values for d in _displays_of(each, found))
    elif isinstance(value, ast.NamedExpr):
        yield from _displays_of(value.value, found)


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


def _spread(call: ast.Call, containers: _Displays) -> ast.Call:
    """``f(*("x",), **{"name": "x"})`` hands ``f`` what ``f("x", name="x")`` does, and so does
    ``f(*ARGS)`` for a name bound to such a display; read the call as the plain one."""

    def held(node: ast.expr) -> list[ast.Tuple | ast.List | ast.Dict]:
        if isinstance(node, (ast.Tuple, ast.List, ast.Dict)):
            return [node]
        return list(containers.get(node.id, ())) if isinstance(node, ast.Name) else []

    args: list[ast.expr] = []
    for arg in call.args:
        if not isinstance(arg, ast.Starred):
            args.append(arg)
            continue
        args += [item for seq in held(arg.value) if not isinstance(seq, ast.Dict) for item in seq.elts]
    keywords = [keyword for keyword in call.keywords if keyword.arg is not None]
    for keyword in (keyword for keyword in call.keywords if keyword.arg is None):
        keywords += [
            ast.keyword(arg=key.value, value=value)
            for table in held(keyword.value)
            if isinstance(table, ast.Dict)
            for key, value in zip(table.keys, table.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
    return ast.Call(func=call.func, args=args, keywords=keywords, lineno=call.lineno, col_offset=call.col_offset)


def _bound_texts(tree: ast.AST, constants: Mapping[str, str], containers: _Displays) -> dict[str, frozenset[str]]:
    """{name: texts} for each name bound to foldable text in any scope of the module. A name bound
    to another name takes that name's texts (``A = "apps.x.impl"; B = A``), a loop target each item
    of the display it walks, a destructured name the item its position takes, a parameter its default
    and each argument a call writes out for it, to a fixed point,
    or for `_PASSES` passes where a composed text keeps growing (``A = A + "x"`` in a loop)."""
    bindings: list[tuple[str, ast.expr]] = []
    for node in _nodes(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            # ``(target,) = ("apps.x.impl",)`` binds each name its position takes.
            bindings += [pair for target in targets for pair in _unpacked(target, node.value, containers)]
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            # ``for part in ("lib", "impl")`` binds the target to each item of the display it walks.
            held = node.iter
            displays = [held] if isinstance(held, ast.Tuple | ast.List) else containers.get(getattr(held, "id", ""), ())
            items = (item for each in displays if not isinstance(each, ast.Dict) for item in each.elts)
            bindings += [
                pair
                for item in items
                if not isinstance(item, ast.Starred)
                for pair in _unpacked(node.target, item, containers)
            ]
        elif isinstance(node, ast.arguments):
            # ``def load(target="apps.x.impl")`` (or a lambda) binds the parameter its default
            # fills: positional defaults belong to the last parameters, keyword-only ones pair up.
            positional = [*node.posonlyargs, *node.args]
            paired = zip(positional[len(positional) - len(node.defaults) :], node.defaults, strict=True)
            bindings += [(arg.arg, default) for arg, default in paired]
            bindings += [(arg.arg, d) for arg, d in zip(node.kwonlyargs, node.kw_defaults, strict=True) if d]
    bindings += _call_bindings(tree)
    texts: dict[str, set[str]] = {}
    changed, passes = True, 0
    while changed and passes < _PASSES:  # ``A = A + "x"`` in a loop grows a text every pass
        changed, passes = False, passes + 1
        for name, value in bindings:
            found = set(_texts(value, constants, texts, containers)) - texts.setdefault(name, set())
            texts[name] |= found
            changed = changed or bool(found)
    return {name: frozenset(found) for name, found in texts.items() if found}


def _call_bindings(tree: ast.AST) -> list[tuple[str, ast.expr]]:
    """(parameter, argument) for each call of a function or lambda the module defines by name, the
    arguments written out at the call (``def load(leaf): ...`` then ``load("impl")``). A method is
    called through an attribute (``self.load("impl")``), so an attribute call pairs both with and
    without a leading ``self``; pairing too much only reads more texts, never fewer."""
    signatures: dict[str, list[ast.arguments]] = {}
    for node in _nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            signatures.setdefault(node.name, []).append(node.args)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            for target in (t for t in node.targets if isinstance(t, ast.Name)):
                signatures.setdefault(target.id, []).append(node.value.args)
    bindings: list[tuple[str, ast.expr]] = []
    for call in (n for n in _nodes(tree) if isinstance(n, ast.Call)):
        callee = call.func
        name = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else ""
        shifts = (0, 1) if isinstance(callee, ast.Attribute) else (0,)
        for arguments in signatures.get(name, ()):
            positional = [arg.arg for arg in (*arguments.posonlyargs, *arguments.args)]
            spread = next((i for i, a in enumerate(call.args) if isinstance(a, ast.Starred)), len(call.args))
            for shift in shifts:
                params = positional[shift:]
                bindings += list(zip(params, call.args[:spread], strict=False))
                # After a spread (``load(*head, "impl")``) an argument may land on any later parameter.
                after = [a for a in call.args[spread:] if not isinstance(a, ast.Starred)]
                bindings += [(param, arg) for arg in after for param in params[spread:]]
            named = {arg.arg for arg in (*arguments.args, *arguments.kwonlyargs)}
            bindings += [(k.arg, k.value) for k in call.keywords if k.arg in named]
    return bindings


def _named_modules(
    call: ast.Call,
    constants: Mapping[str, str],
    bound_texts: Mapping[str, frozenset[str]] = MappingProxyType({}),
    containers: _Displays = MappingProxyType({}),
) -> Iterator[str]:
    """Each module a call's literal arguments name, dotted or as a ``.py`` path. Every argument is
    read for a dotted name, since a wrapper (``load(module="apps.x.impl")``) forwards it to a loader
    under any keyword; a path is read only where a loader takes one (``location=``, ``path=``), since
    a record such as ``WriteSurface(guard_site="apps/x/impl.py")`` names a file it never runs. A
    command (an argument, or ``args=``/``cmd=``/``command=``) is read for what ``-m`` runs too:
    ``"-mapps.x.impl"`` joins the option to its module, and a command line
    (``os.system("python -m apps.x.impl")``) holds both as words; a record's prose does not run.
    An interpreter's ``-m`` or ``-c`` with an operand no reader folds is `DYNAMIC`."""
    handed = [(arg, True) for arg in call.args] + [(k.value, k.arg in _TARGETS) for k in call.keywords]
    commands = {id(arg) for arg in call.args} | {id(k.value) for k in call.keywords if k.arg in _COMMANDS}
    # Codex, PR #6182: ``run([sys.executable, "-m", "apps.x.impl"])`` names its module inside a display.
    held = [(each, by_path, id(arg) in commands) for arg, by_path in handed for each in _held_items(arg, containers)]
    for arg, by_path, command in held:
        for text in _texts(arg, constants, bound_texts, containers):
            words = _shell_words(text) if command else []
            ran = [after for flag, after in itertools.pairwise(words) if flag == "-m"]
            ran += [word[2:] for word in words if word.startswith("-m") and len(word) > 2]
            yield from (path for path in map(_module_path, ran) if path)
            if not by_path and ("/" in text or "\\" in text or text.endswith(".py")):
                continue
            name = _module_path(text if text.endswith(".py") else text.partition(":")[0])
            if name and name != DYNAMIC:  # the marker text itself names no module
                yield name
    if _runs_unread_operand(call, constants, containers):
        yield DYNAMIC


def _shell_words(line: str) -> list[str]:
    """The words a shell splits a command line into, its quotes removed (``"-m" "mod"`` reads
    ``-m mod``); a line with an unclosed quote, which the shell refuses, splits on spaces."""
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _sources_in(text: str) -> Iterator[str]:
    """The code a text may run: the text itself, and each ``-c`` operand of it read as a command
    line (``os.system("python -c 'import apps.x.impl'")``, Codex, PR #6182)."""
    yield text
    if text.startswith("-c"):  # one argument joins the option to its code: ``"-cimport x"``
        yield text[2:]
    try:
        words = shlex.split(text)
    except ValueError:  # an unclosed quote: the shell refuses it too, so it runs nothing
        return
    yield from (after for flag, after in itertools.pairwise(words) if flag == "-c")
    yield from (word[2:] for word in words if word.startswith("-c") and len(word) > 2)


# The interpreter options whose operand is what it runs: ``python -m mod`` and ``python -c code``.
_RUN_OPTIONS = frozenset({"-m", "-c"})
# The keywords a process launcher takes its command by (``subprocess.run(args=[...])``).
_COMMANDS = frozenset({"args", "cmd", "command", "argv"})
# Words that mark a program as a Python interpreter: ``sys.executable``, ``"python3"``,
# ``str(interpreter)``. ``git log -m`` takes ``-m`` too, and runs no module.
_INTERPRETER_WORDS = ("python", "interpreter", "executable")


def _names_interpreter(node: ast.expr, constants: Mapping[str, str]) -> bool:
    """Whether ``node`` names a Python interpreter by any name, attribute or text inside it."""
    words = [node.id if isinstance(node, ast.Name) else "", _literal(node, constants) or ""]
    for part in ast.walk(node):
        if isinstance(part, ast.Name):
            words.append(part.id)
        elif isinstance(part, ast.Attribute):
            words.append(part.attr)
        elif isinstance(part, ast.expr):
            words.append(_literal(part, constants) or "")
    return any(marker in word.lower() for word in words for marker in _INTERPRETER_WORDS)


def _runs_unread_operand(call: ast.Call, constants: Mapping[str, str], containers: _Displays) -> bool:
    """Whether a call hands an interpreter ``-m`` or ``-c`` with an operand no reader folds
    (``run([sys.executable, "-m", target])``, ``spawnl(mode, exe, python, "-c", code)``): what it
    runs is unknown, so the call is `DYNAMIC` (Codex, PR #6182). An option counts where an item
    before it in its sequence names an interpreter (`_names_interpreter`). The call's own arguments
    are read as a sequence too, a display anywhere inside an argument is read, and an option with
    nothing after it in its display (``[sys.executable, "-m"] + rest``) has an unread operand."""
    handed = [*call.args, *(k.value for k in call.keywords)]
    parents = {id(child): part for arg in handed for part in ast.walk(arg) for child in ast.iter_child_nodes(part)}
    # A root is a sequence as the callee sees it: ``[*base, "-m", t]``, ``base + ["-m", t]`` or a
    # name grown over several statements (``cmd = [exe]`` then ``cmd += ["-m", t]``), each read whole.
    roots = [
        part
        for arg in handed
        for part in ast.walk(arg)
        if isinstance(part, ast.List | ast.Tuple | ast.Name | ast.BinOp)
        and not isinstance(parents.get(id(part)), ast.Starred | ast.BinOp)
    ]
    sequences = [_flat_sequence(ast.Tuple(elts=list(call.args), ctx=ast.Load()), containers)]
    sequences += [_flat_sequence(root, containers) for root in roots]
    for items in sequences:
        for at, item in enumerate(items):
            if _literal(item, constants) not in _RUN_OPTIONS:
                continue
            if not any(_names_interpreter(before, constants) for before in items[:at]):
                continue
            operand = items[at + 1] if at + 1 < len(items) else None
            # Only a literal or a module constant pins an operand: a name another scope binds to
            # text (``target = "json"`` beside ``def launch(target)``) may hold anything here.
            if operand is None or _literal(operand, constants) is None:
                return True
    commands = [*call.args, *(k.value for k in call.keywords if k.arg in _COMMANDS)]
    lines = (_template(each, constants) for arg in commands for each in _held_items(arg, containers))
    return any(_line_runs_unread(line) for line in lines if line)


# What `_template` writes for a part of a built string no reader folds.
_UNREAD = "\0"


def _template(node: ast.expr, constants: Mapping[str, str]) -> str | None:
    """The text a string built at run time holds, with `_UNREAD` for each part no reader folds:
    ``f"python -m {target}"`` reads ``python -m \\0``, and ``"python -m " + target`` the same.
    A ``%`` or ``str.format`` template keeps its text, its fields left unread. None for a node
    that builds no string from parts."""
    if isinstance(node, ast.JoinedStr):
        parts = (
            _literal(value.value, constants) if isinstance(value, ast.FormattedValue) else _literal(value, constants)
            for value in node.values
        )
        return "".join(_UNREAD if part is None else part for part in parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = (_template(side, constants) or _literal(side, constants) for side in (node.left, node.right))
        return (left or _UNREAD) + (right or _UNREAD)
    formatted: ast.expr | None = None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        formatted = node.func.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        formatted = node.left
    text = _literal(formatted, constants)
    return None if text is None else re.sub(r"%[-#0 +]*\d*(?:\.\d+)?[a-zA-Z]|\{[^{}]*\}", _UNREAD, text)


def _line_runs_unread(line: str) -> bool:
    """Whether a command line hands an interpreter ``-m`` or ``-c`` an operand holding an unread
    part (``python -m \\0``), or ends on the option: what it runs is unknown."""
    words = _shell_words(line)
    for at, word in enumerate(words):
        joined = word[:2] in _RUN_OPTIONS and len(word) > 2
        if word not in _RUN_OPTIONS and not joined:
            continue
        # The program is an interpreter by name, or unread and right before the option (``{exe} -m``).
        named = any(marker in before.lower() for before in words[:at] for marker in _INTERPRETER_WORDS)
        if not named and not (at == 1 and _UNREAD in words[0]):
            continue
        operand = word[2:] if joined else (words[at + 1] if at + 1 < len(words) else _UNREAD)
        if _UNREAD in operand:
            return True
    return False


def _flat_sequence(node: ast.expr, containers: _Displays, seen: frozenset[str] = frozenset()) -> list[ast.expr]:
    """The items a sequence expression holds in order: a display's, with a starred name spread in
    place; a name's displays joined in the order the source writes them (``cmd = [exe]`` then
    ``cmd += ["-m", t]``), an insert placed at its index as ``list.insert`` places it; both sides
    of a ``+``. Anything else is one item no reader folds."""
    if isinstance(node, ast.List | ast.Tuple):
        spread = (
            _flat_sequence(item.value, containers, seen) if isinstance(item, ast.Starred) else [item]
            for item in node.elts
        )
        return [each for items in spread for each in items]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_flat_sequence(node.left, containers, seen), *_flat_sequence(node.right, containers, seen)]
    if isinstance(node, ast.Name) and node.id not in seen and node.id in containers:
        parts = sorted(
            (d for d in containers[node.id] if isinstance(d, ast.List | ast.Tuple)),
            key=lambda d: (d.lineno, d.col_offset),
        )
        items: list[ast.expr] = []
        for part in parts:
            added = _flat_sequence(part, containers, seen | {node.id})
            if not hasattr(part, _INSERT_AT):
                items += added
            elif (at := getattr(part, _INSERT_AT)) is None:
                # ``cmd.insert(i, x)`` at an index no reader folds may land anywhere: read it at
                # both ends, so an option and its operand meet the program on either side.
                items = [*added, *items, *added]
            else:
                where = max(0, len(items) + at) if at < 0 else min(at, len(items))
                items[where:where] = added
        return items
    return [node]


def _held_items(node: ast.expr, containers: _Displays, seen: frozenset[int] = frozenset()) -> Iterator[ast.expr]:
    """``node`` and every item a display it is, or a name bound to one, holds at any depth (a
    list, tuple or set's elements, a dict's keys and values, what a starred item spreads, either
    side of a ``+``)."""
    yield node
    if isinstance(node, ast.Starred):
        yield from _held_items(node.value, containers, seen)
        return
    sides = (node.left, node.right) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) else ()
    # ``base + ["apps.x.impl"]`` joins sequences; ``"apps.x.impl" + suffix()`` builds other text.
    if any(
        isinstance(side, ast.List | ast.Tuple) or (isinstance(side, ast.Name) and side.id in containers)
        for side in sides
    ):
        for side in sides:
            yield from _held_items(side, containers, seen)
        return
    displays: list[ast.expr] = [node] if isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict) else []
    if isinstance(node, ast.Name):
        displays.extend(containers.get(node.id, ()))
    for display in displays:
        if id(display) in seen:  # ``A = [A]`` holds itself, so each display is read once
            continue
        if isinstance(display, ast.Dict):
            items: list[ast.expr | None] = [*display.keys, *display.values]
        elif isinstance(display, ast.List | ast.Tuple | ast.Set):
            items = list(display.elts)
        else:
            items = []
        for item in items:
            if item is not None:  # ``{**rest}`` keeps no key
                yield from _held_items(item, containers, seen | {id(display)})


def _imports_in_code(code: str | None, module: str, is_package: bool, known: frozenset[str]) -> list[tuple[str, int]]:
    """What a literal handed to ``exec``, ``eval`` or ``compile`` imports, read as its own module."""
    try:
        tree = ast.parse(code) if code else None
    except SyntaxError:  # it raises at run time too, so it imports nothing
        return []
    if tree is None:
        return []
    return _imports_in(_nodes(tree), module, is_package, known, tree)


def _called_name(func: ast.expr | None) -> str | None:
    if isinstance(func, ast.NamedExpr):  # ``(load := __import__)("x")`` calls what it binds
        return _called_name(func.value)
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _loaded(call: ast.Call, constants: Mapping[str, str]) -> str:
    """What ``import_module("x")`` or ``__import__("x")`` loads, when its name is a literal or a
    module constant (`_constants`), and ``import_module(".x", "apps.y")`` when its anchor is one
    too, ``runpy.run_module("x")`` and ``runpy.run_path("x/y.py")``; anything else (a computed
    name, an anchor such as ``__package__``, a spec's ``exec_module``, a path outside the
    repository) is `DYNAMIC`."""
    target = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg in _TARGETS), None)
    text = _literal(target, constants)
    # ``pkgutil.resolve_name("x.y:attr")`` imports the module before the colon.
    name = _module_path(text if text is None or text.endswith(".py") else text.partition(":")[0])
    if not (name and all(part.isidentifier() for part in name.lstrip(".").split("."))):
        return DYNAMIC  # computed, or a path that names no module from the repository root
    if not name.startswith("."):
        return name
    anchor = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "package"), None)
    package = _literal(anchor, constants)  # __import__ passes a globals dict here, never text, so stays DYNAMIC
    try:
        return importlib.util.resolve_name(name, package) if package else DYNAMIC
    except ImportError:  # more dots than the anchor has parts: it raises at run time too
        return DYNAMIC


def _module_path(text: str | None) -> str | None:
    """``runpy.run_path("apps/x/impl.py")`` runs the module that path holds, read from the
    repository root as every gate's paths are. An absolute or parent-relative path keeps a
    part (``/``, ``..``, ``C:``) that is no identifier, so it reads as no module name."""
    if text is None or not text.endswith(".py"):
        return text
    parts = PurePosixPath(text.replace("\\", "/")).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _literal(node: ast.expr | None, constants: Mapping[str, str]) -> str | None:
    """The text ``node`` always evaluates to, folding what has no side effect: a literal, a
    module constant, ``"a." + "b"``, ``f"a.{B}"``, ``".".join(("a", "b"))`` and ``("a", "b")[1]``."""
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _joined("", [node.left, node.right], constants)
    if isinstance(node, ast.JoinedStr):
        # A conversion (``!r``) or a format spec changes the text, so only a bare ``{B}`` folds.
        plain = (
            v.value if isinstance(v, ast.FormattedValue) and v.conversion == -1 and not v.format_spec else v
            for v in node.values
        )
        return _joined("", list(plain), constants)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Tuple | ast.List)
    ):
        sep = _literal(node.func.value, constants)
        return None if sep is None else _joined(sep, node.args[0].elts, constants)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Tuple | ast.List | ast.Dict):
        return _literal(_picked(node.value, node.slice, constants), constants)
    return None


def _picked(display: ast.Tuple | ast.List | ast.Dict, key: ast.expr, constants: Mapping[str, str]) -> ast.expr | None:
    """The item ``display[key]`` selects when both are written out, else None."""
    if isinstance(display, ast.Dict):
        text, picked = _literal(key, constants), None
        for k, v in zip(display.keys, display.values, strict=True):
            # A later key wins, and ``**other`` may hold any key, so nothing before it is certain.
            picked = None if k is None else v if text is not None and _literal(k, constants) == text else picked
        return picked
    negative = isinstance(key, ast.UnaryOp) and isinstance(key.op, ast.USub)
    index = key.operand if isinstance(key, ast.UnaryOp) and negative else key
    if not (isinstance(index, ast.Constant) and type(index.value) is int) or any(
        isinstance(item, ast.Starred) for item in display.elts
    ):
        return None
    position = -index.value if negative else index.value
    return display.elts[position] if -len(display.elts) <= position < len(display.elts) else None


def _joined(sep: str, parts: Sequence[ast.expr], constants: Mapping[str, str]) -> str | None:
    texts = [_literal(part, constants) for part in parts]
    return None if any(t is None for t in texts) else sep.join(t for t in texts if t is not None)


def _pure_scope_files(repo: Path, enforced: Iterable[str], container: str) -> Iterator[tuple[str, Path, bool]]:
    """(module, file, is_package) for each enforced package's root and contract files."""
    for pkg in sorted(enforced):
        root = repo / container / pkg
        yield f"{container}.{pkg}", root / "__init__.py", True
        single = root / f"{CONTRACT}.py"
        if single.is_file():
            yield f"{container}.{pkg}.{CONTRACT}", single, False
        for path in sorted((root / CONTRACT).rglob("*.py")):
            rel = path.relative_to(repo).with_suffix("").parts
            is_package = rel[-1] == "__init__"
            yield ".".join(rel[:-1] if is_package else rel), path, is_package


def pure_scope_imports(
    repo: Path, enforced: Iterable[str], known_modules: Iterable[str], container: str = CONTAINER
) -> dict[tuple[str, str], list[int]]:
    """{(importer, imported): lines} for every import in an enforced root or contract.

    Feeding these pairs to `surface_violations` alongside the graph's holds the purity
    rule over type-checking imports too, which the graph drops.
    """
    known = frozenset(known_modules) | compiled_modules(repo, container)
    pairs: dict[tuple[str, str], list[int]] = {}
    for module, path, is_package in _pure_scope_files(repo, enforced, container):
        if not path.is_file():
            continue
        for imported, line in source_imports(path.read_text(encoding="utf-8"), module, is_package, known):
            pairs.setdefault((module, imported), []).append(line)
    return pairs


# Every platform's forms of a module no import graph reads: a Windows .pyd or .pyw imports there
# even when the gate runs on Linux, and a bytecode file imports with no source beside it. A stub
# (.pyi) is read by type checkers only, so its imports are type-only coupling the graph never sees
# (Codex, PR #6182); the tree ships none, so one is refused rather than read.
NON_SOURCE_SUFFIXES = tuple(
    sorted(
        {
            ".so", ".pyd", ".pyc", ".pyw", ".pyi",
            *importlib.machinery.EXTENSION_SUFFIXES, *importlib.machinery.BYTECODE_SUFFIXES,
        }
    )
)  # fmt: skip


def compiled_modules(repo: Path, container: str = CONTAINER) -> frozenset[str]:
    """Each module under ``container`` held only in a form the graph does not read (`NON_SOURCE_SUFFIXES`).

    The graph knows the modules this interpreter would import from source, so ``from . import
    helper`` with only ``helper.pyd`` beside it reads as an import of the package itself, which a
    root may make; the readers add these so the import names the module it runs on Windows.
    """
    return frozenset(module for module, _ in _compiled_files(repo, container))


def _compiled_files(repo: Path, container: str) -> Iterator[tuple[str, PurePosixPath]]:
    """(module, file relative to ``repo``) for each module under ``container`` held in a non-source form."""
    for folder, dirs, files in os.walk(repo / container):
        # A directory whose name is no identifier (``node_modules/@x``) holds no importable module.
        dirs[:] = sorted(d for d in dirs if d.isidentifier() and d != "__pycache__")
        package = Path(folder).relative_to(repo).parts
        for name in sorted(f for f in files if f.endswith(NON_SOURCE_SUFFIXES)):
            stem = name.split(".", 1)[0]
            if stem.isidentifier():
                yield ".".join(package if stem == "__init__" else (*package, stem)), PurePosixPath(*package, name)


def unread_modules(repo: Path, containers: Iterable[str] = (CONTAINER,)) -> list[tuple[str, str]]:
    """(module, file) for each module under ``containers`` (the gate passes every root its graph
    holds: a compiled ``scripts/native.pyd`` imports as unseen as one in ``apps``) that only a
    non-source file holds and
    that the tree ships: no gate can read what a compiled extension imports, so it could import
    any implementation unseen. Build output git ignores never reaches a checkout and is left
    out; a file git tracks is kept even where an ignore rule matches it. Where git cannot answer
    (no repository, no git), nothing is taken as ignored, so the gate fails rather than passes."""
    return _shipped(repo, [found for container in containers for found in _compiled_files(repo, container)])


def unread_root_modules(repo: Path) -> list[tuple[str, str]]:
    """`unread_modules` for the files at the repository root itself, which import as top-level
    modules (``_native.so`` as ``_native``) and which no container walk reaches: a root module
    only a compiled file holds is read by nothing, wherever a local build drops it."""
    held = [
        (name.split(".", 1)[0], PurePosixPath(name))
        for name in sorted(p.name for p in repo.iterdir() if p.is_file())
        if name.endswith(NON_SOURCE_SUFFIXES) and name.split(".", 1)[0].isidentifier()
    ]
    return _shipped(repo, held)


def _shipped(repo: Path, held: list[tuple[str, PurePosixPath]]) -> list[tuple[str, str]]:
    """(module, file) for each of ``held`` but build output git ignores (`unread_modules`)."""
    git = shutil.which("git")
    ignored: set[str] = set()
    if held and git is not None:
        listed = "".join(f"{path}\0" for _, path in held)
        ran = subprocess.run(
            [git, "-C", str(repo), "check-ignore", "-z", "--stdin"],
            input=listed,
            capture_output=True,
            text=True,
            check=False,
        )
        # 0: some are ignored, 1: none is; anything else is no answer, so none counts as ignored.
        if ran.returncode == 0:
            ignored = set(filter(None, ran.stdout.split("\0")))
    return sorted((module, str(path)) for module, path in held if str(path) not in ignored)


def _module_file(repo: Path, module: str) -> tuple[Path, bool] | None:
    base = repo.joinpath(*module.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py", True
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py"), False
    return None


def statement_imports(repo: Path, modules: Iterable[str]) -> dict[tuple[str, str], list[int]]:
    """{(importer, imported): lines} for every import statement in ``modules``, however guarded.

    The graph `.importlinter` builds drops imports under a type-checking guard, but a consumer
    that names an implementation type in an annotation is still coupled to it, so the deep-import
    rule reads them from source. A guard can take any form (``if TYPE_CHECKING and FLAG:``,
    ``from typing import TYPE_CHECKING as TC``, ``if not TYPE_CHECKING: ... else:``), and no reader
    can tell every one apart, so every statement is read: the ones the graph keeps read the same
    pairs again, which changes no verdict. Purity stays where `surface_violations` puts it.
    """
    known = frozenset(modules) | compiled_modules(repo)
    pairs: dict[tuple[str, str], list[int]] = {}
    for module, is_package, source in _sources(repo, known):
        tree = ast.parse(source)
        statements = (n for n in _nodes(tree) if isinstance(n, ast.Import | ast.ImportFrom))
        for imported, line in _imports_in(statements, module, is_package, known, tree):
            pairs.setdefault((module, imported), []).append(line)
    return pairs


def call_loads(repo: Path, modules: Iterable[str]) -> dict[tuple[str, str], list[int]]:
    """{(importer, imported): lines} for every module a call in ``modules`` loads or names,
    `DYNAMIC` where a loader's target is a name no reader can resolve.

    ``importlib.import_module("apps.X.impl")`` couples a consumer to an implementation as
    surely as an import statement, but the graph never sees it, so the deep-import rule
    reads these from source too, and `surface_violations` refuses the unresolved ones.
    """
    known = frozenset(modules) | compiled_modules(repo)
    pairs: dict[tuple[str, str], list[int]] = {}
    # Every module is read: a name can be folded from parts no text filter sees whole
    # (``registry.get("apps" + ".x.impl")``), so a filter on the raw text is unsound.
    for module, is_package, source in _sources(repo, known):
        tree = ast.parse(source)
        calls = (n for n in _nodes(tree) if isinstance(n, ast.Call))
        found = _imports_in(calls, module, is_package, known, tree)
        # ``getattr(importlib, name)(target)`` reaches a loader no reader can name: a load by
        # reflection is as unreadable as an unresolved target, so it is DYNAMIC too.
        # A consumer calls computed callees for many reasons (``HANDLERS[kind](row)``); one
        # counts only where the module holds a loader module loosely, so a loader can reach it.
        reached = _holds_loader_module_loosely(tree)
        found += [(DYNAMIC, line) for line in _reflective_loads(tree, _loader_names(tree), computed=reached)]
        found += _module_table_loads(tree, known)
        for imported, line in sorted(set(found)):
            pairs.setdefault((module, imported), []).append(line)
    return pairs


def loaded_imports(repo: Path, modules: Iterable[str]) -> dict[tuple[str, str], list[int]]:
    """`call_loads` less the loads by a name no reader can resolve."""
    return {pair: lines for pair, lines in call_loads(repo, modules).items() if pair[1] != DYNAMIC}


def _sources(repo: Path, modules: Iterable[str]) -> Iterator[tuple[str, bool, str]]:
    """(module, is_package, source) for each of ``modules`` held as source on disk."""
    for module in sorted(modules):
        located = _module_file(repo, module)
        if located is not None:
            path, is_package = located
            yield module, is_package, path.read_text(encoding="utf-8")
