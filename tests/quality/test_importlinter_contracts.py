"""The .importlinter package lists are complete, not curated.

A contract only covers the packages it names. The lists were written by hand on
Sun 17 Aug 2026 and by Tue 22 Sep 2026 had silently stopped covering sync_hub,
lyrics, engine_core, mik, launcher and the analysis_* packages; three of the
edges that held a 31-scope import cycle together went through exactly those gaps
(SMARTEST-CI round 12). Every `layers` contract is now `exhaustive` over apps/,
and this holds each one's package list equal to the packages under apps/, so a
new package cannot land outside them; it also holds the directories that are not
packages to importing only apps.shared.

[if] a package is added under apps/ and not to the contracts [then] fail here, [else stop].
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / ".importlinter"

# The delivery layers: they compose domain packages and may import each other.
# Nothing below them may import them.
DELIVERY = frozenset({"engine_core", "opendj_cli", "webui"})

# Directories under apps/ that hold Python files but are not packages, so no contract can
# name them (import-linter builds its graph from `__init__.py` packages). Each is classified
# here with why, and held below to importing nothing from this repository but the stable
# core every package may import, so an upward import cannot land in one unseen.
NOT_PACKAGES = {
    "launcher": "a Tauri app; its scripts/*.py run by path and import only apps.shared",
    "learn--explainers": (
        "explainer content (Markdown terms and ASCII animations) plus two stdlib scripts run by"
        " path, build_index.py and inject_sidebar.py; the hyphenated name cannot be imported and"
        " the scripts import nothing from this repository"
    ),
}
STABLE_CORE = "apps.shared"


def _packages() -> frozenset[str]:
    return frozenset(p.name for p in (REPO / "apps").iterdir() if p.is_dir() and (p / "__init__.py").exists())


def _contracts() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS, encoding="utf-8")
    return parser


def _layered_packages(layers: str) -> frozenset[str]:
    return frozenset(
        n.strip(" ()") for line in layers.splitlines() for n in line.replace("|", ":").split(":") if n.strip(" ()")
    )


def _python_files_outside_packages() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for directory in (REPO / "apps").iterdir():
        if directory.is_dir() and not (directory / "__init__.py").exists():
            files = [p for p in directory.rglob("*.py") if "node_modules" not in p.parts]
            if files:
                found[directory.name] = files
    return found


def _repo_imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            names.append("." * node.level + (node.module or ""))
    return [
        n
        for n in names
        if n.split(".")[0] in {"apps", "scripts", "tests", ""}
        and n != STABLE_CORE
        and not n.startswith(STABLE_CORE + ".")
    ]


def test_every_python_directory_under_apps_is_a_package_or_classified() -> None:
    assert set(_python_files_outside_packages()) == set(NOT_PACKAGES)


def test_a_non_package_directory_imports_only_the_stable_core() -> None:
    offenders = {
        str(path.relative_to(REPO)): _repo_imports(path)
        for files in _python_files_outside_packages().values()
        for path in files
        if _repo_imports(path)
    }
    assert offenders == {}, (
        f"a non-package directory is outside every contract and may import only {STABLE_CORE}; "
        "make it a package so the contracts can name it"
    )


def test_the_delivery_layers_exist() -> None:
    assert _packages() >= DELIVERY


def test_type_checking_imports_are_excluded_by_the_config() -> None:
    assert _contracts()["importlinter"]["exclude_type_checking_imports"] == "True"


def test_every_layers_contract_is_exhaustive_and_names_every_package_under_apps() -> None:
    contracts = _contracts()
    layered = [s for s in contracts.sections() if contracts[s].get("type") == "layers"]
    assert layered, "no layers contract in .importlinter"
    for section in layered:
        contract = contracts[section]
        assert contract["containers"].split() == ["apps"], section
        assert contract.get("exhaustive") == "true", section
        assert _layered_packages(contract["layers"]) == _packages(), section
