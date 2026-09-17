"""Wiring tests for scripts/affected_tests.py, the static-import test selector.

The affected-test canary (SMARTEST-CI rounds 1 and 2) ran the tests this module names
BEFORE the sharded lane; round 6a replaced that job with the fast tier, and the selector
stays as the static lower bound for the round 5 planner. It ran first, so
the cost of being wrong is one-sided: naming too few tests loses some of the fast signal,
naming too many wastes a little time. Nothing is ever skipped on this basis. The tests
below pin that asymmetry rather than an exact selection.

[if] the selector stops naming the tests a change reaches, or starts naming
everything [then] fail, [else stop].

Regression lines:
  - [if] a test importing a changed module directly is not named [then] fail, [else stop].
  - [if] a test reaching a changed module only transitively is not named [then] fail, [else stop].
  - [if] a changed test file does not name itself [then] fail, [else stop].
  - [if] an unrelated test is named [then] fail, [else stop]. The selection would carry no
    information at 720 modules if everything reached everything.
  - [if] a file that fails to parse aborts the whole graph [then] fail, [else stop]. A syntax
    error is the suite's problem to report, not this script's.
  - [if] `from x import y` records only `x.y` and drops the `x` edge [then] fail, [else stop].
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.affected_tests import (
    UnresolvedPaths,
    _changed_against,
    affected_tests,
    build_graph,
    main,
)

pytestmark = pytest.mark.requirement("OPS-16")


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def test_a_direct_importer_is_named(tmp_path):
    _tree(
        tmp_path,
        {
            "apps/engine.py": "VALUE = 1\n",
            "tests/test_engine.py": "from apps.engine import VALUE\n",
            "apps/__init__.py": "",
        },
    )
    assert affected_tests(["apps/engine.py"], root=tmp_path) == ["tests/test_engine.py"]


def test_a_transitive_importer_is_named(tmp_path):
    """A test two hops from the change still runs. This is the case a naive
    'grep the module name in test files' selector misses, and the one that makes
    an import graph worth building at all."""
    _tree(
        tmp_path,
        {
            "apps/low.py": "VALUE = 1\n",
            "apps/mid.py": "from apps.low import VALUE\n",
            "tests/test_high.py": "from apps.mid import VALUE\n",
            "apps/__init__.py": "",
        },
    )
    assert affected_tests(["apps/low.py"], root=tmp_path) == ["tests/test_high.py"]


def test_a_changed_test_names_itself(tmp_path):
    _tree(tmp_path, {"tests/test_alone.py": "def test_x():\n    assert True\n"})
    assert affected_tests(["tests/test_alone.py"], root=tmp_path) == ["tests/test_alone.py"]


def test_an_unrelated_test_is_not_named(tmp_path):
    """If everything reaches everything the selection carries no information."""
    _tree(
        tmp_path,
        {
            "apps/engine.py": "VALUE = 1\n",
            "apps/other.py": "OTHER = 2\n",
            "tests/test_engine.py": "from apps.engine import VALUE\n",
            "tests/test_other.py": "from apps.other import OTHER\n",
            "apps/__init__.py": "",
        },
    )
    assert affected_tests(["apps/engine.py"], root=tmp_path) == ["tests/test_engine.py"]


def test_an_unparseable_file_does_not_abort_the_graph(tmp_path):
    _tree(
        tmp_path,
        {
            "apps/engine.py": "VALUE = 1\n",
            "apps/broken.py": "def (((\n",
            "tests/test_engine.py": "from apps.engine import VALUE\n",
            "apps/__init__.py": "",
        },
    )
    assert affected_tests(["apps/engine.py"], root=tmp_path) == ["tests/test_engine.py"]


def test_from_import_records_the_module_edge_not_only_the_attribute(tmp_path):
    """`from apps.engine import VALUE` is a dependency on apps.engine itself.

    Resolving only the longest prefix would bind the edge to a module named
    `apps.engine.VALUE` if one ever existed, and otherwise silently drop it.
    """
    _tree(
        tmp_path,
        {
            "apps/engine.py": "VALUE = 1\n",
            "tests/test_engine.py": "from apps.engine import VALUE\n",
            "apps/__init__.py": "",
        },
    )
    importers, module_of = build_graph(tmp_path)
    assert "apps.engine" in module_of
    assert "tests.test_engine" in importers["apps.engine"]


# ----- fail loud: a path the graph cannot resolve is UNKNOWN, never zero -----
#
# Measured Wed 16 Sep 2026: the canary selected zero modules on 15 of 28 runs, and the
# selector returned zero with exit 0 for a path that does not exist, so "this change reaches
# no test" and "this selector could not read the change" printed the same thing.
#
#   - [if] a changed .py path missing from the graph yields [] and exit 0 [then] fail,
#     [else stop].
#   - [if] a leaf module no test reaches raises instead of yielding [] [then] fail, [else stop].
#     This is the overshoot control: UNKNOWN is for unresolvable paths, not empty selections.
#   - [if] a rename lists only its new path, hiding the deleted module [then] fail, [else stop].
#   - [if] the canary step reads the selector through process substitution, which discards
#     its exit status [then] fail, [else stop].


def test_a_changed_path_missing_from_the_graph_is_unknown_and_named(tmp_path):
    _tree(tmp_path, {"apps/engine.py": "VALUE = 1\n", "apps/__init__.py": ""})
    with pytest.raises(UnresolvedPaths) as raised:
        affected_tests(["apps/engine.py", "apps/gone.py"], root=tmp_path)
    assert raised.value.paths == ["apps/gone.py"]


def test_a_leaf_no_test_reaches_is_an_empty_selection_not_unknown(tmp_path):
    _tree(
        tmp_path,
        {
            "apps/leaf.py": "VALUE = 1\n",
            "tests/test_other.py": "def test_x():\n    assert True\n",
            "apps/__init__.py": "",
        },
    )
    assert affected_tests(["apps/leaf.py"], root=tmp_path) == []


def test_main_exits_3_and_names_an_unresolvable_path(capsys):
    missing = "apps/this_module_does_not_exist_anywhere.py"
    assert main(["--files", missing]) == 3
    assert missing in capsys.readouterr().err


def test_main_exits_0_for_a_resolvable_path(capsys):
    """Positive control for the exit-3 test: the same entry point on a real path."""
    assert main(["--files", "scripts/affected_tests.py"]) == 0
    assert "tests/scripts/test_affected_tests.py" in capsys.readouterr().out


def test_a_rename_lists_the_deleted_old_path(tmp_path):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ci@example.com")
    git("config", "user.name", "ci")
    _tree(tmp_path, {"apps/old_name.py": "VALUE = 1\n" * 20})
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("mv", "apps/old_name.py", "apps/new_name.py")
    git("commit", "-q", "-m", "rename")
    changed = _changed_against("HEAD~1", root=tmp_path)
    assert "apps/old_name.py" in changed
    assert "apps/new_name.py" in changed

