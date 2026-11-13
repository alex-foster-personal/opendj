"""Decode pytest inputs and read import edges from source text."""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.ci_plan_sources_ast import _type_checking_only


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
    return {p for p in _imported_names(text, where) if p.split(".")[0].casefold() == "tests"}  # `Tests` too
