"""The module public-surface rule (ADR-NEW-module-contracts) bites, and covers every new package.

`scripts.arch_surface.surface_violations` is the whole rule; `.importlinter` runs it through
`scripts.arch_surface_contract` on the real graph. These tests hold three things:

1. the rule fires on a deep import and on an impure contract, and stays quiet on the imports
   the ADR allows (each allowance has a control that breaks if the rule overshoots);
2. `.importlinter` registers the contract type and names real packages;
3. every package under apps/ is either enforced or in the frozen list of packages that
   predate the ADR, so a package added later is enforced from its first commit; a directory
   holding Python without an __init__.py (a namespace package) is refused unless named, and
   a top-level module (apps/foo.py) is refused outright.

[if] a package is added under apps/ and not to `enforced` [then] fail here, [else stop].
"""

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
# to `enforced`; test_owed_contracts_are_not_enforced_yet refuses the overlap. apps/autodj (#7681)
# cannot be enforced until apps/transition_score (#7708) stops reaching its planner modules.
# apps/plugins came in with #6522 (plugin store) and owes its contract, a Protocol over frozen
# models for the engine and webui importers, before it can be enforced.
OWED_CONTRACT = {"autodj": "#7681", "transition_score": "#7708", "plugins": "#6522"}


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
ROOT_NOT_PACKAGES = frozenset({"docs"})  # one-off probes run by path
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


# ----- type-checking imports are read from source (Codex, PR #6182) -------------

_TC_CONTRACT = """
from __future__ import annotations
from typing import TYPE_CHECKING
from pydantic import BaseModel
from . import ids
if TYPE_CHECKING:
    from apps.lib.store import Row
    from ..service import Impl
"""


def test_source_imports_keep_type_checking_imports_and_resolve_relative_ones() -> None:
    known = {"apps.lib.contract.ids", "apps.lib.store", "apps.lib.service"}
    got = {m for m, _ in source_imports(_TC_CONTRACT, "apps.lib.contract", True, known)}
    assert {"apps.lib.store", "apps.lib.service", "apps.lib.contract.ids", "pydantic", "typing"} <= got


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


def test_a_type_checking_import_in_a_contract_is_impure(tmp_path: Path) -> None:
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/contract/__init__.py": _TC_CONTRACT})
    pairs = pure_scope_imports(repo, ["lib"], {"apps.lib.contract.ids", "apps.lib.store", "apps.lib.service"})
    found = {(v.importer, v.imported) for v in _check(*pairs)}
    assert found == {("apps.lib.contract", "apps.lib.store"), ("apps.lib.contract", "apps.lib.service")}
    assert pairs[("apps.lib.contract", "apps.lib.store")] == [7]


def test_type_checking_imports_outside_the_contract_are_not_read(tmp_path: Path) -> None:
    # Overshoot control: the implementation may annotate with anything it likes.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/service.py": _TC_CONTRACT})
    assert set(pure_scope_imports(repo, ["lib"], set())) == set()


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


def test_a_dynamic_import_in_a_pure_scope_is_impure(tmp_path: Path) -> None:
    # Codex, PR #6182: neither the graph nor the AST reader saw a module loaded by a call.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": _DYNAMIC_ROOT, "apps/lib/contract/__init__.py": ""})
    pairs = pure_scope_imports(repo, ["lib"], set())
    found = {(v.imported, *pairs[(v.importer, v.imported)]) for v in _check(*pairs)}
    # Lines 11, 13 and 15 call a loader through a name the module bound it to (Codex, PR #6182).
    assert found == {
        ("apps.lib.store", 4),
        ("apps.other.impl", 6, 19),  # 19 runs it by its repository path (Codex, PR #6182)
        ("apps.other.alias", 11),
        ("apps.other.bound", 13),
        ("apps.other.again", 15),  # bound to a name that was itself bound to a loader
        ("apps.other.ran", 18),  # runpy runs a module by name (Codex, PR #6182)
        # Codex, PR #6182: a loader bound by an assignment expression (27, 28), and pkgutil's
        # resolver, which imports the module before the colon (31)
        ("apps.other.walrus", 27),
        ("apps.other.rebound", 28),
        ("apps.other.resolved", 31),
        # 12, 14 and 35 hand a loader on rather than call it, so what it loads is not read (Codex, PR #6182);
        # 21-25, 29 and 32 reach a loader by reflection (32 through any callee but a name or attribute);
        # 26 runs a path outside the repository; 36 and 37 bind an evaluator to a name
        (DYNAMIC, 5, 7, 8, 12, 14, 21, 22, 23, 24, 25, 26, 29, 32, 35, 36, 37),
        # Sol, PR #6182: the loader machinery itself, through which reflection reaches a loader
        ("importlib", 2, 3, 10),
        ("runpy", 17),
        ("pkgutil", 30),
    }
    # Controls: a literal stdlib name (line 9) is read like `import json`, and so is pure;
    # a call that loads nothing (line 16) is not read as a load because it names a module.
    assert pairs[("apps.lib", "json")] == [9]
    assert next(v for v in _check(*pairs) if v.imported == DYNAMIC).describe() == (
        "apps.lib: loads a module by a name no gate can read; a contract imports statically"
    )


def test_a_module_held_only_compiled_is_read_as_itself(tmp_path: Path) -> None:
    # Codex, PR #6182: with only ``helper.pyd`` beside it, ``from . import helper`` read as an
    # import of the root itself, since the graph knows only modules it reads from source; Windows
    # runs the extension. Controls: a name with no file, bytecode under __pycache__, and a file
    # whose name is no module's stay the package; a consumer's load and type-only import read it too.
    root = "from . import helper\nfrom . import fast\nfrom . import cached\nfrom . import nothing\n"
    root += "from . import contract\nfrom . import odd\n"
    files = {
        "apps/lib/__init__.py": root,
        "apps/lib/contract/__init__.py": "",
        "apps/lib/helper.pyd": "",
        "apps/lib/fast.cpython-311-x86_64-linux-gnu.so": "",
        "apps/lib/__pycache__/cached.cpython-311.pyc": "",
        "apps/lib/odd-name.pyd": "",
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": 'getattr(LIB, NAME)("apps.lib.helper")\nif TYPE_CHECKING:\n    from apps.lib import helper\n',
    }
    repo = _tree(tmp_path, files)
    assert compiled_modules(repo) == {"apps.lib.helper", "apps.lib.fast"}
    pairs = pure_scope_imports(repo, ["lib"], {"apps.lib.contract"})
    assert pairs == {
        ("apps.lib", "apps.lib.helper"): [1],
        ("apps.lib", "apps.lib.fast"): [2],
        ("apps.lib", "apps.lib"): [3, 4, 6],
        ("apps.lib", "apps.lib.contract"): [5],
    }
    found = {(v.importer, v.imported) for v in _check(*pairs)}
    assert found == {("apps.lib", "apps.lib.helper"), ("apps.lib", "apps.lib.fast")}
    consumer = {"apps.ui", "apps.ui.page"}
    assert loaded_imports(repo, consumer) == {("apps.ui.page", "apps.lib.helper"): [1]}
    assert statement_imports(repo, consumer) == {("apps.ui.page", "apps.lib.helper"): [3]}


def test_a_load_no_gate_can_read_fails_closed_outside_the_listed_loaders(tmp_path: Path) -> None:
    # Codex, PR #6182: each new way to build a load target (an alias of a wrapper, a spread into
    # it) read as an unresolved load, which the deep-import rule dropped, so the gate passed. An
    # unresolved load is now refused wherever it is, unless its module is a listed loader.
    consumer = (
        "import importlib\n"
        'def load(leaf):\n    importlib.import_module("apps.lib." + leaf)\n'
        'alias = load\nalias("impl")\nload(*("impl",))\n'
    )
    files = {"apps/ui/__init__.py": "", "apps/ui/page.py": consumer, "apps/ui/named.py": "import json\n"}
    known = {"apps.ui", "apps.ui.page", "apps.ui.named", "apps.lib.impl"}
    loads = call_loads(_tree(tmp_path, files), known)
    assert loads == {("apps.ui.page", DYNAMIC): [3]}
    assert loaded_imports(_tree(tmp_path, files), known) == {}  # the resolved view drops it
    pairs = [*loads, ("apps.lib", DYNAMIC), ("scripts.tool", DYNAMIC)]
    refused = Violation("dynamic-load", "apps.ui.page", DYNAMIC)
    # Codex, PR #6182: a script outside the container loads an implementation as surely as an
    # app does, so it is refused too. Controls: a pure scope's unresolved load is refused once,
    # as impure; a listed loader passes, in the container or out; a listed module that loads
    # nothing that way is stale, so the list only shrinks.
    impure = Violation("impure-contract", "apps.lib", DYNAMIC)
    script = Violation("dynamic-load", "scripts.tool", DYNAMIC)
    assert surface_violations(pairs, ["lib"], DELIVERY, composition_roots=COMPOSITION_ROOTS) == [
        refused,
        script,
        impure,
    ]
    listing = ["apps.ui.page", "apps.ui.named", "scripts.tool"]
    listed = surface_violations(pairs, ["lib"], DELIVERY, dynamic_loaders=listing, composition_roots=COMPOSITION_ROOTS)
    assert listed == [impure, Violation("stale-dynamic-loader", "apps.ui.named", DYNAMIC)]
    assert refused.describe() == (
        "apps.ui.page: loads a module by a name no gate can read; name the module in source,"
        " or list this module under dynamic_loaders"
    )


def test_code_run_by_a_qualified_or_aliased_evaluator_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``builtins.exec(code)`` ran text no reader sees, but only a bare ``exec``
    # call read as DYNAMIC. Controls: ``re.compile`` and ``model.eval()`` in a module that never
    # names builtins are no evaluator.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/bare.py": "exec(code)\n",
        "apps/ui/qualified.py": "import builtins\nbuiltins.exec(code)\n",
        "apps/ui/aliased.py": "import builtins as b\nb.eval(code)\n",
        "apps/ui/imported.py": "from builtins import exec as run\nrun(code)\n",
        "apps/ui/namespace.py": "__builtins__.compile(code, 'x', 'exec')\n",
        "apps/ui/controls.py": "import re\nre.compile(pattern)\nmodel.eval()\nimport os as builtins_\nbuiltins_.exec(c)\n",
        # Codex, PR #6182: the builtins module held under a name a load returns, or taken from
        # the module table. Which name holds it is not followed: in a module that can hold it at
        # all, every attribute call named like an evaluator is one. Control: a module that
        # imports sys but never reaches its module table.
        "apps/ui/loaded.py": 'b = __import__("builtins")\nb.exec(code)\n',
        "apps/ui/table.py": "import sys\nm = sys.modules[k]\nm.eval(code)\n",
        "apps/ui/globals.py": 'g = f.__globals__["__builtins__"]\ng.exec(code)\n',
        "apps/ui/plain_sys.py": "import sys\nimport re\nsys.exit(re.compile(p))\nmodel.eval()\n",
        # Control: ``re`` holds only the re module, though this module reaches builtins; but a
        # name an import binds and something else rebinds may hold anything, and so may a chain.
        "apps/ui/regex.py": "import sys, re\nm = sys.modules\nre.compile(p)\n",
        "apps/ui/rebound.py": 'import sys, re\nm = sys.modules\nre = m["builtins"]\nre.eval(code)\n',
        "apps/ui/chained.py": 'import sys\nsys.modules["builtins"].exec(code)\n',
        "apps/ui/attr_chain.py": "import sys, os\nm = sys.modules\nos.held.exec(code)\n",
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair[0]: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        "apps.ui.bare": [1],
        "apps.ui.qualified": [2],
        "apps.ui.aliased": [2],
        "apps.ui.imported": [2],
        "apps.ui.namespace": [1],
        "apps.ui.loaded": [2],
        "apps.ui.table": [2, 3],  # the module table read by a key no reader folds, or handed on, too
        "apps.ui.globals": [2],
        "apps.ui.regex": [2],
        "apps.ui.rebound": [2, 4],
        "apps.ui.chained": [2],
        "apps.ui.attr_chain": [2, 3],
    }


def test_a_load_through_a_name_bound_only_to_text_is_read(tmp_path: Path) -> None:
    # Codex, PR #6182: ``target = "json"; import_module(target)`` inside a function names its
    # module as plainly as a literal, but read as DYNAMIC and was refused. Controls: a name also
    # bound some other way (reassigned from input, a loop target, a parameter, ``+=``) may hold
    # anything and stays DYNAMIC.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/local.py": 'import importlib\ndef f():\n    target = "json"\n    importlib.import_module(target)\n',
        "apps/ui/deep.py": (
            "import importlib\n"
            'def f(on):\n    if on:\n        t = "apps.lib.impl"\n    else:\n        t = "json"\n'
            "    importlib.import_module(t)\n"
        ),
        "apps/ui/mixed.py": 'import importlib\nt = "json"\nt = input()\nimportlib.import_module(t)\n',
        "apps/ui/looped.py": 'import importlib\nfor t in names():\n    importlib.import_module(t)\nt = "json"\n',
        "apps/ui/param.py": "import importlib\ndef f(t):\n    importlib.import_module(t)\nt = 'json'\n",
        "apps/ui/grown.py": 'import importlib\nt = "apps"\nt += suffix\nimportlib.import_module(t)\n',
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if "__init__" not in f), "apps.lib.impl"}
    loads = call_loads(_tree(tmp_path, files), known)
    # ``json`` is no module of the repository, so a load of it is no pair at all.
    assert {pair: lines for pair, lines in loads.items() if pair[0] in {"apps.ui.local", "apps.ui.deep"}} == {
        ("apps.ui.deep", "apps.lib.impl"): [7],
    }
    assert sorted(pair[0] for pair in loads if pair[1] == DYNAMIC) == [
        "apps.ui.grown",
        "apps.ui.looped",
        "apps.ui.mixed",
        "apps.ui.param",
    ]


def test_a_consumer_load_by_reflection_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``getattr(importlib, name)(target)`` reaches a loader by reflection, which
    # only the pure-scope reader refused; a consumer's read nothing. Controls: a computed callee
    # in a module that only reads attributes off a loader module (or off anything imported from
    # one), and a computed callee on a module no loader lives in, are no load. A dunder read off a
    # loader module (``b.__dict__[name]``) hands out its namespace, so it holds the module loosely;
    # a loader's own name (``b.__import__``) and a namespace of another module do not.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/reflect.py": "import importlib\ngetattr(importlib, loader)(target)\n",
        "apps/ui/named.py": 'import importlib as il\nf = getattr(il, "import_module")\n',
        "apps/ui/handed.py": "import importlib\nload_with(importlib.import_module)\n",
        "apps/ui/table.py": 'import importlib\nimportlib.import_module("json")\nHANDLERS[kind](row)\n',
        "apps/ui/resources.py": 'from importlib import resources\nresources.files("x")\nTABLE[k]()\n',
        "apps/ui/ctypes_.py": 'import ctypes\ngetattr(ctypes, "WinDLL")("kernel32")\n',
        "apps/ui/loose.py": "from importlib import util\nkeep(util)\nTABLE[k](name)\n",
        # Codex, PR #6182: a loader module taken from a load or the module table, not an import.
        # Control: a module that names no loader module as text and never reaches the table.
        "apps/ui/loaded.py": 'm = __import__("importlib")\nm.__dict__[loader](target)\n',
        "apps/ui/tabled.py": "import sys\nm = sys.modules[k]\nTABLE[m](target)\n",
        "apps/ui/texted.py": 'import json\njson.loads("{}")\nTABLE[k](row)\n',
        "apps/ui/notsys.py": "import config\nconfig.modules\nTABLE[k](row)\n",
        "apps/ui/spaced.py": 'import builtins as b\nb.__dict__["__im" + "port__"](target)\n',
        "apps/ui/deep.py": "import importlib\nimportlib.util.__dict__[k](target)\n",
        "apps/ui/own.py": 'import builtins as b\nb.__import__("json")\nTABLE[k](row)\n',
        "apps/ui/other.py": "import json\njson.__dict__[k](row)\n",
    }
    known = {"apps.ui", *(f"apps.ui.{Path(f).stem}" for f in files if f.endswith(".py") and "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("apps.ui.reflect", DYNAMIC): [2],
        ("apps.ui.named", DYNAMIC): [2],
        ("apps.ui.handed", DYNAMIC): [2],
        ("apps.ui.loose", DYNAMIC): [2, 3],  # importlib.util handed on (`_machinery_handed`)
        ("apps.ui.spaced", DYNAMIC): [2],
        ("apps.ui.deep", DYNAMIC): [2],
        ("apps.ui.loaded", DYNAMIC): [2],
        ("apps.ui.tabled", DYNAMIC): [2, 3],
    }


def test_a_loader_name_only_tested_or_resolved_is_no_load(tmp_path: Path) -> None:
    # Sol, PR #6182: a gate's own table of loader names, and importlib.util.resolve_name (which
    # imports nothing, unlike pkgutil's), read as loads, so a module listed only for them kept its
    # dynamic_loaders entry alive. Controls: the same text handed to a lookup, a table read other
    # than by ``in``, pkgutil's resolve_name, resolve_name off a rebound or aliased importlib, and
    # another importlib.util member bound to a loader's name stay loads.
    files = {
        "scripts/__init__.py": "",
        "scripts/tested.py": (
            'LOADERS = frozenset({"import_module", "run_path"})\n'
            'def f(node):\n    return node.attr in LOADERS or node.attr == "exec_module" or node in {"load_module"}\n'
        ),
        "scripts/resolves.py": (
            "import importlib\nimport importlib.util\nfrom importlib.util import resolve_name as rn\n"
            "importlib.util.resolve_name(name, pkg)\nrn(name, pkg)\n"
        ),
        "scripts/iterated.py": 'NAMES = {"import_module"}\ndef f(m):\n    for n in NAMES:\n        getattr(m, n)(x)\n',
        "scripts/handed.py": 'def f(m):\n    return getattr(m, "import_module")\n',
        "scripts/copied.py": 'NAMES = {"import_module"}\nALL = set(NAMES)\n',
        "scripts/pkg.py": "import pkgutil\npkgutil.resolve_name(name)\n",
        "scripts/rebound.py": "import importlib.util\nimportlib = pkgutil\nimportlib.util.resolve_name(name, pkg)\n",
        "scripts/aliased.py": "import pkgutil as importlib\nimportlib.util.resolve_name(name, pkg)\n",
        "scripts/renamed.py": "from importlib.util import find_spec as load_module\nload_module(name)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.iterated", DYNAMIC): [1],
        ("scripts.handed", DYNAMIC): [2],
        ("scripts.copied", DYNAMIC): [1],
        ("scripts.pkg", DYNAMIC): [2],
        ("scripts.rebound", DYNAMIC): [3],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.renamed", DYNAMIC): [2],
    }


def test_pydoc_locate_loads_the_module_its_object_path_names(tmp_path: Path) -> None:
    # Codex, PR #6182: ``pydoc.locate("apps.other.impl.run")`` imports apps.other.impl to find
    # ``run``, but no reader knew pydoc's loader. A dotted object path given to a loader now loads its
    # longest module prefix (pkgutil's resolver walks it the same way without a colon). Controls:
    # ``locate`` is no loader where pydoc cannot be held (``apps.reconcile.locate`` is a module, and a
    # CLI command may be named "locate"), a path no module prefixes stays as written, and a module
    # that only compares a text to "pydoc" holds no pydoc.
    files = {
        "apps/__init__.py": "",
        "apps/other/__init__.py": "",
        "apps/other/impl.py": "",
        "scripts/__init__.py": "",
        "scripts/located.py": 'import pydoc\npydoc.locate("apps.other.impl.run")\n',
        "scripts/walked.py": 'import pkgutil\npkgutil.resolve_name("apps.other.impl.run")\n',
        "scripts/aliased.py": "from pydoc import locate as find\nfind(name)\n",
        "scripts/bare.py": "from pydoc import locate\nlocate(name)\n",
        "scripts/reflected.py": 'import pydoc\nf = getattr(pydoc, "locate")\n',
        "scripts/texted.py": 'm = __import__("pydoc")\nm.locate(name)\n',
        "scripts/outside.py": 'import pydoc\npydoc.locate("json.dumps")\n',
        "scripts/other.py": 'from apps.other import impl as locate\nlocate.run()\ncli("locate", locate)\n',
        # Codex, PR #6182: every pydoc entry point resolves a dotted text by importing it
        "scripts/rendered.py": 'import pydoc as p\np.render_doc("apps.other.impl.run")\np.resolve(thing)\n',
        "scripts/member.py": 'from pydoc import render_doc as show\nshow("apps.other.impl.run")\n',
        "scripts/named.py": 'm = __import__("pydoc")\nm.render_doc("apps.other.impl.run")\n',
        "scripts/tested.py": 'def f(name):\n    return name == "pydoc" and render_doc(name) and log.info(name)\n',
    }
    known = {"apps", "apps.other", "apps.other.impl", "scripts"} | {
        f"scripts.{Path(f).stem}" for f in files if f.startswith("scripts/") and "__init__" not in f
    }
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] in {DYNAMIC, "apps.other.impl"}} == {
        ("scripts.located", "apps.other.impl"): [2],
        ("scripts.walked", "apps.other.impl"): [2],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.bare", DYNAMIC): [2],
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.texted", DYNAMIC): [2],
        ("scripts.rendered", "apps.other.impl"): [2],
        ("scripts.rendered", DYNAMIC): [3],
        ("scripts.member", "apps.other.impl"): [2],
        ("scripts.named", "apps.other.impl"): [2],
    }
    assert loads[("scripts.outside", "json.dumps")] == [2]


def test_a_loader_taken_from_a_scope_namespace_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``globals()["import" + "_module"](target)`` calls an imported loader that no
    # name in the source reads. A module that imports from a loader module and reads a whole
    # scope's namespace holds the loader loosely. Controls: the same read in a module that imports
    # no loader module, and an imported loader module read only by attribute, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/scoped.py": 'from importlib import import_module\nglobals()["import" + "_module"](target)\n',
        "scripts/local.py": "import importlib\ndef f():\n    return locals()[k](target)\n",
        "scripts/framed.py": "import runpy\nimport sys\nsys._getframe().f_globals[k](target)\n",
        "scripts/plain.py": "import json\nglobals()[k](row)\n",
        "scripts/named.py": "import importlib\nimportlib.util.decode_source(data)\nHANDLERS[k](row)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.scoped", DYNAMIC): [2],
        ("scripts.local", DYNAMIC): [3],
        ("scripts.framed", DYNAMIC): [3],
    }


def test_an_entry_point_load_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``EntryPoint(name, value, group).load()`` imports the ``module:attr`` text
    # it carries, runtime data no reader can see. Where importlib.metadata (or its backport) can be
    # held, a bare ``.load()`` reads as DYNAMIC. Controls: a ``.load()`` where no entry point can be
    # held, a load handed an argument, a load read straight off an imported module, and a text
    # only compared to "importlib.metadata" are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/built.py": (
            'from importlib.metadata import EntryPoint\nEntryPoint(name="x", value=value, group="g").load()\n'
        ),
        "scripts/listed.py": (
            'import importlib.metadata\nfor ep in importlib.metadata.entry_points(group="g"):\n    ep.load()\n'
        ),
        "scripts/backport.py": "import importlib_metadata\nplugin.load()\n",
        "scripts/texted.py": 'md = __import__("importlib.metadata")\nplugin.load()\n',
        "scripts/store.py": "STORE.load()\n",
        "scripts/argued.py": "import importlib.metadata\nimport json\njson.load(fh)\nstore.load(path)\n",
        "scripts/offmod.py": "import importlib.metadata\nimport cache\ncache.load()\n",
        "scripts/compared.py": 'def f(name):\n    return name == "importlib.metadata" and store.load()\n',
        # Codex, PR #6182: the loader handed on before it is called
        "scripts/handed.py": "import importlib.metadata\nload = ep.load\nload()\n",
        "scripts/offhand.py": "import importlib.metadata\nimport json\nread = json.load\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.built", DYNAMIC): [2],
        ("scripts.listed", DYNAMIC): [3],
        ("scripts.backport", DYNAMIC): [2],
        ("scripts.texted", DYNAMIC): [2],
        ("scripts.handed", DYNAMIC): [2],
    }


def test_an_evaluator_named_as_text_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run = getattr(builtins, "exec")`` then ``run(code)`` runs code no reader
    # sees. Where builtins can be held, an evaluator's name as text reads as DYNAMIC, as a
    # loader's does. Controls: the same text where builtins cannot be held (a ``compile`` mode),
    # and one only compared, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/reflected.py": 'import builtins\nrun = getattr(builtins, "exec")\nrun(code)\n',
        "scripts/spaced.py": 'b = __builtins__\nrun = b.get("eval")\n',
        "scripts/mode.py": 'MODE = "exec"\ndef f(tree):\n    return parse(tree, MODE)\n',
        "scripts/compared.py": 'import builtins\ndef f(name):\n    return name == "exec"\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.spaced", DYNAMIC): [1, 2],
    }


def test_import_machinery_calls_are_loaders(tmp_path: Path) -> None:
    # Codex, PR #6182: ``files(target)`` from importlib.resources imports ``target``, as does
    # ``find_spec`` for a dotted name's parents. Every call into the import machinery reads as a
    # load: text names its module, and any argument that is not text is DYNAMIC. Controls: calls the machinery makes
    # without importing (a distribution's version, decoding source bytes, listing a path's
    # modules) are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/resourced.py": "from importlib.resources import files\nfiles(target)\n",
        "scripts/named.py": 'import importlib.resources\nimportlib.resources.files("scripts.other")\n',
        "scripts/probed.py": "import importlib.util\nimportlib.util.find_spec(name)\n",
        "scripts/anchored.py": 'import importlib.util\nimportlib.util.find_spec("scripts.other", package)\n',
        "scripts/versioned.py": "import importlib.metadata\nimportlib.metadata.version(name)\n",
        "scripts/decoded.py": "import importlib.util\nimportlib.util.decode_source(data)\n",
        "scripts/listed.py": "import pkgutil\npkgutil.iter_modules(path)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.resourced", DYNAMIC): [2],
        ("scripts.named", "scripts.other"): [2],
        ("scripts.probed", DYNAMIC): [2],
        ("scripts.anchored", "scripts.other"): [2],
        ("scripts.anchored", DYNAMIC): [2],
    }


def test_a_module_table_lookup_is_a_load(tmp_path: Path) -> None:
    # Codex, PR #6182: ``sys.modules["pkg.mod"]`` hands out a module another module imported,
    # with no import statement or loader call, and so does the table of a ``sys`` a loader
    # returns (``__import__("sys").modules``); ``sys`` handed on is DYNAMIC. A lookup by text is an edge to that module, a
    # lookup by any other key or any other read of the table is DYNAMIC. Controls: a membership
    # test, the module's own entry, writing or deleting an entry, a key no module answers, and
    # one named member of ``sys`` are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/keyed.py": 'import sys\nsys.modules["scripts.other"].run()\n',
        "scripts/fetched.py": 'import sys\nsys.modules.get("scripts.other")\nsys.modules.get(name)\n',
        "scripts/imported.py": 'from sys import modules\nmodules["scripts.other"]\n',
        "scripts/handed.py": 'import sys\nlookup("scripts.other", sys.modules.get)\n',
        "scripts/loaded.py": '__import__("sys").modules["scripts.other"]\n',
        "scripts/imported_sys.py": 'import importlib\nimportlib.import_module("sys").modules.get(name)\n',
        "scripts/reflected.py": 'import sys\ngetattr(sys, "modules")["scripts.other"]\n',
        "scripts/aliased.py": "import sys\ns = sys\ns.modules\n",
        "scripts/dunder.py": 'import sys\nsys.__dict__["modules"]\n',
        "scripts/argv.py": 'import sys\n__import__("sys").argv\nsys.argv\n',
        "scripts/walked.py": "import sys\nfor m in sys.modules.values():\n    m.run()\n",
        "scripts/member.py": 'import sys\nif "scripts.other" in sys.modules:\n    pass\n',
        "scripts/own.py": "import sys\nsys.modules[__name__]\n",
        "scripts/written.py": "import sys\nsys.modules[k] = v\ndel sys.modules[k]\n",
        "scripts/popped.py": 'import sys\nsys.modules.pop("rbox", None)\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    expected = {
        ("scripts.keyed", "scripts.other"): [2],
        ("scripts.fetched", "scripts.other"): [2],
        ("scripts.fetched", DYNAMIC): [3],
        ("scripts.imported", "scripts.other"): [2],
        ("scripts.handed", "scripts.other"): [2],  # text naming a module is read wherever it is
        ("scripts.handed", DYNAMIC): [2],
        ("scripts.walked", DYNAMIC): [2],
        ("scripts.loaded", "scripts.other"): [1],
        ("scripts.imported_sys", DYNAMIC): [2],
        ("scripts.reflected", DYNAMIC): [2],
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.dunder", DYNAMIC): [2],
    }
    # A loader handed ``sys`` loads ``sys`` too; that edge leaves the tree.
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1] and pair[1] != "sys"} == expected
    keyed = files["scripts/keyed.py"]
    assert sorted(source_imports(keyed, "scripts.keyed", False, known)) == [("scripts.other", 2), ("sys", 1)]


def test_a_pickle_reader_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: unpickling imports every module the stream names, so ``pickle.loads(payload)``
    # can load ``apps.x.impl``. Every call into a pickle reader, or one handed on, is DYNAMIC,
    # whatever it is handed, since even literal bytes name modules no reader parses. A part of the
    # machinery imported in two functions is held as one import would hold it. Controls: writing
    # a pickle, and a name ``pickle`` no import binds, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/loaded.py": "import pickle\npickle.loads(payload)\n",
        "scripts/literal.py": 'import pickle\npickle.loads(b"cscripts.other\\nrun\\n.")\n',
        "scripts/reader.py": "from pickle import Unpickler\nUnpickler(f).load()\n",
        "scripts/shelved.py": 'import shelve\nshelve.open("db")\n',
        "scripts/model.py": "def a():\n    import joblib\n    joblib.dump(m, p)\ndef b():\n    import joblib\n    joblib.load(p)\n",
        "scripts/probed.py": "import importlib.util\nimport importlib.metadata\nimportlib.util.find_spec(name)\n",
        "scripts/handed.py": "import pickle\nread = pickle.load\n",
        "scripts/written.py": "import pickle\npickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)\n",
        "scripts/local.py": "pickle = make()\npickle.loads(payload)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.loaded", DYNAMIC): [2],
        ("scripts.literal", DYNAMIC): [2],
        ("scripts.reader", DYNAMIC): [2],
        ("scripts.shelved", DYNAMIC): [2],
        ("scripts.model", DYNAMIC): [6],
        ("scripts.probed", DYNAMIC): [3],
        ("scripts.handed", DYNAMIC): [2],
    }


def test_the_interactive_interpreter_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``code.InteractiveInterpreter().runsource(text)`` runs text as code, as
    # ``exec`` does, through an interpreter object no reader follows. Every call into ``code`` or
    # ``codeop``, or either handed on, reads as DYNAMIC. Control: a name ``code`` no import binds.
    files = {
        "scripts/__init__.py": "",
        "scripts/sourced.py": "import code\ncode.InteractiveInterpreter().runsource(src)\n",
        "scripts/console.py": "from code import InteractiveConsole\nInteractiveConsole().push(src)\n",
        "scripts/compiled.py": "import codeop\ncodeop.compile_command(src)\n",
        "scripts/handed.py": "import code\nrun = code.interact\n",
        "scripts/local.py": "code = make()\ncode.run(src)\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.sourced", DYNAMIC): [2],
        ("scripts.console", DYNAMIC): [2],
        ("scripts.compiled", DYNAMIC): [2],
        ("scripts.handed", DYNAMIC): [2],
    }


def test_a_module_named_inside_a_display_is_loaded(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run([sys.executable, "-m", "apps.x.impl"])`` names its module inside a
    # list, inline or bound to a name. Every item a display holds, at any depth, is read as an
    # argument is. Controls: texts that name no module, and a path in a display handed to a
    # keyword no loader takes a path by, are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/argv.py": 'import sys\nrun([sys.executable, "-m", "scripts.other"])\n',
        "scripts/bound.py": 'import sys\nCMD = (sys.executable, "-m", "scripts.other")\nrun(CMD)\n',
        "scripts/nested.py": 'run(env={"M": ["scripts.other"]})\n',
        "scripts/spread.py": 'TAIL = ["scripts.other"]\nrun(["-m", *TAIL])\n',
        "scripts/summed_names.py": 'TAIL = ["scripts.other"]\nrun(base + TAIL)\n',
        "scripts/pathed.py": 'import sys\nrun([sys.executable, "scripts/other.py"])\n',
        "scripts/plain.py": 'run(["ls", "-l"])\n',
        "scripts/listed.py": 'report(files=["scripts/other.py"])\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == "scripts.other"} == {
        ("scripts.argv", "scripts.other"): [2],
        ("scripts.bound", "scripts.other"): [3],
        ("scripts.nested", "scripts.other"): [1],
        ("scripts.spread", "scripts.other"): [2],
        ("scripts.summed_names", "scripts.other"): [2],
        ("scripts.pathed", "scripts.other"): [2],
    }


def test_an_interpreter_run_of_an_unread_operand_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``run([sys.executable, "-m", target])`` runs whatever ``target`` holds. An
    # ``-m`` or ``-c`` whose operand no reader folds, or with nothing after it in its display, is
    # DYNAMIC, in a display or as the call's own arguments, where an item before it names an
    # interpreter. ``"-mmod"`` and a command line name their module, and a command line built at
    # run time (an f-string, ``+``, ``%`` or ``.format``) with an unread operand is DYNAMIC. An argv
    # grown by ``+=``, ``extend``, ``append`` or ``insert`` is read whole. Controls: a folded operand is
    # its edge alone; ``-m`` as a word inside other text, another option with an unread operand,
    # ``git log -m``, an option with no program before it, a record's prose, and the DYNAMIC
    # marker's own text are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/other.py": "",
        "scripts/target.py": 'import sys\nrun([sys.executable, "-m", target])\n',
        "scripts/code.py": 'import sys\nrun((sys.executable, "-c", code))\n',
        "scripts/spawned.py": 'import os\nos.spawnl(mode, python, python, "-m", target)\n',
        "scripts/trailing.py": 'import sys\nrun([sys.executable, "-m"] + rest)\n',
        "scripts/keyword.py": 'run(args=["/usr/bin/python3", "-m", target])\n',
        "scripts/git.py": 'run([git, "log", "-m", span])\n',
        "scripts/flagged.py": 'word.startswith("-m")\n',
        "scripts/record.py": 'Surface(entrypoint="python -m scripts.other --live")\n',
        "scripts/cmdkw.py": 'run(cmd="python -m scripts.other")\n',
        "scripts/marker.py": 'note("<dynamic import>")\n',
        "scripts/fstring.py": 'import os\nos.system(f"python -m {target}")\n',
        "scripts/added.py": 'run("python3 -c " + code, shell=True)\n',
        "scripts/percent.py": 'run(cmd="%s -m %s" % (exe, target))\n',
        "scripts/formatted.py": 'run("python -m{}".format(target))\n',
        "scripts/folded_f.py": 'BASE = "scripts"\nos.system(f"python -m {BASE}.other")\n',
        "scripts/git_f.py": 'run(f"{git} log -m {span}")\n',
        "scripts/prose_f.py": 'Surface(entrypoint=f"python -m {target}")\n',
        "scripts/shell_c.py": "import os\nos.system(\"python -c 'import scripts.other'\")\n",
        "scripts/shell_true.py": "run('python3 -c \"from scripts import other\"', shell=True)\n",
        "scripts/quoted_c.py": "os.system(\"python '-cimport scripts.other'\")\n",
        "scripts/grown.py": 'import sys\ncmd = [sys.executable]\ncmd += ["-m", target]\nrun(cmd)\n',
        "scripts/extended.py": 'import sys\ncmd = [sys.executable]\ncmd.extend(["-c", code])\nrun(cmd)\n',
        "scripts/appended.py": 'cmd = ["python3"]\ncmd.append("-m")\ncmd.append(target)\nrun(cmd)\n',
        "scripts/inserted.py": 'cmd = ["-m", "scripts.other"]\ncmd.insert(0, "python3")\nrun(cmd)\n',
        "scripts/front.py": 'import sys\ncmd = ["-m", target]\ncmd.insert(0, sys.executable)\nrun(cmd)\n',
        "scripts/placed.py": 'import sys\ncmd = [sys.executable]\ncmd.insert(1, "-m")\ncmd.insert(2, target)\nrun(cmd)\n',
        "scripts/placed_back.py": 'import sys\ncmd = [sys.executable, "x", target]\ncmd.insert(-1, "-m")\nrun(cmd)\n',
        "scripts/placed_anywhere.py": 'import sys\ncmd = [sys.executable]\ncmd.insert(i, "-m")\ncmd.insert(j, target)\nrun(cmd)\n',
        "scripts/placed_front.py": 'import sys\ncmd = ["-m", target]\ncmd.insert(i, sys.executable)\nrun(cmd)\n',
        "scripts/placed_first.py": 'import sys\ncmd = [target, sys.executable]\ncmd.insert(0, "-m")\nrun(cmd)\n',
        "scripts/quoted_f.py": 'import sys\nrun(f\'"{sys.executable}" "-m" "{target}"\', shell=True)\n',
        "scripts/quoted_m.py": 'os.system(\'python "-m" "scripts.other"\')\n',
        "scripts/shadowed.py": 'import sys\ntarget = "json"\ndef launch(target):\n    run([sys.executable, "-m", target])\n',
        "scripts/constant.py": 'import sys\nMOD = "scripts.other"\nrun([sys.executable, "-m", MOD])\n',
        "scripts/summed.py": 'import sys\ncmd = [sys.executable] + ["-m", target]\nrun(cmd)\n',
        "scripts/based.py": 'import sys\nPY = [sys.executable, "-m"]\nrun([*PY, "scripts.other"])\nrun(PY + ["scripts.other"])\n',
        "scripts/spread_runtime.py": 'import sys\nB = ["-m", target]\nrun([sys.executable, *B])\n',
        "scripts/grown_git.py": 'cmd = ["git", "log"]\ncmd += ["-m", span]\nrun(cmd)\n',
        "scripts/joined_c.py": "run(['python3', '-cimport scripts.other'])\n",
        "scripts/unclosed.py": 'os.system("python -c \'import scripts.other")\n',
        "scripts/joined.py": 'run([exe, "-mscripts.other"])\n',
        "scripts/shell.py": 'os.system("python -m scripts.other --fast")\n',
        "scripts/folded.py": 'run([exe, "-m", "scripts.other"])\n',
        "scripts/coded.py": 'import sys\nrun([sys.executable, "-c", "import scripts.other"])\n',
        "scripts/held_code.py": 'CMD = ("python3", "-c", "from scripts import other")\nrun(CMD)\n',
        "scripts/worded.py": 'log("rerun with -m and a module")\n',
        "scripts/other_option.py": 'run([exe, "-W", level])\n',
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[0] != pair[1]} == {
        ("scripts.target", DYNAMIC): [2],
        ("scripts.code", DYNAMIC): [2],
        ("scripts.spawned", DYNAMIC): [2],
        ("scripts.trailing", DYNAMIC): [2],
        ("scripts.keyword", DYNAMIC): [1],
        ("scripts.cmdkw", "scripts.other"): [1],
        ("scripts.fstring", DYNAMIC): [2],
        ("scripts.added", DYNAMIC): [1],
        ("scripts.percent", DYNAMIC): [1],
        ("scripts.formatted", DYNAMIC): [1],
        ("scripts.folded_f", "scripts.other"): [2],
        ("scripts.shell_c", "scripts.other"): [2],
        ("scripts.shell_true", "scripts.other"): [1],
        ("scripts.joined_c", "scripts.other"): [1],
        ("scripts.grown", DYNAMIC): [4],
        ("scripts.extended", DYNAMIC): [4],
        ("scripts.appended", DYNAMIC): [4],
        ("scripts.inserted", "scripts.other"): [3],
        ("scripts.summed", DYNAMIC): [3],
        ("scripts.front", DYNAMIC): [4],
        ("scripts.shadowed", DYNAMIC): [4],
        ("scripts.constant", "scripts.other"): [3],
        ("scripts.placed", DYNAMIC): [5],
        ("scripts.placed_back", DYNAMIC): [4],
        ("scripts.placed_anywhere", DYNAMIC): [5],
        ("scripts.placed_front", DYNAMIC): [4],
        ("scripts.quoted_f", DYNAMIC): [2],
        ("scripts.quoted_m", "scripts.other"): [1],
        ("scripts.based", "scripts.other"): [3, 4],
        ("scripts.spread_runtime", DYNAMIC): [3],
        ("scripts.quoted_c", "scripts.other"): [1],
        ("scripts.joined", "scripts.other"): [1],
        ("scripts.shell", "scripts.other"): [1],
        ("scripts.folded", "scripts.other"): [1],
        ("scripts.coded", "scripts.other"): [2],
        ("scripts.held_code", "scripts.other"): [2],
    }


def test_import_machinery_handed_on_is_dynamic(tmp_path: Path) -> None:
    # Codex, PR #6182: ``load = files`` then ``load(target)`` calls the machinery through a name no
    # import binds. A loading part of the machinery read anywhere but as a callee reads as DYNAMIC
    # there. Controls: a non-loading part handed on, a loading part called, and a name the
    # machinery binds twice (no longer known to hold it) are no load.
    files = {
        "scripts/__init__.py": "",
        "scripts/aliased.py": "from importlib.resources import files\nload = files\nload(target)\n",
        "scripts/unpacked.py": "import importlib.util\n(probe,) = (importlib.util.find_spec,)\n",
        "scripts/passed.py": "import importlib\nrun(importlib.resources.files, name)\n",
        "scripts/module.py": "import pkgutil as p\nlib = p\n",
        "scripts/version.py": "import importlib.metadata\nread = importlib.metadata.version\n",
        "scripts/decode.py": "import importlib.util\nkeep(importlib.util.decode_source)\n",
        "scripts/called.py": 'from importlib.resources import files\nfiles("json")\n',
        "scripts/rebound.py": "from importlib.resources import files\nfiles = make()\nload = files\n",
    }
    known = {"scripts", *(f"scripts.{Path(f).stem}" for f in files if "__init__" not in f)}
    loads = call_loads(_tree(tmp_path, files), known)
    assert {pair: lines for pair, lines in loads.items() if pair[1] == DYNAMIC} == {
        ("scripts.aliased", DYNAMIC): [2],
        ("scripts.unpacked", DYNAMIC): [2],
        ("scripts.passed", DYNAMIC): [2],
        ("scripts.module", DYNAMIC): [2],
    }


def test_this_tree_lists_every_dynamic_loader_it_has() -> None:
    listed = _section()["dynamic_loaders"].split()
    assert listed, "no loader listed: the setting is read from the wrong place"
    assert all(m.split(".")[0] in {"apps", "scripts", "ops"} for m in listed)


def test_a_shipped_module_held_only_compiled_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Codex, PR #6182: a consumer held only as ``native.pyd`` imports what no gate reads, so it
    # could import any implementation unseen. Controls: build output git ignores never ships and
    # passes; a tracked file stays refused though an ignore rule matches it; where git cannot
    # answer (no repository here yet, or no git), nothing passes as ignored; a source module is never listed.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": "",
        "apps/ui/native.pyd": "",
        "apps/ui/types.pyi": "import apps.dedup.impl\n",  # Codex, PR #6182: type-only coupling no graph reads
        "apps/ui/target/debug/built.so": "",
        "apps/ui/kept/tracked.so": "",
        "apps/ui/.gitignore": "/target/\n/kept/\n",
        "scripts/__init__.py": "",
        "scripts/tool.so": "",
    }
    repo = _tree(tmp_path, files)
    everything = [
        ("apps.ui.kept.tracked", "apps/ui/kept/tracked.so"),
        ("apps.ui.native", "apps/ui/native.pyd"),
        ("apps.ui.target.debug.built", "apps/ui/target/debug/built.so"),
        ("apps.ui.types", "apps/ui/types.pyi"),
    ]
    # Codex, PR #6182: every root the graph holds is scanned, ``scripts`` as well as ``apps``;
    # a root not asked for (the default asks for ``apps`` alone) is not.
    script = ("scripts.tool", "scripts/tool.so")
    assert unread_modules(repo, ["apps", "scripts"]) == sorted([*everything, script])
    assert unread_modules(repo) == everything
    git = shutil.which("git")
    assert git is not None, "git is what tells shipped files from build output"
    subprocess.run([git, "-C", str(repo), "init", "-q"], check=True)
    subprocess.run([git, "-C", str(repo), "add", "-f", "apps/ui/kept/tracked.so"], check=True)
    assert unread_modules(repo) == [everything[0], everything[1], everything[3]]
    with monkeypatch.context() as patched:  # no git to ask: nothing passes as ignored
        patched.setattr(shutil, "which", lambda _: None)
        assert unread_modules(repo) == everything
    assert (
        Violation("unread-module", *everything[1])
        .describe()
        .startswith("apps.ui.native: held only as apps/ui/native.pyd, which no gate can read")
    )


def test_this_tree_ships_no_module_held_only_compiled() -> None:
    parser = configparser.ConfigParser()
    parser.read(CONTRACTS)
    roots = parser["importlinter"]["root_packages"].split()
    assert "apps" in roots and len(roots) > 1, f"read the wrong roots: {roots}"
    assert unread_modules(REPO, roots) == []


def test_a_dynamic_import_outside_a_pure_scope_is_not_read(tmp_path: Path) -> None:
    # Overshoot control: the implementation may load what it likes.
    repo = _tree(tmp_path, {"apps/lib/__init__.py": "", "apps/lib/service.py": _DYNAMIC_ROOT})
    assert set(pure_scope_imports(repo, ["lib"], set())) == set()


_CONSUMER = """
from typing import TYPE_CHECKING
from apps.lib.impl import runtime_thing
if TYPE_CHECKING:
    from apps.lib.service import Impl
    from apps.lib.contract import Model
if FLAG:
    from apps.lib.other import runtime_too
from typing import TYPE_CHECKING as TC
if TC:
    from apps.lib.aliased import A
if TYPE_CHECKING and FLAG:
    from apps.lib.joined import J
if not TYPE_CHECKING:
    pass
else:
    from apps.lib.negated import N
"""


def test_a_type_only_deep_import_in_a_consumer_is_a_violation(tmp_path: Path) -> None:
    # Sol, PR #6182: a guard in any form (an alias, ``and``, an ``else``) is still a type-only
    # import the graph drops, so every import statement is read from source, however guarded;
    # the ones the graph keeps read the same pairs again. Control: a contract import is no violation.
    repo = _tree(tmp_path, {"apps/ui/__init__.py": "", "apps/ui/page.py": _CONSUMER})
    deep = ("impl", "service", "other", "aliased", "joined", "negated")
    known = {"apps.ui", "apps.ui.page", "apps.lib.contract", *(f"apps.lib.{name}" for name in deep)}
    pairs = statement_imports(repo, known)
    assert pairs == {
        ("apps.ui.page", "apps.lib.impl"): [3],
        ("apps.ui.page", "apps.lib.service"): [5],
        ("apps.ui.page", "apps.lib.contract"): [6],
        ("apps.ui.page", "apps.lib.other"): [8],
        ("apps.ui.page", "apps.lib.aliased"): [11],
        ("apps.ui.page", "apps.lib.joined"): [13],
        ("apps.ui.page", "apps.lib.negated"): [17],
        ("apps.ui.page", "typing"): [2, 9],
    }
    assert _check(*pairs) == sorted(Violation("deep-import", "apps.ui.page", f"apps.lib.{name}") for name in deep)


def test_a_consumer_loading_an_implementation_by_name_is_a_deep_import(tmp_path: Path) -> None:
    # Codex, PR #6182: outside a pure scope only TYPE_CHECKING blocks were read from source.
    consumer = 'import importlib\nimportlib.import_module("apps.lib.impl")\nimportlib.import_module(NAME)\n'
    consumer += 'importlib.import_module("apps.lib.contract")\n'
    consumer += (
        'importlib.import_module(".impl", "apps.lib")\nimportlib.import_module("..contract", package="apps.lib.x")\n'
    )
    consumer += 'importlib.import_module(".impl", __package__)\nimportlib.import_module("...impl", "apps.lib")\n'
    consumer += 'TARGET = "apps.lib.impl"\nimportlib.import_module(TARGET)\n'
    consumer += 'SHIFTY = "apps.lib.contract"\nSHIFTY = "apps.ui.other"\nimportlib.import_module(SHIFTY)\n'
    consumer += 'import runpy\nrunpy.run_path("apps/lib/impl.py")\nrunpy.run_path("./apps/lib/contract/__init__.py")\n'
    consumer += 'importlib.import_module(name="apps.lib.impl")\nrunpy.run_module(mod_name="apps.lib.impl")\n'
    consumer += 'runpy.run_path(path_name="apps/lib/impl.py")\nimportlib.import_module(package="apps.lib")\n'
    consumer += 'getattr(importlib, LOADER)("apps.lib.impl")\n(LOAD or OTHER)("apps.lib.unknown")\n'
    repo = _tree(tmp_path, {"apps/ui/__init__.py": "", "apps/ui/page.py": consumer})
    known = {"apps.ui", "apps.ui.page", "apps.lib.impl", "apps.lib.contract"}
    pairs = loaded_imports(repo, known)
    # A relative name with a literal anchor (lines 5 and 6) is read too, and so is a module
    # constant bound once to text (line 10) (Codex, PR #6182). Controls: a computed name (line 3)
    # or anchor (line 7) is purity's concern, a name with more dots than its anchor has parts
    # (line 8) fails at run time, and a contract is allowed. A name bound twice (line 13) may hold
    # either text, so each is read as loaded. Lines 15 and 16 run a module by its repository path, and 17 to 19 name
    # the target by keyword, while 20 names only an anchor; 21 hands a module's name to a computed
    # callee, while 22 hands it a name no module has (Codex, PR #6182).
    impl, contract = ("apps.ui.page", "apps.lib.impl"), ("apps.ui.page", "apps.lib.contract")
    assert pairs == {impl: [2, 5, 10, 15, 17, 18, 19, 21], contract: [4, 6, 13, 16]}
    assert _check(*pairs) == [Violation("deep-import", "apps.ui.page", "apps.lib.impl")]


def test_a_consumer_naming_no_loader_is_still_read_for_a_module_name(tmp_path: Path) -> None:
    # Codex, PR #6182: a file is read only when its text holds a loader's name, and
    # `getattr(LIB, NAME)("apps.lib.impl")` holds none. Control: a name no module has is no load.
    files = {
        "apps/ui/__init__.py": "",
        "apps/ui/page.py": 'print("apps.lib.none")\ngetattr(LIB, NAME)("apps.lib.impl")\n',
        "apps/ui/path.py": "getattr(LIB, NAME)('apps/lib/impl.py')\n",  # each needle on its own
        # Codex, PR #6182: a loader bound by destructuring is a name no binding rule follows.
        "apps/ui/unpacked.py": 'import importlib\n(load,) = (importlib.import_module,)\nload("apps.lib.impl")\n',
        # Codex, PR #6182: code run from text imports what it holds. Controls: text that is no
        # code, and code naming no module, import nothing.
        "apps/ui/ran.py": 'exec("pass")\nexec("import apps.lib.impl")\neval("(")\ncompile("from apps.lib import impl", "f", "exec")\n'
        # Codex, PR #6182: code handed by keyword runs too. Control: keyword code naming no module.
        'compile(source="import apps.lib.impl", filename="f", mode="exec")\ncompile(source="pass", filename="f", mode="exec")\n',
        # Codex, PR #6182: a spec factory names the module in its second argument.
        "apps/ui/spec.py": 'import importlib.util as u\ns = u.spec_from_file_location("alias", "apps/lib/impl.py")\n'
        'f(x, location="apps/lib/impl.py")\nRecord(guard_site="apps/lib/impl.py")\n',  # control: a record, no load
        # Codex, PR #6182: text built from literals with no side effect folds to the name it
        # always makes. Controls: ``!r`` adds quotes, and a call's result is not known.
        "apps/ui/folded.py": (
            'import importlib\nLEAF = "impl"\nimportlib.import_module("apps.lib." + "impl")\n'
            'importlib.import_module(f"apps.lib.{LEAF}")\nimportlib.import_module(".".join(("apps", "lib", LEAF)))\n'
            'importlib.import_module(f"apps.lib.{LEAF!r}")\nimportlib.import_module("apps.lib.impl" + suffix())\n'
            # Codex, PR #6182: an item picked from a written-out display. Controls: the wrong
            # item, and an index nobody can read.
            'importlib.import_module(("apps.lib.impl",)[0])\nimportlib.import_module({"k": "apps.lib.impl"}["k"])\n'
            'importlib.import_module(["x", "apps.lib.impl", "y"][-2])\nimportlib.import_module(("apps.lib.impl", "x")[1])\n'
            'importlib.import_module(("apps.lib.impl",)[i])\n'
            # A key after ``**other`` is certain; one before it may be replaced (control).
            'importlib.import_module({**o, "k": "apps.lib.impl"}["k"])\n'
            'importlib.import_module({"k": "apps.lib.impl", **o}["k"])\n'
        ),
        # Codex, PR #6182: an evaluator bound to a name, code handed to a callee named nothing
        # a reader knows, and a module constant folded from parts. Controls: text with no import, and
        # an import of no module from the repository.
        "apps/ui/aliased.py": (
            'run = exec\nrun("import apps.lib.impl")\nTARGET = "apps.lib." + "impl"\n'
            'importlib.import_module(TARGET)\nrun("x = 1")\nrun("import os")\n'
        ),
        "apps/ui/handed.py": 'r("import apps.lib.impl")\n',
        # Codex, PR #6182: a wrapper forwards a module name to its loader under any keyword.
        # Codex, PR #6182: arguments unpacked from a display, or from a name bound to one. Controls:
        # a path under a record keyword stays unread, and an unknown spread loads nothing known.
        "apps/ui/spread.py": (
            'import importlib\nimportlib.import_module(*("apps.lib.impl",))\n'
            'importlib.import_module(**{"name": "apps.lib.impl"})\nARGS = ("apps.lib.impl",)\n'
            'importlib.import_module(*ARGS)\nKW = {"name": "apps.lib.impl"}\nimportlib.import_module(**KW)\n'
            'Record(**{"guard_site": "apps/lib/impl.py"})\nimportlib.import_module(*rest)\n'
        ),
        # Codex, PR #6182: an item of a name bound to a display, read directly or through a name.
        # Controls: a display that picks itself ends, and an index past the end picks nothing.
        "apps/ui/named.py": (
            'import importlib\nTARGETS = ("apps.lib.impl",)\nimportlib.import_module(TARGETS[0])\n'
            'TABLE = {"k": "apps.lib.impl"}\nimportlib.import_module(TABLE["k"])\n'
            "SELF = (SELF[0],)\nimportlib.import_module(SELF[0])\nimportlib.import_module(TARGETS[1])\n"
            "B = TARGETS[0]\nimportlib.import_module(B)\n"
        ),
        # Codex, PR #6182: ``__import__`` imports each fromlist entry below the package it names,
        # by keyword, positionally, or through a bound name. Control: an entry naming no module.
        "apps/ui/fromlist.py": (
            '__import__("apps", fromlist=["lib.impl"])\n__import__("apps.lib", None, None, ("impl",))\n'
            'NAMES = ["impl"]\n__import__("apps.lib", fromlist=NAMES)\n__import__("apps.lib", fromlist=["none"])\n'
            # Codex, PR #6182: a package named through a name bound in a function scope.
            'def f():\n    base = "apps"\n    __import__(base, fromlist=["lib.impl"])\n'
            # Codex, PR #6182: a loader bound by destructuring, and one reached by reflection.
            '(load,) = (__import__,)\nload("apps", fromlist=["lib.impl"])\n'
            'getattr(builtins, NAME)("apps.lib", None, None, ["impl"])\n'
        ),
        "apps/ui/wrapped.py": 'def load(**kw):\n    return importlib.import_module(kw["module"])\nload(module="apps.lib.impl")\n',
        # Codex, PR #6182: a name bound in a function scope, to a module's name or to code.
        "apps/ui/local.py": (
            'def f():\n    T = "apps.lib.impl"\n    importlib.import_module(T)\n'
            'def g():\n    C = "import apps.lib.impl"\n    run(C)\n    T = "apps.lib.none"\n'
            # Codex, PR #6182: a name bound to a name holds its texts, whichever binding is read
            # first. Control: a name bound to an unknown name holds none.
            'def h():\n    B = A\n    if ready:\n        A = "apps.lib.impl"\n    importlib.import_module(B)\n'
            "    D = other\n    importlib.import_module(D)\n"
        ),
        # Codex, PR #6182: a bound name inside a composed text, by f-string, concatenation or
        # join, through each display a name joined is bound to, and grown in a loop. Controls: a
        # ``!r`` part, a part nobody can read, a separator that makes no module's name, and a spread.
        "apps/ui/composed.py": (
            'def f():\n    leaf = "impl"\n    importlib.import_module(f"apps.lib.{leaf}")\n'
            '    importlib.import_module("apps.lib." + leaf)\n    importlib.import_module(".".join(("apps", "lib", leaf)))\n'
            '    parts = ["apps", "lib", leaf]\n    importlib.import_module(".".join(parts))\n'
            '    importlib.import_module(f"apps.lib.{leaf!r}")\n    importlib.import_module(f"apps.lib.{other}")\n'
            '    importlib.import_module("/".join(parts))\n'
            '    name = "apps"\n    for part in ("lib", "impl"):\n        name = name + "." + part\n'
            "    importlib.import_module(name)\n"
            '    BOTH = ["x"]\n    BOTH = ["apps", "lib", "impl"]\n    importlib.import_module(".".join(BOTH))\n'
            '    importlib.import_module(".".join(("apps", "lib", *rest)))\n'
        ),
        # Codex, PR #6182: a parameter bound by its default, positional, positional-only,
        # keyword-only or a lambda's. Control: a parameter with no default holds no text.
        "apps/ui/defaults.py": (
            'def a(target="apps.lib.impl"):\n    importlib.import_module(target)\n'
            'def b(x, /, y="apps.lib.impl"):\n    importlib.import_module(y)\n'
            'def c(*, k="apps.lib.impl", j):\n    importlib.import_module(k)\n'
            'd = lambda t="apps.lib.impl": importlib.import_module(t)\n'
            'def e(p, q="x"):\n    importlib.import_module(p)\n    importlib.import_module(j)\n'
        ),
        # Codex, PR #6182: a template filled by ``%`` or ``str.format``, by literal or bound name,
        # one value or a tuple, by position, index or keyword, or a composed template. Controls:
        # a field that formats (``%r``, ``{!r}``), reaches into its value (``{0.x}``) or names
        # nothing given, and a value left over, fill nothing.
        "apps/ui/formatted.py": (
            'importlib.import_module("apps.lib.%s" % "impl")\n'
            'def f():\n    leaf = "impl"\n    importlib.import_module("apps.%s.%s" % ("lib", leaf))\n'
            '    importlib.import_module("apps.{}.{}".format("lib", leaf))\n'
            '    importlib.import_module("{1}.{0}.impl".format("lib", "apps"))\n'
            '    importlib.import_module("apps.lib.{leaf}".format(leaf=leaf))\n'
            '    importlib.import_module("apps.lib.%r" % leaf)\n    importlib.import_module("apps.lib.{!r}".format(leaf))\n'
            '    importlib.import_module("apps.lib.{0.x}".format(leaf))\n'
            '    importlib.import_module("apps.lib.{2}".format(leaf))\n'
            '    importlib.import_module("apps.lib.%s" % ("impl", "x"))\n'
            '    importlib.import_module(("apps.lib.%s" + "") % leaf)\n'
        ),
        # Codex, PR #6182: an expression that evaluates to one of its parts, either branch of a
        # conditional, any operand of ``or``/``and``, a walrus's value, nested in a composed text
        # too. Control: a conditional whose branches name no module.
        "apps/ui/branches.py": (
            'importlib.import_module("apps.lib.contract" if on else "apps.lib.impl")\n'
            'importlib.import_module(name or "apps.lib.impl")\nimportlib.import_module(ok and "apps.lib.impl")\n'
            'importlib.import_module((t := "apps.lib.impl"))\n'
            'importlib.import_module("apps.lib." + ("impl" if on else "x"))\n'
            'importlib.import_module("x" if on else "y")\n'
        ),
        # Codex, PR #6182: a name bound by destructuring, written out, nested, through a name bound
        # to a display, as a loop's target, and beside a starred target. Controls: the starred
        # target itself (a list), a length mismatch, and a spread value that shifts every position.
        "apps/ui/unpacked_text.py": (
            'def f():\n    (target,) = ("apps.lib.impl",)\n    importlib.import_module(target)\n'
            '    a, (b, c) = "x", ("y", "apps.lib.impl")\n    importlib.import_module(c)\n'
            '    PAIR = ("x", "apps.lib.impl")\n    _, d = PAIR\n    importlib.import_module(d)\n'
            '    for _, e in (("x", "apps.lib.impl"),):\n        importlib.import_module(e)\n'
            '    *g, h = ("apps.lib.impl", "x")\n    importlib.import_module(g)\n'
            '    k, *rest = ("apps.lib.impl", "x")\n    importlib.import_module(k)\n'
            '    i, j = ("apps.lib.impl",)\n    importlib.import_module(i)\n'
            '    m, n = (*rest, "apps.lib.impl")\n    importlib.import_module(n)\n'
        ),
        # Codex, PR #6182: a display reached through a name bound to another name, read before or
        # after it, or through a conditional's branch. Control: an alias of a display picking nothing.
        "apps/ui/aliased_display.py": (
            'TARGETS = ("apps.lib.impl",)\nALIASED = TARGETS\nimportlib.import_module(ALIASED[0])\n'
            'def f():\n    LATE = EARLY\n    EARLY = ["apps.lib.impl"]\n    importlib.import_module(LATE[0])\n'
            '    EITHER = TARGETS if on else ("x",)\n    importlib.import_module(EITHER[0])\n'
            "    importlib.import_module(ALIASED[1])\n"
        ),
        # Codex, PR #6182: a text a text method or a slice computes, by position or keyword,
        # chained, and on a name bound to a text. Controls: a method that leaves no module's name,
        # one that raises, a slice cutting a name short, and a method that makes no text.
        "apps/ui/methods.py": (
            'importlib.import_module("apps.lib.contract".replace("contract", "impl"))\n'
            'importlib.import_module("  apps.lib.impl ".strip())\nimportlib.import_module("APPS.LIB.IMPL".lower())\n'
            'importlib.import_module("_apps.lib.impl"[1:])\nT = "apps.lib.impl_"\nimportlib.import_module(T[:-1])\n'
            'importlib.import_module("xapps.lib.impl".removeprefix("x").replace("q", "z"))\n'
            'importlib.import_module("APPS.LIB.IMPL".lower().expandtabs(tabsize=4))\n'
            'importlib.import_module("apps.lib.contract".replace("contract", "none"))\n'
            'importlib.import_module("apps.lib.impl".replace(1, 2))\nimportlib.import_module(T[:-2])\n'
            'importlib.import_module("apps.lib.impl".split("."))\n'
            'importlib.import_module("apps.lib.imp" + "xl"[1])\nimportlib.import_module("apps.lib.imp" + "xl"[0])\n'
        ),
        # Sol, PR #6182: a file whose text names no loader and no whole module was never read,
        # though its parts fold to one. Control: parts folding to no module's name.
        "apps/ui/split.py": 'registry.get("apps" + ".lib.impl")\nregistry.get("apps" + ".lib.none")\n',
        # Codex, PR #6182: a wrapper's parameter takes the arguments its calls write out, by
        # position, keyword, through a method and a lambda, and after a spread onto any later
        # parameter. Controls: an argument naming no module, a call of a function this module does
        # not define, and an argument before a spread, which keeps its own position. The load
        # reads at the loader, never at a call handing a part.
        "apps/ui/wrapper.py": (
            'def load(leaf, *, pkg="lib"):\n    importlib.import_module("apps." + pkg + "." + leaf)\n'
            'load("impl")\nload(leaf="impl")\nload("x", pkg="none")\n'
            'class C:\n    def get(self, part):\n        importlib.import_module(f"apps.lib.{part}")\n'
            'C().get("impl")\nfetch = lambda tail: importlib.import_module("apps.lib." + tail)\nfetch("impl")\n'
            'def other(item):\n    importlib.import_module("apps.lib." + item)\nunknown("impl")\n'
            'def kw(*, only):\n    importlib.import_module("apps.lib." + only)\nkw(only="impl")\n'
            'def two(a, b):\n    importlib.import_module("apps.lib." + b)\ntwo(*rest, "impl")\n'
            'def three(c, d):\n    importlib.import_module("apps.lib." + d)\nthree("impl", *rest)\n'
        ),
        # Codex, PR #6182: a name stored on an attribute, of an instance or a class, set in one
        # place and handed to a call in another. Control: an attribute set to no module's name.
        "apps/ui/attribute.py": (
            'class C:\n    kind = "apps.lib.impl"\n    def __init__(self):\n        self.target = "apps.lib.impl"\n'
            "    def run(self):\n        registry.get(self.target)\n        registry.get(C.kind)\n"
            '        self.other = "apps.lib.none"\n        registry.get(self.other)\n'
        ),
        # The price: any call handed the name reads as a load; the same literal outside a call does not.
        "apps/ui/kept.py": 'SELF = {"apps.lib.impl": 1}\nitems = [SELF.get("apps.lib.impl")]\n',
    }
    known = {
        "apps.ui",
        "apps.ui.page",
        "apps.ui.path",
        "apps.ui.unpacked",
        "apps.ui.kept",
        "apps.ui.ran",
        "apps.ui.spec",
        "apps.ui.folded",
        "apps.ui.aliased",
        "apps.ui.handed",
        "apps.ui.wrapped",
        "apps.ui.fromlist",
        "apps.ui.named",
        "apps.ui.spread",
        "apps.ui.local",
        "apps.ui.composed",
        "apps.ui.defaults",
        "apps.ui.formatted",
        "apps.ui.branches",
        "apps.ui.unpacked_text",
        "apps.ui.aliased_display",
        "apps.ui.methods",
        "apps.ui.split",
        "apps.ui.wrapper",
        "apps.ui.attribute",
        "apps.lib.impl",
    }
    assert loaded_imports(_tree(tmp_path, files), known) == {
        ("apps.ui.page", "apps.lib.impl"): [2],
        ("apps.ui.path", "apps.lib.impl"): [1],
        ("apps.ui.unpacked", "apps.lib.impl"): [3],
        ("apps.ui.kept", "apps.lib.impl"): [2],
        ("apps.ui.ran", "apps.lib.impl"): [2, 4, 5],
        ("apps.ui.spec", "apps.lib.impl"): [2, 3],
        ("apps.ui.folded", "apps.lib.impl"): [3, 4, 5, 8, 9, 10, 13],
        ("apps.ui.folded", "x"): [11],  # the item picked, not the display it sits in
        ("apps.ui.aliased", "apps.lib.impl"): [2, 4],
        ("apps.ui.handed", "apps.lib.impl"): [1],
        ("apps.ui.wrapped", "apps.lib.impl"): [3],
        ("apps.ui.fromlist", "apps.lib.impl"): [1, 2, 4, 8, 10, 11],
        ("apps.ui.fromlist", "apps"): [1, 10],  # the package each call names is loaded too
        ("apps.ui.fromlist", "apps.lib"): [2, 4, 5],
        ("apps.ui.named", "apps.lib.impl"): [3, 5, 10],
        ("apps.ui.spread", "apps.lib.impl"): [2, 3, 5, 7],
        ("apps.ui.local", "apps.lib.impl"): [3, 6, 12],
        ("apps.ui.composed", "apps.lib.impl"): [3, 4, 5, 7, 14, 17],
        ("apps.ui.defaults", "apps.lib.impl"): [2, 4, 6, 7],
        ("apps.ui.formatted", "apps.lib.impl"): [1, 4, 5, 6, 7, 13],
        ("apps.ui.branches", "apps.lib.impl"): [1, 2, 3, 4, 5],
        ("apps.ui.unpacked_text", "apps.lib.impl"): [3, 5, 8, 10, 14],
        ("apps.ui.aliased_display", "apps.lib.impl"): [3, 7, 9],
        ("apps.ui.methods", "apps.lib.impl"): [1, 2, 3, 4, 6, 7, 8, 13],
        ("apps.ui.split", "apps.lib.impl"): [1],
        ("apps.ui.wrapper", "apps.lib.impl"): [2, 8, 10, 16, 19],
        ("apps.ui.attribute", "apps.lib.impl"): [6, 7],
    }


def test_enforced_roots_and_contracts_on_this_tree_are_pure_from_source() -> None:
    pairs = pure_scope_imports(REPO, sorted(_enforced()), set())
    assert pairs, "read no imports at all: the scan is pointed at nothing"
    assert _check(*pairs, enforced=tuple(sorted(_enforced()))) == []


# ----- what a contract declares (Codex, PR #6182) --------------------------------


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


def test_a_closed_frozen_contract_declares_nothing_wrong() -> None:
    # Overshoot control for every rule below: models, frozen dataclasses with clean methods,
    # enums, exceptions, a private self-referencing type, Protocols over paths, containers,
    # literals, optionals, JsonValue and contract models, a clean setter and __repr__, a
    # function, type aliases, constants, a submodule, a re-export from another contract, a
    # stdlib exception, a private sqlite3 binding, a Literal of an enum member and the typing
    # and pydantic tooling, `Any` included, imported under their own names.
    assert _fixture("clean") == []


def test_open_or_mutable_declarations_are_findings() -> None:
    assert _fixture("open_types") == [
        '.Loose: a Pydantic model needs model_config = ConfigDict(frozen=True, extra="forbid")',
        ".Mutable: a dataclass needs frozen=True (or make it a frozen Pydantic model)",
        ".Plain: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
    ]


def test_a_frozen_type_with_an_open_field_is_a_finding() -> None:
    # Controls inside the fixture: Defaulted.name, Factoried.tags (an empty tuple) and DataAware.name have
    # clean defaults. Any other factory is refused: one call proves nothing about the next. A field
    # holds a tuple or frozenset only (Sol, PR #6182): Mutables.kept, .tags and .notes are controls.
    # JsonValue stays a boundary type for a parameter or return (the clean fixture's payload).
    assert _fixture("open_fields") == [
        ".BareContainers: field rows: typing.List has no element type",
        ".DataAware: field label: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".Defaulted.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".Defaulted: field conn: default holds a Connection, which is not a boundary value",
        ".Defaulted: field rows: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".Defaulted: field rows: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Factoried: field rows: "
        "default comes from a factory other than an empty list, dict, set, tuple or frozenset; a contract default is a value",
        ".HoldsConnection: field conn: Connection is not a boundary type",
        ".ModelWithAny: field rows: Any is not a boundary type",
        ".MutableRecord: field by_name: dict[str, int] "
        "can be changed in place; a contract field holds a tuple or frozenset",
        ".MutableRecord: field labels: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables.__pydantic_core_schema__: holds a _SpecialGenericAlias, which Pydantic does not put there",
        ".Mutables: field maybe: list[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field nested: set[str] can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field payload: JsonValue can hold a list or dict, "
        "which can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field payloads: JsonValue can hold a list or dict, "
        "which can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field seq: collections.abc.Sequence[str] "
        "can be changed in place; a contract field holds a tuple or frozenset",
        ".Mutables: field table: collections.abc.Mapping[str, int] "
        "can be changed in place; a contract field holds a tuple or frozenset",
    ]


def test_a_protocol_member_that_leaks_an_open_type_is_a_finding() -> None:
    # Controls inside the fixture: Leaky.name(), InheritsOpen.name(), LeakyAttribute.path,
    # LeakySetter's getter, CallsOpen.__repr__ and Store.__hash__ (inert) are clean and stay
    # unreported. A hand-written special method beyond the inert ones is refused, not read.
    assert _fixture("protocols") == [
        ".BoundAttribute.label: binds a value to an annotated attribute; "
        "a contract declares only fields, methods and properties on a class",
        ".CallsOpen.__call__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".InheritsOpen.open(): return: Connection is not a boundary type",
        ".Leaky.open(): return: Connection is not a boundary type",
        ".LeakyAttribute.connection: Connection is not a boundary type",
        ".LeakyFactories.connect(): return: Connection is not a boundary type",
        ".LeakyFactories.open(): return: Connection is not a boundary type",
        ".LeakyProperty.conn(): return: Connection is not a boundary type",
        ".LeakySetter.db.setter(): value: Connection is not a boundary type",
        ".Store.__setitem__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Unannotated.run(): return has no annotation",
        ".Unannotated.run(): x has no annotation",
        ".Untyped.load(): payload: Any is not a boundary type",
        ".Untyped.load(): return: Any is not a boundary type",
        "._OpensBase.open(): return: Connection is not a boundary type",
    ]


def test_value_types_functions_and_enums_are_read_through_or_refused() -> None:
    # Controls inside the fixture: OpensItself.name(), reads(), Pinned.side, ContractError.reason(),
    # StoreError (a refined sqlite3.Error with a typed constructor), TypedError, runs()'s limit
    # default, ContractError.code, RETRIES, BATCH (a bare Final), ROOT, NAMES, LOOKUP, UTC_STAMP,
    # Titled, Sealed, Resealed (a frozen dataclass's decorated subclass), Restated (a plain value
    # over an inherited field's default), bounded() and
    # TaggedModel.size (Annotated tooling and plain metadata), Tone (a StrEnum),
    # Compares.__hash__ (inert), DIAMOND (one list reached twice), Normalized's __post_init__
    # (it resets only its own declared field), TypedError's __init__ (it sets only the attribute
    # it annotates) and every generated __init__ are clean. A __post_init__, or an error's
    # __init__ (Sol, PR #6182), that sets an undeclared or computed name, or hands its instance
    # on, is refused. An enum with more than its
    # members (an attribute stored on a member included), an undecorated dataclass subclass, a
    # ClassVar, an InitVar, a cached property, a nested class, a function attribute, a held
    # exception and a custom metaclass are refused, not read.
    assert _fixture("values") == [
        ".ATTACHED: holds a Attached that has field connection holds a Connection, which is not a boundary value",
        ".Annkeyed.__annotations__: is keyed by a Connection, where its tooling keys by name",
        ".Attached.__post_init__(): sets connection, which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".CachesConnection.conn: binds a cached_property; "
        "a contract declares only fields, methods and properties on a class",
        ".Compares.__eq__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Compiled.__repr__(): stores connection on the function; a contract declares only fields, methods and properties on a class",
        ".Computed: computed field label holds a Connection, which is not a boundary value",
        ".ComputedAs: computed field label: return_type Connection is not a boundary type",
        ".Configured: model_config holds a Connection, which is not a boundary value",
        ".ContractError.__init__(): is BaseException's, whose "
        "*args take any object and hand it out as .args; declare a typed __init__",
        ".ContractError.connection: Connection is not a boundary type",
        ".ContractError.open(): return: Connection is not a boundary type",
        ".Corecycled.__pydantic_core_schema__: holds itself through a dict, which no boundary can carry",
        ".Coreheld.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".Corekeyed.__pydantic_core_schema__: is keyed by a int, where Pydantic keys by name",
        ".Custom.__init__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".DISGUISED: holds a _Disguised, which is not a boundary value",
        ".Dcfaked.TAG: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Dcfaked.__dataclass_fields__: maps 'TAG' to a _FakeClassVar the field scan never reads",
        ".Dcghost.__dataclass_fields__: maps 'ghost' to a _NotAField the field scan never reads",
        ".Dckeyed.__dataclass_fields__: is keyed by a Connection, where its tooling keys by name",
        ".Dcpseudo.__dataclass_fields__: maps 'ghost' to a Field the field scan never reads",
        ".Dcrenamed.__dataclass_fields__: maps 'alias' to a field of another name",
        ".Decorated.__pydantic_decorators__: validators is keyed by a Connection, where Pydantic keys by name",
        ".DefaultedRow: field conn: default holds a Connection, which is not a boundary value",
        ".Delegated.__post_init__(): hands its instance on; a contract declares only fields, methods and properties on a class",
        ".Described.cleared.deleter(): return: Connection is not a boundary type",
        ".Described.cursor: binds a builtin_function_or_method as an accessor; "
        "a contract declares only fields, methods and properties on a class",
        ".Described.held: binds a _Held; a contract declares only fields, methods and properties on a class",
        ".Described.label: its docstring holds a Connection, which is not a boundary value",
        ".Dundered.__connection__: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Envelope.Payload: binds a type; a contract declares only fields, methods and properties on a class",
        ".Envelope: field payload: Envelope.Payload has field conn: Connection is not a boundary type",
        ".EqSpoofed.__eq__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".Evaluated.__bool__(): return: annotation holds a Connection, which is not a boundary type",
        ".Evaluated.__format__(): held: default holds a Connection, which is not a boundary value",
        ".Evaluated.__str__(): its closure's held holds a Connection, which is not a boundary value",
        ".FieldMetadata: field name: metadata holds a Connection, which is not a boundary value",
        ".Fieldkeyed.__pydantic_fields__: is keyed by a Connection, where its tooling keys by name",
        ".Flagged: field name: its compare flag holds a Connection, where a bool belongs",
        ".Genericheld.__pydantic_generic_metadata__: names a type where Connection is not a boundary type",
        ".HELD_ERROR: holds a TypedError instance; a contract raises errors, it does not hold them",
        ".HandedError.__init__(): hands its instance on; a contract declares only fields, methods and properties on a class",
        ".HeldReceiver.read(): self: annotation holds a Connection, which is not a boundary value",
        ".HeldReceiver.write(): self: default holds a Connection, which is not a boundary value",
        ".Hidden: a Pydantic model declares private attributes (_connection)",
        ".Hoards._Hoards__pool: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Hoards._connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".HoldsHandle: field handle: _Handle.open(): return: Connection is not a boundary type",
        ".Hooked.__pydantic_core_schema__: holds a method, which Pydantic does not put there",
        ".LOOP: holds itself through a list, which no boundary can carry",
        ".ListPayload: inherits builtins.list, which is not a contract type",
        ".LiveConnection: inherits sqlite3.Connection, which is not a contract type",
        ".Machined._abc_impl: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".MachinedPort._abc_impl: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Matched.__match_args__: binds a tuple; a contract declares only fields, methods and properties on a class",
        ".MetaBuiltin: uses the metaclass builtins._BuiltinMeta, which is neither the language's nor the tooling's",
        ".MetaSpoofed: uses the metaclass abc._SpoofedMeta, which is neither the language's nor the tooling's",
        ".Metered: uses the metaclass "
        f"{FIXTURES}.values.contract._Meta, which is neither the language's nor the tooling's",
        ".Misnamed: is an enum with more than plain str or int members",
        ".Mixed: inherits pydantic._Mixin, which is not a contract type",
        ".Mode: is an enum with more than plain str or int members",
        ".Opener: is an enum with more than plain str or int members",
        ".OpensItself.open(): return: Connection is not a boundary type",
        ".Outer: field inner: _Inner has field conn: Connection is not a boundary type",
        ".Paramfaked: its dataclass parameter frozen holds a str, where the decorator stores a bool",
        ".Paramheld: its dataclass parameter init holds a Connection, where the decorator stores a bool",
        ".Paramswapped: holds a _Params as its dataclass parameters",
        ".Payload: re-exports typing.Payload, which is not a contract type",
        ".Pinned: field mode: holds a member of _Hidden, which is an enum with more than plain str or int members",
        ".PrivatePort._open(): return: Connection is not a boundary type",
        ".ROWS: holds a _Rows, which is not a boundary value",
        ".Raised.open(): return: Connection is not a boundary type",
        ".Reassigned: field label: Annotated metadata holds a Connection, which is not a boundary value",
        ".Reassigned: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Rebased.__init__(): its closure's __class__ holds a type, which is not a boundary value",
        ".Reclassed.__init__(): its closure's __class__ holds a Connection, which is not a boundary value",
        ".Recorded: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Redecorated.__pydantic_decorators__: computed_fields['size'] records a function other than the one its class binds under that name",
        ".Redescribed: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Redoc.__repr__(): its __doc__ holds a Connection, where text belongs",
        ".Redoc.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Redoc.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Redocumented: its __doc__ holds a Connection, where text belongs",
        ".Registered._abc_impl: binds a _abc_data; a contract declares only fields, methods and properties on a class",
        ".Rehomed: is an enum with more than plain str or int members",
        ".Rekeyed: is an enum with more than plain str or int members",
        ".Remapped: is an enum with more than plain str or int members",
        ".Renamed: is an enum with more than plain str or int members",
        ".Renumbered: model_fields names 'ghost', a field no model class in its MRO declares",
        ".Rerecorded: field name: Annotated metadata _attributes_set has a Connection key, where Pydantic names a setting by text",
        ".Rerepr.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Rerepr.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Rerouted.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Rerouted.__repr__(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        ".Reserialized.__pydantic_serializer__: holds a SchemaSerializer built from another schema than the class's own",
        ".Respelled.__post_init__(): sets a computed name 'na' + 'me', which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".Retagged.__repr__(): its closure's user_function holds a function, which is not a boundary value",
        ".Retagged.__repr__(): stores __wrapped__, connection on the function; a contract declares only fields, methods and properties on a class",
        ".Retains.__format__(): held: default holds a Connection, which is not a boundary value",
        ".Retargeted: is an enum with more than plain str or int members",
        ".Retyped: field name: its retained type is a Connection, not the annotation its class wrote",
        ".Revalidated.__pydantic_validator__: holds a SchemaValidator built from another schema than the class's own",
        ".Revalued: is an enum with more than plain str or int members",
        ".Rewrapped: computed field label keeps a property other than the one its class declares",
        ".Rogue: is an enum with more than plain str or int members",
        ".Rows.__iter__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".SHARED: Connection is not a boundary type",
        ".SHOWN: holds a Connection, which is not a boundary type",
        ".SLOTTED: holds a Slotted that has field extra holds a Connection, which is not a boundary value",
        ".STAMP: holds a time zone _Zone; a contract time is naive or has a fixed datetime.timezone",
        ".Seeded.conn: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Settings.CONN: is annotated but no field; a contract declares only fields, methods and properties on a class",
        ".Shadowed.token: shadows its field's default and holds a Connection, which is not a boundary value",
        ".Slotted.__post_init__(): sets extra, which no field declares; "
        "a contract declares only fields, methods and properties on a class",
        ".Slotted.extra: binds a member_descriptor; a contract declares only fields, methods and properties on a class",
        ".SpoofedError.__init__(): is BaseException's, whose *args take any object and hand it out as .args; declare a typed __init__",
        ".SpoofedError.connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        ".Stamped: is an enum with more than plain str or int members",
        ".Stashed: is an enum with more than plain str or int members",
        ".StashedError.__init__(): sets connection, which no annotated attribute declares; "
        "a contract declares only fields, methods and properties on a class",
        ".StaticSelf.build(): cls: Connection is not a boundary type",
        ".Stored.__bool__(): stores connection on the function; a contract declares only fields, methods and properties on a class",
        ".Stored.__str__(): its closure's held holds a Connection, which is not a boundary value",
        ".Subscripts.__class_getitem__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".TABLE: holds a _Table, which is not a boundary value",
        ".TOKEN: holds a _Token, which is not a boundary value",
        ".TaggedModel.__pydantic_core_schema__: holds a Connection, which Pydantic does not put there",
        ".TaggedModel: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedModel: field note: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedPort.name: Annotated metadata holds a Connection, which is not a boundary value",
        ".TaggedRow: field name: Annotated metadata holds a Connection, which is not a boundary value",
        ".Unlisted: is an enum with more than plain str or int members",
        ".Unsealed: subclasses a dataclass without its own @dataclass(frozen=True), "
        "so its instances take new attributes",
        ".Unvalued: is an enum with more than plain str or int members",
        ".UsesOpener: field opener: Opener is not a closed frozen type",
        ".Validtyped.__pydantic_decorators__: field_validators['_stripped'] records an annotation where Connection is not a boundary type",
        ".Wired: is an enum with more than plain str or int members",
        "._Bare: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Bound: re-exports annotated_types._Bound, which is not a contract type",
        "._BuiltinMeta: re-exports builtins._BuiltinMeta, which is not a contract type",
        "._Callable: re-exports collections.abc.Callable, which is not a contract type",
        "._Connection: re-exports sqlite3.Connection, which is not a contract type",
        "._Disguised: re-exports builtins._Disguised, which is not a contract type",
        "._FakeClassVar._field_type: holds a _FIELD_BASE, which is not a boundary value",
        "._FakeClassVar.connection: holds a Connection, which is not a boundary value",
        "._FakeClassVar: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._FileHandle: inherits _io.FileIO, which is not a contract type",
        "._Handle.open(): return: Connection is not a boundary type",
        "._Held.connection: holds a Connection, which is not a boundary value",
        "._Held: inherits builtins.property, which is not a contract type",
        "._Hidden.CONN: holds a member of _Hidden, which is an enum with more than plain str or int members",
        "._Hidden: is an enum with more than plain str or int members",
        "._Inner: field conn: Connection is not a boundary type",
        "._Iterator: re-exports collections.abc.Iterator, which is not a contract type",
        "._Kind: inherits builtins.type, which is not a contract type",
        "._LIVE: holds a Connection, which is not a boundary value",
        "._LOOP: holds itself through a dict, which no boundary can carry",
        "._Lenient.__call__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Lenient.__eq__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Lenient: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        '._Loose: a Pydantic model needs model_config = ConfigDict(frozen=True, extra="forbid")',
        "._Made: uses the metaclass tests.quality.contract_fixtures.values.contract._Kind, which is neither the language's nor the tooling's",
        "._Meta.__getattr__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Meta: inherits builtins.type, which is not a contract type",
        "._Mixin: re-exports pydantic._Mixin, which is not a contract type",
        "._Mutable: a dataclass needs frozen=True (or make it a frozen Pydantic model)",
        "._Name.held: holds a Connection, which is not a boundary value",
        "._Name: inherits builtins.str, which is not a contract type",
        "._NotAField.connection: holds a Connection, which is not a boundary value",
        "._NotAField: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Opener.Inner.cursor(): return: Cursor is not a boundary type",
        "._Opener.Inner: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Opener.current(): return: Connection is not a boundary type",
        "._Opener.open(): return: Connection is not a boundary type",
        "._Opener.reopen(): return: Connection is not a boundary type",
        "._Opener: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Params.__init__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Params: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Registry: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        "._Rows.connection: holds a Connection, which is not a boundary value",
        "._Rows: inherits builtins.list, which is not a contract type",
        "._Spoofed: re-exports _Spoofed, with connection: binds a Connection; a contract declares only fields, methods and properties on a class",
        "._SpoofedError: re-exports _SpoofedError, with open(): return: Connection is not a boundary type",
        "._SpoofedMeta: re-exports abc._SpoofedMeta, which is not a contract type",
        "._Table.connection: holds a Connection, which is not a boundary value",
        "._Table: inherits builtins.dict, which is not a contract type",
        "._Token.__new__(): is a hand-written special method; a contract declares only fields, methods and properties on a class",
        "._Token: inherits builtins.str, which is not a contract type",
        "._Zone.connection: holds a Connection, which is not a boundary value",
        "._Zone: inherits datetime.tzinfo, which is not a contract type",
        ".__annotations__['_hidden']: holds a Connection, which is not a boundary type",
        ".__annotations__['unbound']: holds a Connection, which is not a boundary type",
        ".__connection__: holds a Connection, which is not a boundary value",
        "._attach(): row: object is not a boundary type",
        "._cached_property: re-exports functools.cached_property, which is not a contract type",
        "._compiled: holds a type, which is not a boundary value",
        "._connect: re-exports connect, a callable with no typed signature",
        "._drop(): return: Connection is not a boundary type",
        "._drop(): self: object is not a boundary type",
        "._elsewhere(): self: object is not a boundary type",
        "._enclosing(): return: Callable is not a boundary type",
        "._guard(): its closure's user_function holds a function, which is not a boundary value",
        "._guard(): return has no annotation",
        "._guard(): self has no annotation",
        "._guard(): stores __wrapped__ on the function; a contract declares only fields, methods and properties on a class",
        "._name(): self: object is not a boundary type",
        "._port: holds a Receiver that has field connection holds a Connection, which is not a boundary value",
        "._pseudo: holds a Field, which is not a boundary value",
        "._rogue: holds a member of Rogue, which is an enum with more than plain str or int members",
        "._root: re-exports the module tests.quality.contract_fixtures.values, which is not this contract's",
        "._unlisted: holds a member of Unlisted, which is an enum with more than plain str or int members",
        ".annkeyed(): an __annotations__ key holds a Connection, which is not a boundary value",
        ".annotated(): annotates connection, naming no parameter",
        ".dispatched._Dispatching.__getattribute__(): is a hand-written special method; "
        "a contract declares only fields, methods and properties on a class",
        ".dispatched._Dispatching: inherits builtins.module, which is not a contract type",
        ".dispatched: is a _Dispatching, where a contract is a plain module",
        ".documented(): its __doc__ holds a Connection, where text belongs",
        ".dynamic: defines a module __getattr__, whose names nothing can read",
        ".extra(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): limit: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".gated(): rows: Annotated metadata comes from a factory other than an empty list, dict, set, tuple or "
        "frozenset; a contract default is a value",
        ".hidden.leak.Unread: a contract declares only frozen Pydantic models, frozen dataclasses, Protocols, exceptions and enums",
        ".kwheld(): __kwdefaults__['connection']: default holds a Connection, which is not a boundary value",
        ".kwkeyed(): a __kwdefaults__ key of type Connection: default holds a Connection, which is not a boundary value",
        ".lenient(): rows: Annotated metadata comes from a factory other than an empty list, dict, set, tuple or "
        "frozenset; a contract default is a value",
        ".opened(): return: Connection is not a boundary type",
        ".opens(): return: Connection is not a boundary type",
        ".posheld(): __defaults__[0]: default holds a Connection, which is not a boundary value",
        ".receives(): self: Connection is not a boundary type",
        ".runs(): conn: default holds a Connection, which is not a boundary value",
        ".spoofed(): limit: Annotated metadata holds a _Bound, which is not a boundary value",
        ".tagged(): name: Annotated metadata holds a Connection, which is not a boundary value",
        ".wired(): stores connection on the function; "
        "a contract declares only fields, methods and properties on a class",
        ".wrapping(): stores _cache on the function; a contract declares only fields, methods and properties on a class",
    ]


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
