"""Part of `arch_surface.py`, split out with no behavior change: graph."""

from __future__ import annotations

import ast
import itertools
import re
import string
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from scripts.arch_surface_paths import (
    _BUILTINS,
    _EVALUATORS,
    _LOADERS,
    _MACHINERY,
    _NON_LOADING,
    _NON_LOADING_WITHIN,
    _PYDOC_LOADERS,
    DYNAMIC,
    _attribute_key,
    _bound_names,
    _callee,
    _Displays,
    _evaluator_names,
    _holds_metadata,
    _is_loader,
    _loads_entry_point,
    _name_resolvers,
    _nodes,
    _pydoc_bindings,
    _reaches_builtins,
    _reaches_module_table,
    _tested_only,
)

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


def _importable(name: str, known: frozenset[str]) -> str:
    """The module a dotted object path loads: ``pydoc.locate`` and ``pkgutil.resolve_name`` import
    the longest prefix of ``apps.x.impl.run`` that is a module, then read the rest as attributes."""
    parts = name.split(".")
    prefixes = (".".join(parts[:end]) for end in range(len(parts), 0, -1))
    return next((prefix for prefix in prefixes if prefix in known), name)


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
