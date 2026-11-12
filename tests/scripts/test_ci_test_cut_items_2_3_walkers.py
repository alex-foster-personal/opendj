"""TEST-CUT items 2 and 3 narrow a PR head further (item 3 walkers, item 2; split from test_ci_test_cut_items_2_3.py).

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
    DEMOTE_SCRIPTS,
    ITEM3,
    _select,
    build_tree,
)

pytestmark = [pytest.mark.requirement("TESTCUT-02"), pytest.mark.requirement("TESTCUT-03")]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return build_tree(tmp_path)


@pytest.mark.parametrize(
    "test",
    [
        'import pkgutil\nNAMES = [m.name for m in pkgutil.iter_modules(["scripts"])]\n',
        'from pkgutil import iter_modules\nNAMES = [m.name for m in iter_modules(["scripts"])]\n',
        'import pkgutil\nNAMES = [m.name for m in pkgutil.walk_packages(["scripts"])]\n',
        "import pkgutil\nimport scripts\nNAMES = [m.name for m in pkgutil.iter_modules(scripts.__path__)]\n",
        "import pkgutil\nimport scripts\nNAMES = [m.name for m in pkgutil.iter_modules(scripts.__path__[:1])]\n",
        "import pkgutil\nimport scripts as s\nNAMES = [m.name for m in pkgutil.iter_modules(s.__path__)]\n",
    ],
    ids=["iter-modules", "iter-modules-imported", "walk-packages", "package-path", "package-path-slice", "aliased"],
)
def test_a_package_enumeration_of_the_scripts_selects_the_test(tree, test):
    # Any package's `__path__` counts, so an alias over-selects rather than being resolved.
    (tree / "tests/other/test_scan.py").write_text(test, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert "tests/other/test_scan.py" in _select(tree, ITEM3, "scripts/ci_new.py").modules


@pytest.mark.parametrize(
    ("paths", "test"),
    [
        # One file under the root, imported and walked, is not the root.
        (
            'ONE = ROOT / "scripts" / "x.py"\n',
            'from scripts.ci_paths import ONE\nS = sorted(ONE.glob("*"))\n',
        ),
        # A subtree under the root is not the root.
        (
            'BENCH = ROOT / "scripts" / "bench"\n',
            'from scripts.ci_paths import BENCH\nS = sorted(BENCH.glob("*"))\n',
        ),
        # A pathspec glob that cannot list a CI script.
        ("SPEC = ('scripts/review_*.py',)\n", "from scripts.ci_paths import SPEC\nS = sorted(ROOT.glob(SPEC[0]))\n"),
        # Prose that mentions the word is not a path.
        (
            "HELP = 'scripts that validate the personas'\n",
            "import argparse\nfrom scripts.ci_paths import HELP\nP = argparse.ArgumentParser(description=HELP)\n",
        ),
        # The root's name as one part of a joined file path is not the root.
        ("X = 1\n", 'import os\nONE = os.path.join(ROOT, "scripts", "x.py")\nS = sorted(TMP.glob("*"))\n'),
    ],
    ids=["one-file", "subtree", "review-pathspec", "prose", "joined-file"],
)
def test_a_module_naming_less_than_the_scripts_root_is_not_a_ci_script_reader(tree, paths, test):
    (tree / "scripts/ci_paths.py").write_text(paths, encoding="utf-8")
    (tree / "tests/other/test_scan.py").write_text(test, encoding="utf-8")
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    selection = _select(tree, ITEM3, "scripts/ci_new.py")
    assert "tests/other/test_scan.py" not in selection.modules
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize(
    "source",
    [
        'import builtins\ngetattr(builtins, "pr" + "int")("apps.core.gone")\nGONE = None\n',
        'from builtins import print as run\nrun("apps.core.gone")\nGONE = None\n',
        'import builtins as b\nb.print("apps.core.gone")\nGONE = None\n',
    ],
    ids=["composed-getattr", "imported-alias", "module-alias"],
)
def test_a_builtin_looked_up_by_a_composed_name_that_runs_no_source_is_still_demoted(tree, source):
    # Control for the builtin runners: `print` executes nothing, however it is reached.
    (tree / "apps/core/loads_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/scripts/test_loads_gone.py").write_text("from apps.core.loads_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_gone.py" in selection.demoted


def test_a_walk_of_tests_scripts_is_not_a_ci_script_reader(tree):
    # Control: `tests/scripts/...` and `scripts.x` imports spell `scripts` without reading that tree.
    (tree / "tests/other/test_walks_tests.py").write_text(
        'from scripts.ci_helper import Y\nT = sorted((ROOT / "tests/scripts").glob("test_*.py"))\n', encoding="utf-8"
    )
    (tree / "scripts/ci_new.py").write_text("Z = 1\n", encoding="utf-8")
    assert "tests/other/test_walks_tests.py" not in _select(tree, ITEM3, "scripts/ci_new.py").modules


@pytest.mark.parametrize("via_helper", [False, True])
def test_a_conftest_that_walks_the_workflows_selects_the_tests_under_it(tree, via_helper):
    # pytest runs tests/area/conftest.py at collection for every test beneath it.
    walk = 'W = sorted((ROOT / ".github" / "workflows").glob("*.yml"))\n'
    (tree / "tests/area").mkdir()
    (tree / "tests/area/__init__.py").write_text("", encoding="utf-8")
    (tree / "tests/area/test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    if via_helper:
        (tree / "scripts/scan_workflows.py").write_text(walk, encoding="utf-8")
        (tree / "tests/area/conftest.py").write_text("from scripts.scan_workflows import W\n", encoding="utf-8")
    else:
        (tree / "tests/area/conftest.py").write_text(walk, encoding="utf-8")
    selection = _select(tree, ITEM3, ".github/workflows/new.yml")
    assert selection.reasons["tests/area/test_x.py"] == ["coupling `by name` via conftest `tests/area/conftest.py`"]
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize("helper_reads", [True, False])
def test_a_test_that_points_a_generic_walker_at_the_workflows_is_selected(tree, helper_reads):
    # The test names the tree, the helper it imports does the walking; neither matches alone.
    body = 'return sorted(root.glob("*.yml"))' if helper_reads else "return [root]"
    (tree / "scripts/walk.py").write_text(f"def files(root):\n    {body}\n", encoding="utf-8")
    (tree / "tests/other/test_split.py").write_text(
        'from pathlib import Path\nfrom scripts.walk import files\nW = files(Path(".github/workflows"))\n',
        encoding="utf-8",
    )
    selection = _select(tree, ITEM3, ".github/workflows/new.yml")
    if helper_reads:
        assert selection.reasons["tests/other/test_split.py"] == ["coupling `by name` via helper"]
    else:
        # Control: a helper that only takes the path reads nothing under it.
        assert "tests/other/test_split.py" not in selection.modules


@pytest.mark.parametrize("scan_reads", [True, False])
def test_a_reader_importing_the_workflow_path_is_selected(tree, scan_reads):
    # paths.py names the tree, scan.py imports that path and walks it, the test imports scan.py.
    (tree / "scripts/paths.py").write_text(
        'from pathlib import Path\nROOT = Path(".github/workflows")\n', encoding="utf-8"
    )
    body = 'sorted(ROOT.glob("*.yml"))' if scan_reads else "[ROOT]"
    (tree / "scripts/scan.py").write_text(f"from scripts.paths import ROOT\nFILES = {body}\n", encoding="utf-8")
    (tree / "tests/other/test_scan.py").write_text("from scripts.scan import FILES\n", encoding="utf-8")
    selected = _select(tree, ITEM3, ".github/workflows/new.yml").modules
    # Control: a module that imports the path and reads nothing under it selects nothing.
    assert ("tests/other/test_scan.py" in selected) is scan_reads
    assert "tests/other/test_other.py" not in selected


@pytest.mark.parametrize("files_reads", [True, False])
def test_a_module_handing_an_imported_path_to_an_imported_reader_is_selected(tree, files_reads):
    # paths.py names the tree, files.py walks whatever it is given, scan.py joins the two.
    (tree / "scripts/paths.py").write_text(
        'from pathlib import Path\nROOT = Path(".github/workflows")\n', encoding="utf-8"
    )
    body = 'sorted(root.glob("*.yml"))' if files_reads else "[root]"
    (tree / "scripts/files.py").write_text(f"def files(root):\n    return {body}\n", encoding="utf-8")
    (tree / "scripts/scan.py").write_text(
        "from scripts.files import files\nfrom scripts.paths import ROOT\nFILES = files(ROOT)\n", encoding="utf-8"
    )
    (tree / "tests/other/test_scan.py").write_text("from scripts.scan import FILES\n", encoding="utf-8")
    selected = _select(tree, ITEM3, ".github/workflows/new.yml").modules
    # Control: when files.py reads nothing under the path, scan.py walks nothing either.
    assert ("tests/other/test_scan.py" in selected) is files_reads
    assert "tests/other/test_other.py" not in selected


def test_a_helper_naming_github_without_walking_it_does_not_select(tree):
    # Control: the coupling needs a tree reader too, in a helper as in a test.
    (tree / "scripts/one_workflow.py").write_text('CI = ROOT / ".github/workflows/ci.yml"\n', encoding="utf-8")
    (tree / "tests/other/test_one.py").write_text("from scripts.one_workflow import CI\n", encoding="utf-8")
    assert "tests/other/test_one.py" not in _select(tree, ITEM3, ".github/workflows/new.yml").modules


def test_an_ast_walk_or_an_os_import_is_not_a_workflow_walker(tree):
    # Control: `ast.walk` walks a syntax tree, and `from os import path` imports no walker.
    (tree / "tests/other/test_ast.py").write_text(
        'import ast\nfrom os import path, sep\nCI = ".github/workflows/ci.yml"\nNODES = list(ast.walk(ast.parse("")))\n'
        "WALK = ast.walk\nimport scripts.walk\nfrom scripts.glob import x\nGLOBBED = scripts.walk.x\n",
        encoding="utf-8",
    )
    assert "tests/other/test_ast.py" not in _select(tree, ITEM3, ".github/workflows/new.yml").modules


def test_a_helper_citing_github_in_prose_is_not_a_workflow_walker(tree):
    # The root tests/conftest.py cites a workflow in a docstring and rglobs test files.
    (tree / "scripts/prose.py").write_text(
        '"""See .github/workflows/ci.yml."""\n# also .github here\nFILES = sorted(ROOT.rglob("test_*.py"))\n',
        encoding="utf-8",
    )
    (tree / "tests/other/test_prose.py").write_text("from scripts.prose import FILES\n", encoding="utf-8")
    assert "tests/other/test_prose.py" not in _select(tree, ITEM3, ".github/workflows/new.yml").modules


def test_a_test_naming_a_workflow_selects_the_tests_importing_it(tree):
    # tests/test_codex_followups_d.py imports tests/test_conformance.py, so a test can be a helper.
    (tree / "tests/other/test_names_workflow.py").write_text(
        'DATA = ROOT / ".github/workflows/helper.yml"\n', encoding="utf-8"
    )
    (tree / ".github/workflows/helper.yml").write_text("{}\n", encoding="utf-8")
    (tree / "tests/other/test_imports_test.py").write_text(
        "from tests.other.test_names_workflow import DATA\n", encoding="utf-8"
    )
    selection = _select(tree, ITEM3, ".github/workflows/helper.yml")
    assert "tests/other/test_imports_test.py" in selection.modules
    assert "tests/other/test_other.py" not in selection.modules


@pytest.mark.parametrize("target", ['"scripts/ci_helper.py"', '"scripts.ci_helper"'])
def test_a_ci_script_named_only_by_a_helper_selects_the_helpers_tests(tree, target):
    (tree / "tests/other/ci_target.py").write_text(f"TARGET = {target}\n", encoding="utf-8")
    (tree / "tests/other/test_ci_target.py").write_text("from tests.other.ci_target import TARGET\n", encoding="utf-8")
    selection = _select(tree, ITEM3, "scripts/ci_helper.py")
    assert "tests/other/test_ci_target.py" in selection.modules
    assert "tests/other/test_other.py" not in selection.modules


def test_a_workflow_named_only_by_a_conftest_selects_its_directory(tree):
    (tree / "tests/other/conftest.py").write_text('DATA = ROOT / ".github/workflows/helper.yml"\n', encoding="utf-8")
    (tree / ".github/workflows/helper.yml").write_text("{}\n", encoding="utf-8")
    selection = _select(tree, ITEM3, ".github/workflows/helper.yml")
    assert "tests/other/test_other.py" in selection.modules
    # control: a module outside the conftest's directory that neither names nor imports it
    assert "tests/scripts/test_reads_notes.py" not in selection.modules


def test_the_always_run_list_stays_full_under_item_3(tree):
    # The list decides what runs, so a head that edits it must not be judged by itself.
    with pytest.raises(FullSuite, match="pr-always-run"):
        _select(tree, ITEM3, "ci/pr-always-run.txt")


def test_a_deleted_module_is_unknown_without_item_3(tree):
    (tree / "apps/core/gone.py").unlink()
    with pytest.raises(FullSuite, match="UNKNOWN"):
        _select(tree, Policy(), "apps/core/gone.py")


def test_a_present_module_the_graph_skips_stays_unknown_under_item_3(tree):
    skipped = tree / "apps/build/made.py"  # build/ is a SKIP_PARTS dir of the import graph
    skipped.parent.mkdir(parents=True)
    skipped.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(FullSuite, match="UNKNOWN"):
        _select(tree, ITEM3, "apps/build/made.py")


# ---------------------------------------------------------------------------
# item 2: weak-link demotion
# ---------------------------------------------------------------------------


def test_without_item_2_weak_links_still_run(tree):
    selection = _select(tree, Policy(), "apps/core/notes.md")
    assert "tests/scripts/test_reads_notes.py" in selection.modules
    assert selection.demoted == {}


def test_a_weak_only_module_in_a_demoted_dir_leaves_the_pr_head(tree):
    selection = _select(tree, DEMOTE_SCRIPTS, "apps/core/notes.md")
    assert "tests/scripts/test_reads_notes.py" not in selection.modules
    assert selection.demoted["tests/scripts/test_reads_notes.py"] == ["named reference"]


def test_a_weak_only_module_outside_the_demoted_dirs_still_runs(tree):
    selection = _select(tree, DEMOTE_SCRIPTS, "apps/core/notes.md")
    assert "tests/other/test_reads_notes_too.py" in selection.modules


def test_an_import_graph_module_is_never_demoted_even_if_also_named(tree):
    selection = _select(tree, DEMOTE_SCRIPTS, "apps/core/engine.py")
    assert "tests/scripts/test_engine.py" in selection.modules
    assert "tests/scripts/test_names_engine.py" in selection.demoted


def test_an_always_run_guard_is_never_demoted(tree):
    selection = _select(tree, Policy(demote_weak_under=("tests",)), "apps/core/notes.md")
    assert "tests/scripts/test_always.py" in selection.modules
    assert "tests/other/test_reads_notes_too.py" in selection.demoted


def test_test_area_data_is_a_weak_link(tree):
    selection = _select(tree, DEMOTE_SCRIPTS, "tests/scripts/data/golden.csv")
    assert selection.demoted["tests/scripts/test_engine.py"] == ["test-area data `tests/scripts/data/golden.csv`"]


def test_a_changed_test_module_is_never_demoted(tree):
    selection = _select(tree, DEMOTE_SCRIPTS, "tests/scripts/test_reads_notes.py")
    assert "tests/scripts/test_reads_notes.py" in selection.modules
