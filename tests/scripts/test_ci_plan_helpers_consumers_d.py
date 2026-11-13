"""Round 12 helper consumer tests, part d (split from test_ci_plan_helpers.py).

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


def test_the_importer_scan_is_reused_only_while_the_tree_is_unchanged(tmp_path: Path) -> None:
    """The three drift guards and the planner ask for the same scan in one process, ~54 s each on
    the real tree (tick 320), so it is cached; the key carries each input's mtime and size.
    [if] an edited helper's new import is not rescanned [then] the guards read a stale tree, so fail.
    [if] a caller's edit to the returned sets leaks into the next call [then] fail."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "helper_a.py").write_text("X = 1\n")
    (tests / "helper_b.py").write_text("Y = 1\n")
    conftest = tests / "conftest.py"
    conftest.write_text("from tests.helper_a import X\n")
    inputs = {f"tests/{p.name}": p for p in sorted(tests.glob("*.py"))}
    first = _helper_importers(inputs, tmp_path)
    assert first["tests/helper_a.py"] == {"tests/conftest.py"} and not first["tests/helper_b.py"], first
    first["tests/helper_a.py"].add("tampered")
    assert _helper_importers(inputs, tmp_path)["tests/helper_a.py"] == {"tests/conftest.py"}
    conftest.write_text("from tests.helper_b import Y  # now B\n")
    second = _helper_importers(inputs, tmp_path)
    assert second["tests/helper_b.py"] == {"tests/conftest.py"} and not second["tests/helper_a.py"], second
