"""Round 12 helper consumer tests, part b (split from test_ci_plan_helpers.py).

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
