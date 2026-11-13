"""Loader and importlib call sites in pytest inputs."""

from __future__ import annotations

import ast
import importlib.util
import ntpath
import posixpath
import string
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

from scripts.ci_plan_sources_ast import (
    _called_name,
    _is_namespace,
    _namespace_writes,
    _string_value,
)
from scripts.ci_plan_sources_read import PlanError

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
