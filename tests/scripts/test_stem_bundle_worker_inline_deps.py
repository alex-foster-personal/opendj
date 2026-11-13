"""The stems worker's inline dependencies cover what its repo imports need.

``scripts/stem_bundle_worker.py`` runs under ``uv run`` in an environment built from
ONLY its PEP 723 dependency block: the repo venv is not there. Every third-party
module that the repo modules it imports pull in at import time must therefore be
declared in that block.

Regression guard for a real defect (Wed 11 Nov 2026, f260e8798ee): the worker gained
``from apps.cloud.stem_index_sync import record_published_bundle_if_shared`` after
``publish_into_place``. That chain reaches ``apps.stems.artifacts``, which imports
pydantic, and pydantic was not declared. Every local stems job then separated the
track, published the bundle, raised ``ModuleNotFoundError: No module named
'pydantic'`` and was reported as failed.

Requirements:
- declared dependencies cover the import-time third-party modules of every ``apps``
  module the worker imports, anywhere in the script. ✔︎
  [if] a repo module the worker imports needs an undeclared package at import [then ⛔️]
  [if] the PEP 723 block cannot be read [then ⛔️]
  [if] an import the worker names resolves to no file in the repo [then ⛔️]
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "scripts" / "stem_bundle_worker.py"
REPO_PACKAGES = frozenset({"apps", "scripts"})
OPTIONAL_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})

# ----------------------------------------------------------------------------- helpers


def _declared_distributions() -> set[str]:
    """Distribution names in the worker's PEP 723 block, lower case with underscores."""
    block = re.search(r"^# /// script\n(.*?)^# ///$", WORKER.read_text(encoding="utf-8"), re.S | re.M)
    assert block is not None, f"no PEP 723 block in {WORKER}"
    body = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in block.group(1).splitlines())
    dependencies = tomllib.loads(body)["dependencies"]
    return {re.split(r"[<>=!~ \[;]", entry, maxsplit=1)[0].lower().replace("-", "_") for entry in dependencies}


def _module_file(name: str) -> Path | None:
    """The repo file a dotted module name resolves to, or None when it is not a repo module."""
    base = ROOT.joinpath(*name.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _catches_import_error(node: ast.Try) -> bool:
    names = {
        leaf.id
        for handler in node.handlers
        if handler.type is not None
        for leaf in ast.walk(handler.type)
        if isinstance(leaf, ast.Name)
    }
    return bool(names & OPTIONAL_IMPORT_ERRORS)


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _import_time_imports(statements: list[ast.stmt]) -> list[ast.Import | ast.ImportFrom]:
    """Imports that run when the module is imported: not inside functions, optional tries or TYPE_CHECKING."""
    found: list[ast.Import | ast.ImportFrom] = []
    for node in statements:
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
        elif isinstance(node, ast.If):
            if not _is_type_checking(node.test):
                found += _import_time_imports(node.body)
            found += _import_time_imports(node.orelse)
        elif isinstance(node, ast.Try):
            if not _catches_import_error(node):
                found += _import_time_imports(node.body)
            found += _import_time_imports(node.orelse) + _import_time_imports(node.finalbody)
        elif isinstance(node, ast.With | ast.ClassDef):
            found += _import_time_imports(node.body)
    return found


def _absolute_names(node: ast.Import | ast.ImportFrom, module: str, is_package: bool) -> list[str]:
    """Every dotted name an import statement may load, with relative imports resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    package = module.split(".") if is_package else module.split(".")[:-1]
    if node.level > 0:
        package = package[: len(package) - (node.level - 1)]
        base = ".".join([*package, *(node.module.split(".") if node.module else [])])
    else:
        base = node.module or ""
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _third_party_at_import(start: list[str]) -> dict[str, str]:
    """Top-level third-party module -> the repo module that imports it, over the import-time closure."""
    third_party: dict[str, str] = {}
    seen: set[str] = set()
    queue = list(start)
    while queue:
        name = queue.pop()
        parts = name.split(".")
        for depth in range(1, len(parts) + 1):  # importing a.b.c runs a and a.b first
            module = ".".join(parts[:depth])
            path = _module_file(module)
            if module in seen or path is None:
                continue
            seen.add(module)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in _import_time_imports(tree.body):
                for imported in _absolute_names(node, module, path.name == "__init__.py"):
                    top = imported.split(".")[0]
                    if top in REPO_PACKAGES:
                        queue.append(imported)
                    elif top and top not in sys.stdlib_module_names and top != "__future__":
                        third_party.setdefault(top, module)
    return third_party


def _repo_modules_the_worker_imports() -> list[str]:
    """Every ``apps`` module the worker script imports, at module level or inside a function."""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"), filename=str(WORKER))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            names += [
                n for n in _absolute_names(node, "scripts.stem_bundle_worker", False) if n.split(".")[0] == "apps"
            ]
    return sorted(set(names))


# ------------------------------------------------------------------------------- tests


@pytest.mark.requirement("STEM-12745")
def test_worker_names_the_post_publish_import() -> None:
    """[if] the worker stops importing apps.cloud.stem_index_sync [then] this guard checks nothing, [else stop]."""
    imported = _repo_modules_the_worker_imports()
    assert "apps.cloud.stem_index_sync" in imported, imported
    unresolved = [
        name for name in imported if _module_file(name) is None and _module_file(name.rpartition(".")[0]) is None
    ]
    assert not unresolved, f"worker imports that resolve to no repo file: {unresolved}"


@pytest.mark.requirement("STEM-12745")
def test_inline_dependencies_cover_import_time_third_party_modules() -> None:
    """[if] a repo module the worker imports needs an undeclared package at import [then] fail, [else stop]."""
    declared = _declared_distributions()
    needed = _third_party_at_import(_repo_modules_the_worker_imports())
    missing = {module: importer for module, importer in needed.items() if module.lower() not in declared}
    assert not missing, (
        "scripts/stem_bundle_worker.py runs with only its PEP 723 dependencies, and these modules are "
        f"imported at import time by repo modules it loads but are not declared there: {missing}"
    )
