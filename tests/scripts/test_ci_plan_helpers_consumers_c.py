"""Round 12 helper consumer tests, part c (split from test_ci_plan_helpers.py).

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
