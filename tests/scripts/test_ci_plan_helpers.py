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
    observed_dependents,
    plan,
    read_config,
)
from scripts.ci_plan_carry import helper_carried_scopes
from scripts.ci_plan_sources import imported_modules, loaded_test_helpers, pytest_inputs
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


# ----- round 12: a helper's consumers decide whether it carries -----


def _root_helper_tree(tmp_path: Path, consumer: str) -> tuple[Config, Path]:
    """A root helper importing scope `engine`, consumed by ONE module at `consumer`."""
    root = tmp_path / "tests"
    (root / "library").mkdir(parents=True)
    (root / "rig.py").write_text("from apps.engine_core import thing\n")
    (root / consumer).parent.mkdir(parents=True, exist_ok=True)
    (root / consumer).write_text("from tests.rig import thing\n")
    return _config(), root


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


@pytest.mark.parametrize(
    "conftest",
    [
        'from importlib import import_module as load\nload("tests.rig")\n',
        'import importlib\nload = importlib.import_module\nload("tests.rig")\n',
        'import importlib\nnames = ["tests.rig"]\nlist(map(importlib.import_module, names))\n',
        'import importlib\nimportlib.import_module(".rig", package=__package__)\n',
        '__import__("rig", globals(), None, [], 1)\n',
        'import runpy\nfrom pathlib import Path\nrunpy.run_path(str(Path("tests") / "rig.py"))\n',
        'import runpy\nfrom pathlib import Path\nrunpy.run_path(str(Path(__file__).parent / "rig.py"))\n',
        'import harness\nharness.client.import_module("tests.rig")\n',
        'import pytest\npytest.importorskip("tests.rig")\n',
        'from pytest import importorskip\nimportorskip(modname="tests.rig")\n',
        'import harness\nharness.load_plugin("tests.rig")\n',
        '__import__("tests", fromlist=["rig"])\n',
        '__import__("tests", globals(), None, ("rig",))\n',
        'from x import NAMES\n__import__("tests", fromlist=NAMES)\n',
        'import runpy\nrunpy.run_path(r"tests\\rig.py")\n',
        'import runpy\nrunpy.run_path(r"Tests\\Rig.py")\n',
        'import runpy\nrunpy.run_path("tests/rig.PY")\n',
        'import os\nimport runpy\nrunpy.run_path(os.path.join(os.getcwd(), "Rig.PY"))\n',
        'import runpy\nrunpy.run_path(r"C:tests\\rig.py")\n',
        "import Tests.Rig\n",
        'def pytest_configure(config):\n    config.pluginmanager.import_plugin("Tests.Rig")\n',
        'def pytest_configure(config):\n    config.pluginmanager.load_plugins(["tests.rig"])\n',
        'register(plugins={"rig": "tests.rig"})\n',
        'import importlib\nimportlib.import_module("..rig", package="tests")\n',
        'def pytest_configure(config):\n    config.pluginmanager.import_plugin("tests." + NAME)\n',
        'def pytest_configure(config):\n    config.pluginmanager.load_plugins([f"tests.{NAME}"])\n',
        'register("tests.{}".format(NAME))\n',
        'from builtins import exec as run\nrun("import tests.rig")\nfrom importlib import import_module as run\n',
        'import builtins as m\nm.exec("import tests.rig")\nimport importlib as m\n',
        'client.run_path(Path(__file__).parent / "rig.py")\n',
        'import runpy\nrunpy.run_path("tests/support/pkg")\n',
        'client.run_path("tests/bundle.zip")\n',
        'import importlib\nimportlib.import_module("Tests." + "rig")\n',
        'import importlib\nclass Holder:\n    pass\nholder = Holder()\nholder.load = importlib.import_module\nholder.load("tests." + "rig")\n',
        'class Holder:\n    pass\nholder = Holder()\nholder.run = exec\nholder.run("import tests.rig")\n',
        'import importlib\nload = importlib.import_module\nload = print\nload("tests." + "rig")\n',
        'import runpy\nrunpy.run_path("/srv/checkout/tests/rig.py")\n',
        'import runpy\nrunpy.run_path(r"C:\\checkout\\tests\\rig.py")\n',
        'exec("import tests.rig")\n',
        'exec("from tests import rig")\n',
        'exec(compile(source="import tests.rig", filename="<s>", mode="exec"))\n',
        'import builtins\nbuiltins.exec("import tests.rig")\n',
        'run = exec\nrun("import tests.rig")\n',
        'list(map(exec, ["import tests.rig"]))\n',
        'import builtins as b\nb.exec("import tests.rig")\n',
        'from builtins import exec as run\nrun("import tests.rig")\n',
        'exec(b"import tests.rig")\n',
        'def helper(exec):\n    return exec\nexec("import tests.rig")\n',
        'def compile(text):\n    return text\ncompile("import tests.rig")\n',
        'exec("from tests import (")\n',
        'exec(compile("import importlib\\nimportlib.import_module(\'tests.\' + NAME)", "<s>", "exec"))\n',
        'from builtins import exec as run\nrun("import tests.rig")\nrun = print\n',
        'import builtins as b\nb.exec("import tests.rig")\nb = None\n',
    ],
    ids=[
        "from-import-alias",
        "assigned-alias",
        "loader-passed-as-a-value",
        "relative-name-unknown-package",
        "dunder-import-relative-level",
        "computed-path",
        "computed-path-naming-the-file",
        "unproven-method-that-may-load",
        "importorskip",
        "importorskip-from-import-keyword",
        "unknown-call-naming-the-helper",
        "dunder-import-fromlist",
        "dunder-import-fromlist-positional",
        "dunder-import-computed-fromlist",
        "windows-separators",
        "windows-path-in-another-case",
        "extension-in-another-case",
        "computed-path-in-another-case",
        "drive-relative-path",
        "import-in-another-case",
        "plugin-name-in-another-case",
        "helper-name-inside-a-list-argument",
        "helper-name-inside-a-dict-keyword",
        "relative-name-beyond-its-package",
        "composed-plugin-name",
        "f-string-plugin-name-in-a-list",
        "template-plugin-name",
        "name-imported-as-two-loaders",
        "name-imported-as-two-modules",
        "computed-path-through-a-lookalike-loader",
        "run-path-of-a-directory",
        "lookalike-run-path-of-an-archive",
        "computed-name-spelled-in-another-case",
        "loader-stored-on-an-attribute",
        "runner-stored-on-an-attribute",
        "alias-whose-proof-is-lost",
        "absolute-path",
        "windows-absolute-path",
        "exec-of-an-import",
        "exec-of-a-from-import",
        "compile-source-keyword",
        "builtins-exec",
        "exec-aliased",
        "exec-passed-as-a-value",
        "builtins-module-alias",
        "exec-from-import-alias",
        "exec-of-bytes",
        "exec-shadowed-in-a-nested-scope",
        "own-compile-function",
        "exec-of-unparsable-source",
        "exec-of-a-computed-load",
        "runner-alias-rebound-after-the-call",
        "runner-module-rebound-after-the-call",
    ],
)
def test_a_helper_loaded_through_an_aliased_or_keyword_loader_keeps_its_carry(tmp_path: Path, conftest: str) -> None:
    """Codex on #7121. A placed test imports the helper, and a conftest also loads it through an
    alias, or the loader handed on as a value. The import alone would place
    it; the second load has to be seen, or counted as unresolved."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "conftest.py").write_text(conftest)
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


@pytest.mark.parametrize(
    ("argument", "unresolved"),
    [
        ('".".join(("tests", name))', True),
        ('".".join(["tests", name])', True),
        ('str.join(".", ("tests", name))', True),
        ('"{}.{}".format("tests", name)', True),
        ('str.format("{}.{}", "tests", name)', True),
        ('".".join(("tests", "rig"))', True),
        ('".".join(("fixtures", name))', False),
        ('os.path.join("tests", name)', False),
    ],
    ids=[
        "joined-tuple",
        "joined-list",
        "str-join",
        "formatted-pieces",
        "str-format",
        "joined-constants",
        "joined-name-outside-tests",
        "path-join",
    ],
)
def test_a_tests_name_joined_or_formatted_for_an_unlisted_call_reads_as_computed(
    argument: str, unresolved: bool
) -> None:
    """Sol on #7121. `import_plugin(".".join(("tests", name)))` builds a `tests` name at run time as
    surely as `"tests." + name`, so it reads as a computed load, all-constant pieces too, as
    `"tests." + "rig"` does. A name outside `tests` and a path join (`os.path.join`) are not one."""
    source = (
        f'import os\nname = "rig"\ndef pytest_configure(config):\n    config.pluginmanager.import_plugin({argument})\n'
    )
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("binding", "unresolved"),
    [
        ('plugin = ".".join(("tests", name))', True),
        ('plugin = "tests." + name', True),
        ('plugin = f"tests.{name}"', True),
        ('plugin = "tests.{}".format(name)', True),
        ('plugin: str = "tests." + name', True),
        ('_ = (plugin := "tests." + name)', True),
        ('first = "tests." + name\n    plugin = first', True),
        ('plugin = "fixtures"\n    plugin = "tests." + name', True),
        ('plugin = os.path.join("tests", name)', False),
        ('plugin = "fixtures." + name', False),
    ],
    ids=[
        "joined",
        "concatenated",
        "f-string",
        "formatted",
        "annotated",
        "walrus",
        "aliased",
        "bound-twice",
        "path-join",
        "outside-tests",
    ],
)
def test_a_tests_name_stored_before_an_unlisted_call_reads_as_computed(binding: str, unresolved: bool) -> None:
    """Sol on #7121. `plugin = ".".join(("tests", name)); import_plugin(plugin)` hands the call the
    same built name as the inline form, so the name it is stored in carries the marker, through
    aliases, and a name bound twice carries it if either binding does."""
    source = f'import os\nname = "rig"\ndef pytest_configure(config):\n    {binding}\n    config.pluginmanager.import_plugin(plugin)\n'
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("binding", "lookalike"),
    [
        ('plugin = "tests.rig"', {"tests.rig"}),
        ('plugin: str = "tests.rig"', {"tests.rig"}),
        ('_ = (plugin := "tests.rig")', {"tests.rig"}),
        ('first = "tests.rig"\n    plugin = first', {"tests.rig"}),
        ('plugin = ["tests.rig", "tests.web"]', {"tests.rig", "tests.web"}),
        ('plugin = "tests.rig"\n    plugin = "tests.web"', {"tests.rig", "tests.web"}),
        ('plugin = "fixtures.rig"', set()),
        ('plugin = "tests/rig.py"', set()),
    ],
    ids=["assigned", "annotated", "walrus", "aliased", "listed", "bound-twice", "outside-tests", "a-path"],
)
def test_a_tests_name_literal_stored_before_an_unlisted_call_reads_like_the_literal(
    binding: str, lookalike: set[str]
) -> None:
    """Codex on #7121. `plugin = "tests.rig"; import_plugin(plugin)` hands the call the same name as
    `import_plugin("tests.rig")`, which reads as a lookalike load (never placed, only widened); stored,
    it recorded nothing, so a placed test importing `tests.rig` became its only consumer. A name
    holds every literal any of its bindings holds, through aliases."""
    source = f"def pytest_configure(config):\n    {binding}\n    config.pluginmanager.import_plugin(plugin)\n"
    assert loaded_test_helpers(source, "tests/conftest.py").lookalike == frozenset(lookalike)


@pytest.mark.parametrize(
    ("source", "lookalike"),
    [
        ('for plugin in ["tests.rig"]:\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('for plugin in ("tests.rig", "tests.web"):\n    pm.import_plugin(plugin)\n', {"tests.rig", "tests.web"}),
        ('NAMES = ["tests.rig"]\nfor plugin in NAMES:\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        (
            'async def f():\n    async for plugin in NAMES:\n        pm.import_plugin(plugin)\nNAMES = ["tests.rig"]\n',
            {"tests.rig"},
        ),
        ('[pm.import_plugin(p) for p in ["tests.rig"]]\n', {"tests.rig"}),
        ('{pm.import_plugin(p) for p in ["tests.rig"]}\n', {"tests.rig"}),
        ('match "tests.rig":\n    case plugin:\n        pm.import_plugin(plugin)\n', {"tests.rig"}),
        (
            'NAMES = ["tests.rig"]\nmatch NAMES:\n    case [first, *rest]:\n        pm.import_plugin(first)\n',
            {"tests.rig"},
        ),
        ('NAME = "tests.rig"\nwith NAME as plugin:\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('match {"k": "tests.rig"}:\n    case {**rest}:\n        pm.import_plugin(rest)\n', {"tests.rig"}),
        ('for plugin in ["fixtures.rig"]:\n    pm.import_plugin(plugin)\n', set()),
        ("for plugin in NAMES:\n    pm.import_plugin(plugin)\n", set()),
    ],
    ids=[
        "for-loop",
        "for-loop-over-two",
        "for-loop-over-a-stored-list",
        "async-for-loop",
        "list-comprehension",
        "set-comprehension",
        "match-capture",
        "match-sequence-capture",
        "with-as",
        "match-mapping-rest",
        "outside-tests",
        "unknown-iterable",
    ],
)
def test_a_tests_name_bound_by_a_loop_comprehension_or_match_reads_like_the_literal(
    source: str, lookalike: set[str]
) -> None:
    """Sol on #7121. `for plugin in ["tests.rig"]: import_plugin(plugin)` and its comprehension bind
    `plugin` to each name the iterable holds, as `plugin = "tests.rig"` does, so the unlisted call
    reads them as lookalike loads; a `match` capture holds what its subject holds."""
    assert loaded_test_helpers(source, "tests/conftest.py").lookalike == frozenset(lookalike)


@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        ('name = "rig"\nfor plugin in ["tests." + name]:\n    pm.import_plugin(plugin)\n', True),
        ('name = "rig"\n[pm.import_plugin(p) for p in [f"tests.{name}"]]\n', True),
        ('name = "rig"\nfor plugin in ["fixtures." + name]:\n    pm.import_plugin(plugin)\n', False),
    ],
    ids=["for-loop", "comprehension", "outside-tests"],
)
def test_a_built_tests_name_bound_by_a_loop_or_comprehension_reads_as_computed(source: str, unresolved: bool) -> None:
    """Sol on #7121. The built-name marker follows the same loop and comprehension bindings."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("source", "lookalike"),
    [
        ('def load(plugin="tests.rig"):\n    pm.import_plugin(plugin)\nload()\n', {"tests.rig"}),
        ('def load(first, plugin="tests.rig"):\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('def load(plugin="tests.rig", /):\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('def load(*, plugin="tests.rig"):\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('async def load(plugin="tests.rig"):\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('load = lambda plugin="tests.rig": pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('NAME = "tests.rig"\ndef load(plugin=NAME):\n    pm.import_plugin(plugin)\n', {"tests.rig"}),
        ('def load(plugin="fixtures.rig"):\n    pm.import_plugin(plugin)\n', set()),
        ('def load(plugin, other="tests.rig"):\n    pm.import_plugin(plugin)\n', set()),
        ('def load(*, plugin, other="tests.rig"):\n    pm.import_plugin(plugin)\n', set()),
    ],
    ids=[
        "positional-default",
        "default-after-a-plain-parameter",
        "positional-only-default",
        "keyword-only-default",
        "async-def-default",
        "lambda-default",
        "default-from-a-stored-name",
        "outside-tests",
        "default-of-another-parameter",
        "keyword-only-default-of-another-parameter",
    ],
)
def test_a_tests_name_bound_as_a_parameter_default_reads_like_the_literal(source: str, lookalike: set[str]) -> None:
    """Sol on #7121. `def load(plugin="tests.rig"): import_plugin(plugin)` binds `plugin` to the
    default whenever a caller passes none, as `plugin = "tests.rig"` does, so the unlisted call reads
    it as a lookalike load. Each default pairs with its own parameter: positional defaults fill the
    last positional parameters, keyword-only ones their parameter or none."""
    assert loaded_test_helpers(source, "tests/conftest.py").lookalike == frozenset(lookalike)


@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        ('NAME = "rig"\ndef load(plugin="tests." + NAME):\n    pm.import_plugin(plugin)\n', True),
        ('NAME = "rig"\ndef load(plugin="fixtures." + NAME):\n    pm.import_plugin(plugin)\n', False),
    ],
    ids=["built-default", "outside-tests"],
)
def test_a_built_tests_name_bound_as_a_parameter_default_reads_as_computed(source: str, unresolved: bool) -> None:
    """Sol on #7121. The built-name marker follows parameter defaults too."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    "conftest",
    [
        'import runpy\nrunpy.run_path(r"tests\\rig.py")\n',
        'import os\nimport runpy\nrunpy.run_path(os.path.join(os.getcwd(), "rig.py"))\n',
    ],
    ids=["path-in-lower-case", "computed-path-in-lower-case"],
)
def test_a_helper_whose_file_name_has_capitals_keeps_its_carry(tmp_path: Path, conftest: str) -> None:
    """Codex on #7121. A case-insensitive (Windows) checkout runs `tests/Rig.py` for a path
    spelled `tests\\rig.py`, so that load reaches the helper on one platform only and no scope
    can own it, however the path or the file is cased."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "rig.py").rename(root / "Rig.py")
    (root / "library" / "test_use.py").write_text("from tests.Rig import thing\n")
    (root / "conftest.py").write_text(conftest)
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})


@pytest.mark.parametrize(
    ("target", "unresolved"),
    [
        ('Path("tests/support/pkg")', True),
        ('PurePath("tests/support/pkg")', True),
        ('os.fspath(Path("tests/bundle.zip"))', True),
        ('str(Path("tests/support/pkg"))', True),
        ('Path(__file__).parent / "pkg"', True),
        ('Path("tests/support/pkg").resolve()', True),
        ('f"{ROOT}/support/pkg"', True),
        ('os.fspath(HERE / f"{first}{second}.py")', True),
        ('Path("tests/rig.py")', False),
        ('Path(__file__).parent / "rig.py"', False),
        ('os.fspath(HERE / "rig.py")', False),
        ('f"{ROOT}/rig.py"', False),
        ("GATE", False),
    ],
    ids=[
        "path",
        "pure-path",
        "fspath-of-an-archive",
        "str-of-a-path",
        "joined-from-the-file",
        "resolved",
        "f-string",
        "fspath-of-an-unreadable-name",
        "path-to-a-py-file",
        "joined-py-file",
        "fspath-of-a-py-file",
        "f-string-py-file",
        "a-name",
    ],
)
def test_a_path_like_run_path_target_that_is_not_a_py_file_reads_as_unresolved(target: str, unresolved: bool) -> None:
    """Sol on #7121. `runpy.run_path(Path("tests/support/pkg"))` runs the `__main__.py` inside a
    directory or archive, as the literal `run_path("tests/support/pkg")` does, which no path names.
    A path-like wrapper (`Path`, `os.fspath`, `str`, `.resolve()`, a join) is read through to the
    path's last piece: one that is known and not a `.py` file is a package run, and an unreadable
    file name stays unresolved through the wrapper too. A name the reader cannot read (`GATE`) is
    the stated floor: it reaches the files the module spells."""
    source = (
        "import os, runpy\nfrom pathlib import Path, PurePath\nHERE = Path(__file__).parent\n"
        f'ROOT = "."\nGATE = "scripts/merge_gate.py"\nrunpy.run_path({target})\n'
    )
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    "consumer",
    [
        'def import_module(name):\n    return name\ndef test_it():\n    import_module("tests.rig")\n',
        "class Client:\n    def import_module(self, name):\n        return name\n"
        'def test_it():\n    Client().import_module("tests.rig")\n',
        'import importlib\nimportlib = None\ndef test_it():\n    importlib.import_module("tests.rig")\n',
        "import importlib\nclass LoaderError(Exception):\n    import_module = staticmethod(str)\n"
        "def test_it():\n    try:\n        raise LoaderError\n    except LoaderError as importlib:\n"
        '        importlib.import_module("tests.rig")\n',
        "import importlib\ndef test_it(value):\n    match value:\n        case importlib:\n"
        '            importlib.import_module("tests.rig")\n',
        "import importlib\ndef test_it(value):\n    match value:\n        case [*importlib]:\n"
        '            importlib.import_module("tests.rig")\n',
        "import importlib\ndef test_it(value):\n    match value:\n        case {**importlib}:\n"
        '            importlib.import_module("tests.rig")\n',
        'from importlib import import_module\nfrom os.path import *\ndef test_it():\n    import_module("tests.rig")\n',
        'import importlib\ndel importlib\ndef test_it():\n    importlib.import_module("tests.rig")\n',
        'import importlib\nglobals()["importlib"] = None\ndef test_it():\n    importlib.import_module("tests.rig")\n',
        'import importlib\nglobals().update(vars(object))\ndef test_it():\n    importlib.import_module("tests.rig")\n',
    ],
    ids=[
        "own-function",
        "unrelated-method",
        "rebound-module",
        "exception-target",
        "match-capture",
        "match-star",
        "match-mapping-rest",
        "star-import",
        "deleted-module",
        "rebound-through-globals",
        "namespace-updated",
    ],
)
def test_a_call_that_only_looks_like_a_loader_does_not_place_the_helper(tmp_path: Path, consumer: str) -> None:
    """Codex on #7121. A placed test's own `import_module`, a method of the same name, or a loader
    module rebound to something else is not a proven load, so it cannot make the only consumer
    of an otherwise unimported helper a placed one."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text(consumer)
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_helper_the_rootdir_conftest_loads_as_a_plugin_keeps_its_carry(tmp_path: Path) -> None:
    """The repository's own `conftest.py` loads `tests.support.spawned_servers` through
    `pytest_plugins` for every session, outside `tests/`. A placed import of the same helper
    cannot place it while the rootdir conftest hands it to every suite."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["tests.rig"]\n')
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


@pytest.mark.parametrize(
    "conftest",
    [
        pytest.param('pytest_plugins = []\npytest_plugins.append("tests.rig")\n', id="append"),
        pytest.param('pytest_plugins = []\npytest_plugins.extend(["tests.rig"])\n', id="extend"),
        pytest.param('pytest_plugins = []\npytest_plugins.insert(0, "tests.rig")\n', id="insert"),
        pytest.param("from x import EXTRA\npytest_plugins = []\npytest_plugins.extend(EXTRA)\n", id="computed-extend"),
        pytest.param(
            'from x import EXTRA\npytest_plugins = ["a"]\npytest_plugins = pytest_plugins + EXTRA\n', id="read-back"
        ),
        pytest.param('globals()["pytest_plugins"] = ["tests.rig"]\n', id="by-key"),
        pytest.param('import sys\nsetattr(sys.modules[__name__], "pytest_plugins", ["tests.rig"])\n', id="by-setattr"),
        pytest.param("from x import EXTRA\npytest_plugins = plugins = []\nplugins.extend(EXTRA)\n", id="chained-alias"),
        pytest.param('pytest_plugins, other = ["tests.rig"], []\n', id="tuple-target"),
        pytest.param('(pytest_plugins := ["tests.rig"])\n', id="walrus"),
        pytest.param("from tests.plugin_list import pytest_plugins\n", id="imported"),
        pytest.param('globals().update(pytest_plugins=["tests.rig"])\n', id="by-keyword"),
        pytest.param('globals().update({"pytest_plugins": ["tests.rig"]})\n', id="by-dict-key"),
        pytest.param('match ["tests.rig"]:\n    case pytest_plugins:\n        pass\n', id="match-capture"),  # Sol
        pytest.param('match ["tests.rig"]:\n    case [*pytest_plugins]:\n        pass\n', id="match-star"),
        pytest.param('match {"p": "tests.rig"}:\n    case {**pytest_plugins}:\n        pass\n', id="match-rest"),
    ],
)
def test_a_plugin_list_built_by_mutation_keeps_the_helper_carried(tmp_path: Path, conftest: str) -> None:
    """`pytest_plugins` filled by `append`/`extend`/`insert`, read back, or set by its name hands
    the helper to every session. A literal mutation names it; any other use may name any helper.
    Either way a placed import of the helper cannot place it."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (tmp_path / "conftest.py").write_text(conftest)
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


def test_a_plugin_list_that_names_no_helper_leaves_the_helper_placed(tmp_path: Path) -> None:
    """A literal `append` of a non-helper plugin, and the name `pytest_plugins` held as data (the
    root-conftest test compares `t.id == "pytest_plugins"`), load no helper, so the placed helper keeps
    its edge and carries nothing."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (tmp_path / "conftest.py").write_text('pytest_plugins = []\npytest_plugins.append("scripts.plugin")\n')
    (root / "conftest.py").write_text(
        'import ast\nFOUND = [n for n in ast.walk(ast.parse("")) if getattr(n, "id", "") == "pytest_plugins"]\n'
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


@pytest.mark.parametrize(
    "call",
    [
        'importlib.util.spec_from_file_location("rig", "tests/rig.py")',
        'importlib.machinery.SourceFileLoader("rig", "tests/rig.py")',
    ],
)
def test_a_spec_built_in_a_placed_test_keeps_the_carry(tmp_path: Path, call: str) -> None:
    """Codex on #7121. `spec_from_file_location` and a `SourceFileLoader` only prepare a load: the
    module runs on `exec_module`, which may happen anywhere the spec goes, or never (a test that
    reads the spec's origin). So they can only widen: the placed test does not place the helper."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text(
        f"import importlib.machinery, importlib.util\ndef test_it():\n    {call}\n"
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})


def test_source_compiled_in_a_placed_test_keeps_the_carry(tmp_path: Path) -> None:
    """`compile("import tests.rig", ...)` holds a load that may run anywhere the code object goes, not
    only in the test that compiles it, so it can only widen: the placed test does not place the helper."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text(
        'CODE = compile("import tests.rig", "<s>", "exec")\ndef test_it():\n    pass\n'
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})


def test_executed_source_that_loads_no_helper_leaves_the_helper_placed(tmp_path: Path) -> None:
    """`exec` of a literal is read like the module's own code: one that loads no helper adds no
    consumer, so the placed helper keeps its edge and carries nothing."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "conftest.py").write_text(
        'import re\nexec("import json")\neval("1 + 1")\nre.compile("tests\\\\.rig")\n'
        'PATTERN = re.compile\nre.compile(PATTERN)\nKNOWN = ["tests.rig"]\n'
        're.compile("from tests import rig")\n'
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


def test_a_computed_path_load_of_another_file_leaves_the_helper_placed(tmp_path: Path) -> None:
    """`tests/test_merge_gate.py` loads a script through a computed path and holds
    `"tests/test_foo.py"` as data. That load can reach only files the module names, none of them a
    helper, so the placed helper keeps its edge and carries nothing."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "conftest.py").write_text(
        "import importlib.util\nfrom pathlib import Path\n"
        'importlib.util.spec_from_file_location("gate", Path("scripts") / "gate.py")\n'
        'CASES = ["tests/test_foo.py"]\n'
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


@pytest.mark.parametrize(
    ("source", "spells_rig"),
    [
        ('import runpy\nlist(map(runpy.run_path, ["tests/rig.py"]))\n', True),
        ('from runpy import run_path\nlist(map(run_path, ["tests/rig.py"]))\n', True),
        ('from runpy import run_path as go\nlist(map(go, ["tests/rig.py"]))\n', True),
        (
            'import importlib.util\nlist(map(importlib.util.spec_from_file_location, ["rig"], ["tests/rig.py"]))\n',
            True,
        ),
        (
            'from importlib.machinery import SourceFileLoader\nholder.load = SourceFileLoader\nPATH = "tests/rig.py"\n',
            True,
        ),
        ('import importlib\nlist(map(importlib.import_module, ["fixtures/rig.py"]))\n', False),
    ],
    ids=[
        "run-path",
        "imported-run-path",
        "aliased-run-path",
        "spec-from-file-location",
        "source-file-loader",
        "name-loader",
    ],
)
def test_a_path_loader_handed_on_as_a_value_reaches_the_files_the_module_spells(source: str, spells_rig: bool) -> None:
    """Sol on #7121. `map(runpy.run_path, ["tests/rig.py"])` hands the loader paths the reader never
    sees, as `runpy.run_path(GATE)` does, so it reaches every file the module spells (`path_targets`)
    rather than reading as a name load that a slashed literal never marks. A name loader handed on
    stays a computed name load."""
    loads = loaded_test_helpers(source, "tests/conftest.py")
    assert ("rig.py" in loads.path_targets, loads.unresolved) == (spells_rig, False)


def test_a_runner_handed_on_stays_a_runner_when_a_nested_scope_shadows_its_name() -> None:
    """Sol on #7121. `def helper(exec)` binds `exec` in one function only, but the proof is per module,
    so `holder.run = exec` read as no runner and `holder.run("import tests.rig")` vanished. A runner
    read as a value is matched through every binding, proven or not: it only widens."""
    source = 'def helper(exec):\n    return exec\nholder.run = exec\nholder.run("import tests.rig")\n'
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved


@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        ('run = exec\nholder.run = run\nholder.run("import tests.rig")\n', True),
        ('import builtins as b\nrun = b.exec\nholder.run = run\nholder.run("import tests.rig")\n', True),
        ('run = exec\nfn = run\nholder.run = fn\nholder.run("import tests.rig")\n', True),
        ('run = exec\nrun("x = 1")\n', False),  # the control: the alias called in place is read, not handed on
    ],
    ids=[
        "assigned-alias-handed-on",
        "assigned-module-alias-handed-on",
        "alias-chain-handed-on",
        "alias-called-in-place",
    ],
)
def test_an_assigned_runner_alias_handed_on_reads_as_unresolved(source: str, unresolved: bool) -> None:
    """Sol on #7121. `run = exec; holder.run = run` hands the runner on through a name only
    `_proven_loaders` follows, so matching against imports and builtins alone missed it and
    `holder.run("import tests.rig")` vanished. Propagated aliases count as runner bindings too."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("alias", "direct"),
    [
        (
            'import builtins\nb = builtins\nb.exec("import tests.rig")\n',
            'import builtins\nbuiltins.exec("import tests.rig")\n',
        ),
        (
            'import importlib\nm = importlib\nn = m\nn.import_module("tests.rig")\n',
            'import importlib\nimportlib.import_module("tests.rig")\n',
        ),
        (
            'import importlib.util\nu = importlib.util\nu.spec_from_file_location("rig", "tests/rig.py")\n',
            'import importlib.util\nimportlib.util.spec_from_file_location("rig", "tests/rig.py")\n',
        ),
        (
            "import builtins\nb = builtins\nholder.run = b.exec\n",
            "import builtins\nholder.run = builtins.exec\n",
        ),
    ],
    ids=["builtins-alias-exec", "importlib-alias-chain", "util-attribute-alias", "builtins-alias-handed-on"],
)
def test_an_assigned_loader_module_alias_reads_like_the_module(alias: str, direct: str) -> None:
    """Sol on #7121. `b = builtins; b.exec("import tests.rig")` was dropped entirely, because only
    loader aliases were propagated, not loader-module aliases. An alias now reads as the module it
    holds; each direct form reads some load, so the comparison cannot pass on two empty readings."""
    expected = loaded_test_helpers(direct, "tests/conftest.py")
    assert expected.by_call or expected.lookalike or expected.unresolved
    assert loaded_test_helpers(alias, "tests/conftest.py") == expected


def test_a_name_bound_to_a_loader_module_and_to_anything_else_proves_nothing() -> None:
    """A module alias rebound to another value holds either, so a call through it is not proven."""
    source = 'import importlib\nm = importlib\nm = client\nm.import_module("tests.rig")\n'
    assert loaded_test_helpers(source, "tests/conftest.py").by_call == frozenset()


def test_a_name_holding_two_loader_modules_reads_as_unresolved() -> None:
    """`m` may hold either module, so `m.exec(...)` may run code: it is unresolved, not dropped."""
    source = 'import importlib\nimport builtins\nm = importlib\nm = builtins\nm.exec("import tests.rig")\n'
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved


@pytest.mark.parametrize(
    ("source", "by_call", "unresolved"),
    [
        (
            'from importlib import import_module as load\nimport builtins\nload = builtins\nload("tests.rig")\n',
            set(),
            True,
        ),
        (
            'import builtins as load\nfrom importlib import import_module\nload = import_module\nload("tests.rig")\n',
            set(),
            True,
        ),
        (
            'from importlib import import_module as load\nimport builtins\nb = builtins\nload = b\nload("tests.rig")\n',
            set(),
            True,
        ),
        (
            'from importlib import import_module\nimport builtins\na = import_module\na = builtins\na("tests.rig")\n',
            set(),
            True,
        ),
        ('from importlib import import_module as load\nload("tests.rig")\n', {"tests.rig"}, False),
    ],
    ids=["module-over-loader", "loader-over-module", "module-alias-over-loader", "assigned-both", "loader-only"],
)
def test_a_name_holding_a_loader_and_a_loader_module_reads_as_unresolved(
    source: str, by_call: set[str], unresolved: bool
) -> None:
    """Sol on #7121. `load` may hold `import_module` or `builtins`, so `load("tests.rig")` is not a
    proven load (as `import_module` it would place the helper): a name in both tables proves
    nothing, and a call through it is unresolved, like a name holding two loaders."""
    found = loaded_test_helpers(source, "tests/conftest.py")
    assert (found.by_call, found.unresolved) == (frozenset(by_call), unresolved)


@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        ('import builtins\nb: object = builtins\nb.exec("import tests.rig")\n', True),
        ('import builtins\n(b := builtins).exec("import tests.rig")\n', True),
        ('import builtins\nfor b in [builtins]:\n    b.exec("import tests.rig")\n', True),
        ("import builtins\nrun_with(builtins)\n", False),  # the stated floor: handed to a call
        (
            'from importlib import import_module as a\nimport builtins as a\nb = a\nb.exec("import tests.rig")\n',
            True,
        ),
        ('import builtins\nbuiltins.exec("x = 1")\n', False),  # controls: a receiver, a followed alias
        ('import builtins\nb = builtins\nb.exec("x = 1")\n', False),
        ("import importlib\nimportlib.reload(module)\n", False),
    ],
    ids=[
        "annotated-alias",
        "walrus-alias",
        "loop-target",
        "module-passed-as-an-argument",
        "alias-of-a-two-binding-name",
        "module-as-a-receiver",
        "followed-module-alias",
        "other-attribute-of-the-module",
    ],
)
def test_a_loader_module_handed_on_as_a_value_reads_as_unresolved(source: str, unresolved: bool) -> None:
    """Sol on #7121. `b: object = builtins` and `b = a` over a name imported as two things hid
    `b.exec("import tests.rig")`, and each binding form followed would leave the next one. A
    loader module bound to another name by any other form reads as unresolved. One handed to a call
    is the stated floor (`monkeypatch.setattr(builtins, ...)` on the real tree)."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    "source",
    [
        'import builtins\nmatch builtins:\n    case b:\n        b.exec("import tests.rig")\n',
        'import builtins\nwith builtins as b:\n    b.exec("import tests.rig")\n',
        'import builtins\ndef f(b=builtins):\n    b.exec("import tests.rig")\n',
        'import builtins\ndef f(*, b=builtins):\n    b.exec("import tests.rig")\n',
        'import builtins\ndef get():\n    return builtins\nget().exec("import tests.rig")\n',
        'import builtins\ndef gen():\n    yield builtins\nnext(gen()).exec("import tests.rig")\n',
        "import builtins\nkeep(lambda: builtins)\n",
    ],
    ids=[
        "match-subject",
        "with-item",
        "parameter-default",
        "keyword-default",
        "returned",
        "yielded",
        "lambda-in-a-call",
    ],
)
def test_a_loader_module_bound_or_returned_by_any_form_reads_as_unresolved(source: str) -> None:
    """Sol on #7121. `match builtins: case b:` bound the module where the reader did not look, and
    a default, a `with` target, a return or a yield hand it on the same way. Every form that binds
    or returns a value counts; only a module handed to a call stays the stated floor."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved


@pytest.mark.parametrize(
    ("source", "unresolved"),
    [
        (
            "from builtins import exec as run\nholder.run = run\nfrom importlib import import_module as run\n"
            'holder.run("import tests.rig")\n',
            True,
        ),
        ("from builtins import exec as run\nkeep(run)\nfrom importlib import import_module as run\n", True),
        ('from builtins import exec as run\nrun("x = 1")\n', False),  # the control: one binding, called in place
    ],
    ids=["conflicting-name-stored", "conflicting-name-passed", "single-binding-called"],
)
def test_a_name_imported_as_two_loaders_handed_on_reads_as_unresolved(source: str, unresolved: bool) -> None:
    """Sol on #7121. `run` imported as `exec` and as `import_module` keeps one identity in the
    import table, so storing it (`holder.run = run`) lost the runner. A name bound to two loaders
    read as a value may hold either, so it reads as unresolved."""
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("call", "spells_rig", "unresolved"),
    [
        ('runpy.run_path(Path(__file__).parent / ("r" + "ig.py"))', True, False),  # folds to `rig.py`
        ("runpy.run_path(Path(__file__).parent / f\"{'r' + 'ig'}.py\")", True, False),  # no literal alone fills it
        ('runpy.run_path(os.path.dirname(__file__) + "/rig.py")', True, False),  # the last piece is whole
        ('stem = "ig.py"\nrunpy.run_path(Path(__file__).parent / ("r" + stem))', True, False),  # filled by a literal
        ('runpy.run_path(os.path.join(HERE, f"{stem}.py"))\nLOADED = ["rig"]', True, False),
        ('runpy.run_path(HERE / ("%s.py" % stem))\nstem = "rig"', True, False),
        ('runpy.run_path(HERE / f"{first}{second}.py")\nPARTS = ["r", "ig"]', False, True),  # two holes
        ('runpy.run_path(os.path.join(HERE, f"{first}{second}.py"))', False, True),
        ('runpy.run_path(HERE / f"{stem}.py")', False, False),  # no literal fills it: the stated floor
        ('runpy.run_path(HERE / "{}{}.py".format("r", "ig"))', True, False),  # a format of literals folds (Sol)
        ('runpy.run_path(HERE / "{0}.py".format(stem))\nNAMES = ["rig"]', True, False),
        ('runpy.run_path(HERE / "{}{}.py".format(first, second))', False, True),
        ('runpy.run_path(HERE / "{name}.py".format(name=stem))', False, True),  # a named field is unreadable
        ('runpy.run_path(HERE / "{0.stem}.py".format(path))\nNAMES = ["rig"]', False, True),  # so is an attribute
    ],
    ids=[
        "folded-concatenation",
        "folded-f-string",
        "whole-last-piece",
        "concatenation-filled-by-a-literal",
        "f-string-filled-by-a-literal",
        "format-filled-by-a-literal",
        "two-holes",
        "two-holes-in-a-join",
        "hole-no-literal-fills",
        "folded-str-format",
        "str-format-filled-by-a-literal",
        "str-format-with-two-holes",
        "str-format-with-a-named-field",
        "str-format-with-an-attribute-field",
    ],
)
def test_a_computed_path_with_a_fragmented_file_name_folds_or_reads_unresolved(
    call: str, spells_rig: bool, unresolved: bool
) -> None:
    """Sol on #7121. A path's file name built from pieces (`"r" + "ig.py"`) is spelled by no single
    literal, so a load of `rig.py` hid from `path_targets`. A build of literals is folded the way
    Python evaluates it, and one hole is filled by each literal the module spells, which reads
    `tests/test_equivalence_audio_probe.py`'s `SCRIPTS / f"{name}.py"` as the scripts it names.
    Two holes are unresolved; a hole no literal fills is the stated floor, as a name read from a
    file is."""
    loads = loaded_test_helpers(f"import os\nimport runpy\nfrom pathlib import Path\n{call}\n", "tests/conftest.py")
    assert ("rig.py" in loads.path_targets, loads.unresolved) == (spells_rig, unresolved)


@pytest.mark.parametrize(
    "call",
    [
        'importlib.import_module("tests.rig")',
        'importlib.import_module(name="tests.rig")',
        'importlib.import_module(".rig", package="tests")',
        'runpy.run_module("tests.rig")',
        'runpy.run_path(r"tests\\rig.py")',
        'runpy.run_path("./tests/rig.py")',
        'load("tests.rig")',
        'fetch = importlib.import_module\n    fetch("tests.rig")',
        'globals().get("x"), globals()["importlib"]\n    importlib.import_module("tests.rig")',
        'pytest.importorskip("tests.rig")',
        'print(REPO / "tests", "tests.rig:Probe", "tests.engine-warning-log")\n    importlib.import_module("tests.rig")',
        'runpy.run_path("Other.PY")\n    from tests.rig import thing',
        'pytest.importorskip(modname="tests.rig")',
        '__import__("tests", fromlist=["rig"])',
    ],
)
def test_a_literal_import_module_in_a_placed_test_places_the_helper(tmp_path: Path, call: str) -> None:
    """`importlib.import_module("tests.rig")` loads the helper for its caller, as an import does,
    and so does the `name=` keyword form; neither is an unresolved load."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text(
        f"import importlib.util, pytest, runpy\nfrom importlib import import_module as load\ndef test_it():\n    {call}\n"
    )
    assert helper_carried_scopes(config, tmp_path) == frozenset()
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {"engine": ("library",)}


@pytest.mark.parametrize(
    ("rebinding", "by_call"),
    [
        ("", frozenset()),  # the control: the sentinel guards dead code
        ("match True:\n    case TYPE_CHECKING:\n        pass\n", frozenset({"tests.rig"})),
        ("match [True]:\n    case [*TYPE_CHECKING]:\n        pass\n", frozenset({"tests.rig"})),
        ('match {"a": 1}:\n    case {**TYPE_CHECKING}:\n        pass\n', frozenset({"tests.rig"})),
    ],
    ids=["sentinel", "match-capture", "match-star", "match-rest"],
)
def test_a_match_capture_of_type_checking_makes_its_block_live(rebinding: str, by_call: frozenset[str]) -> None:
    """Sol on #7121. `case TYPE_CHECKING:` rebinds the name to a runtime value, so an `if
    TYPE_CHECKING:` block after it may run and its load counts, as after any other rebinding."""
    source = f'from typing import TYPE_CHECKING\nimport importlib\n{rebinding}if TYPE_CHECKING:\n    importlib.import_module("tests.rig")\n'
    assert loaded_test_helpers(source, "tests/conftest.py").by_call == by_call


@pytest.mark.parametrize(
    ("imports", "guard", "loaded"),
    [
        ("from typing import TYPE_CHECKING as gate\n", "gate", frozenset()),
        (
            "from typing import TYPE_CHECKING as gate\nfrom typing import cast as gate\n",
            "gate",
            frozenset({"tests.rig"}),
        ),
        (
            "from typing import TYPE_CHECKING as gate\nfrom typing_extensions import Any as gate\n",
            "gate",
            frozenset({"tests.rig"}),
        ),
        (
            "from typing import TYPE_CHECKING\nfrom typing import cast as TYPE_CHECKING\n",
            "TYPE_CHECKING",
            frozenset({"tests.rig"}),
        ),
        ("from typing import TYPE_CHECKING as gate\nimport typing as gate\n", "gate", frozenset({"tests.rig"})),
        ("from typing import TYPE_CHECKING\nfrom typing import *\n", "TYPE_CHECKING", frozenset({"tests.rig"})),
        ("from typing import TYPE_CHECKING, cast\n", "TYPE_CHECKING", frozenset()),
        (
            "from typing import TYPE_CHECKING\nfrom typing_extensions import TYPE_CHECKING\n",
            "TYPE_CHECKING",
            frozenset(),
        ),
        ("import typing as t\nfrom typing import cast\n", "t.TYPE_CHECKING", frozenset()),
    ],
    ids=[
        "sentinel-alias",
        "alias-rebound-by-typing",
        "alias-rebound-by-typing-extensions",
        "name-rebound-by-typing",
        "alias-rebound-to-the-typing-module",
        "star-import-from-typing",
        "another-typing-name-beside-it",
        "sentinel-from-both-modules",
        "typing-module-alias",
    ],
)
def test_a_typing_import_rebinding_the_sentinel_makes_its_block_live(
    imports: str, guard: str, loaded: frozenset[str]
) -> None:
    """Sol on #7121. Only `from typing import TYPE_CHECKING [as name]` binds the sentinel: any other
    import from `typing` or `typing_extensions` binding the same name (`cast as gate`, `import typing
    as gate`) leaves a truthy value, so an `if gate:` block after it may run and its load counts
    (a star import rebinds `importlib` too, so its load only widens)."""
    loads = loaded_test_helpers(
        f'{imports}import importlib\nif {guard}:\n    importlib.import_module("tests.rig")\n', "tests/conftest.py"
    )
    assert loads.by_call | loads.lookalike == loaded


@pytest.mark.parametrize(
    ("rebinding", "guard", "loaded"),
    [
        ("", "TYPE_CHECKING", frozenset()),  # the control: the sentinel guards dead code
        ('globals()["TYPE_CHECKING"] = True\n', "TYPE_CHECKING", frozenset({"tests.rig"})),
        ('vars()["TYPE_CHECKING"] = True\n', "TYPE_CHECKING", frozenset({"tests.rig"})),
        ('globals().__setitem__("TYPE_CHECKING", True)\n', "TYPE_CHECKING", frozenset({"tests.rig"})),
        ("globals().update(TYPE_CHECKING=True)\n", "TYPE_CHECKING", frozenset({"tests.rig"})),
        ("globals().update(flags)\n", "TYPE_CHECKING", frozenset({"tests.rig"})),
        ('setattr(sys.modules[__name__], "TYPE_CHECKING", True)\n', "TYPE_CHECKING", frozenset({"tests.rig"})),
        ("typing.TYPE_CHECKING = True\n", "typing.TYPE_CHECKING", frozenset({"tests.rig"})),
        ('setattr(typing, "TYPE_CHECKING", True)\n', "typing.TYPE_CHECKING", frozenset({"tests.rig"})),
        ('globals().get("TYPE_CHECKING")\n', "TYPE_CHECKING", frozenset()),
        ('globals()["OTHER"] = True\n', "TYPE_CHECKING", frozenset()),
    ],
    ids=[
        "sentinel",
        "globals-key",
        "vars-key",
        "setitem-method",
        "update-keyword",
        "update-unread-keys",
        "setattr-own-module",
        "typing-attribute",
        "setattr-typing",
        "namespace-read",
        "another-key",
    ],
)
def test_a_namespace_or_attribute_write_of_type_checking_makes_its_block_live(
    rebinding: str, guard: str, loaded: frozenset[str]
) -> None:
    """Sol on #7121. `globals()["TYPE_CHECKING"] = True` rebinds the sentinel as surely as an
    assignment, so the block after it may run: its load and its import count. So does any write
    through the namespace whose key the reader cannot read (`globals().update(flags)`), and an
    attribute write (`typing.TYPE_CHECKING = True`, `setattr(...)`). A read and another key do not."""
    source = (
        "import importlib, sys, typing\nfrom typing import TYPE_CHECKING\nflags = {}\n"
        f'{rebinding}if {guard}:\n    import apps.core\n    importlib.import_module("tests.rig")\n'
    )
    loads = loaded_test_helpers(source, "tests/conftest.py")
    assert loads.by_call | loads.lookalike == loaded  # a namespace method leaves `importlib` unproven: it widens
    assert ("apps.core" in imported_modules(source, "tests/conftest.py")) is bool(loaded)


@pytest.mark.parametrize(
    ("use", "unresolved"),
    [
        ("pytest_plugins = PLUGINS\n", True),  # the control: an unread value
        ('globals().__setitem__("pytest_plugins", PLUGINS)\n', True),
        ('globals().setdefault("pytest_plugins", PLUGINS)\n', True),
        ('vars().__setitem__("pytest_plugins", PLUGINS)\n', True),
        ('plugins = globals().get("pytest_plugins")\n', True),
        ("globals().update(BY_NAME)\n", True),
        ("globals()[KEY] = PLUGINS\n", True),
        ('settings = globals().get("other")\n', False),
        ('globals()["other"] = PLUGINS\n', False),
    ],
    ids=[
        "declared",
        "setitem-method",
        "setdefault-method",
        "vars-setitem",
        "namespace-get",
        "update-unread-keys",
        "computed-key",
        "control-read-other",
        "control-write-other",
    ],
)
def test_a_namespace_method_on_pytest_plugins_reads_as_unresolved(use: str, unresolved: bool) -> None:
    """Sol on #7121. `globals().setdefault("pytest_plugins", PLUGINS)` hands pytest a list the reader
    never reads, as `pytest_plugins = PLUGINS` does, so it is unresolved; so is any namespace method
    naming the list, and any namespace write whose key the reader cannot read."""
    source = f'PLUGINS = ["tests.rig"]\nBY_NAME = load_mapping()\nKEY = "pytest_plugins"\n{use}'
    assert loaded_test_helpers(source, "tests/conftest.py").unresolved is unresolved


@pytest.mark.parametrize(
    ("consumer", "carried"),
    [
        (
            'from typing import TYPE_CHECKING\nimport importlib\nif TYPE_CHECKING:\n    importlib.import_module("tests.rig")\n',
            frozenset({"engine"}),
        ),
        (
            'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from importlib import import_module\nimport_module("tests.rig")\n',
            frozenset({"engine"}),
        ),
        ("if False:\n    from tests.rig import thing\n", frozenset()),
    ],
    ids=["type-checking-load-call", "loader-imported-only-for-type-checking", "false-literal-guard-reads-as-live"],
)
def test_a_load_only_for_type_checking_does_not_place_the_helper(
    tmp_path: Path, consumer: str, carried: frozenset[str]
) -> None:
    """Codex on #7121. Code under `if TYPE_CHECKING:` never runs, so a load there is none at all
    and a loader bound only there proves nothing. Other code that never runs (`if False:`, after
    a `return`) reads as live, the stated floor: it can cost a carry only when the helper's other
    consumers are kinds the reader cannot see, and modelling it grew a dead-code analysis that
    every review extended (nightly code-quality pass on #7121)."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "library" / "test_use.py").write_text(consumer)
    assert helper_carried_scopes(config, tmp_path) == carried


@pytest.mark.parametrize(
    ("source", "lookalike", "unresolved"),
    [
        (b"# coding: latin-1\nimport tests.r\xe9\n", frozenset({"tests.r\xe9"}), False),
        (b"import tests.rig  # \xff\n", frozenset(), True),
    ],
    ids=["declared-latin-1", "invalid-utf-8"],
)
def test_executed_bytes_are_decoded_as_python_decodes_source(
    source: bytes, lookalike: frozenset[str], unresolved: bool
) -> None:
    """Sol on #7121. `exec(b"...")` decodes its bytes by PEP 263, so the reader does too: a coding
    declaration is honored, and bytes Python cannot decode are unresolved, like text that does not
    parse, never a replacement character read as if it ran."""
    loads = loaded_test_helpers(f"exec({source!r})\n", "tests/test_use.py")
    assert (loads.lookalike, loads.unresolved) == (lookalike, unresolved)


@pytest.mark.parametrize(
    ("conftest", "plugins"),
    [
        ('from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pytest_plugins = ["tests.rig"]\n', frozenset()),
        ('pytest_plugins = ["tests.rig"]\n', frozenset({"tests.rig"})),
    ],
    ids=["declared-only-for-type-checking", "declared-at-run-time"],
)
def test_a_plugin_list_only_for_type_checking_hands_pytest_nothing(conftest: str, plugins: frozenset[str]) -> None:
    """Sol on #7121. `pytest_plugins` under `if TYPE_CHECKING:` never runs, like an import there."""
    assert loaded_test_helpers(conftest, "tests/conftest.py").by_plugins == plugins


@pytest.mark.parametrize(
    ("source", "by_call", "unresolved"),
    [
        (  # each name may hold either loader, so a call through one proves nothing, and propagation
            # must converge rather than swap the two loaders forever (Codex on #7121)
            'from importlib import import_module as a\nfrom runpy import run_module as b\na = b\nb = a\na("tests.rig")\n',
            frozenset(),
            True,
        ),
        (  # a chain of single-loader names still resolves, however many passes it takes
            'from importlib import import_module as a\nb = a\nc = b\nc("tests.rig")\n',
            frozenset({"tests.rig"}),
            False,
        ),
        (  # an alias of a two-loader name may hold either loader too (Sol on #7121)
            "from importlib import import_module as a\nfrom importlib.util import spec_from_file_location\n"
            'a = spec_from_file_location\nc = a\nc("rig", "tests/rig.py")\n',
            frozenset(),
            True,
        ),
        (  # the walk reaches `b = a` inside the function after `c = b`, so `c` resolves on a later pass
            'from importlib import import_module as a\ndef bind():\n    global b\n    b = a\nc = b\nc("tests.rig")\n',
            frozenset({"tests.rig"}),
            False,
        ),
    ],
    ids=["aliases-assigned-to-each-other", "alias-chain", "alias-of-a-two-loader-name", "alias-bound-on-a-later-pass"],
)
def test_loader_aliases_converge_and_a_name_holding_two_loaders_proves_nothing(
    source: str, by_call: frozenset[str], unresolved: bool
) -> None:
    loads = loaded_test_helpers(source, "tests/test_use.py")
    assert (loads.by_call, loads.unresolved) == (by_call, unresolved)


@pytest.mark.parametrize(
    "call",
    [
        'importlib.import_module("..rig", package="tests")',
        'client.import_module(".rig")',
        'client.__import__("rig", level=1)',
    ],
)
def test_a_relative_name_python_cannot_resolve_does_not_crash_the_plan(call: str) -> None:
    """Sol on #7121. `importlib.util.resolve_name` raises on `..rig` beyond `tests`, and on a relative
    name with no package. The reader reads code that may never run, so such a call is unresolved,
    never a crash, and proves no load; so is a relative load through a call that only shares a
    loader's name."""
    loads = loaded_test_helpers(f"import importlib\n{call}\n", "tests/test_use.py")
    assert (loads.by_call, loads.unresolved) == (frozenset(), True)


@pytest.mark.parametrize(
    ("call", "by_call"),
    [
        ('runpy.run_module("tests.pkg")', frozenset({"tests.pkg", "tests.pkg.__main__"})),
        ('runpy.run_path("tests/rig.py")', frozenset({"tests.rig"})),
    ],
    ids=["run-module-of-a-package", "run-path-of-a-file"],
)
def test_run_module_also_reads_the_package_main(call: str, by_call: frozenset[str]) -> None:
    """Sol on #7121. `runpy.run_module("tests.pkg")` runs `tests/pkg/__main__.py` when `tests.pkg` is a
    package, so that helper is loaded too; a path load names its one file."""
    assert loaded_test_helpers(f"import runpy\n{call}\n", "tests/conftest.py").by_call == by_call


@pytest.mark.parametrize("consumer", ["import tests.__init__\n", "from tests import __init__\n"])
def test_a_package_init_helper_keeps_its_carry(tmp_path: Path, consumer: str) -> None:
    """Codex on #7121. pytest runs a package's `__init__.py` (here `tests/__init__.py`, a global
    helper under `tests/*.py`) while collecting every test beneath it, so an importer that names
    it is never its only consumer: like a `conftest.py`, it is never placed, however it is named."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "rig.py").unlink()
    (root / "__init__.py").write_text("from apps.engine_core import thing\n")
    (root / "library" / "test_use.py").write_text(consumer)
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})
    assert observed_dependents(config, pytest_inputs(root), root=tmp_path) == {}


@pytest.mark.parametrize(
    ("importer", "source", "carried"),
    [
        ("web/test_star.py", "from tests import *\n", {"engine"}),
        ("web/test_star.py", "from .. import *\n", {"engine"}),
        ("web/conftest.py", "from tests import *\n", {"engine"}),
        ("web/test_star.py", "from tests.other import *\n", set()),
        ("web/test_star.py", "from apps.engine_core import *\n", set()),
    ],
    ids=["absolute", "relative", "in-a-conftest", "another-package", "a-source-package"],
)
def test_a_wildcard_import_of_a_test_package_consumes_every_module_in_it(
    tmp_path: Path, importer: str, source: str, carried: set[str]
) -> None:
    """Sol on #7121. `from tests import *` runs `tests/__init__.py` and imports every submodule its
    `__all__` lists, `rig` here, so the importer consumes that helper too: unowned, it unplaces it.
    A wildcard import names every module under the package, a superset of what `__all__` can
    list; a wildcard import of another package names none of them."""
    config, root = _root_helper_tree(tmp_path, "library/test_use.py")
    (root / "__init__.py").write_text('__all__ = ["rig"]\n')
    (root / importer).parent.mkdir(parents=True, exist_ok=True)
    (root / importer).write_text(source)
    assert helper_carried_scopes(config, tmp_path) == frozenset(carried)


def test_a_root_helper_nobody_imports_still_carries(tmp_path: Path) -> None:
    """No importer is not the same as no consumer: `pytest_plugins` and fixture names reach
    a helper without an import statement, and unplaceable stays widest."""
    config, root = _root_helper_tree(tmp_path, "test_root.py")
    (root / "test_root.py").write_text("")
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})


def test_a_root_helper_consumed_by_another_helper_still_carries(tmp_path: Path) -> None:
    config, _root = _root_helper_tree(tmp_path, "other_rig.py")
    assert helper_carried_scopes(config, tmp_path) == frozenset({"engine"})


def test_a_conftest_carries_even_when_only_root_tests_sit_beside_it(tmp_path: Path) -> None:
    """A `conftest.py` is loaded by pytest, not imported, so its consumers are every module
    under its directory whether or not any of them names it."""
    root = tmp_path / "tests"
    root.mkdir()
    (root / "conftest.py").write_text("from apps.engine_core import thing\n")
    (root / "test_root.py").write_text("from tests.conftest import thing\n")
    assert helper_carried_scopes(_config(), tmp_path) == frozenset({"engine"})
