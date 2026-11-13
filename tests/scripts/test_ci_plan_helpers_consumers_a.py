"""Round 12 helper consumer tests, part a (split from test_ci_plan_helpers.py).

[if] a helper's carried scopes are dropped from a plan [then] fail here, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_plan import (
    Change,
    Config,
    PlanError,
    Scope,
    Verdict,
    _helper_importers,
    observed_dependents,
    plan,
    read_config,
)
from scripts.ci_plan_carry import helper_carried_scopes
from scripts.ci_plan_sources import imported_modules, loaded_test_helpers, pytest_inputs
from scripts.ci_plan_sources import (
    test_modules as discover_test_modules,
)

from tests.scripts.ci_plan_helpers_fixtures import (
    _changed,
    _config,
    _mention_tree,
    _nested_config,
    _root_helper_tree,
    _support_tree,
)

pytestmark = pytest.mark.requirement("OPS-16")

# ----- round 12: a helper's consumers decide whether it carries -----


def test_a_root_helper_consumed_only_by_always_run_tests_carries_nothing(tmp_path: Path) -> None:
    """Every plan runs `tests/test_root.py`, and that module is what exercises the helper's
    import of `engine`, so a change to `engine` is already covered without going FULL."""
    config, _ = _root_helper_tree(tmp_path, "test_root.py")
    assert helper_carried_scopes(config, tmp_path) == frozenset()


def test_a_root_helper_consumed_by_a_scope_owned_suite_carries_nothing_and_edges_its_owner(
    tmp_path: Path,
) -> None:
    """Round 19 reverses round 12 here. `tests/library/` is not in every plan, but its owner is
    known, so an `engine` change has to run `library`: an edge says that, where carrying made
    every `engine` change run everything."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


def test_a_root_helper_consumed_by_an_owned_and_an_always_run_test_edges_only_the_owner(
    tmp_path: Path,
) -> None:
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "test_root.py").write_text("from tests.rig import thing\n")
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


def test_a_placed_root_helper_edges_its_owner_from_everything_its_scope_imports(tmp_path: Path) -> None:
    """The edge closes over the source graph like any other: `engine` imports `core`, so a
    `core` change also has to run the suite that uses the helper."""
    _, root = _root_helper_tree(tmp_path, "library/test_use.py")
    config = _config(scopes=(*_config().scopes, Scope("core", ("apps/core/",), ("tests/core/",))))
    (tmp_path / "apps" / "engine_core").mkdir(parents=True)
    (tmp_path / "apps" / "engine_core" / "thing.py").write_text("from apps.core import base\n")
    (tmp_path / "apps" / "core").mkdir()
    (tmp_path / "apps" / "core" / "base.py").write_text("")
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {
        "core": ("library",),
        "engine": ("library",),
    }


def test_a_root_helper_with_a_helper_among_its_consumers_still_carries_and_adds_no_edge(
    tmp_path: Path,
) -> None:
    """One consumer is a helper whose own consumers the derivation does not follow, so the
    owned consumer's edge alone would understate the reach. Widest stays, and no edge."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "other_rig.py").write_text("from tests.rig import thing\n")
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_placed_helper_also_loaded_through_pytest_plugins_still_carries_and_adds_no_edge(
    tmp_path: Path,
) -> None:
    """Codex on #7121. One placed test imports the helper and another suite loads it through
    `pytest_plugins`, which no import statement shows. Counting only the import placed the helper,
    so an `engine` change planned SCOPED without the suite that loads it. The plugin string is a
    consumer too, and a `conftest.py` one cannot be placed: widest stays, and no edge."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "other").mkdir()
    (root / "other" / "conftest.py").write_text('pytest_plugins = ["tests.rig"]\n')
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_placed_test_holding_a_helper_name_as_data_does_not_place_it(tmp_path: Path) -> None:
    """Sol and Codex on #7121. A string equal to the helper's name is test data, not a load:
    counted as one, it made an unimported helper look placed and dropped its carry."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text('def test_it():\n    assert "tests.rig"\n')
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_computed_plugin_name_keeps_the_helper_carried(tmp_path: Path) -> None:
    """Codex on #7121. A placed test imports the helper, and a conftest also loads it for the whole
    session through a plugin name it builds, so no literal names it. The import alone would place the
    helper; a load the reader cannot resolve keeps every helper wide."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "conftest.py").write_text('pytest_plugins = ["tests." + "rig"]\n')
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_computed_import_of_a_tests_name_keeps_the_helper_carried(tmp_path: Path) -> None:
    """A conftest imports `"tests." + name`: it may load the placed helper, and no literal says so."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "conftest.py").write_text('import importlib\nname = "rig"\nimportlib.import_module("tests." + name)\n')
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}
