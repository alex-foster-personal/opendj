"""No module imports a POSIX-only stdlib module at module scope without a guard.

``import fcntl`` / ``import resource`` (and their kin) raise ``ModuleNotFoundError``
on Windows, so an unguarded top-level import makes the whole module unimportable
there. The fix is a guard (``try``/``except ImportError`` or ``if sys.platform``)
or moving the import into the function that needs it, never a bare top-level
import. Found by a scan of main at ``0ba0fa270`` (Thu 15 Oct 2026): five modules.

This is a structural check on the source, so it runs the same on every OS and
needs no patching. It sees only module-body imports; a guarded import is any
import nested inside an ``if`` or ``try``, and a function-local import is
a runtime use, which this guard leaves to the platform tests.

[if] a POSIX-only module is imported at module scope unguarded [then] fail, [else stop].

Regression lines:
  - if a module under apps/ or scripts/ imports a POSIX-only module at top level, unguarded, then broken
  - if the scan stops finding any Python files (an empty scan passing vacuously) then broken
  - if the scan stops seeing a known guarded import as guarded (it would then flag it) then broken
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("CROSS-6892")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = ("apps", "scripts")
POSIX_ONLY_MODULES = frozenset({"fcntl", "termios", "resource", "pwd", "grp", "syslog", "posix"})
SKIPPED_PARTS = frozenset({"node_modules", ".venv", "target", "__pycache__", "vendor"})


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            if SKIPPED_PARTS.isdisjoint(path.relative_to(REPO_ROOT).parts):
                files.append(path)
    return sorted(files)


def _unguarded_posix_imports(source: str) -> list[tuple[int, str]]:
    """(line, module) for each module-body import of a POSIX-only module."""
    hits: list[tuple[int, str]] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            hits += [
                (node.lineno, alias.name) for alias in node.names if alias.name.split(".")[0] in POSIX_ONLY_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in POSIX_ONLY_MODULES:
                hits.append((node.lineno, node.module))
    return hits


def test_the_scan_sees_python_files_and_tells_guarded_from_bare() -> None:
    """[if] the scan runs [then] it finds files and separates bare from guarded imports, [else stop]."""
    assert len(_python_files()) > 100, "the scan found almost no Python files"
    bare = "import fcntl\nimport os\n"
    guarded_try = "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n"
    guarded_if = "import sys\nif sys.platform != 'win32':\n    import resource\n"
    local = "def f():\n    import fcntl\n    return fcntl\n"
    assert _unguarded_posix_imports(bare) == [(1, "fcntl")]
    assert _unguarded_posix_imports(guarded_try) == []
    assert _unguarded_posix_imports(guarded_if) == []
    assert _unguarded_posix_imports(local) == []


def test_no_module_imports_a_posix_only_module_unguarded() -> None:
    """[if] a module under apps/ or scripts/ is scanned [then] it has no bare POSIX-only import, [else stop]."""
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line} import {module}"
        for path in _python_files()
        for line, module in _unguarded_posix_imports(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these modules cannot be imported on Windows; guard the import or move it into the "
        "function that needs it:\n  " + "\n  ".join(offenders)
    )
