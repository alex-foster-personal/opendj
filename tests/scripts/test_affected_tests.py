"""Wiring tests for scripts/affected_tests.py, the affected-test canary's selector.

The canary runs the tests this module names BEFORE the sharded lane runs everything, so
the cost of being wrong is one-sided: naming too few tests loses some of the fast signal,
naming too many wastes a little time. Nothing is ever skipped on this basis. The tests
below pin that asymmetry rather than an exact selection.

Regression lines:
  - if a test that imports a changed module directly is not named then broken
  - if a test that reaches a changed module only transitively is not named then broken
  - if a changed test file does not name itself then broken
  - if an unrelated test is named then broken (the selection would be worthless at
    720 modules if everything reached everything)
  - if a file that fails to parse aborts the whole graph then broken (a syntax error is
    the suite's problem to report, not this script's)
  - if `from x import y` is treated as a dependency on `x.y` only, missing the `x` edge,
    then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.affected_tests import affected_tests, build_graph

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
