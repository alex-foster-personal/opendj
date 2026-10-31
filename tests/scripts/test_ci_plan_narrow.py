"""Round 23: a DEPENDENT scope contributes only the test modules that can reach the change.

A scope is a dependent of another when ANY of its test modules imports it, and the planner then ran
ALL of its test modules: a change to two `scripts/` modules ran 1330 test modules, of which
narrowing keeps 532 (the built replay). `narrow` keeps the changed scopes' tests and the
always-run tests whole and, from a dependent scope, the modules whose static reach (their imports,
their conftest chain's, and the repository modules their literals name, closed transitively)
holds a changed `.py` file, or spells a mention of a changed non-Python file.

[if] a test module that can reach a changed file is left out of a narrowed plan [then] fail here, [else stop].
"""

from __future__ import annotations

import posixpath
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from scripts.ci_plan import REPO, Change, Config, Plan, Scope, Verdict, plan
from scripts.ci_plan_narrow import DECLARED_WALKS, Reach, declared_walk_targets, narrow, read_reach_index

pytestmark = pytest.mark.requirement("OPS-16")

# web depends on core and docs; raw on core, as the derived `dependents` in ci/test-scopes.yml say.
CONFIG = Config(
    (),
    ("tests/*.py",),
    (
        Scope("core", ("apps/core/",), ("tests/core/",), ("raw", "web")),
        Scope("web", ("apps/web/",), ("tests/web/",)),
        Scope("docs", ("docs/",), (), ("web",), mentions=("docs",)),
        Scope("raw", ("apps/raw/",), ("tests/raw/",)),
        Scope("x", ("apps/x/",), ("tests/x/",)),  # two idle scopes keep three under the cap
        Scope("y", ("apps/y/",), ("tests/y/",)),
    ),
)
MODULES = (
    "tests/test_root.py",
    "tests/core/test_core.py",
    "tests/web/test_uses_core.py",
    "tests/web/test_reads_docs.py",
    "tests/web/test_unrelated.py",
    "tests/raw/test_raw.py",
)


def _reach(**files: Reach) -> dict[str, Reach]:
    unrelated = Reach(frozenset({"apps/web/views.py"}), frozenset({"hello"}), frozenset())
    return {module: files.get(Path(module).stem, unrelated) for module in MODULES}


def _narrowed(paths: tuple[str, ...], index: Mapping[str, Reach | None], guards: frozenset[str] = frozenset()) -> Plan:
    changes = tuple(Change("M", p) for p in paths)
    return narrow(plan(changes, CONFIG), changes, CONFIG, MODULES, index, guards)


# ----- narrow: pure -----


def test_a_dependent_module_that_cannot_reach_the_change_is_dropped() -> None:
    index = _reach(test_uses_core=Reach(frozenset({"apps/core/model.py"}), frozenset(), frozenset()))
    got = _narrowed(("apps/core/model.py",), index)
    assert got.verdict is Verdict.SCOPED, got.reason
    assert "tests/web/test_uses_core.py" in got.test_paths
    assert "tests/web/test_unrelated.py" not in got.test_paths
    assert "tests/web/" not in got.test_paths


def test_the_changed_scopes_tests_and_the_always_run_tests_stay_whole() -> None:
    got = _narrowed(("apps/core/model.py",), _reach())
    assert "tests/core/" in got.test_paths
    assert "tests/*.py" in got.test_paths


def test_a_dependent_scope_no_module_of_which_reaches_the_change_contributes_nothing() -> None:
    got = _narrowed(("apps/core/model.py",), _reach())
    assert not [p for p in got.test_paths if p.startswith(("tests/web/", "tests/raw/"))], got.test_paths
    assert got.scopes == ("core",), "a dependent that contributes no test is not a selected scope"


def test_a_module_whose_reach_cannot_be_read_is_kept() -> None:
    index: dict[str, Reach | None] = dict(_reach())
    index["tests/web/test_unrelated.py"] = None
    got = _narrowed(("apps/core/model.py",), index)
    assert "tests/web/test_unrelated.py" in got.test_paths


def test_a_module_missing_from_the_index_is_kept() -> None:
    """The universe is passed in, so a module the index never read is kept, never forgotten."""
    index: dict[str, Reach | None] = dict(_reach())
    del index["tests/raw/test_raw.py"]
    got = _narrowed(("apps/core/model.py",), index)
    assert "tests/raw/test_raw.py" in got.test_paths


@pytest.mark.parametrize("walked", ["apps", "apps/core", "."])
def test_a_module_that_walks_a_directory_holding_the_change_is_kept(walked: str) -> None:
    """A scanner names the directory it walks, never the file: `REPO / "apps"` then `rglob("*.py")`
    reads every file below it (`tests/quality/test_underscored_param_callers.py`, Codex on #7495)."""
    index = _reach(test_unrelated=Reach(frozenset({"tests/web/test_unrelated.py"}), frozenset(), frozenset({walked})))
    got = _narrowed(("apps/core/model.py",), index)
    assert "tests/web/test_unrelated.py" in got.test_paths


def test_a_walked_directory_that_does_not_hold_the_change_keeps_nothing() -> None:
    index = _reach(
        test_unrelated=Reach(frozenset({"tests/web/test_unrelated.py"}), frozenset({"apps"}), frozenset({"apps/web"}))
    )
    got = _narrowed(("apps/core/model.py",), index)
    assert "tests/web/test_unrelated.py" not in got.test_paths, "a spelled directory that is not walked keeps nothing"


def test_a_non_python_change_keeps_the_dependent_modules_that_spell_its_mention() -> None:
    index = _reach(
        test_reads_docs=Reach(frozenset({"tests/web/test_reads_docs.py"}), frozenset({"docs/guide.md"}), frozenset())
    )
    got = _narrowed(("docs/guide.md",), index)
    assert "tests/web/test_reads_docs.py" in got.test_paths
    assert "tests/web/test_unrelated.py" not in got.test_paths


def test_a_non_python_change_with_no_matching_mention_keeps_its_dependents_whole() -> None:
    """`docs` declares the needle `docs`; a path that does not contain it was claimed by a source
    prefix alone, so no module-level reading can find its readers."""
    config = Config(
        CONFIG.full_triggers,
        CONFIG.always,
        (*CONFIG.scopes[:2], Scope("docs", ("handbook/",), (), ("web",), mentions=("docs",)), *CONFIG.scopes[3:]),
    )
    changes = (Change("M", "handbook/guide.md"),)
    got = narrow(plan(changes, config), changes, config, MODULES, _reach(), frozenset())
    assert "tests/web/" in got.test_paths


def test_a_full_plan_is_returned_unchanged() -> None:
    config = Config(("pyproject.toml",), CONFIG.always, CONFIG.scopes)
    changes = (Change("M", "pyproject.toml"),)
    before = plan(changes, config)
    assert narrow(before, changes, config, MODULES, _reach(), frozenset()) == before


def test_a_whole_tree_guard_is_never_narrowed_away() -> None:
    """`ci/pr-always-run.txt` lists the guards that scan every tracked file without spelling one
    (the OSS audit, the DDL authority scan), so no reach can show they read the change (Codex on #7495)."""
    got = _narrowed(("docs/guide.md",), _reach(), guards=frozenset({"tests/web/test_unrelated.py"}))
    assert "tests/web/test_unrelated.py" in got.test_paths
    assert "tests/web/test_uses_core.py" not in got.test_paths


def test_a_module_that_runs_code_of_the_changed_data_files_scope_is_kept() -> None:
    """`Path(__file__).parent / "data.json"` spells no needle, so code of the scope that owns a changed
    non-Python file may read it unseen: data reads stay scope-granular, as in round 22 (Sentry on #7495)."""
    index = _reach(test_unrelated=Reach(frozenset({"docs/build.py"}), frozenset({"data.json"}), frozenset()))
    got = _narrowed(("docs/data.json",), index)
    assert "tests/web/test_unrelated.py" in got.test_paths
    assert "tests/web/test_uses_core.py" not in got.test_paths


def test_a_module_that_runs_a_test_helper_of_the_changed_data_files_scope_is_kept() -> None:
    """A scope owns its tests too, so a helper under them may read its data unseen (Sol on #7495)."""
    config = Config(
        CONFIG.full_triggers,
        CONFIG.always,
        (Scope("core", ("apps/core/",), ("tests/core/",), ("raw", "web"), mentions=("apps/core",)), *CONFIG.scopes[1:]),
    )
    index = _reach(test_unrelated=Reach(frozenset({"tests/core/rig.py"}), frozenset({"weights.json"}), frozenset()))
    changes = (Change("M", "apps/core/weights.json"),)
    got = narrow(plan(changes, config), changes, config, MODULES, index, frozenset())
    assert "tests/web/test_unrelated.py" in got.test_paths
    assert "tests/web/test_uses_core.py" not in got.test_paths


def test_the_reason_states_the_narrowing() -> None:
    index = _reach(test_uses_core=Reach(frozenset({"apps/core/model.py"}), frozenset(), frozenset()))
    got = _narrowed(("apps/core/model.py",), index)
    assert got.reason.endswith("dependents narrowed to 1 of 4 test modules"), got.reason


# ----- the reach index: reads the tree -----


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)


def test_reach_follows_imports_transitively_and_the_conftest_chain(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "apps/core/model.py": "import os\n",
            "apps/web/views.py": "from apps.core import model\n",
            "apps/raw/thing.py": "import os\n",
            "tests/web/conftest.py": "import apps.raw.thing\n",
            "tests/web/test_a.py": "from apps.web import views\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None
    assert {"apps/core/model.py", "apps/web/views.py", "apps/raw/thing.py"} <= reach.files


def test_reach_follows_the_package_initializers_pytest_runs_first(tmp_path: Path) -> None:
    """pytest imports `tests/web/__init__.py` before collecting a module beneath it (round 15)."""
    _write(
        tmp_path,
        {
            "apps/raw/thing.py": "import os\n",
            "tests/__init__.py": "",
            "tests/web/__init__.py": "import apps.raw.thing\n",
            "tests/web/test_a.py": "import os\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "apps/raw/thing.py" in reach.files


def test_reach_follows_a_module_a_literal_names(tmp_path: Path) -> None:
    """A test that runs `python -m apps.raw.cli` or opens `apps/raw/job.py` reaches them without an import."""
    _write(
        tmp_path,
        {
            "apps/raw/cli.py": "from apps.raw import helper\n",
            "apps/raw/helper.py": "import os\n",
            "apps/raw/job.py": "import os\n",
            "tests/raw/test_a.py": 'ARGS = ["-m", "apps.raw.cli"]\nJOB = "apps/raw/job.py"\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/raw/test_a.py",))["tests/raw/test_a.py"]
    assert reach is not None
    assert {"apps/raw/cli.py", "apps/raw/helper.py", "apps/raw/job.py"} <= reach.files


def test_reach_follows_the_main_module_of_a_package_a_literal_names(tmp_path: Path) -> None:
    """`python -m apps.raw` runs `apps/raw/__main__.py`, as round 19 reads `run_module` (Codex on #7495)."""
    _write(
        tmp_path,
        {
            "apps/raw/__init__.py": "",
            "apps/raw/__main__.py": "from apps.raw import helper\n",
            "apps/raw/helper.py": "import os\n",
            "tests/raw/test_a.py": 'ARGS = ["-m", "apps.raw"]\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/raw/test_a.py",))["tests/raw/test_a.py"]
    assert reach is not None
    assert {"apps/raw/__main__.py", "apps/raw/helper.py"} <= reach.files


@pytest.mark.parametrize(
    "target",
    [
        'ROOT / "scripts" / "worker.py"',
        'os.path.join(ROOT, "scripts", "worker.py")',
        'Path(ROOT, "scripts", "worker.py")',
        'ROOT.joinpath("scripts", "worker.py")',
        'Path("scripts") / "worker.py"',
        'Path("scripts").joinpath("worker.py")',
        '(ROOT / "scripts").joinpath("worker.py")',
        'ROOT.joinpath("scripts") / "worker.py"',
    ],
)
def test_reach_follows_a_python_file_a_constant_path_join_names(tmp_path: Path, target: str) -> None:
    """`REPO_ROOT / "scripts" / "worker.py"` spells no whole path, yet the test runs that file (Codex on #7495)."""
    _write(
        tmp_path,
        {
            "scripts/worker.py": "from scripts import helper\n",
            "scripts/helper.py": "import os\n",
            "tests/webui/test_a.py": f"import os\nfrom pathlib import Path\nROOT = Path(__file__).parents[2]\nWORKER = {target}\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/webui/test_a.py",))["tests/webui/test_a.py"]
    assert reach is not None
    assert {"scripts/worker.py", "scripts/helper.py"} <= reach.files
    assert "scripts/worker.py" in reach.literals, "a joined path is matched against mention needles too"


def test_a_path_join_with_a_runtime_segment_names_nothing(tmp_path: Path) -> None:
    """Only the trailing run of constants is joined: `ROOT / "scripts" / name / "worker.py"` is unresolved."""
    _write(
        tmp_path,
        {
            "scripts/worker.py": "import os\n",
            "tests/webui/test_a.py": 'from pathlib import Path\nROOT = Path(".")\nname = "x"\nW = ROOT / "scripts" / name / "worker.py"\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/webui/test_a.py",))["tests/webui/test_a.py"]
    assert reach is not None and "scripts/worker.py" not in reach.files


def test_reach_resolves_a_relative_import_in_an_ancestor_conftest(tmp_path: Path) -> None:
    """Ancestor paths are POSIX on every OS, so `from .real import x` in `tests/a/conftest.py` resolves
    inside `tests.a` (Codex on #7495: Windows backslashes broke the relative resolution)."""
    _write(
        tmp_path,
        {
            "tests/__init__.py": "",
            "tests/a/__init__.py": "",
            "tests/a/conftest.py": "from .real import x\n",
            "tests/a/real.py": "x = 1\n",
            "tests/a/b/test_z.py": "import os\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/a/b/test_z.py",))["tests/a/b/test_z.py"]
    assert reach is not None and "tests/a/real.py" in reach.files
    assert all("\\" not in f for f in reach.files)


@pytest.mark.parametrize(
    "program",
    [
        '("from apps.web import app as m\\n" "m.build()\\n")',
        'textwrap.dedent("""\\n    import apps.web.app\\n    apps.web.app.build()\\n""")',
    ],
)
def test_reach_follows_the_imports_of_a_program_held_as_a_string(tmp_path: Path, program: str) -> None:
    """`[sys.executable, "-c", BOOT]` runs the program the literal holds: its imports are the module's
    reach (Codex on #7495: `tests/cloudsync/test_dev_cloudsync_guard.py` boots the web app this way)."""
    _write(
        tmp_path,
        {
            "apps/web/app.py": "from apps.web import views\n",
            "apps/web/views.py": "import os\n",
            "tests/cloudsync/test_a.py": (
                "import subprocess, sys, textwrap\n"
                f"BOOT = {program}\n"
                "def test_boot():\n"
                '    subprocess.run([sys.executable, "-c", BOOT], check=True)\n'
            ),
        },
    )
    reach = read_reach_index(tmp_path, ("tests/cloudsync/test_a.py",))["tests/cloudsync/test_a.py"]
    assert reach is not None
    assert {"apps/web/app.py", "apps/web/views.py"} <= reach.files


def test_text_that_mentions_import_but_is_no_program_is_ignored(tmp_path: Path) -> None:
    """An error message is not a program: it neither adds reach nor makes the module unreadable."""
    _write(
        tmp_path,
        {
            "apps/web/views.py": "import os\n",
            "tests/web/test_a.py": 'MESSAGE = "cannot import name x from apps.web.views"\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.files == frozenset({"tests/web/test_a.py"})


@pytest.mark.parametrize("spelled", ["./scripts/worker.py", "tests/../scripts/worker.py"])
def test_a_literal_path_is_normalized_before_it_names_a_root(tmp_path: Path, spelled: str) -> None:
    """`./scripts/worker.py` and `tests/../scripts/worker.py` run `scripts/worker.py` (Sol on #7495)."""
    _write(tmp_path, {"scripts/worker.py": "import os\n", "tests/web/test_a.py": f'W = "{spelled}"\n'})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "scripts/worker.py" in reach.files


@pytest.mark.parametrize(
    "target",
    [
        'Path("/scripts", "worker.py")',
        'os.path.join(ROOT, "/scripts", "worker.py")',
        'ROOT / "/scripts" / "worker.py"',
        'Path("tests", "/scripts", "worker.py")',
    ],
)
def test_an_absolute_segment_in_a_join_leaves_the_repository(tmp_path: Path, target: str) -> None:
    """An absolute segment restarts the path, as `os.path.join` and `Path` do, so it names nothing (Sol on #7495)."""
    _write(
        tmp_path,
        {
            "scripts/worker.py": "import os\n",
            "tests/scripts/worker.py": "import os\n",
            "tests/web/test_a.py": f"import os\nfrom pathlib import Path\nROOT = Path(__file__).parents[2]\nW = {target}\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and not {"scripts/worker.py", "tests/scripts/worker.py"} & reach.files


@pytest.mark.parametrize("spelled", ["tools/worker.py", "(private path)", "./tools/worker.py"])
def test_a_literal_path_outside_the_import_packages_names_a_root(tmp_path: Path, spelled: str) -> None:
    """A test may run any repository Python file by path, not only one under `apps`, `scripts`, `ops`
    or `tests` (Sol on #7495)."""
    _write(
        tmp_path,
        {
            "tools/worker.py": "import os\n",
            "(private path)": "import os\n",
            "tests/web/test_a.py": f'W = "{spelled}"\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and posixpath.normpath(spelled) in reach.files


@pytest.mark.parametrize(
    "spelled", ["../scripts/worker.py", "../tools/worker.py", "{outside}/tools/worker.py", "/scripts/worker.py"]
)
def test_a_literal_path_outside_the_repository_names_nothing(tmp_path: Path, spelled: str) -> None:
    """The escaping targets exist, beside the repository, so only the path check can reject them."""
    _write(tmp_path, {"scripts/worker.py": "import os\n", "tools/worker.py": "import os\n"})
    repo = tmp_path / "repo"
    literal = spelled.format(outside=tmp_path.as_posix())
    _write(
        repo,
        {
            "scripts/worker.py": "import os\n",
            "tools/worker.py": "import os\n",
            "tests/web/test_a.py": f'W = "{literal}"\n',
        },
    )
    reach = read_reach_index(repo, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.files == frozenset({"tests/web/test_a.py"})


DECLARED = 'def _contains_rb_parity_marker(path):\n    return list(path.rglob("test_*.py"))\n'


@pytest.mark.parametrize(
    ("conftest", "walked"),
    [
        (DECLARED, set()),
        (DECLARED + 'def scan(path):\n    return list(path.rglob("*.py"))\n', {"."}),
        (DECLARED + 'FILES = list(Path(__file__).parent.rglob("*.py"))\n', {"tests"}),
        ('def owned(path):\n    return list(path.rglob("test_*.py"))\n', {"."}),
    ],
)
def test_a_declared_walk_exempts_only_its_own_call(tmp_path: Path, conftest: str, walked: set[str]) -> None:
    """`tests/conftest.py`'s collection hook walks a parameter; every other walk in the file still
    counts, so a scanner added beside it is not exempted with it (Sol on #7495)."""
    _write(tmp_path, {"tests/conftest.py": "from pathlib import Path\n" + conftest, "tests/web/test_a.py": ""})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "tests/conftest.py" in reach.files and reach.walked == frozenset(walked)


def test_a_declared_function_name_exempts_nothing_in_another_file(tmp_path: Path) -> None:
    """The declaration is a file and a function: the same name elsewhere still walks the root."""
    _write(tmp_path, {"tests/conftest.py": "", "tests/web/test_a.py": "from pathlib import Path\n" + DECLARED})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.walked == frozenset({"."})


@pytest.mark.parametrize(
    ("conftest", "targets"),
    [
        (DECLARED + 'FILES = list(Path(__file__).parent.rglob("*.py"))\n', [None]),
        ('def _contains_rb_parity_marker(path):\n    return list(Path(__file__).parent.rglob("*.py"))\n', ["tests"]),
        (DECLARED + 'def _contains_rb_parity_marker(path):\n    return list(path.glob("*.py"))\n', [None, None]),
    ],
)
def test_the_declared_walk_guard_reads_only_the_named_function(conftest: str, targets: list[str | None]) -> None:
    """The guard sees the declared function's walks alone, resolved: a walk elsewhere in the file
    is not its business, and a resolvable or second walk inside it fails the real-tree guard."""
    text = "from pathlib import Path\n" + conftest
    assert declared_walk_targets(text, "tests/conftest.py", "_contains_rb_parity_marker") == targets


@pytest.mark.parametrize(("declared", "function"), sorted(DECLARED_WALKS.items()))
def test_a_declared_walk_is_one_unresolved_call_on_the_real_tree(declared: str, function: str) -> None:
    """The declaration names one call: the function must hold exactly one walk, and the resolver
    must be unable to place it. A second walk there, or a resolvable one, needs the entry reviewed."""
    text = (REPO / declared).read_text(encoding="utf-8")
    assert declared_walk_targets(text, declared, function) == [None]


@pytest.mark.parametrize(
    ("scan", "walked"),
    [
        ('ROOTS = (REPO / "apps",)\nFILES = [p for r in ROOTS for p in r.rglob("*.py")]\n', {"."}),
        ('FILES = list((REPO / "apps/core").glob("*.py"))\n', {"apps/core"}),
        ('FILES = glob.glob("apps/**/*.py", recursive=True)\n', {"apps"}),
        ('FILES = [p for p in REPO.rglob("*.py")]\n', {"."}),
        ('FILES = [f for f in os.listdir("./apps/") if f.endswith(".py")]\n', {"apps"}),
        ('FILES = list((REPO / "apps/core").rglob("*.json"))\n', set()),
        ('OUT = subprocess.run(["git", "ls-files", "apps"], capture_output=True)\n', {"."}),
        (
            'OUT = subprocess.run(["git", "ls-files"], capture_output=True, text=True)\nPY = [n for n in OUT.stdout.split() if n.endswith(".py")]\n',
            {"."},
        ),
        ('OUT = subprocess.check_output("git ls-files -z".split(), text=True)\n', {"."}),
        ('ROOT = REPO / "apps"\nTREES = [ast.walk(ast.parse(""))]\n', set()),
        ('ROOT = REPO / "apps"\n', set()),
        ('FILES = list(REPO.rglob("*.json"))\n', set()),
        ('FILES = list(Path(DRIVE).rglob("*"))\n', set()),
        ('FILES = list(REPO.rglob("**/test_*.py"))\n', {"."}),
        ('DOCS = REPO / "apps"\nFILES = list(REPO.rglob("*.py"))\n', {"."}),
        ('FILES = [p for p, _, _ in os.walk(REPO) if p.endswith(".py")]\nDOCS = "apps"\n', {"."}),
        ('def scan(path):\n    return list(path.rglob("test_*.py"))\nVENV = REPO / "apps"\n', {"."}),
        ('DOCS = "apps/core"\ndef scan(path):\n    return list(path.rglob("*.py"))\nFILES = scan(REPO)\n', {"."}),
        ('FIX = Path(__file__).parent / "fixtures"\nFILES = list(FIX.rglob("*.py"))\n', {"tests/web/fixtures"}),
        ('ROOT = REPO / "apps"\ndef scan(ROOT):\n    return list(ROOT.rglob("*.py"))\n', {"."}),
        ('FILES = [p for p in Path("apps").rglob("*.py")]\n', {"apps"}),
        ('APPS = Path(__file__).parents[2] / "apps"\nFILES = list(APPS.rglob("*.py"))\n', {"apps"}),
        (
            'TOP: Path = Path(__file__).parents[2]\nDOCS = REPO / "apps"\nFILES = list(TOP.rglob("*.py"))\n',
            {"."},
        ),
        (
            'HERE = Path(__file__).parent\nTOP = HERE.parent.parent\nDOCS = "apps"\nFILES = list(TOP.rglob("*.py"))\n',
            {"."},
        ),
        ('DOCS = "apps"\nif (TOP := Path(__file__).parents[2]):\n    FILES = list(TOP.rglob("*.py"))\n', {"."}),
        (
            'from os import walk as scan\nDOCS = "apps"\nFILES = [p for p, _, _ in scan(REPO) if p.endswith(".py")]\n',
            {"."},
        ),
        ('scan = os.walk\nDOCS = "apps"\nFILES = [p for p, _, _ in scan(REPO) if p.endswith(".py")]\n', {"."}),
        ('from glob import glob as find\nFILES = find("apps/**/*.py", recursive=True)\n', {"apps"}),
        ('FILES = list(Path().rglob("*.py"))\n', {"."}),  # `Path()` is `Path(".")` (Sol on #7495)
        ('FILES = [p for p, _, _ in os.walk(Path()) if p.endswith(".py")]\n', {"."}),
        ('FILES = list((Path() / "apps").rglob("*.py"))\n', {"apps"}),
        ('FILES = list((Path(".") / "apps").rglob("*.py"))\n', {"apps"}),
    ],
)
def test_reach_records_the_directories_a_reached_file_walks(tmp_path: Path, scan: str, walked: set[str]) -> None:
    """A directory counts only where the same file walks one, and the root only for a Python-file
    pattern that names no directory: most files spell "tests" or "scripts" for other reasons."""
    _write(
        tmp_path,
        {
            "apps/core/model.py": "",
            "tests/web/helper.py": f"import ast, glob, os, subprocess\nfrom pathlib import Path\nREPO = Path(__file__).parents[2]\n{scan}",
            "tests/web/test_a.py": "from tests.web import helper\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.walked == frozenset(walked)


_SCANS_PY = 'REPO = Path(__file__).parents[2]\nFILES = [p for p, _, _ in {call}(REPO) if p.endswith(".py")]\n'


@pytest.mark.parametrize(
    ("helpers", "test_imports", "call", "walked"),
    [
        ({"tests/web/helper.py": "from os import walk as scan\n"}, "from tests.web.helper import scan", "scan", {"."}),
        ({"tests/web/helper.py": "import os\nscan = os.walk\n"}, "from tests.web.helper import scan", "scan", {"."}),
        (
            {
                "tests/web/base.py": "from os import walk as scan\n",
                "tests/web/helper.py": "from tests.web.base import scan\n",
            },
            "from tests.web.helper import scan",
            "scan",
            {"."},
        ),
        ({"tests/web/helper.py": "from os import walk as scan\n"}, "from .helper import scan as find", "find", {"."}),
        (
            {"tests/web/helper.py": "from os import walk as scan\n"},
            "from tests.web import helper",
            "helper.scan",
            {"."},
        ),
        ({"tests/web/helper.py": "from os import walk as scan\n"}, "import tests.web.helper as h", "h.scan", {"."}),
        (
            {"tests/web/a.py": "from tests.web.b import scan\n", "tests/web/b.py": "from tests.web.a import scan\n"},
            "from tests.web.a import scan",
            "scan",
            {"."},
        ),
        (
            {"tests/web/helper.py": "def scan(path):\n    return []\n"},
            "from tests.web.helper import scan",
            "scan",
            set(),
        ),
        ({"tests/web/helper.py": "from os import walk as scan\n"}, "from tests.web.helper import *", "scan", {"."}),
        ({"tests/web/helper.py": "from os import walk as scan\n"}, "from .helper import *", "scan", {"."}),
        (
            {
                "tests/web/base.py": "from os import walk as scan\n",
                "tests/web/helper.py": "from tests.web.base import *\n",
            },
            "from tests.web.helper import *",
            "scan",
            {"."},
        ),
        (
            {"tests/web/a.py": "from tests.web.b import *\n", "tests/web/b.py": "from tests.web.a import *\n"},
            "from tests.web.a import *",
            "scan",
            {"."},
        ),
        ({"tests/web/helper.py": "def scan(path):\n    return []\n"}, "from tests.web.helper import *", "scan", set()),
    ],
    ids=[
        "imported-alias",
        "imported-rebinding",
        "re-export-chain",
        "relative-import-renamed",
        "module-attribute",
        "module-alias-attribute",
        "import-cycle",
        "imported-non-walker",
        "star-import",
        "relative-star-import",
        "star-re-export-chain",
        "star-import-cycle",
        "star-imported-non-walker",
    ],
)
def test_a_walker_alias_imported_from_a_repository_module_walks(
    tmp_path: Path, helpers: dict[str, str], test_imports: str, call: str, walked: set[str]
) -> None:
    """Sol on #7495 (twice). A helper exporting `scan` as an alias of `os.walk` holds no call, and the
    test calling `scan(REPO)` bound no walker, so neither file recorded a walk and the scanner could
    be dropped. A name imported from a repository module is a walker where that module binds it as
    one, through re-exports; a name caught in an import cycle counts as one. A star import takes
    every walker the module binds (Sol on #7495), and one caught in a cycle walks the root."""
    files = {
        "apps/core/model.py": "",
        "tests/web/__init__.py": "",
        "tests/web/test_a.py": f"from pathlib import Path\n{test_imports}\n" + _SCANS_PY.format(call=call),
        **helpers,
    }
    _write(tmp_path, files)
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.walked == frozenset(walked)


@pytest.mark.parametrize(
    ("spelled", "target"),
    [("tools.worker", "tools/worker.py"), ("worker", "worker.py"), ("tools.pkg", "tools/pkg/__main__.py")],
)
def test_reach_follows_a_module_name_outside_the_import_packages(tmp_path: Path, spelled: str, target: str) -> None:
    """`python -m tools.worker` runs `tools/worker.py`; a module name is bounded by the files that
    exist, not by four package roots (Sol on #7495)."""
    _write(
        tmp_path,
        {
            "tools/worker.py": "import os\n",
            "worker.py": "import os\n",
            "tools/pkg/__init__.py": "",
            "tools/pkg/__main__.py": "import os\n",
            "tests/web/test_a.py": f'CMD = ["python", "-m", "{spelled}"]\n',
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and target in reach.files


LONG_DIRS = "/".join(("tools", "a" * 70, "b" * 70, "c" * 70))  # a 222-character path, each part a short name


@pytest.mark.parametrize(
    ("spelled", "target"),
    [
        ('["python", "-m", "tools.beat-grid"]', "tools/beat-grid.py"),
        ('["python", "-m", "tools.2fast"]', "tools/2fast.py"),
        ('["python", "-m", "tools.beat grid"]', "tools/beat grid.py"),
        ('"tools/beat grid.py"', "tools/beat grid.py"),
        ('"./tools/beat grid.py"', "tools/beat grid.py"),
        (f'"{LONG_DIRS}/worker.py"', f"{LONG_DIRS}/worker.py"),
    ],
    ids=["hyphen-module", "digit-module", "space-module", "space-path", "space-path-normalized", "long-path"],
)
def test_a_literal_root_that_is_no_identifier_or_short_path_still_names_its_file(
    tmp_path: Path, spelled: str, target: str
) -> None:
    """Sol on #7495. `python -m tools.beat-grid` runs `tools/beat-grid.py`, and carry already reads
    such a name; a `.py` path may hold spaces or run past 200 characters. Each literal is normalized
    and held inside the checkout, then bounded by the files that exist, not by its spelling."""
    _write(
        tmp_path,
        {
            "tools/__init__.py": "",
            target: "import os\n",
            "tests/web/test_a.py": f"CMD = {spelled}\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and target in reach.files


@pytest.mark.parametrize(
    "spelled",
    [
        '"tools/missing grid.py"',  # no such file
        '["python", "-m", "tools.missing-grid"]',  # no such module
        '"../tools/beat grid.py"',  # beside the checkout, where the file exists
        f'"tools/{"x" * 300}.py"',  # a part too long for any file name: no file, and no error
        f'["python", "-m", "tools.{"x" * 300}"]',
        '"tools/a\\\\b.py"',  # a backslash part: `tools/a/b.py` on Windows, so neither host reaches it
    ],
    ids=["missing-path", "missing-module", "outside", "overlong-path", "overlong-module", "backslash-part"],
)
def test_a_literal_root_naming_no_file_in_the_checkout_reaches_nothing(tmp_path: Path, spelled: str) -> None:
    """Controls for the test above: existence and the checkout still bound every literal."""
    _write(tmp_path, {"tools/beat grid.py": "import os\n"})
    repo = tmp_path / "repo"
    _write(
        repo,
        {
            "tools/a/b.py": "import os\n",  # no `tools/__init__.py`, which `-m tools.x` would import on the way
            "tests/web/test_a.py": f"CMD = {spelled}\n",
        },
    )
    reach = read_reach_index(repo, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.files == frozenset({"tests/web/test_a.py"})


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="writes files named `C:probe.py` and `a\\b.py`: on Windows a data stream and a separator",
)
@pytest.mark.parametrize(
    ("spelled", "file"),
    [
        ('"tools/a\\\\b.py"', "tools/a\\b.py"),
        ('"tools/C:probe.py"', "tools/C:probe.py"),
        ('["python", "-m", "tools.a\\\\b"]', "tools/a\\b/__main__.py"),
        ('["python", "-m", "tools.C:probe"]', "tools/C:probe/__main__.py"),
    ],
    ids=["backslash-path", "drive-path", "backslash-module", "drive-module"],
)
def test_a_literal_root_with_a_windows_separator_or_drive_part_reaches_no_file(
    tmp_path: Path, spelled: str, file: str
) -> None:
    """Sol on #7495. On POSIX the file exists under that odd name, so a reach to it would show; on
    Windows the same literal names another file or a data stream, so each part must be one plain
    component under both hosts' rules and plans do not depend on the host."""
    _write(tmp_path, {file: "import os\n", "tests/web/test_a.py": f"CMD = {spelled}\n"})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and reach.files == frozenset({"tests/web/test_a.py"})


def test_reach_collects_the_literals_of_every_module_reached(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "tests/support/paths.py": 'GUIDE = "docs/guide.md"\n',
            "tests/web/test_a.py": "from tests.support import paths\n",
        },
    )
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "docs/guide.md" in reach.literals


def test_an_unparsable_module_in_the_reach_makes_it_unreadable(tmp_path: Path) -> None:
    _write(tmp_path, {"apps/web/broken.py": "def (:\n", "tests/web/test_a.py": "from apps.web import broken\n"})
    assert read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"] is None


RAW = {"apps/raw/__init__.py": "", "apps/raw/worker.py": "import os\n", "apps/web/__init__.py": ""}


@pytest.mark.parametrize(
    "load",
    [
        'importlib.import_module(".worker", "apps.raw")',
        'importlib.import_module(".worker", package="apps.raw")',
        'importlib.import_module("..raw.worker", "apps.web")',
        'importlib.import_module("apps.raw.worker", "tools")',
        '__import__("apps.raw", fromlist=["worker"])',  # each entry may be a submodule
    ],
)
def test_a_relative_load_reaches_what_its_literal_package_resolves_to(tmp_path: Path, load: str) -> None:
    """`import_module(".worker", "apps.raw")` runs `apps/raw/worker.py`, which no literal spells
    whole (Sol on #7495)."""
    _write(tmp_path, {**RAW, "tests/web/test_a.py": f"import importlib\nMOD = {load}\n"})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "apps/raw/worker.py" in reach.files


@pytest.mark.parametrize(
    "load",
    [
        'importlib.import_module(".worker", PACKAGE)',
        'importlib.import_module(".worker")',
        'importlib.import_module("...worker", "apps.raw")',  # beyond the top-level package
        'importlib.import_module(NAME, "apps.raw")',
        '__import__("worker", globals(), None, [], 1)',  # level 1: relative to the caller
        '__import__("apps.raw", fromlist=["*"])',  # the submodules `__all__` lists
    ],
)
def test_a_load_whose_target_is_not_exact_keeps_the_module(tmp_path: Path, load: str) -> None:
    _write(tmp_path, {**RAW, "tests/web/test_a.py": f"import importlib\nMOD = {load}\n"})
    assert read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"] is None


@pytest.mark.parametrize(
    ("source", "kept"),
    [
        ('from importlib import import_module as load\nMOD = load(".worker", "apps.raw")\n', True),
        ('import importlib\nload = importlib.import_module\nMOD = load(".worker", "apps.raw")\n', True),
        ("import importlib\nMODS = list(map(importlib.import_module, NAMES))\n", True),
        ("import builtins\nload = builtins.__import__\n", True),
        ('from importlib import import_module\nMOD = import_module(".worker", "apps.raw")\n', False),
        ('import importlib as il\nMOD = il.import_module(".worker", "apps.raw")\n', False),
    ],
)
def test_a_loader_called_by_another_name_keeps_the_module(tmp_path: Path, source: str, kept: bool) -> None:
    """An alias or a reference the reader cannot follow to its call may load anything, so the
    module is kept; a call by the loader's own name, through any module alias, is resolved
    (Sol on #7495)."""
    _write(tmp_path, {**RAW, "tests/web/test_a.py": source})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert (reach is None) if kept else (reach is not None and "apps/raw/worker.py" in reach.files)


def test_a_lone_computed_load_is_the_stated_floor(tmp_path: Path) -> None:
    """`import_module(name)` with one computed argument names nothing to follow: the ADR's floor,
    so the reach stands without it."""
    loader = "import importlib\ndef load(name):\n    return importlib.import_module(name)\n"
    _write(tmp_path, {**RAW, "tests/web/test_a.py": loader})
    reach = read_reach_index(tmp_path, ("tests/web/test_a.py",))["tests/web/test_a.py"]
    assert reach is not None and "apps/raw/worker.py" not in reach.files
