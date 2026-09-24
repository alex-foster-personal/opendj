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
    # Named per record since round 35 typed every package's version, root included.
    assert "[[package]] 'demo-project' version = 0.1 is not a string" in message
    assert "uv rejects the file" in message
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


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (lambda lock: lock.replace("version = 1\n", "version = 999\n", 1), "schema version = 999"),
        (lambda lock: lock.replace("version = 1\n", "version = 2\n", 1), "schema version = 2"),
        (lambda lock: lock.replace("version = 1\n", "", 1), "schema version = None"),
        (
            lambda lock: lock.replace("version = 1\n", "version = true\n", 1),
            "schema version = True",
        ),
        (
            lambda lock: lock.replace("version = 1\n", "version = false\n", 1),
            "schema version = False",
        ),
    ],
    ids=["v999", "v2", "missing", "true", "false"],
)
def test_a_lock_schema_version_other_than_1_is_unknown(tmp_path: Path, edit, named: str) -> None:
    """`version = 999` and `version = 2` are "uses an unsupported schema version",
    a missing header is "missing field `version`"; each `uv lock --check` exit 2
    (measured uv 0.8.17, Codex P2 on #3763, round 32) while the gate never read the
    header. `version = true` and `false` are "invalid type: boolean, expected u32"
    (round 35): `True == 1` in Python, so `!= 1` alone had read `true` as the schema.
    `revision` is the control: uv accepted 1, 999 and no revision at all."""
    assert MARKED_LOCK.startswith("version = 1\nrevision = 3\n")
    lock = edit(MARKED_LOCK)
    assert lock != MARKED_LOCK
    code, message = _run(tmp_path, MARKED_PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert named in message and "only 1 is readable by uv" in message
    for revision in ("revision = 1\n", "revision = 999\n", ""):
        lock = MARKED_LOCK.replace("revision = 3\n", revision, 1)
        code, message = _run(tmp_path, MARKED_PYPROJECT, lock)
        assert code == EXIT_OK, message


@pytest.mark.parametrize("text", ["six;", "six; ", "six ;"])
def test_an_empty_marker_after_the_semicolon_is_unknown(tmp_path: Path, text: str) -> None:
    """`dep;` is "`project.dependencies[0]` must be pep508", `uv lock --check` exit 2
    (measured uv 0.8.17, Codex P2 on #3763, round 32); the optional marker group read
    it as the unmarked `dep` the lock records, so the pair passed. The unmarked pair
    itself is the control."""
    unmarked_pyproject = MARKED_PYPROJECT.replace("\"six; os_name == 'x'\"", '"six"')
    unmarked_lock = MARKED_LOCK.replace(", marker = \"os_name == 'x'\"", "")
    assert unmarked_pyproject != MARKED_PYPROJECT and unmarked_lock.count("marker") == 0
    code, message = _run(tmp_path, unmarked_pyproject, unmarked_lock)
    assert code == EXIT_OK, message
    edited = unmarked_pyproject.replace('"six"', f'"{text}"')
    assert edited != unmarked_pyproject
    code, message = _run(tmp_path, edited, unmarked_lock)
    assert code == EXIT_UNKNOWN, message
    assert f"unparseable requirement: {text!r}" in message


UNMARKED_PYPROJECT = MARKED_PYPROJECT.replace("\"six; os_name == 'x'\"", '"six"')
UNMARKED_LOCK = MARKED_LOCK.replace(", marker = \"os_name == 'x'\"", "")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("six[]", EXIT_OK),
        ("six[ ]", EXIT_OK),
        ("six[,]", EXIT_UNKNOWN),
        ("six[a,]", EXIT_UNKNOWN),
        ("six[,a]", EXIT_UNKNOWN),
        ("six[a,,b]", EXIT_UNKNOWN),
    ],
)
def test_an_empty_extra_slot_is_unknown_and_empty_brackets_are_not(
    tmp_path: Path, text: str, code: int
) -> None:
    """Measured uv 0.8.17 (Codex P2 on #3763, round 33): `six[]` and `six[ ]` lock
    clean as `six` (exit 0), while `six[,]`, `six[a,]`, `six[,a]` and `six[a,,b]` are
    "`project.dependencies[0]` must be pep508" (exit 2). Filtering empty slots had
    read all six as `six`."""
    assert UNMARKED_PYPROJECT != MARKED_PYPROJECT and "marker" not in UNMARKED_LOCK
    edited = UNMARKED_PYPROJECT.replace('"six"', f'"{text}"')
    assert edited != UNMARKED_PYPROJECT
    got, message = _run(tmp_path, edited, UNMARKED_LOCK)
    assert got == code, message
    if code == EXIT_UNKNOWN:
        assert f"unparseable requirement (empty extra): {text!r}" in message


@pytest.mark.parametrize(
    ("appended", "named"),
    [
        (
            '\n[[package]]\nversion = "9.9"\nsource = { registry = "https://pypi.org/simple" }\n',
            "[[package]] entry without 'name'",
        ),
        (
            '\n[[package]]\nname = "ghost"\nversion = "9.9"\n',
            "[[package]] entry without 'source'",
        ),
        (
            '\n[[package]]\nname = 1\nsource = { registry = "https://pypi.org/simple" }\n',
            "name = 1 is not a string",
        ),
    ],
    ids=["no-name", "no-source", "name-integer"],
)
def test_a_malformed_lock_package_beside_the_root_is_unknown(
    tmp_path: Path, appended: str, named: str
) -> None:
    """A `[[package]]` without `name` or `source` is "missing field", `uv lock --check`
    exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 33); the root filter had
    dropped it and reported the pair clean. A record without `version` is the control:
    uv reads it (exit 0), as a path source records none."""
    lock = UNMARKED_LOCK + appended
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert got == EXIT_UNKNOWN, message
    assert named in message
    control = UNMARKED_LOCK + (
        '\n[[package]]\nname = "ghost"\nsource = { registry = "https://pypi.org/simple" }\n'
    )
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, control)
    assert got == EXIT_OK, message


@pytest.mark.parametrize("spec", ["localdep=>1", "localdep ==x"])
def test_a_malformed_specifier_on_a_path_source_is_unknown(tmp_path: Path, spec: str) -> None:
    """`localdep=>1` and `localdep ==x` on a path-sourced dependency are refused by uv
    (`uv lock --check` exit 2, measured 0.8.17, Codex P2 on #3763, round 36), while
    the gate blanked the specifier for the path source before validating it and read
    the pair clean. `localdep>=1` is the control: uv reads it (exit 0), the path
    decides the version, and the pair stays clean (a trailing `,` is read too)."""
    assert MINI_PYPROJECT.count('dependencies = ["localdep"]') == 1
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    bad = MINI_PYPROJECT.replace('dependencies = ["localdep"]', f'dependencies = ["{spec}"]', 1)
    code, message = _run(tmp_path, bad, MINI_LOCK)
    assert code == EXIT_UNKNOWN, message
    assert "UNKNOWN" in message
    good = MINI_PYPROJECT.replace(
        'dependencies = ["localdep"]', 'dependencies = ["localdep>=1"]', 1
    )
    code, message = _run(tmp_path, good, MINI_LOCK)
    assert code == EXIT_OK, message


ROOT_DEPS = 'dependencies = [\n    { name = "localdep" },\n]'


@pytest.mark.parametrize(
    "deps",
    [
        "dependencies = {}",
        "dependencies = [1]",
        "dependencies = [{}]",
        "dependencies = [{ name = 1 }]",
        'dependencies = [{ name = "localdep", extra = 1 }]',
        'dependencies = [{ name = "localdep", marker = 1 }]',
        'dependencies = [{ name = "localdep", marker = "bad" }]',
    ],
    ids=[
        "table",
        "integer-item",
        "nameless",
        "name-integer",
        "extra-int",
        "marker-int",
        "marker-bad",
    ],
)
def test_a_lock_dependency_record_that_uv_cannot_read_is_unknown(tmp_path: Path, deps: str) -> None:
    """Each shape is "Failed to parse `uv.lock`", `uv lock --check` exit 2 (measured uv
    0.8.17, Codex P2 on #3763, round 37), while the record loop typed only name, source
    and version and reported the pair clean. Controls: an unknown key beside a valid
    record and an extra nothing provides are read by uv (exit 0), and stay EXIT_OK."""
    assert MINI_LOCK.count(ROOT_DEPS) == 1
    code, message = _run_mini(tmp_path, MINI_LOCK.replace(ROOT_DEPS, deps, 1))
    assert code == EXIT_UNKNOWN, message
    assert "[[package]] 'demo' dependencies" in message
    for ok in (
        'dependencies = [{ name = "localdep", bogus = 1 }]',
        'dependencies = [{ name = "localdep", extra = ["x"] }]',
    ):
        code, message = _run_mini(tmp_path, MINI_LOCK.replace(ROOT_DEPS, ok, 1))
        assert code == EXIT_OK, message


@pytest.mark.parametrize(
    "entry",
    [
        "undeclared = { path = 1 }",
        'undeclared = "bad"',
        "undeclared = {}",
        'undeclared = { bogus = "x" }',
        "undeclared = { git = 1 }",
        'undeclared = { path = "dep", git = "https://example.com/x.git" }',
        "undeclared = []",
        "undeclared = [{ path = 1 }]",
        'undeclared = { path = "dep", editable = 1 }',
        'undeclared = { path = "dep", marker = 1 }',
    ],
    ids=[
        "path-int",
        "string",
        "empty",
        "unknown-kind",
        "git-int",
        "two-kinds",
        "empty-list",
        "list-path-int",
        "editable-int",
        "marker-int",
    ],
)
def test_an_undeclared_source_entry_that_uv_cannot_read_is_unknown(
    tmp_path: Path, entry: str
) -> None:
    """A `[tool.uv.sources]` entry no requirement names is still parsed by uv: each shape
    is "Failed to parse: `pyproject.toml`", `uv lock --check` exit 2 (measured uv
    0.8.17, Codex P2 on #3763, round 37), while the gate read only the entries a
    requirement named. Controls uv reads (exit 0), kept EXIT_OK: `{ workspace = false }`,
    a path that resolves nowhere, a git URL, an index name, and a one-item list."""
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    code, message = _run(tmp_path, MINI_PYPROJECT + entry + "\n", MINI_LOCK)
    assert code == EXIT_UNKNOWN, message
    assert "[tool.uv.sources] undeclared" in message
    for ok in (
        "undeclared = { workspace = false }",
        'undeclared = { path = "nowhere" }',
        'undeclared = { git = "https://example.com/x.git" }',
        'undeclared = { index = "pypi" }',
        'undeclared = [{ path = "dep" }]',
    ):
        code, message = _run(tmp_path, MINI_PYPROJECT + ok + "\n", MINI_LOCK)
        assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("version", "named"),
    [
        ("version = 0.0", "version = 0.0 is not a string"),
        ("version = 1", "version = 1 is not a string"),
    ],
    ids=["float", "integer"],
)
def test_a_non_root_package_version_that_is_not_a_string_is_unknown(
    tmp_path: Path, version: str, named: str
) -> None:
    """`version = 0.0` on the registry package six is "invalid type: floating point
    `0.0`, expected a string", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2
    on #3763, round 35); the record check read only `name` and `source`, and
    `_version_delta` types the ROOT's version alone. The unedited pair is the control
    (EXIT_OK)."""
    assert UNMARKED_LOCK.count('version = "1.17.0"') == 1
    lock = UNMARKED_LOCK.replace('version = "1.17.0"', version, 1)
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert got == EXIT_UNKNOWN, message
    assert f"[[package]] 'six' {named}" in message
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK)
    assert got == EXIT_OK, message


@pytest.mark.parametrize(
    ("pyproject_edit", "lock_edit", "named"),
    [
        (
            lambda text: text.replace('requires-python = ">=3.11"', "requires-python = 0", 1),
            lambda text: text.replace('requires-python = ">=3.11"\n', "", 1),
            "[project] requires-python = 0 is not a string",
        ),
        (
            lambda text: text,
            lambda text: text.replace('requires-python = ">=3.11"', "requires-python = 0", 1),
            "uv.lock requires-python = 0 is not a string",
        ),
        (
            lambda text: text.replace('requires-python = ">=3.11"', "requires-python = 0", 1),
            lambda text: text.replace('requires-python = ">=3.11"', "requires-python = 0", 1),
            "[project] requires-python = 0 is not a string",
        ),
    ],
    ids=["pyproject-0-lock-omits", "lock-0", "both-0"],
)
def test_a_requires_python_that_is_not_a_string_is_unknown(
    tmp_path: Path, pyproject_edit, lock_edit, named: str
) -> None:
    """`requires-python = 0` is "invalid type: integer `0`, expected a string" in
    pyproject.toml and in uv.lock alike, `uv lock --check` exit 2 (measured uv 0.8.17,
    Codex P2 on #3763, round 35). With the lock omitting the field, `0 or ""` had
    compared two empty specifiers and passed; with the lock holding it, the pair read
    as STALE, a verdict on a file uv cannot parse."""
    pyproject = pyproject_edit(UNMARKED_PYPROJECT)
    lock = lock_edit(UNMARKED_LOCK)
    assert (pyproject, lock) != (UNMARKED_PYPROJECT, UNMARKED_LOCK)
    got, message = _run(tmp_path, pyproject, lock)
    assert got == EXIT_UNKNOWN, message
    assert named in message and "uv rejects the file" in message


def test_a_lock_package_container_that_is_not_a_list_is_unknown(tmp_path: Path) -> None:
    """`package = "bad"` in uv.lock is "invalid type: string, expected a sequence",
    exit 2 (measured uv 0.8.17, round 33); `.get("package", [])` had iterated it."""
    head = UNMARKED_LOCK[: UNMARKED_LOCK.index("[[package]]")]
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, head + 'package = "bad"\n')
    assert got == EXIT_UNKNOWN, message
    assert "uv.lock package = 'bad' is not a list" in message


@pytest.mark.parametrize(
    ("source", "code"),
    [
        ('"bad"', EXIT_UNKNOWN),
        ("{}", EXIT_UNKNOWN),
        ("{ registry = 1 }", EXIT_UNKNOWN),
        ('{ bogus = "x" }', EXIT_UNKNOWN),
        ('{ registry = "https://pypi.org/simple", directory = "d" }', EXIT_OK),
    ],
    ids=["string", "empty", "registry-integer", "unknown-key", "extra-key-beside-valid"],
)
def test_a_non_root_package_source_that_uv_cannot_read_is_unknown(
    tmp_path: Path, source: str, code: int
) -> None:
    """Measured uv 0.8.17 (Codex P2 on #3763, round 34) on six's registry source:
    `"bad"`, `{}`, `{ registry = 1 }` and `{ bogus = "x" }` are "did not match any
    variant of untagged enum SourceWire" (exit 2); a stray key beside the registry is
    read (exit 0, the control). The record check had only asked that `source` exist."""
    registry = 'source = { registry = "https://pypi.org/simple" }'
    assert UNMARKED_LOCK.count(registry) == 1
    lock = UNMARKED_LOCK.replace(registry, f"source = {source}")
    got, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert got == code, message
    if code == EXIT_UNKNOWN:
        assert "'six' source = " in message and "is not a source table" in message
