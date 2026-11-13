"""Shared helpers and constants for module public-surface contract tests (#10941)."""

from __future__ import annotations

import configparser
import importlib.machinery
import os
import py_compile
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.arch_contract_schema import declaration_problems
from scripts.arch_surface import (
    DYNAMIC,
    Violation,
    call_loads,
    compiled_modules,
    loaded_imports,
    package_of,
    pure_scope_imports,
    source_imports,
    statement_imports,
    surface_violations,
    unread_modules,
    unread_root_modules,
)

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / ".importlinter"
SECTION = "importlinter:contract:module-public-surface"
DELIVERY = ["webui", "engine_core", "opendj_cli"]
COMPOSITION_ROOTS = ["scripts"]

# Packages under apps/ on Fri 9 Oct 2026 that were not yet behind a contract. This set
# only shrinks: migrate a package, then move it from here to `enforced` in .importlinter.
# Never add a name; a new package goes straight into `enforced`.
PREDATES_ADR = frozenset(
    {
        "adapters", "agentbox", "analysis", "analysis_beatgrid", "analysis_bench", "analysis_key",
        "analysis_loudness", "analysis_waveform", "audit", "cloud", "database",
        "diagnostics", "dj_copilot", "engine_core", "entitlements", "equivalence",
        "feature_flags", "genre_infer", "loudness", "lyrics", "mik", "native_tools",
        "open_dj", "opendj_cli", "sets", "shared",
        "smartlists", "spotify", "stems", "sync", "sync_hub", "vocals", "voice", "webui",
    }
)  # fmt: skip

# Packages that landed after the ADR WITHOUT a contract, each named with the tech-debt issue that
# owes it. This is not PREDATES_ADR (never add there). A name here must leave in the PR that adds it
# to `enforced`; test_owed_contracts_are_not_enforced_yet refuses the overlap. apps/autodj and
# apps/transition_score (#7708) and apps/now_playing (#10336, contract in #10341) are now
# enforced.
# apps/plugins came in with #6522 (plugin store) and owes its contract, a Protocol over frozen
# models for the engine and webui importers, before it can be enforced.
OWED_CONTRACT = {
    "ableton_link": "#7949",
    "b2b_booth": "#7949",
    "b2b_pairing": "#7949",
    "b2b_session": "#7949",
    "b2b_sync": "#7949",
    "b2b_transport": "#7949",
    "plugins": "#6522",
    "stream": "#10345",
}


def _check(*pairs: tuple[str, str], enforced: tuple[str, ...] = ("lib",)) -> list[Violation]:
    return surface_violations(pairs, enforced, DELIVERY, composition_roots=COMPOSITION_ROOTS)


def _section() -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS, encoding="utf-8")
    return parser[SECTION]


def _enforced() -> set[str]:
    return set(_section()["enforced"].split())


def _packages() -> set[str]:
    return {p.name for p in (REPO / "apps").iterdir() if p.is_dir() and (p / "__init__.py").exists()}


# Directories under apps/ that hold Python but no __init__.py. Python still imports each as the
# namespace package apps.X, which _packages() does not see, so each is named here with why it
# is not a package. This set only shrinks; a new one gets an __init__.py and joins `enforced`.
NOT_PACKAGES = frozenset({"launcher"})  # standalone scripts run by path; nothing imports apps.launcher


# What Python imports a module from: source, stubs, sourceless bytecode, extension modules.
_MODULE_SUFFIXES = frozenset({".py", ".pyi", ".pyc", ".so", ".pyd"})


def _importable(name: str) -> bool:
    # Only an identifier-named directory imports as a package: a hyphenated build tree
    # (audio-engine/target) does not, nor does a bytecode cache or vendored node_modules.
    return name.isidentifier() and name not in {"__pycache__", "node_modules"}


def _holds_python(directory: Path) -> bool:
    for _root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if _importable(d)]
        if any(Path(f).suffix in _MODULE_SUFFIXES for f in files):
            return True
    return False


def _namespace_dirs(container: Path) -> set[str]:
    return {
        p.name
        for p in container.iterdir()
        if p.is_dir() and _importable(p.name) and not (p / "__init__.py").exists() and _holds_python(p)
    }


def _top_level_modules(container: Path) -> set[str]:
    # apps/foo.py imports as apps.foo, a module no package census would see.
    return {p.name for p in container.iterdir() if p.is_file() and p.suffix in _MODULE_SUFFIXES} - {"__init__.py"}


# Importable code at the repository root that no import-linter root covers, each named with why
# (Codex, PR #6182). `docs/` imports as a namespace package (its `(private path)/` probes, moved
# from `research/` by #5829) and `conftest.py` as a module, so either could deep-import an
# enforced package unseen by the graph; the scan below reads them from source instead. A new
# production directory gets an __init__.py and joins `root_packages`.
ROOT_NOT_PACKAGES = frozenset(
    {"docs", "spikes"}
)  # (private path): probes and throwaway research; (private path)/*.py sits under test-cull (not identifier) so specs is not a namespace root here
# A compiled root module (the native waveform extension a local build drops here) is no entry:
# git ignores it, and one the tree ships is read by `unread_root_modules` (Sol, PR #6182).
ROOT_MODULES = frozenset({"conftest"})  # pytest's root hooks


def _module_name(path: Path) -> str:
    return path.name.split(".", maxsplit=1)[0]


def _dotted(path: Path, repo: Path) -> tuple[str, bool]:
    parts = path.relative_to(repo).with_suffix("").parts
    is_package = parts[-1] == "__init__"
    return ".".join(parts[:-1] if is_package else parts), is_package


def _python_sources(directory: Path) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if _importable(d))
        found.extend(Path(root) / f for f in sorted(files) if f.endswith(".py"))
    return found


def _outside_graph_sources(repo: Path) -> list[Path]:
    """Python sources importable from the repository root that no graph root reads."""
    sources = [p for p in sorted(repo.iterdir()) if p.is_file() and p.suffix == ".py"]
    for name in sorted(_namespace_dirs(repo)):
        sources.extend(_python_sources(repo / name))
    return sources


def _outside_graph_violations(repo: Path, enforced: set[str]) -> list[Violation]:
    known = {_dotted(p, repo)[0] for p in _python_sources(repo / "apps")}
    pairs: list[tuple[str, str]] = []
    for path in _outside_graph_sources(repo):
        module, is_package = _dotted(path, repo)
        source = path.read_text(encoding="utf-8")
        pairs.extend((module, imported) for imported, _line in source_imports(source, module, is_package, known))
    return surface_violations(pairs, enforced, DELIVERY, composition_roots=COMPOSITION_ROOTS)


_TC_CONTRACT = """
from __future__ import annotations
from typing import TYPE_CHECKING
from pydantic import BaseModel
from . import ids
if TYPE_CHECKING:
    from apps.lib.store import Row
    from ..service import Impl
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


_DYNAMIC_ROOT = """
import importlib
from importlib import import_module
importlib.import_module("apps.lib.store")
import_module(NAME)
__import__("apps.other.impl")
import_module(".service", __package__)
SPEC.loader.exec_module(MOD)
importlib.import_module("json")
from importlib import import_module as load
load("apps.other.alias")
fetch = importlib.import_module
fetch("apps.other.bound")
again = fetch
again("apps.other.again")
print("apps.other.plain")
import runpy
runpy.run_module("apps.other.ran")
runpy.run_path("apps/other/impl.py")
print(NAME)
F = getattr(LIB, "import_module")
getattr(LIB, LOADER)(NAME)
LIB.__dict__[LOADER](NAME)
B = __builtins__
exec(TEXT)
runpy.run_path("/opt/apps/other/impl.py")
(fetched := __import__)("apps.other.walrus")
fetched("apps.other.rebound")
(peek := getattr(LIB, LOADER))(NAME)
import pkgutil
pkgutil.resolve_name("apps.other.resolved:main")
(LOAD if FLAG else OTHER)(NAME)
def load_with(loader):
    return loader("apps.other.forwarded")
load_with(__import__)
run = exec
ev = builtins.eval
"""


def _findings(pkg: str) -> list[str]:
    """``declaration_problems`` less the ``sqlite3`` binding a fake needs to resolve its annotations."""
    binding = f"apps.{pkg}.contract.sqlite3: re-exports the module sqlite3, which is not this contract's"
    found = declaration_problems(pkg)
    return [p for p in found if p != binding]


FIXTURES = "tests.quality.contract_fixtures"  # real contract packages, one per concern


def _fixture(case: str) -> list[str]:
    """`declaration_problems` on ``contract_fixtures/<case>``, names relative to its contract."""
    prefix = f"{FIXTURES}.{case}.contract"
    return [p.removeprefix(prefix) for p in declaration_problems(case, container=FIXTURES)]


def _contract_packages() -> list[str]:
    # Every package already migrated to a contract/ package, plus every enforced package.
    # engine_core's older contract.py (the OpenAPI digest) predates the ADR.
    apps = REPO / "apps"
    migrated = {p.name for p in apps.iterdir() if (p / "contract").is_dir()}
    return sorted(migrated | _enforced())


def test_every_enforced_package_has_a_contract() -> None:
    # Enforcement says "reach me only through my contract", so the contract must exist and
    # carry CONTRACT_VERSION, or the declaration check below would have nothing to read.
    apps = REPO / "apps"
    missing = [
        p for p in sorted(_enforced()) if not ((apps / p / "contract").is_dir() or (apps / p / "contract.py").is_file())
    ]
    assert missing == []


# Protocol methods on main on Sat 17 Oct 2026, in packages .importlinter does not yet
# enforce, that still pass Any (or, for loudness, a type only the M4 analysis contract will
# declare). This set only shrinks: give the method a contract model, then drop its line.
# Never add one, and never one for an enforced package (the test below holds that).
LOOSE_BOUNDARIES = frozenset(
    {
        "apps.analysis_loudness.contract.LoudnessAnalysisService.produce_lane_result(): cannot resolve its annotations (NameError: name 'LaneResult' is not defined)",  # noqa: E501
    }
)  # fmt: skip
