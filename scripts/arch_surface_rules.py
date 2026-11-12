"""Part of `arch_surface.py`, split out with no behavior change: rules."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import itertools
import re
import shlex
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from pathlib import PurePosixPath
from types import MappingProxyType

from scripts.arch_surface_graph import (
    _importable,
    _literal,
    _machinery_refs,
    _texts,
)
from scripts.arch_surface_paths import (
    _DESERIALIZERS,
    _INTERPRETERS,
    _TARGETS,
    DYNAMIC,
    _bound_names,
    _Displays,
    _nodes,
    _unpacked,
)


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


def _machinery_calls(tree: ast.AST) -> dict[int, str]:
    """Callees taken from the import machinery (`_MACHINERY`) that may import by a name they are
    handed: an attribute read off a name an import of it binds (``importlib.util.find_spec``,
    ``resources.files``), or a name ``from importlib.resources import files`` binds, each by its
    qualified name, unless `_NON_LOADING` says it never imports. Nearly all of the machinery
    imports, so it is read whole rather than listed a function at a time."""
    return {id(node): qualified for node, called, qualified in _machinery_refs(tree) if called}


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


_PASSES = 32  # a name bound through a chain longer than this reads the texts the chain reached


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
