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
from pathlib import Path


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
    """Every name bound by anything but an import from `typing`: an assignment or loop
    target, a parameter, a def or class, or an import from any other module."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(
            node, ast.ExceptHandler | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module not in _TYPING_MODULES:
            bound.update(alias.asname or alias.name for alias in node.names)
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
            sentinel_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "TYPE_CHECKING"
            )
        elif isinstance(node, ast.Import):
            typing_aliases.update(
                alias.asname or alias.name for alias in node.names if alias.name in _TYPING_MODULES
            )
    rebound = _names_bound_otherwise(tree)
    sentinel_names -= rebound
    typing_aliases -= rebound
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
    return {p for p in _imported_names(text, where) if p.split(".")[0] == "tests"}


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
