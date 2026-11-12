"""TEST-CUT items 2 and 3 narrow a PR head further, and only when their variables ask.

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
    global_reason,
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


# ---------------------------------------------------------------------------
# item 3: workflows, CI scripts and deleted modules by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", "ci/runner/setup.sh", "scripts/ci_helper.py", "tests/fixtures/library.json"]
)
def test_without_item_3_ci_config_still_forces_full(path):
    assert global_reason(path) is not None
    assert global_reason(path, Policy()) is not None


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "scripts/ci_helper.py"])
def test_item_3_selects_ci_config_by_name(path):
    assert global_reason(path, ITEM3) is None


@pytest.mark.parametrize("path", ["ci/runner/setup.sh", "ci/other.txt", "tests/fixtures/library.json"])
def test_item_3_leaves_ci_dir_and_fixtures_full(path):
    # Sat 17 Oct 2026: each review round found another way these reach a test, so they stay
    # FULL until one strong/weak edge graph replaces the per-rule seeding.
    assert global_reason(path, ITEM3) is not None


@pytest.mark.parametrize(
    "path",
    [
        "scripts/ci_commented_list.py",
        "scripts/ci_test_selection.py",
        "scripts/ci_test_selection_rules.py",
        "scripts/affected_tests.py",
    ],
)
def test_the_selectors_own_source_stays_global_under_item_3(path):
    assert global_reason(path, ITEM3) is not None


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "tests/conftest.py"])
def test_item_3_leaves_every_other_global_path_global(path):
    assert global_reason(path, ITEM3) is not None


def test_a_workflow_edit_selects_the_tests_that_name_it_or_walk_the_workflows(tree):
    selected = _select(tree, ITEM3, ".github/workflows/ci.yml").modules
    assert "tests/scripts/test_workflow.py" in selected
    assert "tests/scripts/test_all_workflows.py" in selected
    assert "tests/other/test_other.py" not in selected


def test_a_new_workflow_nothing_names_still_reaches_the_tree_walker(tree):
    selected = _select(tree, ITEM3, ".github/workflows/new.yml").modules
    assert "tests/scripts/test_all_workflows.py" in selected
    assert "tests/scripts/test_workflow.py" not in selected


@pytest.mark.parametrize(
    "walker",
    [
        'import glob\nFILES = list(glob.iglob(".github/workflows/*.yml"))\n',
        'from glob import glob\nFILES = glob(".github/workflows/*.yml")\n',
        'from glob import iglob\nFILES = list(iglob(".github/workflows/*.yml"))\n',
        'import os\nFILES = [f for _, _, fs in os.fwalk(".github") for f in fs]\n',
        'from pathlib import Path\nFILES = [f for _, _, fs in Path(".github").walk() for f in fs]\n',
        'from pathlib import Path\nFILES = [f for _, _, fs in Path(".github").walk(top_down=False) for f in fs]\n',
        'from pathlib import Path\nFILES = [f for _, _, fs in Path(".github").walk(False) for f in fs]\n',
        'from os import walk\nFILES = [f for _, _, fs in walk(".github/workflows") for f in fs]\n',
        'from os import walk as w\nFILES = [f for _, _, fs in w(".github/workflows") for f in fs]\n',
        'import os\nwalk = os.walk\nFILES = [f for _, _, fs in walk(".github/workflows") for f in fs]\n',
        'from os import listdir as ls\nFILES = ls(".github/workflows")\n',
        'from glob import iglob as g\nFILES = list(g(".github/workflows/*.yml"))\n',
        'from pathlib import Path\ng = Path.glob\nFILES = list(g(Path(".github/workflows"), "*.yml"))\n',
        'import os\nFILES = list(map(os.listdir, [".github/workflows"]))\n',
        'from pathlib import Path\ng = getattr(Path, "rglob")\nFILES = list(g(Path(".github"), "*.yml"))\n',
    ],
)
def test_every_glob_and_walk_form_reaches_a_new_workflow(tree, walker):
    (tree / "tests/other/test_walker.py").write_text(walker, encoding="utf-8")
    selected = _select(tree, ITEM3, ".github/workflows/new.yml").modules
    assert "tests/other/test_walker.py" in selected
    assert "tests/other/test_other.py" not in selected


@pytest.mark.parametrize(
    "reader",
    [
        'NAME = "new" + ".yml"\nDATA = (ROOT / ".github/workflows" / NAME).read_text()\n',
        'NAME = "n" + "ew.yml"\nDATA = (ROOT / ".github/workflows" / NAME).read_text()\n',
        'STEM = "new"\nDATA = (ROOT / ".github/workflows" / f"{STEM}.yml").read_text()\n',
        'DATA = (ROOT / ".github/workflows" / ("%s.yml" % "new")).read_text()\n',
        'DATA = (ROOT / ".github/workflows" / "{}.{}".format("new", "yml")).read_text()\n',
        # A chain of names, each written before the one it is built from: one pass per link.
        'def load():\n    return (ROOT / ".github/workflows" / NAME).read_text()\n'
        'NAME = BASE + ".yml"\nBASE = MID\nMID = STEM\nSTEM = "new"\n',
    ],
)
def test_a_workflow_named_in_pieces_is_found(tree, reader):
    # `"new" + ".yml"` writes neither `new.yml` nor `.github/workflows/new.yml`.
    (tree / "tests/other/test_pieces.py").write_text(reader, encoding="utf-8")
    selected = _select(tree, ITEM3, ".github/workflows/new.yml").modules
    assert "tests/other/test_pieces.py" in selected
    assert "tests/other/test_other.py" not in selected


def test_a_workflow_named_in_pieces_selects_through_a_helper(tree):
    (tree / "scripts/pieces.py").write_text('STEM = "new"\nNAME = STEM + ".yml"\n', encoding="utf-8")
    (tree / "tests/other/test_uses_pieces.py").write_text("from scripts.pieces import NAME\n", encoding="utf-8")
    assert "tests/other/test_uses_pieces.py" in _select(tree, ITEM3, ".github/workflows/new.yml").modules


def test_a_different_workflow_named_in_pieces_is_not_selected(tree):
    # Control: `"old" + ".yml"` folds to a workflow that did not change.
    (tree / "tests/other/test_old_pieces.py").write_text(
        'NAME = "old" + ".yml"\nDATA = (ROOT / ".github/workflows" / NAME).read_text()\n', encoding="utf-8"
    )
    assert "tests/other/test_old_pieces.py" not in _select(tree, ITEM3, ".github/workflows/new.yml").modules


def test_a_fixture_edit_is_full_under_item_3(tree):
    with pytest.raises(FullSuite, match="global path changed"):
        _select(tree, ITEM3, "tests/fixtures/library.json")


def test_a_ci_config_edit_is_full_without_item_3(tree):
    with pytest.raises(FullSuite, match="global path changed"):
        _select(tree, Policy(), ".github/workflows/ci.yml")


def test_a_deleted_module_selects_the_test_still_importing_it(tree):
    (tree / "apps/core/gone.py").unlink()
    selected = _select(tree, ITEM3, "apps/core/gone.py").modules
    assert "tests/scripts/test_imports_gone.py" in selected
    assert "tests/other/test_other.py" not in selected


@pytest.mark.parametrize(
    "source",
    ["from apps.core import gone\n", "from apps.core import (\n    engine,\n    gone as g,\n)\n"],
)
def test_a_deleted_module_selects_a_test_importing_it_from_its_package(tree, source):
    # `from apps.core import gone` never writes `apps.core.gone`, and the graph cannot
    # reach a deleted module, so the from-import itself has to name it.
    (tree / "tests/scripts/test_from_package.py").write_text(source, encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selected = _select(tree, ITEM3, "apps/core/gone.py").modules
    assert "tests/scripts/test_from_package.py" in selected
    assert "tests/other/test_other.py" not in selected


def test_a_deleted_module_selects_tests_reaching_it_through_a_surviving_importer(tree):
    # apps/core/uses_gone.py still imports the deleted module and is not in the diff, so
    # the graph has no edge to follow: the module naming it seeds the reach instead.
    (tree / "apps/core/uses_gone.py").write_text("from apps.core.gone import GONE\n", encoding="utf-8")
    (tree / "tests/other/test_uses_gone.py").write_text("from apps.core.uses_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selected = _select(tree, ITEM3, "apps/core/gone.py").modules
    assert "tests/other/test_uses_gone.py" in selected
    assert "tests/other/test_other.py" not in selected


@pytest.mark.parametrize(
    "source",
    [
        "from .gone import GONE\n",
        "from . import gone\n",
        "from .. import core\nfrom ..core.gone import GONE\n",
        "from apps.core import \\\n    gone\n",
        "from apps.core import (engine,  # comment\n    gone)\n",
    ],
)
def test_a_deleted_module_selects_tests_reaching_it_through_any_import_form(tree, source):
    # `from .gone import GONE` writes neither `apps.core.gone` nor a path to it.
    (tree / "apps/core/uses_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/other/test_uses_gone.py").write_text("from apps.core.uses_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selected = _select(tree, ITEM3, "apps/core/gone.py").modules
    assert "tests/other/test_uses_gone.py" in selected
    assert "tests/other/test_other.py" not in selected


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nGONE = importlib.import_module(".gone", __package__)\n',
        'from importlib import import_module\nGONE = import_module("..core.gone", package="apps.core")\n',
        'import importlib\nNAME = ".gone"\nGONE = importlib.import_module(NAME, "apps.core")\n',
        'import importlib\nGONE = importlib.import_module(".core.gone", "apps")\n',
        'import importlib\nGONE = importlib.import_module(name=".gone", package=__package__)\n',
        'import importlib\nGONE = importlib.import_module(**{"name": ".gone", "package": __package__})\n',
        'import importlib\nKW = {"name": ".gone", "package": __package__}\nGONE = importlib.import_module(**KW)\n',
        # An escaped loader may be any call, so its relative name still counts.
        'import importlib\nLOADERS = {"py": importlib.import_module}\nGONE = LOADERS["py"](".gone", __package__)\n',
    ],
)
def test_a_deleted_module_loaded_by_a_relative_dynamic_name_is_found(tree, source):
    # `import_module(".gone", __package__)` names neither `apps.core.gone` nor `gone.py`.
    (tree / "apps/core/uses_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/other/test_uses_gone.py").write_text("from apps.core.uses_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selected = _select(tree, ITEM3, "apps/core/gone.py").modules
    assert "tests/other/test_uses_gone.py" in selected
    assert "tests/other/test_other.py" not in selected


def test_a_relative_dynamic_name_of_another_module_does_not_reach_a_deleted_one(tree):
    # Control: `.engine` resolves to apps.core.engine, which is not the deleted module.
    (tree / "apps/core/uses_engine.py").write_text(
        'import importlib\nENGINE = importlib.import_module(".engine", __package__)\n', encoding="utf-8"
    )
    (tree / "tests/other/test_uses_engine.py").write_text(
        "from apps.core.uses_engine import ENGINE\n", encoding="utf-8"
    )
    (tree / "apps/core/gone.py").unlink()
    assert "tests/other/test_uses_engine.py" not in _select(tree, ITEM3, "apps/core/gone.py").modules


@pytest.mark.parametrize(
    "source",
    [
        'def helper(name, package):\n    return name\nGONE = helper(".gone", __package__)\n',
        'import importlib\nGONE = importlib.import_module(".gone", "apps.other")\n',
        'def helper(name, package):\n    return name\nKW = {"name": ".gone", "package": __package__}\nGONE = helper(**KW)\n',
    ],
    ids=["not-a-loader", "explicit-package", "not-a-loader-spread"],
)
def test_a_relative_name_is_only_an_import_of_what_a_loader_anchors_it_to(tree, source):
    # Control: a relative string handed to another call, or anchored elsewhere, does not import `gone`.
    (tree / "apps/core/names_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/scripts/test_names_gone.py").write_text("from apps.core.names_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_names_gone.py" not in selection.modules
    assert "import graph" in selection.reasons["tests/scripts/test_imports_gone.py"]


@pytest.mark.parametrize(
    "source",
    [
        'import runpy\ndef run():\n    runpy.run_module("apps.core.gone")\n',
        # An import call elsewhere in the module does not make the runpy name an import.
        'import importlib, runpy\nENGINE = importlib.import_module("apps.core.engine")\n'
        'def run():\n    runpy.run_module("apps.core.gone")\n',
        # A method that shares a builtin's name does not run code.
        'import re\nPATTERN = re.compile("apps.core.gone")\ndef run():\n    return PATTERN\n',
    ],
    ids=["runpy", "runpy-beside-import-module", "re-compile"],
)
def test_a_module_only_naming_a_deleted_module_is_a_weak_link(tree, source):
    # `runpy.run_module("apps.core.gone")` names it without importing it: item 2 may demote that.
    (tree / "tests/scripts/runs_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/scripts/test_runs_gone.py").write_text("from tests.scripts.runs_gone import run\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert selection.demoted["tests/scripts/test_runs_gone.py"] == ["named reference via helper"]
    # Control: a real importer of the deleted module is still a strong link.
    assert "import graph" in selection.reasons["tests/scripts/test_imports_gone.py"]


def test_a_composed_name_without_a_loader_is_not_an_import(tree):
    # Control: composing a name only counts as an import when a loader call could not be read.
    (tree / "tests/scripts/runs_gone.py").write_text(
        'import runpy\ndef run():\n    runpy.run_module("apps.core" + ".gone")\n', encoding="utf-8"
    )
    (tree / "tests/scripts/test_runs_gone.py").write_text("from tests.scripts.runs_gone import run\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_runs_gone.py" not in selection.modules
    assert "import graph" in selection.reasons["tests/scripts/test_imports_gone.py"]


def test_a_relative_dynamic_import_of_a_deleted_module_is_not_demoted(tree):
    # `import_module(".gone", __package__)` imports it, so item 2 must not demote its consumer.
    (tree / "apps/core/loads_gone.py").write_text(
        'import importlib\nGONE = importlib.import_module(".gone", __package__)\n', encoding="utf-8"
    )
    (tree / "tests/scripts/test_loads_gone.py").write_text("from apps.core.loads_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_gone.py" in selection.modules
    assert "tests/scripts/test_loads_gone.py" not in selection.demoted


@pytest.mark.parametrize(
    "source",
    [
        'import importlib\nGONE = importlib.import_module("apps.core.gone")\n',
        'from importlib import import_module\nGONE = import_module(name="apps.core.gone")\n',
        'from importlib import import_module as load\nGONE = load("apps.core" + ".gone")\n',
        'import importlib\nload = importlib.import_module\nNAME = "apps.core.gone"\nGONE = load(NAME)\n',
        'GONE = __import__("apps.core.gone.sub")\n',
        'from importlib import import_module as load\nagain = load\nGONE = again("apps.core.gone")\n',
        'import importlib\nfor name in ("apps.core.engine", "apps.core.gone"):\n    importlib.import_module(name)\n',
        'import importlib\nload: object = importlib.import_module\nGONE = load("apps.core.gone")\n',
        'import importlib\nif (load := importlib.import_module):\n    GONE = load("apps.core.gone")\n',
        'import importlib\nNAMES = ("apps.core.engine", "apps.core.gone")\nfor name in NAMES:\n    importlib.import_module(name)\n',
        'import importlib\ndef load(names=["apps.core.gone"]):\n    return [importlib.import_module(n) for n in names]\n',
        'import importlib\nload, other = importlib.import_module, None\nGONE = load("apps.core.gone")\n',
        'import importlib\nLOADERS = {"py": importlib.import_module}\nGONE = LOADERS["py"]("apps.core.gone")\n',
        'GONE = __import__("gone", globals(), locals(), (), 1)\n',
        'GONE = __import__("core.gone", level=2)\n',
        'import importlib\nLOADERS = {"py": importlib.import_module}\nGONE = LOADERS["py"]("apps.core" + ".gone")\n',
        'LEVEL = 1\nGONE = __import__("gone", globals(), locals(), (), LEVEL)\n',
        'LEVEL = 1\ndef f():\n    LEVEL = object()\n    return LEVEL\nGONE = __import__("gone", globals(), locals(), (), LEVEL)\n',
        'def load(level):\n    return __import__("gone", globals(), locals(), (), level)\nGONE = load(1)\n',
        'import importlib\nload = getattr(importlib, "import_module")\nGONE = load("apps.core.gone")\n',
        'import importlib\nWHICH = "import_module"\nload = getattr(importlib, WHICH)\nGONE = load("apps.core.gone")\n',
        'exec("import apps.core.gone")\nGONE = None\n',
        'import builtins\nbuiltins.exec(compile("import apps.core.gone", "x", "exec"))\nGONE = None\n',
        'import importlib\nWHICH = "import_" + "module"\nload = getattr(importlib, WHICH)\nGONE = load("apps.core.gone")\n',
        'import importlib\nGONE = importlib.import_module(*["apps.core.gone"])\n',
        'import importlib\nNAMES = ["apps.core.gone"]\nGONE = importlib.import_module(*NAMES)\n',
        'run = exec\nrun("import apps.core.gone")\nGONE = None\n',
        'import builtins\ngetattr(builtins, "exec")("import apps.core.gone")\nGONE = None\n',
        'import builtins\ngetattr(builtins, "ex" + "ec")("import apps.core.gone")\nGONE = None\n',
        'RUN = "ev" + "al"\n__builtins__[RUN]("__import__(\'apps.core.gone\')")\nGONE = None\n',
        'from builtins import exec as run\nrun("import apps.core.gone")\nGONE = None\n',
        'import builtins as b\nb.exec("import apps.core.gone")\nGONE = None\n',
    ],
    ids=[
        "attribute",
        "name-keyword",
        "alias-composed",
        "bound-name",
        "dunder-child",
        "alias-chain",
        "loop",
        "annotated-alias",
        "walrus-alias",
        "named-tuple",
        "parameter",
        "destructured-alias",
        "loader-in-dict",
        "dunder-level",
        "dunder-level-keyword",
        "escaped-loader-composed",
        "dunder-level-name",
        "dunder-level-shadowed",
        "dunder-level-parameter",
        "getattr-loader",
        "getattr-loader-named",
        "exec-string",
        "builtins-exec-compile",
        "getattr-loader-folded",
        "spread-positional",
        "spread-named",
        "exec-alias",
        "exec-getattr",
        "exec-getattr-composed",
        "eval-subscript-composed",
        "exec-imported-as-alias",
        "builtins-module-alias",
    ],
)
def test_an_absolute_dynamic_import_of_a_deleted_module_is_not_demoted(tree, source):
    # `import_module("apps.core.gone")` imports it, so item 2 must not demote its consumer.
    (tree / "apps/core/loads_gone.py").write_text(source, encoding="utf-8")
    (tree / "tests/scripts/test_loads_gone.py").write_text("from apps.core.loads_gone import GONE\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_gone.py" in selection.modules
    assert "tests/scripts/test_loads_gone.py" not in selection.demoted


@pytest.mark.parametrize(
    "source",
    [
        'LEVEL = 1\nENGINE = __import__("engine", globals(), locals(), (), LEVEL)\n',
        'LEVEL = 0\nGONE = __import__("gone", globals(), locals(), (), LEVEL)\n',
        # An unreadable level tries every level, but only for the name it is given.
        'def load(level):\n    return __import__("engine", globals(), locals(), (), level)\nENGINE = load(1)\n',
    ],
    ids=["named-level-sibling", "named-level-absolute", "unreadable-level-sibling"],
)
def test_a_named_import_level_resolves_to_its_own_target(tree, source):
    # Control: a folded `level` anchors the name it is given, and 0 leaves it absolute.
    (tree / "apps/core/loads_other.py").write_text(source, encoding="utf-8")
    (tree / "tests/scripts/test_loads_other.py").write_text("import apps.core.loads_other\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_other.py" not in selection.modules


@pytest.mark.parametrize(
    ("helper", "test"),
    [
        (
            "import importlib\ndef load(package):\n    return importlib.import_module('.gone', package)\n",
            "from scripts.load_helper import load\nGONE = load('apps.core')\n",
        ),
        (
            "import importlib\ndef load(**kw):\n    return importlib.import_module('.gone', **kw)\n",
            "from scripts.load_helper import load\nGONE = load(package='apps.core')\n",
        ),
        ("import importlib\nGONE = importlib.import_module(f'{__package__}.gone')\n", None),
        ("import importlib\nGONE = importlib.import_module(__package__ + '.gone')\n", None),
        ("import importlib\nGONE = importlib.import_module('.'.join([__package__, 'gone']))\n", None),
        ("import importlib\nNAME = f'{__package__}.gone'\nGONE = importlib.import_module(NAME)\n", None),
        ("import importlib\nGONE = importlib.import_module('{}.gone'.format(__package__))\n", None),
        ("import importlib\nGONE = importlib.import_module('%s.gone' % __package__)\n", None),
        ("exec(f'import {__package__}.gone')\nGONE = None\n", None),
        (
            "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
            "from scripts.load_helper import load\nGONE = load('apps.core.gone')\n",
        ),
        (
            "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
            "from scripts.load_wrapper import load\nGONE = load('apps.core.gone')\n",
        ),
        (
            "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
            "from scripts.load_helper import load\nGONE = load('apps.core' + '.gone')\n",
        ),
        (
            "import importlib\ndef load(name, package):\n    return importlib.import_module(name, package)\n",
            "from scripts.load_helper import load\nGONE = load('.gone', 'apps.core')\n",
        ),
    ],
    ids=[
        "unread-anchor",
        "unread-anchor-keywords",
        "package-fstring",
        "package-plus",
        "package-join",
        "package-bound",
        "package-format",
        "package-percent",
        "exec-package-fstring",
        "generic-loader-named-by-caller",
        "generic-loader-re-exported",
        "generic-loader-composed-name",
        "generic-loader-relative-name",
    ],
)
def test_a_loader_name_whose_head_does_not_read_matches_by_its_tail(tree, helper, test):
    # `import_module(".gone", package)` in a helper anchors wherever its caller says, and
    # `f"{__package__}.gone"` names no package: either may be the deleted apps.core.gone.
    home = "scripts/load_helper.py" if test else "apps/core/load_helper.py"
    (tree / home).write_text(helper, encoding="utf-8")
    (tree / "scripts/load_wrapper.py").write_text("from scripts.load_helper import load\n", encoding="utf-8")
    module = home.removesuffix(".py").replace("/", ".")
    (tree / "tests/scripts/test_loads_gone.py").write_text(test or f"import {module}\n", encoding="utf-8")
    (tree / "apps/core/gone.py").unlink()
    selection = _select(tree, Policy(ci_config_by_name=True, demote_weak_under=("tests/scripts",)), "apps/core/gone.py")
    assert "tests/scripts/test_loads_gone.py" in selection.modules
    assert "tests/scripts/test_loads_gone.py" not in selection.demoted
    assert "tests/other/test_other.py" not in selection.modules
