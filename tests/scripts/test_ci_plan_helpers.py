"""Support modules and the scopes a global helper CARRIES.

Split out of test_ci_plan.py, which crossed the file size ceiling. A collected test module is
not the whole input: a helper beside it can be the only carrier of a cross-scope import, and
a helper imported by every suite drags its scopes into every run. Both are ways a plan can
look narrow while resting on an edge nobody derived.

`_config` and `_changed` are duplicated from test_ci_plan.py deliberately, for the reason
given in test_ci_plan_inputs.py.

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
    helper_carried_scopes,
    observed_dependents,
    plan,
    read_config,
)
from scripts.ci_plan_sources import pytest_inputs
from scripts.ci_plan_sources import (
    test_modules as discover_test_modules,
)

pytestmark = pytest.mark.requirement("OPS-16")


def _config(**overrides) -> Config:
    base = {
        "full_triggers": ("pyproject.toml", "**/conftest.py"),
        "always": ("tests/*.py",),
        "scopes": (
            Scope("engine", ("apps/engine_core/",), ("tests/engine_core/",)),
            Scope("library", ("apps/library/",), ("tests/library/",)),
        ),
    }
    return Config(**{**base, **overrides})


def _changed(*paths: str) -> tuple[Change, ...]:
    return tuple(Change("M", path) for path in paths)


# ----- Sol's third round of P1s on #3339 -----


def _support_tree(tmp_path: Path) -> tuple[Config, Path]:
    """A tests tree where a SUPPORT module is the only carrier of a cross-scope import."""
    root = tmp_path / "tests"
    (root / "b").mkdir(parents=True)
    # The collected module names only its own helper; the helper is what reaches scope a.
    (root / "b" / "test_thing.py").write_text("from tests.b.rig import fixture\n")
    (root / "b" / "rig.py").write_text("from apps.a import thing\n")
    config = Config(
        (),
        (),
        (
            Scope("a", ("apps/a/",), ("tests/a/",)),
            Scope("b", ("apps/b/",), ("tests/b/",)),
        ),
    )
    return config, root


def test_a_support_module_carries_its_scopes_dependency(tmp_path: Path) -> None:
    """`conftest.py` runs automatically and helper modules are imported by the tests that use
    them, so their imports are the suite's dependencies. Feeding only COLLECTED modules to the
    derivation loses the edge whenever a helper reaches a scope its own tests never name: 24
    support modules in this repository import a scope other than the one that owns them."""
    config, root = _support_tree(tmp_path)
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"a": ("b",)}


def test_collected_modules_alone_would_miss_it(tmp_path: Path) -> None:
    """The half that makes the test above mean something. An assertion that the new input set
    finds an edge proves nothing unless the old one demonstrably did not."""
    config, root = _support_tree(tmp_path)
    assert observed_dependents(config, discover_test_modules(root), root=tmp_path) == {}


def test_pytest_inputs_keeps_support_modules_and_test_modules_apart(tmp_path: Path) -> None:
    """The opposite direction: widening the DERIVATION's input set must not widen what counts
    as a collected test module, or conftest and helpers land in the owner index with no suite
    to own and the completeness invariant starts demanding scopes for them."""
    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "rig.py").write_text("")
    (tmp_path / "test_real.py").write_text("")
    assert {p.name for p in pytest_inputs(tmp_path)} == {"conftest.py", "rig.py", "test_real.py"}
    assert {p.name for p in discover_test_modules(tmp_path)} == {"test_real.py"}


def test_an_unclaimed_support_module_that_imports_a_scope_is_refused(tmp_path: Path) -> None:
    """A global helper importing scope `a`, consumed by suites in scope `b`, leaves `b` unrun
    when `a` changes. The derivation cannot place that edge, and a tool that cannot measure
    must say so rather than return a smaller answer. Sol's P1 on #3339."""
    root = tmp_path / "tests"
    (root / "helpers").mkdir(parents=True)
    (root / "helpers" / "rig.py").write_text("from apps.a import thing\n")
    config = Config((), (), (Scope("a", ("apps/a/",), ("tests/a/",)),))
    with pytest.raises(PlanError, match="claimed by no scope"):
        observed_dependents(config, pytest_inputs(root), root=tmp_path)


def test_an_always_run_support_module_is_skipped_rather_than_refused(tmp_path: Path) -> None:
    """The opposite direction, and the reason one `continue` could serve both cases. An
    `always` path runs in EVERY scoped plan, so it needs no edge and refusing it would break
    the 128 files in this repository that are reached exactly that way."""
    root = tmp_path / "tests"
    (root / "helpers").mkdir(parents=True)
    (root / "helpers" / "rig.py").write_text("from apps.a import thing\n")
    config = Config((), ("tests/helpers/",), (Scope("a", ("apps/a/",), ("tests/a/",)),))
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_an_unclaimed_support_module_with_no_scope_import_is_harmless(tmp_path: Path) -> None:
    """The other overshoot: refusing every unowned file regardless of what it imports would
    make the derivation reject ordinary scratch modules that carry no dependency at all."""
    root = tmp_path / "tests"
    (root / "helpers").mkdir(parents=True)
    (root / "helpers" / "rig.py").write_text("import os\n")
    config = Config((), (), (Scope("a", ("apps/a/",), ("tests/a/",)),))
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_the_committed_helper_carried_list_is_exactly_the_derived_one() -> None:
    """A global pytest helper is not a test and does not run; it is imported by suites across
    the tree, so a change to a scope it imports has to run everything. The anti-rot guard for
    that, recomputed from the tree rather than trusting the committed list: a new import in
    `tests/conftest.py` fails this test instead of silently leaving suites unrun.

    An EQUALITY, in both directions. The earlier form asked only whether every carried scope
    was covered, and a mutation that shrank the carried set passed it (round 5); a list that
    carries a scope no helper reaches is a constant, not a derivation, and is refused too.
    Named by scope rather than written as path triggers since round 9, because ownership is
    longest-prefix and a prefix trigger on `apps/webui/` would swallow `apps/webui/frontend/`.
    """
    config = read_config()
    carried = helper_carried_scopes(config, Path("."))
    assert carried, "no helper-carried scope found; the derivation measured nothing"
    assert tuple(sorted(carried)) == tuple(sorted(config.helper_carried)), (
        "ci/test-scopes.yml `helper_carried` no longer matches what the global helpers reach"
    )


def test_a_scope_no_global_helper_imports_is_not_dragged_in(tmp_path: Path) -> None:
    """The opposite direction. Marking every scope a full trigger satisfies the guard above
    completely and turns the config into a constant, so the derivation has to be able to
    answer NO for a scope no global helper reaches."""
    root = tmp_path
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text("from apps.a import thing\n")
    config = Config(
        (),
        ("tests/*.py",),
        (Scope("a", ("apps/a/",), ("tests/a/",)), Scope("b", ("apps/b/",), ("tests/b/",))),
    )
    assert helper_carried_scopes(config, root) == frozenset({"a"})


def test_helper_carriage_is_closed_over_the_source_graph(tmp_path: Path) -> None:
    """A helper importing scope `a` also depends on everything `a` imports, so a change to the
    TRANSITIVE scope has to run everything too.

    This test exists because a mutation found nothing. Replacing the closure with the directly
    imported scope alone left the whole suite green: the committed config lists the direct
    scopes as full triggers anyway, and the guard only asks whether the carried set is covered,
    so a SMALLER carried set passed it. An unmoved suite is a result, not a relief."""
    (tmp_path / "apps" / "a").mkdir(parents=True)
    (tmp_path / "apps" / "b").mkdir(parents=True)
    (tmp_path / "apps" / "a" / "mod.py").write_text("from apps.b import thing\n")
    (tmp_path / "apps" / "b" / "mod.py").write_text("import os\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "conftest.py").write_text("from apps.a import mod\n")
    config = Config(
        (),
        ("tests/*.py",),
        (
            Scope("a", ("apps/a/",), ("tests/a/",)),
            Scope("b", ("apps/b/",), ("tests/b/",)),
            Scope("c", ("apps/c/",), ("tests/c/",)),
        ),
    )
    carried = helper_carried_scopes(config, tmp_path)
    assert "b" in carried, "the scope reached only through a's sources was dropped"
    assert carried == frozenset({"a", "b"}), f"c is reached by nothing and must stay out: {carried}"


@pytest.mark.parametrize(
    "path",
    [
        "apps/engine_core/ui.ts",
        "apps/engine_core/View.svelte",
        "apps/engine_core/run.sh",
        "apps/engine_core/fixtures.json",
    ],
)
def test_a_claimed_path_the_closure_cannot_read_answers_full(path: str) -> None:
    """Owning the changed path is not enough. `_source_reachability` reads Python imports and
    nothing else, so a scope's TypeScript, Svelte, shell or JSON sources contribute no edges,
    and a SCOPED plan built on them omits whatever consumes them across a scope boundary.
    2058 of this repository's 4646 claimed files are not Python, so this is the common case.
    Sol's P1 on #3339."""
    result = plan(_changed(path), _config())
    assert result.verdict is Verdict.FULL, result.reason
    assert "cannot be derived" in result.reason


def test_a_claimed_python_path_still_scopes() -> None:
    """The opposite direction, and the one that decides whether this is a gate or a constant.
    Refusing every claimed path satisfies the finding completely and makes SCOPED unreachable
    for the one file type the closure CAN read."""
    got = plan(_changed("apps/engine_core/deck.py"), _config())
    assert got.verdict is Verdict.SCOPED
    assert got.scopes == ("engine",)


def test_one_underivable_path_in_a_python_change_still_answers_full() -> None:
    """The mixed diff, which is what real pull requests look like. A single `.ts` file beside
    a dozen Python ones has to decide the plan, because the suites consuming it are exactly
    the ones the closure could not name."""
    got = plan(_changed("apps/engine_core/deck.py", "apps/engine_core/ui.ts"), _config())
    assert got.verdict is Verdict.FULL


# ----- round 9: a scope nested inside another, consumed by mention rather than import -----


def _nested_config() -> Config:
    return Config(
        (),
        ("tests/*.py",),
        (
            Scope("webui", ("apps/webui/",), ("tests/webui/",)),
            Scope("front", ("apps/webui/frontend/",), (), mentions=("apps/webui/frontend",)),
            Scope("a", ("apps/a/",), ("tests/a/",)),
        ),
        helper_carried=("webui",),
    )


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


def _mention_tree(tmp_path: Path, test_text: str) -> tuple[Config, Path]:
    root = tmp_path / "tests"
    (root / "a").mkdir(parents=True)
    (root / "a" / "test_build.py").write_text(test_text)
    return _nested_config(), root


def test_a_test_that_spells_the_path_is_a_dependent_of_the_mentioned_scope(
    tmp_path: Path,
) -> None:
    config, root = _mention_tree(
        tmp_path, 'from pathlib import Path\nD = Path("apps/webui/frontend/dist")\n'
    )
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
