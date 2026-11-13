"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Live-tree contract inventory, source-only contracts, and owed waivers.
"""

from __future__ import annotations

import configparser
import importlib
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

from tests.quality import module_surface_contract_support as sup


_check = sup._check
_section = sup._section
_enforced = sup._enforced
_packages = sup._packages
_tree = sup._tree
_findings = sup._findings
_fixture = sup._fixture
_contract_packages = sup._contract_packages
_dotted = sup._dotted
_hold_python = sup._holds_python
_importable = sup._importable
_module_name = sup._module_name
_namespace_dirs = sup._namespace_dirs
_outside_graph_sources = sup._outside_graph_sources
_outside_graph_violations = sup._outside_graph_violations
_python_sources = sup._python_sources
_top_level_modules = sup._top_level_modules
_TC_CONTRACT = sup._TC_CONTRACT
_DYNAMIC_ROOT = sup._DYNAMIC_ROOT
REPO = sup.REPO
CONTRACTS = sup.CONTRACTS
SECTION = sup.SECTION
DELIVERY = sup.DELIVERY
COMPOSITION_ROOTS = sup.COMPOSITION_ROOTS
PREDATES_ADR = sup.PREDATES_ADR
OWED_CONTRACT = sup.OWED_CONTRACT
NOT_PACKAGES = sup.NOT_PACKAGES
ROOT_NOT_PACKAGES = sup.ROOT_NOT_PACKAGES
ROOT_MODULES = sup.ROOT_MODULES
FIXTURES = sup.FIXTURES
LOOSE_BOUNDARIES = sup.LOOSE_BOUNDARIES


def test_every_public_binding_is_surface_whatever_all_says() -> None:
    # Controls inside the fixture: the root's clean Level enum, TypedError (its own typed
    # constructor), PLAIN, the `Name` alias and the GREETING string are offered with no finding.
    # ``Any`` imported under its own name is offered like any public name (Sol, PR #6182);
    # TypeAlias, imported the same way, is tooling and stays clean.
    assert _fixture("reexports") == [
        ".Any: Any is not a boundary type",
        ".BOXED: holds a Boxed that has field value holds a Connection, which is not a boundary value",
        ".CONN: holds a Connection, which is not a boundary value",
        ".Connection: re-exports sqlite3.Connection, which is not a contract type",
        ".DEFAULT: holds a _Payload that has field conn: Connection is not a boundary type",
        ".Declared: re-exports Declared, which is an enum with more than plain str or int members",
        f".Engine: re-exports {FIXTURES}.reexports.impl.Engine, which is not a contract type",
        ".Failure.__init__(): is BaseException's, whose "
        "*args take any object and hand it out as .args; declare a typed __init__",
        ".Failure.reopen(): return: Connection is not a boundary type",
        ".Handle: Connection is not a boundary type",
        ".Metered: re-exports Metered, which uses the metaclass "
        f"{FIXTURES}.reexports._ConnectingMeta, which is neither the language's nor the tooling's",
        ".Mode: re-exports Mode, which is an enum with more than plain str or int members",
        ".NESTED: holds a object, which is not a boundary value",
        ".Opener: re-exports Opener, which is an enum with more than plain str or int members",
        ".Payload: Any is not a boundary type",
        ".RootError: re-exports RootError, with connection: Connection is not a boundary type",
        ".Rows: Any is not a boundary type",
        ".UntypedError: re-exports UntypedError, with __init__(): is BaseException's, whose "
        "*args take any object and hand it out as .args; declare a typed __init__",
        ".Wired: re-exports Wired, which is an enum with more than plain str or int members",
        "._BaseFailure: re-exports BaseFailure, with reopen(): return: Connection is not a boundary type",
        "._Payload: field conn: Connection is not a boundary type",
        ".connect: re-exports connect, a callable with no typed signature",
        ".sqlite3: re-exports the module sqlite3, which is not this contract's",
    ]


def test_a_re_exported_contract_type_is_checked_where_it_is_offered() -> None:
    # Control inside the fixture: the re-exported Shared model is clean.
    assert _fixture("relay") == [
        ".LooseService.load(): payload: Any is not a boundary type",
        ".LooseService.load(): return: Any is not a boundary type",
    ]


def test_a_missing_contract_version_is_a_finding() -> None:
    assert _fixture("no_version") == [": CONTRACT_VERSION must be a string 'no_version/<n>', got None"]


def test_all_is_a_list_of_names() -> None:
    # Codex, PR #6182: a mapping's keys read as exports while its values went unread.
    # Control: every other fixture lists __all__ as a list or leaves it out, with no such finding.
    assert _fixture("listing") == [".__all__: is a dict; a contract lists names"]


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


def test_no_enforced_package_has_a_loose_boundary() -> None:
    packages = {package_of(entry.split(":")[0]) for entry in LOOSE_BOUNDARIES}
    loose = {p for p in packages if p is not None}
    assert loose == packages, "every loose boundary names an apps.<pkg> member"
    assert sorted(loose & _enforced()) == []


def test_every_contract_on_this_tree_declares_only_closed_frozen_types() -> None:
    packages = _contract_packages()
    assert packages, "found no contracts at all: the scan is pointed at nothing"
    problems = {p for pkg in packages for p in declaration_problems(pkg)}
    assert sorted(problems - LOOSE_BOUNDARIES) == []
    assert sorted(LOOSE_BOUNDARIES - problems) == [], "a loose boundary was fixed: drop it from LOOSE_BOUNDARIES"


def test_a_contract_is_python_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex, PR #6182: a compiled extension or bytecode file below a contract imports as a
    # module that neither gate reads, and a sourceless contract.pyc is the contract itself.
    root = tmp_path / "sourcefx"
    native = root / "native" / "contract"
    (native / "__pycache__").mkdir(parents=True)
    for package in (root, root / "native", root / "shadow"):
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (native / "__init__.py").write_text('CONTRACT_VERSION = "native/1"\n', encoding="utf-8", newline="\n")
    # Codex, PR #6182: the compiled initializers below would win over these sources on the platform
    # whose suffix they carry, so the packages are imported first, as a real install already would be.
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.import_module("sourcefx.native")
    suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    extension = f"fast{suffix}"
    (native / extension).write_bytes(b"")
    (native / "legacy.pyc").write_bytes(b"")
    for foreign in ("other.pyd", "other.so", "gui.pyw"):  # Codex, PR #6182: what another platform imports
        (native / foreign).write_bytes(b"")
    # Codex, PR #6182: a compiled package-root initializer runs before the __init__.py the purity scan reads.
    initializers = sorted({"__init__.pyd", f"__init__{suffix}"})  # this platform's form too
    for initializer in initializers:
        (root / "native" / initializer).write_bytes(b"")
        (root / initializer).write_bytes(b"")  # Codex, PR #6182: the container's initializer runs first too
    (root / "native" / "helper.pyd").write_bytes(b"")  # control: an implementation module may be compiled
    (root / "contract.pyd").write_bytes(b"")  # control: no module the contract imports
    (native / "__pycache__" / "cached.pyc").write_bytes(b"")  # control: the import cache is no module
    source = tmp_path / "shadow_contract.py"
    source.write_text('CONTRACT_VERSION = "shadow/1"\n', encoding="utf-8", newline="\n")
    py_compile.compile(str(source), cfile=str(root / "shadow" / "contract.pyc"), doraise=True)
    rule = "a contract is Python source, the only form these gates read"
    assert declaration_problems("native", container="sourcefx") == [
        f"sourcefx.native.contract: holds {name}; {rule}"
        for name in sorted((extension, "gui.pyw", "legacy.pyc", "other.pyd", "other.so"))
    ] + [f"sourcefx.native: holds {name}; {rule}" for name in initializers] + [
        f"sourcefx: holds {name}; {rule}" for name in initializers
    ]
    assert declaration_problems("shadow", container="sourcefx") == [
        f"sourcefx.shadow.contract: is loaded from contract.pyc; {rule}",
        *(f"sourcefx: holds {name}; {rule}" for name in initializers),
    ]


def test_a_module_outside_the_graph_reads_local_texts_too() -> None:
    # Codex, PR #6182: conftest.py or (private path) are scanned by `source_imports`, which
    # missed the function-local texts the consumer scan reads. Control: a name with no text.
    source = 'import importlib\ndef f():\n    T = "apps.lib.impl"\n    importlib.import_module(T)\n'
    source += "    importlib.import_module(OTHER)\n"
    found = source_imports(source, "conftest", False, {"apps.lib.impl"})
    assert [line for imported, line in found if imported == "apps.lib.impl"] == [4]


def test_owed_contracts_are_not_enforced_yet() -> None:
    """[if] a package owing a contract is enforced and still waived [then] the waiver outlives its debt, [else stop]."""
    assert not (set(OWED_CONTRACT) & _enforced()), "drop it from OWED_CONTRACT in the PR that enforces it"
    assert set(OWED_CONTRACT) <= _packages(), "an owed package left apps/: drop it from OWED_CONTRACT"
    assert not (set(OWED_CONTRACT) & PREDATES_ADR)
