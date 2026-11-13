"""Round 9 mention-based scope tests (split from test_ci_plan_helpers.py).

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

# ----- round 9: a scope nested inside another, consumed by mention rather than import -----


def test_a_nested_scope_owns_its_files_and_the_carried_parent_does_not_swallow_them() -> None:
    """`apps/webui/` is helper-carried, so every change under it was FULL, including the
    Svelte app that no Python imports. Longest prefix gives the frontend to its own scope."""
    got = plan(_changed("apps/webui/frontend/src/App.svelte"), _nested_config())
    assert got.verdict is Verdict.SCOPED, got.reason
    assert "front" in got.scopes and "webui" not in got.scopes


def test_a_python_change_under_the_carried_parent_is_still_full() -> None:
    """The overshoot control: narrowing the child must not free the parent."""
    got = plan(_changed("apps/webui/library_assets.py"), _nested_config())
    assert got.verdict is Verdict.FULL
    assert "carries the scope owning" in got.reason


def test_a_non_python_change_under_a_scope_without_mentions_is_still_full() -> None:
    """Round 6's rule survives for every scope that declares no way to find its consumers."""
    got = plan(_changed("apps/a/View.svelte"), _nested_config())
    assert got.verdict is Verdict.FULL
    assert "cannot be derived" in got.reason


def test_a_test_that_spells_the_path_is_a_dependent_of_the_mentioned_scope(
    tmp_path: Path,
) -> None:
    config, root = _mention_tree(tmp_path, 'from pathlib import Path\nD = Path("apps/webui/frontend/dist")\n')
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"front": ("a",)}


def test_a_mention_in_a_comment_or_docstring_makes_no_edge(tmp_path: Path) -> None:
    """The control that separates the reader from a grep; see `mentioned_strings`."""
    config, root = _mention_tree(
        tmp_path, '"""Checks apps/webui/frontend indirectly."""\n# apps/webui/frontend\nX = 1\n'
    )
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_global_helper_that_spells_the_path_carries_the_scope(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text('DIST = "apps/webui/frontend/dist"\n')
    assert "front" in helper_carried_scopes(_nested_config(), tmp_path)


def test_a_global_helper_that_only_comments_on_the_path_carries_nothing(tmp_path: Path) -> None:
    """The real `tests/conftest.py` case, reduced. Raw-text matching carried the frontend."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("# the frontend typing gate\nX = 1\n")
    assert helper_carried_scopes(_nested_config(), tmp_path) == frozenset()


def test_an_unclaimed_support_module_that_mentions_a_scope_is_refused(tmp_path: Path) -> None:
    """Same rule as the import form: a dependency the derivation cannot place must not narrow."""
    root = tmp_path / "tests"
    (root / "orphan").mkdir(parents=True)
    (root / "orphan" / "rig.py").write_text('DIST = "apps/webui/frontend/dist"\n')
    with pytest.raises(PlanError, match="claimed by no scope"):
        observed_dependents(_nested_config(), pytest_inputs(root), root=tmp_path)


def test_an_import_of_a_nested_scope_is_attributed_to_the_nested_scope(tmp_path: Path) -> None:
    """Codex on #3780. `imported_packages` cuts a name to two components, so an import of
    `apps.webui.frontend.support.x` read as `apps.webui` and the edge went to the PARENT.
    Import syntax carries no string literal, so the mention reader cannot recover it; the
    owner lookup has to see the full name. No mentions here, so only the import can answer."""
    root = tmp_path / "tests"
    (root / "a").mkdir(parents=True)
    (root / "a" / "test_fixture.py").write_text("from apps.webui.frontend.support import x\n")
    config = Config(
        (),
        ("tests/*.py",),
        (
            Scope("webui", ("apps/webui/",), ("tests/webui/",)),
            Scope("front", ("apps/webui/frontend/",), ()),
            Scope("a", ("apps/a/",), ("tests/a/",)),
        ),
    )
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"front": ("a",)}
