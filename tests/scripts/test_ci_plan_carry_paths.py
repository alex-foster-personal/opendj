"""Round 22: helper carry at FILE granularity.

`helper_carried` names the scopes a global pytest helper reaches, and until round 22 any change in
one ran everything. Most of a carried scope is code no helper imports: on the 60-merge replay
window ending c454c7b27d, helper-carry FULLs fall from 9 to 3 with this round, and on the one
ending 0ba0fa2703 from 4 to 1 (`(private path)`, round 22). `helper_carried_paths` lists
the helpers' module closure, so a `.py` change outside it plans like any other change in its scope.

What stays FULL, because the import reader cannot see it:
- a non-Python file in a carried scope (a helper may read it as data);
- every `.py` file in a scope where a closure module loads code by a name it cannot show, or a
  helper reaches the scope by MENTION rather than import (the entry is then the scope's source
  prefix);
- every carried scope, when such a load sits in a global helper or an unowned closure module. A
  load whose names are literals (`_can_import("soundfile")`) is followed like an import.

[if] a change a global helper can reach plans narrower than everything [then] fail here, [else stop].
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts import ci_plan
from scripts.ci_derived_inputs import CARRY_PATHS
from scripts.ci_plan import (
    SESSION_LOAD,
    Change,
    Config,
    PlanError,
    Scope,
    Verdict,
    _scan_helper_importers,
    plan,
    read_config,
)
from scripts.ci_plan_carry import _is_plain_component, helper_carried_paths

pytestmark = pytest.mark.requirement("OPS-16")


def _changed(*paths: str) -> tuple[Change, ...]:
    return tuple(Change("M", path) for path in paths)


def _carried_config(paths: tuple[str, ...] | None) -> Config:
    return Config(
        (),
        ("tests/*.py",),
        (Scope("a", ("apps/a/",), ("tests/a/",)), Scope("b", ("apps/b/",), ("tests/b/",))),
        helper_carried=("a",),
        helper_carried_paths=paths,
    )


# ----- plan time -----


def test_a_python_file_no_helper_imports_plans_like_its_scope() -> None:
    got = plan(_changed("apps/a/other.py"), _carried_config(("apps/a/used.py",)))
    assert got.verdict is Verdict.SCOPED, got.reason
    assert "a" in got.scopes


def test_a_python_file_in_the_helper_closure_runs_everything() -> None:
    got = plan(_changed("apps/a/other.py", "apps/a/used.py"), _carried_config(("apps/a/used.py",)))
    assert got.verdict is Verdict.FULL
    assert got.reason == "a global pytest helper carries the scope owning: apps/a/used.py"


@pytest.mark.parametrize("path", ["apps/a/weights.json", "apps/a/used.pyi", "apps/a/native.so"])
def test_a_non_python_file_in_a_carried_scope_runs_everything(path: str) -> None:
    """A helper may read it as data, and no import reader sees a read. The reason is asserted: round
    6's rule also makes an undeclared non-Python file FULL, and must not be what this test sees."""
    got = plan(_changed(path), _carried_config(("apps/a/used.py",)))
    assert got.reason == f"a global pytest helper carries the scope owning: {path}"


def test_a_scope_prefix_entry_carries_every_python_file_in_it() -> None:
    got = plan(_changed("apps/a/other.py"), _carried_config(("apps/a/",)))
    assert got.verdict is Verdict.FULL, got.reason


def test_a_config_without_the_path_list_carries_whole_scopes() -> None:
    """Round 19's behavior, not a narrower one: a Config built without the list never frees a file."""
    got = plan(_changed("apps/a/other.py"), _carried_config(None))
    assert got.verdict is Verdict.FULL, got.reason


def test_a_listed_path_outside_every_carried_scope_carries_nothing() -> None:
    """The list narrows `helper_carried`; it never adds a scope the derivation left out."""
    got = plan(_changed("apps/b/mod.py"), _carried_config(("apps/b/mod.py",)))
    assert got.verdict is Verdict.SCOPED, got.reason


# ----- the derivation -----


def _tree(tmp_path: Path, files: dict[str, str], carried: tuple[str, ...] = ("a", "b", "c", "front")) -> Config:
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text)
    return Config(
        (),
        ("tests/*.py",),
        (
            Scope("a", ("apps/a/",), ("tests/a/",)),
            Scope("b", ("apps/b/",), ("tests/b/",)),
            Scope("c", ("apps/c/",), ("tests/c/",)),
            Scope("front", ("apps/front/",), (), mentions=("apps/front",)),
        ),
        helper_carried=carried,
    )


def test_the_closure_holds_what_the_helper_imports_and_the_packages_on_the_way(tmp_path: Path) -> None:
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from apps.a import used\n",
            "apps/a/__init__.py": "",
            "apps/a/used.py": "import os\n",
            "apps/a/other.py": "import os\n",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/__init__.py", "apps/a/used.py"})


def test_the_closure_follows_imports_across_scopes_and_relative_imports(tmp_path: Path) -> None:
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "import apps.a.used\n",
            "apps/a/used.py": "from . import sibling\nfrom apps.b.mod import thing\n",
            "apps/a/sibling.py": "import os\n",
            "apps/b/mod.py": "import os\n",
            "apps/b/unused.py": "import os\n",
            "apps/c/mod.py": "import os\n",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/used.py", "apps/a/sibling.py", "apps/b/mod.py"})


def test_a_scope_owned_test_helper_a_global_helper_imports_is_reached(tmp_path: Path) -> None:
    """`tests/a/rig.py` belongs to scope a, and every suite loading the global conftest runs it."""
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from tests.a.rig import probe\n",
            "tests/a/rig.py": "import os\n",
            "tests/a/test_x.py": "",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"tests/a/rig.py"})


@pytest.mark.parametrize(
    "load",
    [
        "importlib.import_module(name)",
        "importlib.import_module('.'.join(('apps', 'c')))",
        "__import__(name)",
        "runpy.run_module(name)",
        "exec(code)",
        "compile(code, 'x', 'exec')",
        "importlib.util.find_spec(name)",  # imports the parent of whatever dotted name it gets (Sol on #7495)
    ],
)
def test_a_dynamic_load_in_the_closure_carries_its_whole_scope(tmp_path: Path, load: str) -> None:
    """Parity with round 19: a by-name load inside the scope was carried, and stays carried. A path
    loader with a computed target carries more since the twenty-ninth review (below)."""
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from apps.a import used\n",
            "apps/a/used.py": f"import importlib, importlib.util, runpy\nfrom apps.b import mod\ndef f(name, code, path):\n    {load}\n",
            "apps/b/mod.py": "import os\n",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/", "tests/a/", "apps/b/mod.py"})


@pytest.mark.parametrize(
    "load",
    [
        "import builtins\ndef f(code):\n    builtins.exec(code)\n",
        "import builtins as b\ndef f(code):\n    b.eval(code)\n",
        "def f(code):\n    __builtins__.compile(code, 'x', 'exec')\n",
        "from builtins import exec as run\ndef f(code):\n    run(code)\n",
    ],
)
def test_a_qualified_or_imported_builtin_runner_carries_its_whole_scope(tmp_path: Path, load: str) -> None:
    """`builtins.exec(code)` and `from builtins import exec as run` run code as bare `exec` does (Sol on #7495)."""
    config = _tree(tmp_path, {"tests/conftest.py": "from apps.a import used\n", "apps/a/used.py": load})
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/", "tests/a/"})


@pytest.mark.parametrize(
    "call",
    ["re.compile(name)", "json.load(name)", "pattern.compile(name)"],
)
def test_a_call_that_loads_no_code_does_not_widen(tmp_path: Path, call: str) -> None:
    """The precision control: 24 of 25 loader-named calls in the real closure load no code."""
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from apps.a import used\n",
            "apps/a/used.py": f"import importlib.util, json, re\ndef f(name, pattern):\n    {call}\n",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/used.py"})


@pytest.mark.parametrize(
    "helper",
    [
        "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
        "def _can_import(name):\n    __import__(name)\nNAME = 'apps.b.mod'\nOK = _can_import(NAME)\n",
        "def _can_import(name):\n    __import__(name)\nOK = _can_import('soundfile')\nCHECK = map(_can_import, NAMES)\n",
        "from importlib import import_module as load\nX = load('soundfile')\n",
        "def run(code):\n    exec(code)\n",
        "def can_import(name):\n    __import__(name)\nOK = can_import('soundfile')\n",  # public: callable from anywhere
        "def _can_import(name):\n    name = 'apps.' + name\n    __import__(name)\nOK = _can_import('b.mod')\n",
        "import importlib\nMOD = importlib.import_module('.mod', PACKAGE)\n",  # relative to a computed package
        "import importlib\nMOD = importlib.import_module('.mod')\n",  # relative to no package
        "import importlib\nMOD = importlib.import_module('apps.b.mod', package='apps', strict=True)\n",
        "MOD = __import__('mod', globals(), None, ['x'], 1)\n",  # level 1: relative to the caller
        "MOD = __import__('apps', fromlist=['*'])\n",  # the submodules `__all__` lists
        "MOD = __import__('apps.b', fromlist=NAMES)\n",
        "MOD = __import__('apps.b', globals(), fromlist=['mod'])\n",
        "def _load(name):\n    return importlib.import_module(name)\nMOD = _load('.mod')\n",
    ],
)
def test_an_unresolved_load_in_a_global_helper_carries_every_carried_scope(tmp_path: Path, helper: str) -> None:
    """A global helper that loads a name it cannot show may run any carried scope's code (Sol on #7495)."""
    config = _tree(tmp_path, {"tests/conftest.py": helper})
    assert helper_carried_paths(config, tmp_path) == frozenset(
        {"apps/a/", "tests/a/", "apps/b/", "tests/b/", "apps/c/", "tests/c/", "apps/front/"}
    )


def test_an_unresolved_load_in_an_unowned_closure_module_carries_every_carried_scope(tmp_path: Path) -> None:
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from tools import loader\n",
            "tools/loader.py": "import importlib\ndef load(name):\n    return importlib.import_module(name)\n",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset(
        {"apps/a/", "tests/a/", "apps/b/", "tests/b/", "apps/c/", "tests/c/", "apps/front/"}
    )


@pytest.mark.parametrize(
    ("helper", "reached"),
    [
        (  # `tests/conftest.py`'s optional-dependency probe: third-party names reach no repository file
            "def _can_import(name):\n    try:\n        __import__(name)\n    except Exception:\n        return False\n"
            "    return True\nOK = _can_import('soundfile') and _can_import('librosa')\n",
            set(),
        ),
        ("import importlib\nMOD = importlib.import_module('apps.b.mod')\n", {"apps/b/mod.py"}),
        ("def _load(name):\n    return __import__(name)\nMOD = _load('apps.b.mod')\n", {"apps/b/mod.py"}),
        ("import importlib\nMOD = importlib.import_module('.mod', 'apps.b')\n", {"apps/b/mod.py"}),
        ("import importlib\nMOD = importlib.import_module('..b.mod', package='apps.a')\n", {"apps/b/mod.py"}),
        ("import importlib\nMOD = importlib.import_module('apps.b.mod', 'tools')\n", {"apps/b/mod.py"}),
        ("MOD = __import__('apps.b', fromlist=['mod'])\n", {"apps/b/mod.py"}),  # each entry may be a submodule
        (
            "def _load(name):\n    return __import__(name, fromlist=('mod',))\nMOD = _load('apps.b')\n",
            {"apps/b/mod.py"},
        ),
    ],
)
def test_a_load_with_literal_names_is_followed_like_an_import(tmp_path: Path, helper: str, reached: set[str]) -> None:
    config = _tree(tmp_path, {"tests/conftest.py": helper, "apps/b/mod.py": "import os\n"})
    assert helper_carried_paths(config, tmp_path) == frozenset(reached)


_WINDOWS_UNSAFE_NAMES = pytest.mark.parametrize(
    ("name", "file"),
    [
        ("apps.b.a\\b", "apps/b/a\\b.py"),  # a backslash part: two components on Windows
        ("apps.b.C:probe", "apps/b/C:probe.py"),  # a drive-qualified part: relative to drive C's own cwd
        ("C:\\probe", "C:\\probe.py"),  # a drive-absolute name: outside the checkout on Windows
        ("apps.b.C:", "apps/b/C:.py"),  # a bare drive part: `Path("apps", "b", "C:")` is just `C:`
    ],
    ids=["backslash-part", "drive-qualified-part", "drive-absolute-name", "bare-drive-part"],
)


@_WINDOWS_UNSAFE_NAMES
def test_a_loader_literal_with_a_windows_separator_or_drive_is_refused_before_any_file_lookup(
    name: str, file: str
) -> None:
    """Sol on #7495, on every host and with no file written: some part of each name is not one plain
    component under Windows rules, so the name is refused before a path is built from it."""
    assert not all(_is_plain_component(part) for part in name.split(".")), file
    assert all(_is_plain_component(part) for part in ("apps", "b", "beat-grid"))  # control: plain parts pass


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="writes files named `C:\\probe.py`, `C:probe.py` and `a\\b.py`: on Windows a drive root or a data stream",
)
@_WINDOWS_UNSAFE_NAMES
def test_a_loader_literal_with_a_windows_separator_or_drive_reaches_no_file(
    tmp_path: Path, name: str, file: str
) -> None:
    """Sol on #7495. On Windows `Path(*parts)` reads a backslash as a separator and `C:` as a drive,
    so such a part can leave the checkout. Each part must be one plain component under POSIX and
    Windows rules alike, so plans do not depend on the host. On POSIX the file exists under that odd
    name and imports a scoped module, so a reach to it would show; Windows never writes it (Sol on
    #7495: `tmp_path / "C:\\probe.py"` is the drive root there), and the test above holds the refusal."""
    files = {
        "tests/conftest.py": f"import importlib\nMOD = importlib.import_module({name!r})\n",
        file: "import apps.b.mod\n",
        "apps/b/mod.py": "import os\n",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset()


def test_a_loader_literal_naming_a_non_identifier_file_still_reaches_it(tmp_path: Path) -> None:
    """Sol on #7495. Only import statements need identifiers: `import_module("apps.b.beat-grid")`
    loads `apps/b/beat-grid.py`, so the file stays in the helper's closure."""
    files = {
        "tests/conftest.py": "import importlib\nMOD = importlib.import_module('apps.b.beat-grid')\n",
        "apps/b/beat-grid.py": "import os\n",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset({"apps/b/beat-grid.py"})


@pytest.mark.parametrize("shape", ["absolute-path", "empty-part"])
def test_a_loader_literal_that_is_not_a_module_name_reaches_no_file(tmp_path: Path, shape: str) -> None:
    """Sol on #7495. `import_module("/tmp/probe")` split into one absolute part, so `root / part`
    dropped the root and the reader read `/tmp/probe.py` outside the checkout. Python imports no
    such name, so a literal with an empty part or a `/` reaches nothing. The outside file imports a
    repository module, so a read of it would show."""
    root = tmp_path / "repo"
    (tmp_path / "probe.py").write_text("import apps.b.mod\n")
    (root / "apps" / "b").mkdir(parents=True)
    (root / "apps" / "b" / "mod.py").write_text("import os\n")
    name = {
        "absolute-path": str(tmp_path / "probe"),
        "empty-part": "apps..b.mod",
    }[shape]
    config = _tree(root, {"tests/conftest.py": f"import importlib\nMOD = importlib.import_module({name!r})\n"})
    assert helper_carried_paths(config, root) == frozenset()


@pytest.mark.parametrize(
    ("probe", "reached"),
    [
        ("importlib.util.find_spec('apps.b.mod')", {"apps/b/__init__.py"}),
        ("importlib.util.find_spec('.mod', 'apps.b')", {"apps/b/__init__.py"}),
        ("find_spec('apps.b.mod')", {"apps/b/__init__.py"}),
        ("importlib.util.find_spec('starlette')", set()),  # `tests/conftest.py`'s probe: a top-level name runs nothing
    ],
)
def test_find_spec_runs_the_parent_package_of_a_dotted_name(tmp_path: Path, probe: str, reached: set[str]) -> None:
    """Sol on #7495. `find_spec("apps.b.mod")` imports `apps.b` first, so its `__init__` runs, and
    only finds `mod`; a top-level name is found without running anything."""
    files = {
        "tests/conftest.py": f"import importlib.util\nfrom importlib.util import find_spec\nSPEC = {probe}\n",
        "apps/b/__init__.py": "import os\n",
        "apps/b/mod.py": "import os\n",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(reached)


def test_a_scope_a_helper_mentions_is_carried_whole(tmp_path: Path) -> None:
    config = _tree(tmp_path, {"tests/conftest.py": 'DIST = "apps/front/dist"\n', "apps/front/app.py": ""})
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/front/"})  # it owns no tests


def test_a_scope_a_closure_module_mentions_is_carried_whole(tmp_path: Path) -> None:
    """A module the helper imports may run or read any file of a carried scope it spells, as the helper
    itself may, so round 22 keeps that scope whole (Sol on #7495)."""
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from apps.a import used\n",
            "apps/a/used.py": 'DIST = "apps/front/dist"\n',
            "apps/front/app.py": "",
        },
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/used.py", "apps/front/"})


_RUNNER = (
    "import os\nimport runpy\nfrom importlib.machinery import SourceFileLoader\n"
    "from importlib.util import spec_from_file_location\nfrom pathlib import Path\nREPO = Path(__file__).parents[2]\n"
)


@pytest.mark.parametrize(
    "run",
    [
        'runpy.run_path(REPO / "apps" / "front" / "plugin.py")',
        'runpy.run_path(os.path.join(REPO, "apps", "front", "plugin.py"))',
        'runpy.run_path(REPO.joinpath("apps").joinpath("front", "plugin.py"))',
        'runpy.run_path(REPO / "apps" / f"{\'fro\'}nt" / "plugin.py")',
        'runpy.run_path(REPO / "apps" / ("fro" + "nt") / "plugin.py")',
    ],
    ids=["division", "os-path-join", "joinpath-chain", "f-string-segment", "concatenated-segment"],
)
def test_a_scope_a_closure_module_spells_through_a_constant_join_is_carried_whole(tmp_path: Path, run: str) -> None:
    """Sol on #7495: each literal was matched alone, so `REPO / "apps" / "front" / "plugin.py"` spelled
    no `apps/front` and a change to the plugin the helper runs avoided FULL. A constant join is read
    as the path it builds, as `narrow` reads it."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f"{_RUNNER}{run}\n",
        "apps/front/plugin.py": "",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset({"apps/a/", "tests/a/", "apps/front/"})


@pytest.mark.parametrize(
    "run",
    [
        'runpy.run_path(REPO / "apps" / "b" / "tool.py")',
        'runpy.run_path("apps/b/tool.py")',
        'runpy.run_path(path_name=str(REPO / "apps" / "b" / "tool.py"))',
        'spec_from_file_location("tool", REPO / "apps" / "b" / "tool.py")',
        'SourceFileLoader("tool", os.fspath(Path(REPO, "apps/b/tool.py")))',
    ],
    ids=["division", "literal", "keyword-str", "spec-from-file-location", "source-file-loader"],
)
def test_a_file_a_closure_module_runs_by_a_readable_path_is_followed_like_an_import(tmp_path: Path, run: str) -> None:
    """A path loader runs its target and everything the target imports, in a scope no mention names
    (Sol on #7495). A target that is a repository `.py` file joins the closure; the caller's own scope
    stays whole, as round 19 carried it."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f"{_RUNNER}{run}\n",
        "apps/b/tool.py": "from apps.c import dep\n",
        "apps/c/dep.py": "",
    }
    reached = {"apps/a/", "tests/a/", "apps/b/tool.py", "apps/c/dep.py"}
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(reached)


@pytest.mark.parametrize(
    ("load", "reached"),
    [
        ('importlib.import_module("apps.c.dep")', {"apps/c/dep.py"}),
        ('__import__("apps.c.dep")', {"apps/c/dep.py"}),
        ("importlib.import_module(NAME)", set()),  # control: a computed name follows nothing more
    ],
    ids=["import-module", "dunder-import", "computed-name"],
)
def test_a_literal_name_load_beside_a_path_load_is_still_followed(tmp_path: Path, load: str, reached: set[str]) -> None:
    """Sentry on #8139: a path loader in the same file made the name loads unshown, so a literal
    `import_module` target in another scope was dropped. Each kind of load is read on its own; the
    caller's scope stays whole, as it does for any path load."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f'import importlib\nNAME = "apps." + "c.dep"\n{_RUNNER}runpy.run_path(REPO / "apps" / "b" / "tool.py")\n{load}\n',
        "apps/b/tool.py": "",
        "apps/c/dep.py": "",
    }
    expected = {"apps/a/", "tests/a/", "apps/b/tool.py", *reached}
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(expected)


def test_a_directory_a_closure_module_runs_by_path_is_followed_through_its_main(tmp_path: Path) -> None:
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f'{_RUNNER}runpy.run_path(REPO / "apps" / "b" / "pkg")\n',
        "apps/b/pkg/__main__.py": "",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(
        {"apps/a/", "tests/a/", "apps/b/pkg/__main__.py"}
    )


@pytest.mark.parametrize(
    "run",
    [
        'runpy.run_path(Path(__file__).parent.parent / "b" / "tool.py")',
        'runpy.run_path(Path(__file__).resolve().parents[1] / "b" / "tool.py")',
        'TOOL = REPO / "apps" / "b" / "tool.py"\nrunpy.run_path(TOOL)',
    ],
    ids=["parent-step", "parents-index", "bound-name"],
)
def test_a_path_a_closure_module_runs_from_its_own_file_resolves_as_narrow_resolves_a_walk(
    tmp_path: Path, run: str
) -> None:
    """The join's head is resolved as `narrow` resolves a walk target, from the module's own path, so
    `Path(__file__).parent.parent / "b" / "tool.py"` in `apps/a/used.py` runs `apps/b/tool.py`, not
    the `b/tool.py` decoy at the checkout root a head-less reading would follow."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f"{_RUNNER}{run}\n",
        "apps/b/tool.py": "from apps.c import dep\n",
        "apps/c/dep.py": "",
        "b/tool.py": "",
    }
    reached = {"apps/a/", "tests/a/", "apps/b/tool.py", "apps/c/dep.py"}
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(reached)


@pytest.mark.parametrize(
    "run",
    [
        "runpy.run_path(NAME)",
        'spec_from_file_location("tool", NAME)',
        'runpy.run_path(REPO / "apps" / NAME / "plugin.py")',
        'runpy.run_path(f"{REPO}/apps/{NAME}/plugin.py")',
        'list(map(runpy.run_path, [REPO / "apps" / "b" / "tool.py"]))',
        'runpy.run_path("/srv/checkout/apps/b/tool.py")',
        'runpy.run_path(REPO / "apps" / "b" / "gone.py")',
    ],
    ids=["name", "spec-name", "runtime-part", "runtime-f-string", "handed-on", "absolute", "no-such-file"],
)
def test_a_path_a_closure_module_runs_that_names_no_repository_file_carries_every_carried_scope(
    tmp_path: Path, run: str
) -> None:
    """A path loader whose target the reader cannot place in the checkout may run any carried scope's
    code, as an unowned module's unresolved load may, so every carried scope is carried whole (Sol on
    #7495). No global helper or closure module on this tree runs a file by path."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f'{_RUNNER}NAME = os.environ["PLUGIN"]\n{run}\n',
        "apps/b/tool.py": "",
    }
    every = {"apps/a/", "tests/a/", "apps/b/", "tests/b/", "apps/c/", "tests/c/", "apps/front/"}
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset(every)


@pytest.mark.parametrize(
    "spelled",
    ['DATA = REPO / "apps" / "c" / "data.json"', 'DATA = REPO / "apps" / NAME / "plugin.py"'],
    ids=["unrelated-join", "runtime-part"],
)
def test_a_join_no_loader_runs_and_no_mention_matches_carries_nothing_more(tmp_path: Path, spelled: str) -> None:
    """Control: a path the module only builds runs nothing, and `apps/c` is no scope's mention."""
    files = {
        "tests/conftest.py": "from apps.a import used\n",
        "apps/a/used.py": f'{_RUNNER}NAME = "x"\n{spelled}\n',
        "apps/c/data.json": "{}",
    }
    assert helper_carried_paths(_tree(tmp_path, files), tmp_path) == frozenset({"apps/a/used.py"})


def test_a_scope_outside_helper_carried_gets_no_entry(tmp_path: Path) -> None:
    """`plan` consults the list only inside `helper_carried` scopes, so an entry elsewhere is dead text
    that claims a carry no plan applies (Sol on #7495). Round 19 never carried a scope a closure module
    only mentions: on this tree that is prose, through 17 docstring and message literals."""
    config = _tree(
        tmp_path,
        {
            "tests/conftest.py": "from apps.a import used\n",
            "apps/a/used.py": 'from apps.b import mod\nDIST = "apps/front/dist"\n',
            "apps/b/mod.py": "",
            "apps/front/app.py": "",
        },
        carried=("a",),
    )
    assert helper_carried_paths(config, tmp_path) == frozenset({"apps/a/used.py"})


def _parallel_tree(tmp_path: Path) -> dict[str, Path]:
    tests = tmp_path / "tests"
    tests.mkdir()
    files = {
        "conftest.py": "pytest_plugins = ['tests.plugin']\nfrom tests.helper_a import X\n",
        "helper_a.py": "X = 1\n",
        "helper_b.py": "import importlib\nM = importlib.import_module('tests.helper_c')\n",
        "helper_c.py": "Y = 1\n",
        "plugin.py": "",
        "test_one.py": "from tests.helper_b import M\n",
    }
    for name, text in files.items():
        (tests / name).write_text(text)
    return {f"tests/{p.name}": p for p in sorted(tests.glob("*.py"))}


def test_the_parallel_importer_scan_equals_the_serial_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] reading inputs in worker processes changes any importer edge [then] the carry-path and
    dependents guards compare against a different derivation, so fail.
    [if] a file a worker cannot parse is skipped instead of raising [then] fail."""
    inputs = _parallel_tree(tmp_path)
    serial = _scan_helper_importers(inputs, tmp_path)
    monkeypatch.setattr(ci_plan, "_PARALLEL_FROM", 1)
    monkeypatch.setattr(ci_plan, "_SCAN_WORKERS", 2)
    assert _scan_helper_importers(inputs, tmp_path) == serial
    assert serial["tests/helper_c.py"] == {"tests/helper_b.py"} and serial["tests/plugin.py"] == {SESSION_LOAD}, serial
    (tmp_path / "tests" / "broken.py").write_text("def (:\n")
    with pytest.raises(PlanError, match=r"cannot parse tests/broken\.py"):
        _scan_helper_importers({**inputs, "tests/broken.py": tmp_path / "tests" / "broken.py"}, tmp_path)


@pytest.mark.timeout(600)  # scans every tests/**/*.py: ~135 s CPU, over 120 s on a loaded runner
def test_the_committed_path_list_is_exactly_the_derived_one() -> None:
    """The anti-rot guard, an equality in both directions like `helper_carried`'s."""
    config = read_config()
    derived = helper_carried_paths(config, Path("."))
    assert derived, "no helper-carried path found; the derivation measured nothing"
    assert config.helper_carried_paths is not None
    committed = config.helper_carried_paths
    # A repeated entry fails the equality below while its add and remove lists both print
    # empty (main at 80d4c76d90, Tue 3 Nov 2026), so name it first.
    repeated = sorted(path for path, n in Counter(committed).items() if n > 1)
    assert not repeated, f"ci/test-scopes.yml `helper_carried_paths` lists {repeated} more than once; keep one"
    assert tuple(sorted(derived)) == tuple(sorted(committed)), (
        "ci/test-scopes.yml `helper_carried_paths` no longer matches the global helpers' module closure:"
        f" add {sorted(derived - set(committed))}, remove {sorted(set(committed) - derived)};"
        f" regenerate: {CARRY_PATHS.regenerate}"
    )
