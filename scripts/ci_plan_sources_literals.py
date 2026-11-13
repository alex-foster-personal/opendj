"""String literals and repository paths spelled in source."""

from __future__ import annotations

import ast
import itertools
import posixpath
import re
from collections import Counter
from collections.abc import Mapping

from scripts.ci_plan_sources_ast import _called_name
from scripts.ci_plan_sources_loader_graph import _joined, _string_pieces, _string_value
from scripts.ci_plan_sources_read import PlanError

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
