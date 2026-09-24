"""scripts/lock_metadata_check.py: a pyproject.toml field of the wrong TOML type.

uv refuses the file ("TOML parse error ... invalid type: integer, expected a string",
`uv lock --check` exit 2; measured uv 0.8.17, Codex P2 on #3763, round 25), so the
checker must never read one as clean by coercing it. One class, several sites: the
three Codex named (an `include-group` integer, a source `path` integer, `dependencies`
as an inline table) and the rest of the same reads.

- [if] a field uv requires as a string, or a list of strings, holds any other TOML type
  [then] exit 2 UNKNOWN naming the field, never a verdict, [else stop]
- [if] the same field holds the required type [then] the verdict is unchanged, [else stop]
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import LOCK, PYPROJECT, _run
from tests.scripts.test_lock_metadata_groups import GROUPS_LOCK, GROUPS_PYPROJECT

# The `lint` group renamed `1`, so an integer include is a plausible mis-edit of a
# clean pair rather than a reference to nothing.
ONE_PYPROJECT = GROUPS_PYPROJECT.replace("\nlint = [", '\n"1" = [').replace(
    '{include-group = "lint"}', '{include-group = "1"}'
)
ONE_LOCK = GROUPS_LOCK.replace("\nlint = [", '\n"1" = [')


def test_an_include_group_that_is_not_a_string_is_unknown(tmp_path: Path) -> None:
    assert ONE_PYPROJECT != GROUPS_PYPROJECT and ONE_LOCK != GROUPS_LOCK
    code, message = _run(tmp_path, ONE_PYPROJECT, ONE_LOCK)
    assert code == EXIT_OK, message
    edited = ONE_PYPROJECT.replace('{include-group = "1"}', "{include-group = 1}")
    assert edited != ONE_PYPROJECT
    code, message = _run(tmp_path, edited, ONE_LOCK)
    assert code == EXIT_UNKNOWN
    assert "[dependency-groups] dev include-group = 1 is not a string" in message


def test_a_source_path_that_is_not_a_string_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "pyproject.toml").write_text(
        '[project]\nname = "localdep"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    pyproject = PYPROJECT.replace('"numpy>=1.26"', '"localdep"')
    lock = LOCK.replace(
        '{ name = "numpy", specifier = ">=1.26" }', '{ name = "localdep", directory = "1" }'
    )
    assert pyproject != PYPROJECT and lock != LOCK
    sources = "\n[tool.uv.sources]\nlocaldep = { path = %s }\n"
    code, message = _run(tmp_path, pyproject + sources % '"1"', lock)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, pyproject + sources % "1", lock)
    assert code == EXIT_UNKNOWN
    assert "[tool.uv.sources] localdep path = 1 is not a string" in message
    # The lock side too: `directory = 1` is "did not match any variant of untagged
    # enum SourceWire", `uv lock --check` exit 2 (measured uv 0.8.17, round 28),
    # where str() read it as the path "1" the pyproject names.
    broken_lock = lock.replace('directory = "1"', "directory = 1")
    assert broken_lock != lock
    code, message = _run(tmp_path, pyproject + sources % '"1"', broken_lock)
    assert code == EXIT_UNKNOWN
    assert "requires-dist directory = 1 is not a string; uv rejects the file" in message


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (
            lambda p: re.sub(
                r"dependencies = \[.*?\n\]\n",
                'dependencies = { numpy = 1, uvicorn = 2, "pyobjc-framework-Quartz" = 3 }\n',
                p,
                count=1,
                flags=re.S,
            ),
            "[project] dependencies = {",
        ),
        (lambda p: p.replace('"numpy>=1.26",', "1,"), "[project] dependencies = [1,"),
        (
            lambda p: p.replace('all = ["Demo_Project[dev]"]', 'all = "Demo_Project[dev]"'),
            "[project.optional-dependencies] all = 'Demo_Project[dev]' is not a list of strings",
        ),
        (
            lambda p: p.replace('all = ["Demo_Project[dev]"]', "all = [1]"),
            "[project.optional-dependencies] all = [1] is not a list of strings",
        ),
        (
            lambda p: re.sub(r"\[project\.optional-dependencies\].*", "", p, flags=re.S)
            + 'optional-dependencies = ["dev"]\n',
            "[project] optional-dependencies = ['dev'] is not a table",
        ),
    ],
    ids=["deps-table", "dep-integer", "extra-string", "extra-integer", "extras-list"],
)
def test_a_requirement_container_of_the_wrong_type_is_unknown(
    tmp_path: Path, edit, named: str
) -> None:
    edited = edit(PYPROJECT)
    assert edited != PYPROJECT
    code, message = _run(tmp_path, edited, LOCK)
    assert code == EXIT_UNKNOWN, message
    assert named in message and "uv rejects the file" in message


@pytest.mark.parametrize(
    ("pyproject_edit", "lock_edit", "named"),
    [
        (
            None,
            lambda lock: lock.replace('extras = ["standard"]', 'extras = "standard"'),
            "requires-dist extras = 'standard' is not a list of strings",
        ),
        (lambda p: p + '\n[tool]\nuv = "bad"\n', None, "[tool] uv = 'bad' is not a table"),
        (
            lambda p: p + '\n[tool.uv]\nsources = "bad"\n',
            None,
            "[tool.uv] sources = 'bad' is not a table",
        ),
    ],
    ids=["lock-extras-string", "tool-uv-string", "sources-string"],
)
def test_a_uv_table_or_lock_extras_of_the_wrong_type_is_unknown(
    tmp_path: Path, pyproject_edit, lock_edit, named: str
) -> None:
    """Measured uv 0.8.17 (Codex P2 on #3763, round 27): `extras = "standard"` in
    uv.lock is "invalid type: string, expected a sequence", `[tool] uv = "bad"` is
    "expected struct ToolUv" and `[tool.uv] sources = "bad"` is "expected a map with
    unique keys", each `uv lock --check` exit 2. The gate iterated the string's
    letters, read the non-table as no sources, or crashed on `.items()`."""
    pyproject = PYPROJECT if pyproject_edit is None else pyproject_edit(PYPROJECT)
    lock = LOCK if lock_edit is None else lock_edit(LOCK)
    assert (pyproject, lock) != (PYPROJECT, LOCK)
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_UNKNOWN, message
    assert named in message and "uv rejects the file" in message


def test_a_top_level_tool_that_is_not_a_table_is_no_uv_table(tmp_path: Path) -> None:
    """`tool = "bad"` at the top level is ignored by uv (`uv lock` exit 0, measured
    0.8.17): no [tool.uv], so no sources, and a clean pair stays clean rather than
    crashing on `.get`."""
    assert PYPROJECT.lstrip().startswith("[")  # a top-level key precedes the first table
    code, message = _run(tmp_path, 'tool = "bad"\n' + PYPROJECT, LOCK)
    assert code == EXIT_OK, message


def test_a_lock_root_version_that_is_not_a_string_is_unknown(tmp_path: Path) -> None:
    """`version = 0.1` in the uv.lock root is "invalid type: floating point `0.1`,
    expected a string", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on
    #3763, round 29); str() had read it as `0.1`, and a project at `0.1` would then
    compare clean against it (the fixture's `0.1.0` is the STALE control)."""
    root = 'name = "demo-project"\nversion = "0.1.0"\n'
    assert LOCK.count(root) == 1
    lock = LOCK.replace(root, 'name = "demo-project"\nversion = 0.1\n')
    code, message = _run(tmp_path, PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert "uv.lock root version = 0.1 is not a string; uv rejects the file" in message
    assert 'version = "0.1.0"' in PYPROJECT
    code, message = _run(tmp_path, PYPROJECT.replace('version = "0.1.0"', 'version = "0.1"'), lock)
    assert code == EXIT_UNKNOWN, message


# A pair `uv lock` wrote (uv 0.8.17, Thu 24 Sep 2026) for a root with one path
# dependency, one EMPTY extra named "1" and one EMPTY dependency group: the shapes
# whose lock containers, once malformed, still iterate to nothing and so matched an
# empty declaration (Codex P2 x4 on #3763, round 30). Each edit below was fed to
# `uv lock --check --offline` on that pair and rejected with the quoted error.
MINI_PYPROJECT = """[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["localdep"]
[project.optional-dependencies]
"1" = []
[dependency-groups]
foo = []
[tool.uv.sources]
localdep = { path = "dep" }
"""
MINI_DEP_PYPROJECT = '[project]\nname = "localdep"\nversion = "0.1.0"\n'
MINI_LOCK = """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "demo"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "localdep" },
]

[package.metadata]
requires-dist = [{ name = "localdep", directory = "dep" }]
provides-extras = ["1"]

[package.metadata.requires-dev]
foo = []

[[package]]
name = "localdep"
version = "0.1.0"
source = { directory = "dep" }
"""


def _run_mini(
    tmp_path: Path, lock: str = MINI_LOCK, dep: str = MINI_DEP_PYPROJECT
) -> tuple[int, str]:
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(dep, encoding="utf-8")
    return _run(tmp_path, MINI_PYPROJECT, lock)


@pytest.mark.parametrize(
    ("lock_edit", "dep_edit", "named"),
    [
        (
            lambda lock: lock.replace(
                'requires-dist = [{ name = "localdep", directory = "dep" }]', "requires-dist = {}"
            ),
            None,
            "uv.lock requires-dist = {} is not a list",
        ),
        (
            lambda lock: lock.replace("foo = []", "foo = {}"),
            None,
            "uv.lock requires-dev foo = {} is not a list",
        ),
        (
            lambda lock: lock.replace('provides-extras = ["1"]', "provides-extras = [1]"),
            None,
            "uv.lock provides-extras = [1] is not a list of strings",
        ),
        (
            None,
            lambda dep: dep + '[tool]\nuv = "bad"\n',
            "dep/pyproject.toml [tool] uv = 'bad' is not a table",
        ),
    ],
    ids=[
        "requires-dist-table",
        "requires-dev-group-table",
        "provides-extras-integer",
        "target-tool-uv-string",
    ],
)
def test_a_lock_container_or_path_target_of_the_wrong_type_is_unknown(
    tmp_path: Path, lock_edit, dep_edit, named: str
) -> None:
    """uv 0.8.17: `requires-dist = {}` and a group `= {}` are "invalid type: map,
    expected a sequence", `provides-extras = [1]` is "invalid type: integer `1`,
    expected a string", and a path target's `[tool] uv = "bad"` is "expected struct
    ToolUv"; each `uv lock --check` exit 2. The checker had iterated the empty
    mappings to nothing, str()'d the integer to the declared "1", and read the
    target's non-table as no [tool.uv]."""
    code, message = _run_mini(tmp_path)
    assert code == EXIT_OK, message  # the control: the pair as uv wrote it is clean
    lock = MINI_LOCK if lock_edit is None else lock_edit(MINI_LOCK)
    dep = MINI_DEP_PYPROJECT if dep_edit is None else dep_edit(MINI_DEP_PYPROJECT)
    assert (lock, dep) != (MINI_LOCK, MINI_DEP_PYPROJECT)
    code, message = _run_mini(tmp_path, lock, dep)
    assert code == EXIT_UNKNOWN, message
    assert named in message and "uv rejects the file" in message


# A second pair `uv lock` wrote (uv 0.8.17, Thu 24 Sep 2026): one marked dependency
# and one empty extra; six's `sdist` and `wheels` rows (hash URLs over 100 columns)
# are dropped, the checker reads only the root package's metadata. Round 31: a marker
# or a name uv refuses, spelled the SAME way in both files, matched exactly and never
# reached the parser or the name rule.
MARKED_PYPROJECT = """[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["six; os_name == 'x'"]
[project.optional-dependencies]
ok = []
"""
MARKED_LOCK = """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "demo"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "six", marker = "os_name == 'x'" },
]

[package.metadata]
requires-dist = [{ name = "six", marker = "os_name == 'x'" }]
provides-extras = ["ok"]

[[package]]
name = "six"
version = "1.17.0"
source = { registry = "https://pypi.org/simple" }
"""


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (
            lambda text: text.replace("os_name == 'x'", "made_up == 'x'"),
            "unknown marker variable 'made_up'",
        ),
        (
            lambda text: text.replace('name = "demo"', 'name = "bad space"'),
            "'bad space' is not a valid package or extra name",
        ),
        (
            lambda text: text.replace("\nok = []", '\n"bad space" = []').replace(
                'provides-extras = ["ok"]', 'provides-extras = ["bad space"]'
            ),
            "'bad space' is not a valid package or extra name",
        ),
        (
            lambda text: text.replace("\nok = []", '\n"-lead" = []').replace(
                'provides-extras = ["ok"]', 'provides-extras = ["-lead"]'
            ),
            "'-lead' is not a valid package or extra name",
        ),
    ],
    ids=[
        "marker-in-both",
        "project-name-in-both",
        "extra-in-both",
        "extra-leading-dash",
    ],
)
def test_an_invalid_marker_or_name_shared_by_both_files_is_unknown(
    tmp_path: Path, edit, named: str
) -> None:
    """uv 0.8.17 on the pair: `made_up == 'x'` in both files is "Expected a quoted
    string or a valid marker name, found `made_up`"; `name = "bad space"` in both,
    the extra `"bad space"` in both and the extra `"-lead"` in both are "Not a valid
    package or extra name"; each `uv lock --check` exit 2, while the gate's exact
    spelling match had returned 0 (Codex P2 x2 on #3763, round 31)."""
    code, message = _run(tmp_path, MARKED_PYPROJECT, MARKED_LOCK)
    assert code == EXIT_OK, message  # the control: the pair as uv wrote it is clean
    pyproject, lock = edit(MARKED_PYPROJECT), edit(MARKED_LOCK)
    assert (pyproject, lock) != (MARKED_PYPROJECT, MARKED_LOCK)
    assert pyproject != MARKED_PYPROJECT and lock != MARKED_LOCK  # edited on BOTH sides
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_UNKNOWN, message
    assert named in message
