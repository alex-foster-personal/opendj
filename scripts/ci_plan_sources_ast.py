"""AST primitives shared by the CI plan source readers."""

from __future__ import annotations

import ast

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


def _called_name(func: ast.expr) -> str | None:
    """`import_module` for `importlib.import_module(...)` and `import_module(...)` alike."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


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


def _string_value(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
