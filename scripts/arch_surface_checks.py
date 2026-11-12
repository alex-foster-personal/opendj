"""Part of `arch_surface.py`, split out with no behavior change: checks."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

from scripts.arch_surface_graph import (
    _constants,
    _importable,
    _literal,
    _module_table_loads,
    _reflective_loads,
    _texts,
)
from scripts.arch_surface_paths import (
    _EVALUATORS,
    _LOADER_MODULES,
    _LOADERS,
    _TARGETS,
    CONTAINER,
    CONTRACT,
    DYNAMIC,
    _called_name,
    _from_base,
    _is_loader,
    _loader_names,
    _name_resolvers,
    _nodes,
    _reaches_module_table,
    _root_name,
)
from scripts.arch_surface_rules import (
    _bound_containers,
    _bound_texts,
    _fromlist,
    _held_items,
    _loaded,
    _machinery_calls,
    _machinery_loads,
    _named_modules,
    _pinned_loads,
    _pinned_texts,
    _sources_in,
    _spread,
)


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


def _is_namespace_read(node: ast.AST) -> bool:
    """A dunder read that hands out a namespace rather than one named member: ``b.__dict__``,
    ``importlib.util.__getattribute__``. A loader's own name (``b.__import__``) is a load already."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr.startswith("__")
        and node.attr.endswith("__")
        and node.attr not in _LOADERS
    )


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


def _imports_in_code(code: str | None, module: str, is_package: bool, known: frozenset[str]) -> list[tuple[str, int]]:
    """What a literal handed to ``exec``, ``eval`` or ``compile`` imports, read as its own module."""
    try:
        tree = ast.parse(code) if code else None
    except SyntaxError:  # it raises at run time too, so it imports nothing
        return []
    if tree is None:
        return []
    return _imports_in(_nodes(tree), module, is_package, known, tree)


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
