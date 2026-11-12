"""TEST-CUT items 2 and 3 narrow a PR head further (item 3 loaders; split from test_ci_test_cut_items_2_3.py).

(private path): item 3 selects workflows, CI scripts and deleted modules by name
(`ci/*` and shared fixtures stay FULL) instead of forcing the full suite; item 2 leaves tests that a
change reaches only through a weak link (a name, a tree coupling, a conftest, test-area
data) to the merge-queue draft. Both are policies on scripts/ci_test_selection_rules.py,
switched by repo variables read in scripts/ci_test_selection.py.

Regression lines:
  - [if] either cut applies with its variable unset [then] the live selector changed without a switch, so fail, [else stop].
  - [if] a CI config change with item 3 on is FULL [then] the cut does nothing, so fail;
    [if] it misses a test that names or walks the file [then] fail, [else stop].
  - [if] the selector's own source stops forcing FULL under item 3 [then] a broken selector judges itself, so fail, [else stop].
  - [if] a deleted module with item 3 on is FULL, or misses the test that still imports it [then] fail, [else stop].
  - [if] item 2 demotes an import-graph or always-run module, or one outside its directories
    [then] fail; [if] it keeps a weak-only module inside them [then] fail, [else stop].
  - [if] a set but malformed variable does anything but FULL with the variable named [then] a typo silently changes a run, so fail, [else stop].
  - [if] a demoted module that fails in a draft is not counted as a weak escape [then] item 2's ratchet reads zero, so fail, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_test_selection_rules import (
    FullSuite,
    Policy,
)
from tests.scripts.ci_test_cut_items_2_3_support import (
    ITEM3,
    _select,
    build_tree,
)

pytestmark = [pytest.mark.requirement("TESTCUT-02"), pytest.mark.requirement("TESTCUT-03")]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return build_tree(tmp_path)


@pytest.mark.parametrize(
    "helper",
    [
        "import importlib\ndef load(package):\n    return importlib.import_module('.engine', package)\n",
        "import importlib\nENGINE = importlib.import_module(f'{__package__}.engine')\n",
        "import importlib\nGONE = importlib.import_module(f'{__package__}.gone_too')\n",
        "import importlib\nONE = importlib.import_module(f'{__package__}.one')\n",
        "exec(f'import {__package__}.kept')\nKEPT = None\n",
    ],
    ids=[
        "unread-anchor-sibling",
        "package-fstring-sibling",
        "package-fstring-longer-name",
        "package-fstring-suffix",
        "exec-package-fstring-sibling",
    ],
)
def test_a_loader_tail_of_another_module_does_not_reach_a_deleted_one(tree, helper):
    # Control: the tail must be the deleted module's own last name(s), not a prefix of one.
    (tree / "apps/core/load_helper.py").write_text(helper, encoding="utf-8")
    (tree / "tests/scripts/test_loads_other.py").write_text("import apps.core.load_helper\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_other.py" not in selection.modules


def test_a_composed_name_handed_to_a_generic_loader_for_another_module_does_not_reach_a_deleted_one(tree):
    # Control for the composed-caller join: the folded name is a surviving sibling.
    helper = "import importlib\ndef load(name):\n    return importlib.import_module(name)\n"
    (tree / "scripts/load_helper.py").write_text(helper, encoding="utf-8")
    test = "from scripts.load_helper import load\nENGINE = load('apps.core' + '.engine')\n"
    (tree / "tests/scripts/test_loads_other.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_other.py" not in selection.modules


def test_a_relative_name_of_another_module_handed_to_a_generic_loader_does_not_reach_a_deleted_one(tree):
    # Control for the relative-name join: `.engine` is a surviving sibling.
    helper = "import importlib\ndef load(name, package):\n    return importlib.import_module(name, package)\n"
    (tree / "scripts/load_helper.py").write_text(helper, encoding="utf-8")
    test = "from scripts.load_helper import load\nENGINE = load('.engine', 'apps.core')\n"
    (tree / "tests/scripts/test_loads_other.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_other.py" not in selection.modules


def test_a_caller_naming_a_deleted_module_to_a_helper_with_no_loader_is_still_demoted(tree):
    # Control for the generic-loader join: a helper that loads nothing leaves the name weak.
    (tree / "scripts/load_helper.py").write_text("def load(name):\n    return name\n", encoding="utf-8")
    test = "from scripts.load_helper import load\nGONE = load('apps.core.gone')\n"
    (tree / "tests/scripts/test_loads_gone.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_gone.py" in selection.demoted


@pytest.mark.parametrize(
    ("helper", "selected"),
    [
        ("import importlib\ndef load(name):\n    return importlib.import_module(name)\n", True),
        ("def load(name):\n    return name\n", False),
    ],
    ids=["generic-loader", "no-loader-control"],
)
@pytest.mark.parametrize(
    "target", ["TARGET = 'apps.core.gone'\n", "TARGET = 'apps.core' + '.gone'\n"], ids=["raw", "composed"]
)
def test_a_name_and_a_loader_imported_apart_meet_in_the_test_that_imports_both(tree, helper, selected, target):
    # `target.py` names the deleted module and `load_helper.py` loads whatever it is
    # handed; the test imports both and runs `load(TARGET)`, so it imports the module.
    (tree / "scripts/load_helper.py").write_text(helper, encoding="utf-8")
    (tree / "scripts/target.py").write_text(target, encoding="utf-8")
    test = "from scripts.load_helper import load\nfrom scripts.target import TARGET\nGONE = load(TARGET)\n"
    (tree / "tests/scripts/test_loads_gone.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    test_path = "tests/scripts/test_loads_gone.py"
    assert (test_path in selection.modules and test_path not in selection.demoted) is selected


@pytest.mark.parametrize(
    ("loader", "fn", "selected"),
    [
        ("from importlib import import_module\n", "import_module", True),
        ("", "str", False),
    ],
    ids=["test-holds-the-loader", "no-loader-control"],
)
def test_a_test_holding_the_loader_meets_a_name_it_imports(tree, loader, fn, selected):
    # The test itself holds the loader and imports the name from `target.py`, then hands
    # both to `invoke`, which only calls its function argument: the test imports `gone`.
    (tree / "scripts/invoke.py").write_text("def invoke(fn, *args):\n    return fn(*args)\n", encoding="utf-8")
    (tree / "scripts/target.py").write_text("TARGET = 'apps.core.gone'\n", encoding="utf-8")
    test = (
        loader
        + "from scripts.invoke import invoke\nfrom scripts.target import TARGET\n"
        + f"GONE = invoke({fn}, TARGET)\n"
    )
    (tree / "tests/scripts/test_loads_gone.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    test_path = "tests/scripts/test_loads_gone.py"
    assert (test_path in selection.modules and test_path not in selection.demoted) is selected


@pytest.mark.parametrize(
    ("loader", "selected"),
    [
        (
            "import importlib\ndef __getattr__(name):\n    return importlib.import_module('.' + name, __package__)\n",
            True,
        ),
        ("gone = 1\n", False),
    ],
    ids=["getattr-loader", "plain-attribute-control"],
)
@pytest.mark.parametrize(
    "test",
    ["from apps.core.loader import gone\n", "import apps.core.loader as loader\nGONE = loader.gone\n"],
    ids=["from-import", "attribute"],
)
def test_an_attribute_asked_of_a_loader_module_reaches_a_deleted_one(tree, loader, selected, test):
    # `loader.py` imports whatever attribute it is asked for, so asking for `gone` imports
    # `apps.core.gone`; a module with a plain `gone` attribute imports nothing.
    (tree / "apps/core/loader.py").write_text(loader, encoding="utf-8")
    (tree / "tests/scripts/test_loads_gone.py").write_text(test, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    test_path = "tests/scripts/test_loads_gone.py"
    assert (test_path in selection.modules and test_path not in selection.demoted) is selected


def test_an_absolute_dynamic_import_of_another_module_does_not_reach_a_deleted_one(tree):
    # Control: an import call naming a surviving sibling is not an import of `gone`.
    (tree / "apps/core/loads_engine.py").write_text(
        'import importlib\nENGINE = importlib.import_module("apps.core.engine")\n', encoding="utf-8"
    )
    (tree / "tests/other/test_loads_engine.py").write_text(
        "from apps.core.loads_engine import ENGINE\n", encoding="utf-8"
    )
    (tree / "apps/core/gone.py").unlink()
    assert "tests/other/test_loads_engine.py" not in _select(tree, ITEM3, "apps/core/gone.py").modules


def test_a_source_that_does_not_parse_is_unknown_when_a_module_is_deleted(tree):
    # Its imports cannot be read, so it may import the deleted module: never narrow past it.
    (tree / "apps/core/broken.py").write_text("from apps.core import (\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    with pytest.raises(FullSuite, match="does not parse"):
        _select(tree, ITEM3, "apps/core/gone.py")


def test_a_relative_import_of_a_surviving_sibling_does_not_select(tree):
    # Control: the dots resolve inside the importer's package, so `.engine` is not `gone`.
    (tree / "apps/core/uses_engine.py").write_text("from .engine import VALUE\n", encoding="utf-8")
    (tree / "tests/other/test_uses_engine.py").write_text("from apps.core.uses_engine import VALUE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    assert "tests/other/test_uses_engine.py" not in _select(tree, ITEM3, "apps/core/gone.py").modules


def test_a_workflow_named_only_by_a_helper_selects_the_helpers_tests(tree):
    # Only a shared helper names the file, so no test does.
    (tree / "tests/scripts/helper_fixtures.py").write_text(
        'DATA = ROOT / ".github/workflows/helper.yml"\n', encoding="utf-8"
    )
    (tree / ".github/workflows/helper.yml").write_text("{}\n", encoding="utf-8")
    (tree / "tests/other/test_uses_helper.py").write_text(
        "from tests.scripts.helper_fixtures import DATA\n", encoding="utf-8"
    )
    selection = _select(tree, ITEM3, ".github/workflows/helper.yml")
    assert selection.reasons["tests/other/test_uses_helper.py"] == ["named reference via helper"]
    assert "tests/other/test_other.py" not in selection.modules


def test_a_workflow_named_by_a_module_plugin_selects_the_test_loading_it(tree):
    # `tests/test_smartlists_delete.py` loads a plugin through module-level `pytest_plugins`.
    (tree / "tests/scripts/plugin_fixtures.py").write_text(
        'DATA = ROOT / ".github/workflows/plugin.yml"\n', encoding="utf-8"
    )
    (tree / ".github/workflows/plugin.yml").write_text("{}\n", encoding="utf-8")
    (tree / "tests/other/test_plugin_user.py").write_text(
        'pytest_plugins = ["tests.scripts.plugin_fixtures"]\n', encoding="utf-8"
    )
    selection = _select(tree, ITEM3, ".github/workflows/plugin.yml")
    assert selection.reasons["tests/other/test_plugin_user.py"] == ["named reference via helper"]
    assert "tests/other/test_other.py" not in selection.modules


def test_a_workflow_only_a_helper_walks_selects_the_helpers_tests(tree):
    # scripts/runner_toolset_scan.py globs every workflow and no test does.
    (tree / "scripts/scan_workflows.py").write_text(
        'def files(root):\n    return sorted((root / ".github" / "workflows").glob("*.yml"))\n', encoding="utf-8"
    )
    (tree / "tests/other/test_scan.py").write_text("from scripts.scan_workflows import files\n", encoding="utf-8")
    selection = _select(tree, ITEM3, ".github/workflows/new.yml")
    assert selection.reasons["tests/other/test_scan.py"] == ["coupling `by name` via helper"]
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize(
    "walk",
    [
        'S = sorted((ROOT / "scripts").glob("ci_*.py"))\n',
        'D = ROOT / "scripts"\nS = sorted(D.rglob("*.py"))\n',
        'S = sorted(ROOT.glob("scripts/ci_*.py"))\n',
        # Split so this file is not itself a git-index reader to the always-run guard.
        'S = subprocess.run(["git", "{}", "scripts/"], capture_output=True)\n'.format("-".join(("ls", "files"))),
        'S = os.listdir(os.path.join(ROOT, "scripts"))\n',
        'walk = (ROOT / "scripts").glob\nS = sorted(walk("ci_*.py"))\n',
        'ROOTS = (ROOT / "scripts", ROOT / "tools")\nS = sorted(ROOTS[0].glob("ci_*.py"))\n',
        'for root in (ROOT / "scripts",):\n    S = sorted(root.glob("ci_*.py"))\n',
        'files(ROOT / "scripts")\n',
    ],
    ids=[
        "joined-root",
        "bound-root",
        "glob-pattern",
        "git-index",
        "listdir",
        "aliased-walk-method",
        "tuple-element",
        "loop-target",
        "handed-to-a-call",
    ],
)
def test_every_spelling_of_a_ci_script_walk_selects_its_walker(tree, walk):
    (tree / "tests/other/test_scan.py").write_text(walk, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert "tests/other/test_scan.py" in _select(tree, ITEM3, "scripts/ci_new.py").modules


@pytest.mark.parametrize(
    "source",
    [
        # One named script beside a walk of another tree: ~60 modules, 473 tests selected.
        'X = REPO_ROOT / "scripts" / "x.py"\nW = sorted((ROOT / ".github").glob("*.yml"))\n',
        # A subtree no top-level CI script is in.
        'R = REPO_ROOT / "scripts" / "bench" / "ratings"\nW = sorted(R.glob("*.json"))\n',
    ],
    ids=["named-file-beside-other-walk", "walk-of-a-subtree"],
)
def test_a_scripts_path_that_no_walk_reaches_is_not_a_ci_script_reader(tree, source):
    (tree / "tests/other/test_names.py").write_text(source, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert "tests/other/test_names.py" not in _select(tree, ITEM3, "scripts/ci_new.py").modules


@pytest.mark.parametrize("via_helper", [False, True])
def test_a_ci_script_only_a_tree_walk_reaches_selects_its_walkers(tree, via_helper):
    # A validator that globs every CI script never names the new one.
    walk = 'S = sorted((ROOT / "scripts").glob("ci_*.py"))\n'
    if via_helper:
        (tree / "scripts/scan_scripts.py").write_text(walk, encoding="utf-8")
        (tree / "tests/other/test_scan.py").write_text("from scripts.scan_scripts import S\n", encoding="utf-8")
    else:
        (tree / "tests/other/test_scan.py").write_text(walk, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    selection = _select(tree, ITEM3, "scripts/ci_new.py")
    assert "tests/other/test_scan.py" in selection.modules
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize(
    "test",
    [
        'from scripts.ci_paths import SCRIPTS\nS = sorted(SCRIPTS.glob("ci_*.py"))\n',
        'from scripts.ci_paths import SCRIPTS as R\nD = R\nS = sorted(D.rglob("*.py"))\n',
        'import scripts.ci_paths\nS = sorted(scripts.ci_paths.SCRIPTS.glob("ci_*.py"))\n',
        "from scripts import ci_paths\nS = sorted(ci_paths.SCRIPTS.iterdir())\n",
        'from scripts.ci_paths import SCRIPTS\nwalk = SCRIPTS.glob\nS = sorted(walk("ci_*.py"))\n',
        'from scripts.ci_paths import SCRIPTS\nROOTS = [SCRIPTS]\nS = sorted(ROOTS[0].glob("ci_*.py"))\n',
        'from scripts.ci_paths import SCRIPTS\nROOTS = {"ci": SCRIPTS}\nS = sorted(ROOTS["ci"].iterdir())\n',
        'from scripts.ci_paths import SCRIPTS\nfor root in [SCRIPTS]:\n    S = sorted(root.glob("ci_*.py"))\n',
        'from scripts.ci_paths import SCRIPTS\nS = [p for root in (SCRIPTS,) for p in root.glob("ci_*.py")]\n',
        'from pathlib import Path\nfrom scripts.ci_paths import SCRIPTS\nS = sorted(Path(SCRIPTS).glob("ci_*.py"))\n',
        "import os\nfrom scripts.ci_paths import SCRIPTS\nS = os.listdir(str(SCRIPTS))\n",
        'from scripts.ci_paths import SCRIPTS\nS = sorted(SCRIPTS.resolve().glob("ci_*.py"))\n',
        'import glob\nfrom scripts.ci_paths import SCRIPTS\nS = glob.glob(str(SCRIPTS / "ci_*.py"))\n',
        'import glob, os\nfrom scripts.ci_paths import SCRIPTS\nS = glob.glob(os.path.join(SCRIPTS, "ci_*.py"))\n',
        'from glob import glob as g\nfrom scripts.ci_paths import SCRIPTS\nS = g(str(SCRIPTS / "ci_*.py"))\n',
        'from pathlib import Path as P\nfrom scripts.ci_paths import SCRIPTS\nS = sorted(P(SCRIPTS).glob("ci_*.py"))\n',
        'from functools import partial\nfrom scripts.ci_paths import SCRIPTS\nwalk = partial(SCRIPTS.glob, "ci_*.py")\nS = sorted(walk())\n',
        "import functools, os\nfrom scripts.ci_paths import SCRIPTS\nS = list(functools.partial(os.walk, SCRIPTS)())\n",
    ],
    ids=[
        "from-import",
        "alias-rebound",
        "module-attribute",
        "submodule-attribute",
        "aliased-walk-method",
        "list-element",
        "dict-value",
        "loop-target",
        "comprehension-target",
        "path-wrapper",
        "str-wrapper",
        "resolved",
        "glob-segment",
        "join-glob-segment",
        "aliased-walker",
        "aliased-path-wrapper",
        "partial-method",
        "partial-function-called",
    ],
)
def test_a_ci_script_root_bound_in_one_module_and_walked_in_another_selects_the_walker(tree, test):
    # `paths.py` binds the root, the test walks it: neither module is a walker alone.
    (tree / "scripts/ci_paths.py").write_text('SCRIPTS = ROOT / "scripts"\n', encoding="utf-8")
    (tree / "tests/other/test_scan.py").write_text(test, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    selection = _select(tree, ITEM3, "scripts/ci_new.py")
    assert "tests/other/test_scan.py" in selection.modules
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize(
    ("wrappers", "test", "selected"),
    [
        (
            {"scripts/ci_wrap.py": "from scripts.ci_paths import SCRIPTS\n"},
            'from scripts.ci_wrap import SCRIPTS\nS = sorted(SCRIPTS.glob("ci_*.py"))\n',
            True,
        ),
        (
            {
                "scripts/ci_wrap.py": "from scripts.ci_paths import SCRIPTS as TREE\n",
                "scripts/ci_wrap2.py": "from scripts.ci_wrap import TREE\n",
            },
            'from scripts.ci_wrap2 import TREE\nS = sorted(TREE.rglob("*.py"))\n',
            True,
        ),
        (
            {"scripts/ci_wrap.py": "import scripts.ci_paths as paths\nSCRIPTS = paths.SCRIPTS\n"},
            'from scripts.ci_wrap import SCRIPTS\nS = sorted(SCRIPTS.glob("ci_*.py"))\n',
            True,
        ),
    ],
    ids=["re-export", "renamed-re-export-chain", "module-attribute-re-export"],
)
def test_a_ci_script_root_re_exported_by_another_module_selects_the_walker(tree, wrappers, test, selected):
    paths = 'SCRIPTS = ROOT / "scripts"\nONE = ROOT / "scripts" / "x.py"\n'
    (tree / "scripts/ci_paths.py").write_text(paths, encoding="utf-8")
    for path, source in wrappers.items():
        (tree / path).write_text(source, encoding="utf-8")
    (tree / "tests/other/test_scan.py").write_text(test, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert ("tests/other/test_scan.py" in _select(tree, ITEM3, "scripts/ci_new.py").modules) is selected


@pytest.mark.parametrize(
    ("helper", "home", "test", "selected"),
    [
        (
            'from pathlib import Path\nS = sorted(Path(__file__).parent.glob("ci_*.py"))\n',
            "scripts/lister.py",
            None,
            True,
        ),
        (
            'from pathlib import Path\nHERE = Path(__file__).resolve()\nS = sorted(HERE.parent.glob("ci_*.py"))\n',
            "scripts/lister.py",
            None,
            True,
        ),
        ("import os\nS = os.listdir(os.path.dirname(os.path.abspath(__file__)))\n", "scripts/lister.py", None, True),
        (
            'import glob, os\nS = glob.glob(os.path.join(os.path.dirname(__file__), "ci_*.py"))\n',
            "scripts/lister.py",
            None,
            True,
        ),
        (
            'from pathlib import Path\nS = sorted(Path(__file__).parents[1].joinpath("x").glob("*"))\n',
            "scripts/sub/lister.py",
            None,
            False,
        ),
        (
            'from pathlib import Path\nS = sorted(Path(__file__).parents[1].glob("ci_*.py"))\n',
            "scripts/sub/lister.py",
            None,
            True,
        ),
        (
            "from pathlib import Path\nHERE = Path(__file__).parent\n",
            "scripts/here.py",
            'from scripts.here import HERE\nS = sorted(HERE.glob("ci_*.py"))\n',
            True,
        ),
        ('import glob\nS = glob.glob(str(ROOT / "scripts" / "ci_*.py"))\n', "scripts/lister.py", None, True),
        # Controls: the directory read off `__file__` is not the root.
        (
            'from pathlib import Path\nS = sorted(Path(__file__).parent.parent.glob("*.yml"))\n',
            "scripts/lister.py",
            None,
            False,
        ),
        (
            'from pathlib import Path\nS = sorted((Path(__file__).parent / "bench").glob("*.py"))\n',
            "scripts/lister.py",
            None,
            False,
        ),
        (
            'from pathlib import Path\nS = sorted(Path(__file__).parent.glob("*.py"))\n',
            "tests/other/lister.py",
            None,
            False,
        ),
        ('from pathlib import Path\nS = sorted(Path(__file__).glob("*"))\n', "scripts/lister.py", None, False),
    ],
    ids=[
        "file-parent",
        "file-resolved-bound",
        "dirname-listdir",
        "dirname-join-glob",
        "parents-subdir-control",
        "parents-index",
        "file-root-exported",
        "literal-glob-segment",
        "file-grandparent-control",
        "file-subdir-control",
        "other-tree-control",
        "file-itself-control",
    ],
)
def test_a_ci_script_root_read_off_dunder_file_selects_the_walker(tree, helper, home, test, selected):
    # `Path(__file__).parent.glob("ci_*.py")` in a module under `scripts/` lists every CI
    # script without spelling `"scripts"`, so a test importing it reads a new one.
    (tree / home).parent.mkdir(parents=True, exist_ok=True)
    (tree / home).write_text(helper, encoding="utf-8")
    module = home.removesuffix(".py").replace("/", ".")
    (tree / "tests/other/test_scan.py").write_text(test or f"import {module}\n", encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert ("tests/other/test_scan.py" in _select(tree, ITEM3, "scripts/ci_new.py").modules) is selected


# The walker side is coarse (nightly review, Sun 18 Oct 2026): a module that binds the root
# itself (`SCRIPTS = Path("scripts")`) selects every test importing it, so a walk through
# another module, an alias, a wrapper, a method, a lambda or `partial` over-selects instead
# of being resolved. These were each resolved one by one before; now none needs a rule.
@pytest.mark.parametrize(
    "test",
    [
        "from scripts.ci_paths import SCRIPTS\nfrom scripts.walker import files\nFOUND = list(files(SCRIPTS))\n",
        "from scripts.ci_paths import SCRIPTS\nfrom scripts import walker\nFOUND = list(walker.files(SCRIPTS))\n",
        "from scripts.ci_paths import SCRIPTS as TREE\nfrom scripts.walker import files as f\nFOUND = list(f(TREE))\n",
        "from scripts.ci_paths import SCRIPTS\nfrom scripts.walker import Walker\nFOUND = list(Walker().files(SCRIPTS))\n",
        "from scripts.ci_paths import SCRIPTS\nfrom scripts.walker import scan\nFOUND = list(scan(SCRIPTS))\n",
        "from functools import partial\nfrom scripts.ci_paths import SCRIPTS\nwalk = partial(SCRIPTS.glob, '*')\n",
        "from scripts.wrapper import wrapped\nFOUND = list(wrapped())\n",
        "from scripts.ci_unpacked import SCRIPTS\nFOUND = sorted(SCRIPTS.glob('ci_*.py'))\n",
        # Over-selection, accepted: the test imports the root and walks another tree.
        'from pathlib import Path\nfrom scripts.ci_paths import SCRIPTS\nFOUND = sorted(Path("tests").glob("*"))\n',
    ],
    ids=[
        "generic-walker",
        "walker-attribute",
        "aliased-root-and-walker",
        "walker-method",
        "walker-alias",
        "partial",
        "wrapper-module",
        "destructured-root",
        "other-tree-over-selected",
    ],
)
def test_any_use_of_an_imported_root_selects_the_caller(tree, test):
    (tree / "scripts/ci_paths.py").write_text('from pathlib import Path\nSCRIPTS = Path("scripts")\n', encoding="utf-8")
    (tree / "scripts/walker.py").write_text(
        'def files(root):\n    return sorted(root.glob("ci_*.py"))\nscan = files\n'
        "class Walker:\n    def files(self, root):\n        return files(root)\n",
        encoding="utf-8",
    )
    (tree / "scripts/wrapper.py").write_text(
        "from scripts.ci_paths import SCRIPTS\nfrom scripts.walker import files\n"
        "def wrapped():\n    return files(SCRIPTS)\n",
        encoding="utf-8",
    )
    (tree / "scripts/ci_unpacked.py").write_text(
        'from pathlib import Path\nSCRIPTS, OTHER = Path("scripts"), None\n', encoding="utf-8"
    )
    (tree / "tests/other/test_scan.py").write_text(test, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert "tests/other/test_scan.py" in _select(tree, ITEM3, "scripts/ci_new.py").modules
