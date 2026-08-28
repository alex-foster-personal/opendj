"""Everything the engine imports on its BOOT PATH must be a declared dep.

The boot path is create_app -> JobStore -> recover() -> reap, and reap.py
imports psutil at module scope. An undeclared import there is not a lint nit:
a `pip install music-dj-tools` consumer gets ImportError the moment the engine
starts, which is the one code path that has no chance to degrade gracefully.

Single-line intent:
  - if a boot-path third-party import is missing from either manifest then a
    clean install boots straight into ImportError
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REAP = REPO_ROOT / "apps" / "engine_core" / "jobs" / "reap.py"

# Third-party modules imported at module scope anywhere on the boot path.
# stdlib and first-party (apps.*) imports need no declaration.
BOOT_PATH_THIRD_PARTY: tuple[str, ...] = ("psutil",)


def _module_scope_imports(path: Path) -> set[str]:
    """Top-level import names only. A lazy import inside a function is a
    different contract (optional extras) and is deliberately not counted."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_reap_still_imports_psutil_at_module_scope() -> None:
    """Guard the premise: if this moves, the manifest test below is vacuous."""
    assert "psutil" in _module_scope_imports(REAP), (
        "reap.py no longer imports psutil at module scope; re-derive "
        "BOOT_PATH_THIRD_PARTY before trusting the manifest assertions"
    )


@pytest.mark.parametrize("manifest_name", ["requirements.txt", "pyproject.toml"])
@pytest.mark.parametrize("package", BOOT_PATH_THIRD_PARTY)
def test_boot_path_imports_are_declared(manifest_name: str, package: str) -> None:
    manifest = (REPO_ROOT / manifest_name).read_text(encoding="utf-8")
    assert package in manifest, (
        f"{package} is imported on the engine boot path but is not declared "
        f"in {manifest_name}; a clean install boots into ImportError"
    )
