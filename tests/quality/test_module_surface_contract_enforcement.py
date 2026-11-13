"""Split from tests/quality/test_module_surface_contract.py (#10941; behaviour unchanged).

Rule firing, ADR overshoot controls, and importlinter wiring.
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

# ----- the rule fires --------------------------------------------------------


def test_a_deep_import_of_an_enforced_package_is_a_violation() -> None:
    assert _check(("apps.ui.page", "apps.lib.impl")) == [Violation("deep-import", "apps.ui.page", "apps.lib.impl")]


def test_importing_the_bare_package_root_is_a_deep_import() -> None:
    assert [v.rule for v in _check(("apps.ui.page", "apps.lib"))] == ["deep-import"]


def test_scripts_are_consumers_too() -> None:
    assert [v.rule for v in _check(("scripts.tool", "apps.lib.impl"))] == ["deep-import"]


def test_scripts_are_composition_roots_and_may_build_a_module_through_http() -> None:
    assert _check(("scripts.tool", "apps.lib.http")) == []


def test_only_named_roots_outside_apps_get_the_http_allowance() -> None:
    assert [v.rule for v in _check(("tools.thing", "apps.lib.http"))] == ["deep-import"]


def test_composition_roots_has_no_default() -> None:
    # Sol, PR #6222: a caller that omits the roots must fail, not inherit `scripts` unseen.
    with pytest.raises(TypeError, match="composition_roots"):
        surface_violations([("scripts.tool", "apps.lib.http")], ["lib"], DELIVERY)  # type: ignore[call-arg]
    assert surface_violations([("scripts.tool", "apps.lib.http")], ["lib"], DELIVERY, composition_roots=[]) != []


def test_a_main_entry_point_is_a_composition_root() -> None:
    assert _check(("apps.genre.__main__", "apps.lib.http")) == []


def test_a_main_entry_point_still_may_not_deep_import() -> None:
    assert [v.rule for v in _check(("apps.genre.__main__", "apps.lib.query"))] == ["deep-import"]


def test_a_main_entry_point_outside_apps_is_not_a_root() -> None:
    # Sol, PR #6222: only named roots outside apps/ get the allowance, `__main__` or not.
    assert [v.rule for v in _check(("tools.__main__", "apps.lib.http"))] == ["deep-import"]


def test_a_module_merely_named_like_main_is_not_a_root() -> None:
    assert [v.rule for v in _check(("apps.genre.main_view", "apps.lib.http"))] == ["deep-import"]


def test_a_domain_package_may_not_import_another_modules_router() -> None:
    assert [v.rule for v in _check(("apps.analysis.x", "apps.lib.http"))] == ["deep-import"]


def test_a_contract_importing_its_own_implementation_is_impure() -> None:
    assert [v.rule for v in _check(("apps.lib.contract.models", "apps.lib.store"))] == ["impure-contract"]


def test_a_package_root_importing_its_implementation_is_impure() -> None:
    # Importing apps.lib.contract executes apps/lib/__init__.py first, so the root is held
    # to the same rule or the contract would drag the implementation in behind it.
    assert [v.rule for v in _check(("apps.lib", "apps.lib.store"))] == ["impure-contract"]


def test_a_contract_importing_its_own_package_root_is_impure() -> None:
    # Sol, PR #6182: the declaration gate reads only what a contract module defines, so a function
    # or value the root defines and the contract re-exports would cross the boundary unread.
    # Control: the root may still import its own contract (the allowed list below holds it).
    assert [v.rule for v in _check(("apps.lib.contract.models", "apps.lib"), enforced=("lib",))] == ["impure-contract"]
    assert [v.rule for v in _check(("apps.lib.contract", "apps.lib"))] == ["impure-contract"]


def test_a_contract_importing_a_third_party_library_is_impure() -> None:
    assert [v.rule for v in _check(("apps.lib.contract", "numpy"))] == ["impure-contract"]


def test_a_contract_importing_another_modules_implementation_is_impure() -> None:
    assert [v.rule for v in _check(("apps.lib.contract", "apps.other.impl"))] == ["impure-contract"]


def test_a_contract_importing_an_unenforced_modules_contract_is_impure() -> None:
    # Codex, PR #6182: importing apps.other.contract runs apps.other's root, which is scanned for
    # purity only when other is enforced, so an unenforced root could carry an implementation in.
    assert [v.rule for v in _check(("apps.lib.contract", "apps.other.contract"))] == ["impure-contract"]
    assert _check(("apps.lib.contract", "apps.other.contract"), enforced=("lib", "other")) == []


# ----- the rule stays quiet where the ADR allows (overshoot controls) ---------


def test_importing_the_contract_is_allowed() -> None:
    assert _check(("apps.ui.page", "apps.lib.contract"), ("apps.ui.page", "apps.lib.contract.models")) == []


def test_a_delivery_layer_may_import_the_router() -> None:
    assert _check(("apps.webui.server.app", "apps.lib.http"), ("apps.engine_core.app", "apps.lib.http.routes")) == []


def test_a_package_reaches_its_own_internals_freely() -> None:
    assert _check(("apps.lib.service", "apps.lib.store"), ("apps.lib.http", "apps.lib.service")) == []


def test_a_contract_may_import_stdlib_pydantic_and_other_contracts() -> None:
    allowed = [
        ("apps.lib.contract", "datetime"),
        ("apps.lib.contract", "typing"),
        ("apps.lib.contract", "__future__"),
        ("apps.lib.contract", "pydantic"),
        ("apps.lib.contract", "typing_extensions"),
        ("apps.lib.contract.models", "apps.other.contract"),
        ("apps.lib.contract.models", "apps.lib.contract.ids"),
        ("apps.lib", "apps.lib.contract"),
    ]
    assert _check(*allowed, enforced=("lib", "other")) == []


def test_unenforced_packages_are_untouched() -> None:
    assert _check(("apps.ui.page", "apps.other.impl"), ("apps.other", "apps.other.impl")) == []


def test_imports_from_outside_the_container_into_it_are_not_confused_with_it() -> None:
    assert _check(("applications.lib", "applications.lib.impl"), ("apps_extra.x", "apps_extra.y")) == []


# ----- the config is wired and complete ---------------------------------------


def test_the_contract_type_is_registered() -> None:
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS, encoding="utf-8")
    assert (
        "public_surface: scripts.arch_surface_contract.PublicSurfaceContract"
        in parser["importlinter"]["contract_types"]
    )
    assert _section()["type"] == "public_surface"
    assert _section()["delivery"].split() == DELIVERY
    # The whole allowance lives in the config: the adapter reads composition_roots from it.
    assert _section()["composition_roots"].split() == COMPOSITION_ROOTS


def test_every_production_package_is_in_the_graph() -> None:
    # A consumer the graph never reads can deep-import an enforced package unseen. The test
    # suite is the one importable root left out: it reaches into implementations on purpose.
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS, encoding="utf-8")
    roots = set(parser["importlinter"]["root_packages"].split())
    importable = {p.name for p in REPO.iterdir() if (p / "__init__.py").is_file()}
    assert "apps" in importable, "the scan found no packages: it is pointed at nothing"
    assert sorted(importable - {"tests"} - roots) == []


def test_the_contract_has_no_ignore_list() -> None:
    assert "ignore_imports" not in _section()


def test_every_enforced_name_is_a_real_package() -> None:
    assert _enforced() <= _packages()


def test_every_package_is_enforced_or_predates_the_adr() -> None:
    unlisted = _packages() - _enforced() - PREDATES_ADR - set(OWED_CONTRACT)
    assert unlisted == set(), f"new packages must be listed under `enforced` in .importlinter: {sorted(unlisted)}"


def test_every_python_directory_under_apps_is_a_package_or_named() -> None:
    # Codex, PR #6182: apps/foo/worker.py with no __init__.py imports as the namespace package
    # apps.foo, which the enforced-or-predates check above would never see.
    assert _namespace_dirs(REPO / "apps") == NOT_PACKAGES, "add an __init__.py and list it under `enforced`"


def test_apps_holds_no_top_level_module() -> None:
    # Codex, PR #6182: apps/foo.py is importable as apps.foo with no contract and no census entry.
    assert _top_level_modules(REPO / "apps") == set(), "make it a package with a contract and list it under `enforced`"


def test_every_importable_root_directory_is_in_the_graph_or_named() -> None:
    # Codex, PR #6182: a jobs/worker.py with no __init__.py imports as the namespace package
    # jobs, which test_every_production_package_is_in_the_graph never sees.
    assert _namespace_dirs(REPO) == ROOT_NOT_PACKAGES, "add an __init__.py and list it under `root_packages`"


def test_every_root_module_is_named() -> None:
    sources = {_module_name(REPO / m) for m in _top_level_modules(REPO) if Path(m).suffix in {".py", ".pyi"}}
    assert sources - ROOT_MODULES == set(), "move it into a package under `root_packages`"


def test_the_root_ships_no_compiled_module() -> None:
    # Sol, PR #6182: no gate reads what a compiled root module imports, so the root ships none, a
    # named one included: Codex, PR #6182, a ``conftest.pyd`` can shadow ``conftest.py``.
    assert unread_root_modules(REPO) == [], "build it under a package root, or have git ignore it"


def test_a_compiled_root_module_is_shipped_unless_git_ignores_it(tmp_path: Path) -> None:
    # Sol, PR #6182: a tracked ``_native.so`` at the root imports what no gate reads, so only build
    # output git ignores is left out. Controls: an ignored build, and a source root module; a
    # compiled shadow of an allowed source module (Codex) is reported all the same.
    exe = shutil.which("git")
    assert exe is not None, "git is not on PATH: the ignore filter cannot be exercised"
    git = [exe, "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    files = {".gitignore": "/_built*.so\n", "_built.so": "", "_tracked.so": "", "_loose.pyd": "", "conftest.py": ""}
    files["conftest.cp311-win_amd64.pyd"] = ""
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    subprocess.run([*git, "add", "-f", ".gitignore", "_tracked.so"], check=True)
    subprocess.run([*git, "commit", "-qm", "t"], check=True)
    (tmp_path / ".gitignore").write_text(
        "/_built*.so\n/_tracked.so\n", encoding="utf-8", newline="\n"
    )  # tracked wins over a rule
    assert unread_root_modules(tmp_path) == [
        ("_loose", "_loose.pyd"),
        ("_tracked", "_tracked.so"),
        ("conftest", "conftest.cp311-win_amd64.pyd"),
    ]


def test_code_outside_the_graph_reaches_enforced_packages_only_through_contracts() -> None:
    assert _outside_graph_sources(REPO), "the scan found no sources: it is pointed at nothing"
    assert _outside_graph_violations(REPO, _enforced()) == []


def _outside_graph_unread(repo: Path) -> list[tuple[str, str]]:
    """`unread_modules` under the namespace roots no graph root reads (``research/``): a shipped
    ``(private path)`` imports what neither the graph nor `_outside_graph_sources` reads."""
    return unread_modules(repo, sorted(_namespace_dirs(repo)))


def test_code_outside_the_graph_ships_no_module_held_only_compiled(tmp_path: Path) -> None:
    # Codex, PR #6182. Controls: a source-only namespace root lists nothing, and a graph root's
    # compiled file is the contract's to refuse, not this scan's.
    assert _namespace_dirs(REPO), "no namespace root found: the scan is pointed at nothing"
    assert _outside_graph_unread(REPO) == []
    files = {
        "(private path)": "",
        "(private path)": "",
        "jobs/deep/built.so": "",
        "notes/run.py": "",
        "scripts/__init__.py": "",
        "scripts/tool.so": "",
    }
    for name in files:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8", newline="\n")
    assert _outside_graph_unread(tmp_path) == [
        ("jobs.deep.built", "jobs/deep/built.so"),
        ("research.native", "(private path)"),
    ]


def test_code_outside_the_graph_is_read(tmp_path: Path) -> None:
    files = {
        "apps/__init__.py": "",
        "apps/lib/__init__.py": "",
        "apps/lib/contract/__init__.py": "",
        "apps/lib/store.py": "",
        "conftest.py": "import apps.lib.store\n",
        "jobs/worker.py": "from apps.lib.store import x\n",
        "jobs/deep/run.py": "from apps.lib import store\n",
        "ok/probe.py": "from apps.lib import contract\nimport apps.lib.contract\n",  # control
        "scripts/__init__.py": "",  # control: a graph root is the graph's to read
        "scripts/tool.py": "import apps.lib.store\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    assert _outside_graph_violations(tmp_path, {"lib"}) == [
        Violation("deep-import", "conftest", "apps.lib.store"),
        Violation("deep-import", "jobs.deep.run", "apps.lib.store"),
        Violation("deep-import", "jobs.worker", "apps.lib.store"),
    ]


def test_a_top_level_module_is_seen(tmp_path: Path) -> None:
    for name in ("foo.py", "bar.so", "__init__.py", "README.md"):  # the last two are controls
        (tmp_path / name).write_text("", encoding="utf-8", newline="\n")
    assert _top_level_modules(tmp_path) == {"foo.py", "bar.so"}


def test_a_directory_link_below_a_contract_is_a_finding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex, PR #6182: Python imports through contract/linked -> ../impl, but rglob never descends it.
    root = tmp_path / "linkfx"
    contract = root / "linked" / "contract"
    impl = root / "linked" / "impl"
    for package in (root, root / "linked", contract, impl):
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (contract / "__init__.py").write_text('CONTRACT_VERSION = "linked/1"\n', encoding="utf-8", newline="\n")
    (impl / "db.py").write_text("import sqlite3\nCONN = sqlite3.connect(':memory:')\n", encoding="utf-8", newline="\n")
    (contract / "plain").mkdir()  # control: a real directory is walked, not reported
    (contract / "plain" / "values.py").write_text("LIMIT = 3\n", encoding="utf-8", newline="\n")
    try:
        (contract / "via").symlink_to(impl, target_is_directory=True)
        (contract / "alias.py").symlink_to(contract / "plain" / "values.py")  # control: rglob lists a file link
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"this host cannot create a directory symlink: {exc}")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert declaration_problems("linked", container="linkfx") == [
        "linkfx.linked.contract: holds via/, a directory link these gates do not walk"
    ]


def test_a_namespace_package_is_seen(tmp_path: Path) -> None:
    (tmp_path / "foo").mkdir()
    (tmp_path / "foo" / "worker.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "pkg").mkdir()  # control: a real package
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "assets").mkdir()  # control: no Python at all
    (tmp_path / "assets" / "logo.svg").write_text("", encoding="utf-8", newline="\n")
    for found in ("ext/worker.so", "stale/worker.pyc", "deep/sub/worker.pyd"):  # Codex, PR #6182
        (tmp_path / found).parent.mkdir(parents=True)
        (tmp_path / found).write_text("", encoding="utf-8", newline="\n")
    for skipped in ("audio-engine/lib.so", "cache/__pycache__/x.pyc", "web/node_modules/x.py", "gen/out-dir/x.so"):
        (tmp_path / skipped).parent.mkdir(parents=True)  # controls: no importable path reaches them
        (tmp_path / skipped).write_text("", encoding="utf-8", newline="\n")
    assert _namespace_dirs(tmp_path) == {"foo", "ext", "stale", "deep"}


def test_the_frozen_list_names_only_packages_that_still_exist_and_are_not_enforced() -> None:
    assert _packages() >= PREDATES_ADR, "a package left apps/: drop it from PREDATES_ADR"
    assert not (PREDATES_ADR & _enforced()), "a migrated package is still in PREDATES_ADR: drop it there"
