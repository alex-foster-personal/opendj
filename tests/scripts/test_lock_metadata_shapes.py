"""scripts/lock_metadata_check.py: a uv.lock or pyproject.toml SHAPE uv refuses.

Continues tests/scripts/test_lock_metadata_types.py (Codex P2 rounds 33-38 on #3763):
a malformed record beside the root, a specifier or marker uv cannot read, a source
entry or artifact record of a shape uv rejects. Each is `uv lock --check` exit 2
(measured uv 0.8.17), so the checker must be UNKNOWN there, never a verdict, and each
test carries the shape uv DOES read as its control.

- [if] a record, specifier, marker, source entry or artifact holds a shape uv refuses
  [then] exit 2 UNKNOWN naming it, never a verdict, [else stop]
- [if] the same site holds a shape uv reads [then] the verdict is unchanged, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import (
    MINI_DEP_PYPROJECT,
    MINI_LOCK,
    MINI_PYPROJECT,
    UNMARKED_LOCK,
    UNMARKED_PYPROJECT,
    _run_mini,
)


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


@pytest.mark.parametrize("side", ["pyproject", "lock"])
@pytest.mark.parametrize("spec", [">=3.11,,", ">=3.11,", ",>=3.11", ">=3.11, ,<4"])
def test_an_empty_requires_python_clause_is_unknown(tmp_path: Path, side: str, spec: str) -> None:
    """Each spelling is refused by uv on either file (`uv lock --check` exit 2, measured
    0.8.17, Codex P2 on #3763, round 38), while the normalizer dropped the empty clauses
    and read `>=3.11,,` as the recorded `>=3.11`, certifying the malformed side."""
    line = 'requires-python = ">=3.11"'
    assert UNMARKED_PYPROJECT.count(line) == 1 and UNMARKED_LOCK.count(line) == 1
    edited = f'requires-python = "{spec}"'
    pyproject, lock = UNMARKED_PYPROJECT, UNMARKED_LOCK
    if side == "pyproject":
        pyproject = pyproject.replace(line, edited)
    else:
        lock = lock.replace(line, edited)
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_UNKNOWN, message
    assert f"empty specifier clause in {spec!r}" in message


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ("localdep>=1,,", EXIT_UNKNOWN),
        ("localdep,>=1", EXIT_UNKNOWN),
        ("localdep>=1, ,<4", EXIT_UNKNOWN),
        ("localdep>=1,", EXIT_OK),
        ("localdep>=1 ,", EXIT_OK),
    ],
)
def test_an_empty_dependency_specifier_clause_is_unknown_except_one_trailing_comma(
    tmp_path: Path, spec: str, code: int
) -> None:
    """`localdep>=1,,`, `localdep,>=1` and `localdep>=1, ,<4` are "Failed to generate
    package metadata", exit 2, while ONE trailing comma is read (exit 0; measured uv
    0.8.17, round 38). The path source discards the specifier only after it is read."""
    assert MINI_PYPROJECT.count('dependencies = ["localdep"]') == 1
    pyproject = MINI_PYPROJECT.replace('dependencies = ["localdep"]', f'dependencies = ["{spec}"]')
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    got, message = _run(tmp_path, pyproject, MINI_LOCK)
    assert got == code, message
    if code == EXIT_UNKNOWN:
        assert "empty specifier clause" in message


SIX_RECORD = 'requires-dist = [{ name = "six" }]'


@pytest.mark.parametrize(
    "field",
    [
        'specifier = ">=1,"',
        'specifier = ",>=1"',
        'specifier = ">=1, ,<4"',
        "specifier = 1",
        'marker = ""',
        "marker = 1",
        'marker = "bad"',
    ],
    ids=[
        "trailing-comma",
        "leading-comma",
        "inner-empty",
        "specifier-int",
        "marker-blank",
        "marker-int",
        "marker-bad",
    ],
)
def test_a_recorded_requirement_field_that_uv_cannot_read_is_unknown(
    tmp_path: Path, field: str
) -> None:
    """Each is "Failed to parse `uv.lock`", exit 2 on a registry requirement (measured
    uv 0.8.17, Codex P2 on #3763, round 38): the lock's specifier gets no trailing-comma
    allowance, and `marker = ""` had read as no marker. Control: `specifier = ""` is
    read (exit 0) and matches an unpinned requirement."""
    assert UNMARKED_LOCK.count(SIX_RECORD) == 1
    lock = UNMARKED_LOCK.replace(SIX_RECORD, f'requires-dist = [{{ name = "six", {field} }}]')
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert "requires-dist 'six'" in message or "empty specifier clause" in message
    control = UNMARKED_LOCK.replace(
        SIX_RECORD, SIX_RECORD.replace('"six"', '"six", specifier = ""')
    )
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, control)
    assert code == EXIT_OK, message


@pytest.mark.parametrize("field", ['specifier = ">=1,,"', "specifier = 1", 'specifier = ">=1"'])
def test_a_path_sourced_record_specifier_is_not_read(tmp_path: Path, field: str) -> None:
    """uv does not read the `specifier` of a directory-sourced record at all: `>=1,,`
    and `= 1` pass `uv lock --check` (exit 0), and `>=1` beside a pyproject `localdep`
    is not stale (measured uv 0.8.17, round 38). Its `marker = ""` IS read, and refused."""
    record = 'requires-dist = [{ name = "localdep", directory = "dep" }]'
    assert MINI_LOCK.count(record) == 1
    lock = MINI_LOCK.replace(record, record.replace('"dep"', f'"dep", {field}'))
    code, message = _run_mini(tmp_path, lock)
    assert code == EXIT_OK, message
    lock = MINI_LOCK.replace(record, record.replace('"dep"', '"dep", marker = ""'))
    code, message = _run_mini(tmp_path, lock)
    assert code == EXIT_UNKNOWN, message
    assert "requires-dist 'localdep' marker = '' is blank" in message


@pytest.mark.parametrize("marker", ['""', '"bad"'])
def test_a_source_entry_marker_that_uv_cannot_parse_is_unknown(tmp_path: Path, marker: str) -> None:
    """`[tool.uv.sources] unused = { path = "x", marker = "" }` (or `"bad"`), on an entry
    no requirement uses, is "Expected marker value", `uv lock --check` exit 2 (measured
    uv 0.8.17, Codex P2 on #3763, round 38); typing the marker as a string had let both
    through. Control: a marker the grammar accepts is read (exit 0)."""
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    bad = MINI_PYPROJECT + f'unused = {{ path = "x", marker = {marker} }}\n'
    code, message = _run(tmp_path, bad, MINI_LOCK)
    assert code == EXIT_UNKNOWN, message
    assert "[tool.uv.sources] unused marker" in message
    good = MINI_PYPROJECT + 'unused = { path = "x", marker = "os_name == \'x\'" }\n'
    code, message = _run(tmp_path, good, MINI_LOCK)
    assert code == EXIT_OK, message


WHEEL_URL = "https://files.example/six-1.17.0-py2.py3-none-any.whl"
WHEELS = "wheels = [\n    {WHEEL},\n]"
SDIST = (
    'sdist = { url = "https://files.example/six-1.17.0.tar.gz", hash = "sha256:ff70", '
    'size = 34031, upload-time = "2024-12-04T17:35:28.174Z" }'
)
WHEEL = (
    f'{{ url = "{WHEEL_URL}", hash = "sha256:4721", '
    'size = 11050, upload-time = "2024-12-04T17:35:26.475Z" }'
)
WHEELS = WHEELS.replace("{WHEEL}", WHEEL)
ARTIFACTS = f"{SDIST}\n{WHEELS}\n"


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (lambda t: t.replace("size = 34031", 'size = "34031"'), "sdist size = '34031'"),
        (lambda t: t.replace("size = 34031", "size = true"), "sdist size = True"),
        (lambda t: t.replace("size = 34031", "size = -1"), "sdist size = -1"),
        (lambda t: t.replace('hash = "sha256:ff70"', "hash = 1"), "sdist hash = 1"),
        (
            lambda t: t.replace('hash = "sha256:ff70"', 'hash = "sha1:ff70"'),
            "sdist hash = 'sha1:ff70'",
        ),
        (lambda t: t.replace('hash = "sha256:ff70"', 'hash = ""'), "sdist hash = ''"),
        (lambda t: t.replace('"2024-12-04T17:35:28.174Z"', "1"), "sdist upload-time = 1"),
        (lambda t: t.replace('"2024-12-04T17:35:28.174Z"', '"bad"'), "upload-time = 'bad'"),
        (lambda t: t.replace('"2024-12-04T17:35:28.174Z"', '"2024-12-04"'), "'2024-12-04'"),
        (lambda t: t.replace('"2024-12-04T17:35:28.174Z"', '"2024-12-04T17:35:28"'), "uv reads"),
        (lambda t: t.replace(SDIST, 'sdist = "bad"'), "sdist = 'bad' is not a table"),
        (lambda t: t.replace(WHEELS, "wheels = {}"), "wheels = {}"),
        (lambda t: t.replace(WHEELS, "wheels = [1]"), "entry 1"),
        (lambda t: t.replace(WHEELS, "wheels = [[]]"), "entry []"),
        (lambda t: t.replace(WHEELS, "wheels = [{}]"), "no url or path"),
        (lambda t: t.replace(f'url = "{WHEEL_URL}"', "url = 1"), "wheels url = 1"),
        (
            lambda t: t.replace("six-1.17.0-py2.py3-none-any.whl", "bad"),
            "does not end in a wheel of 'six'",
        ),
        (
            lambda t: t.replace("six-1.17.0-py2.py3-none-any.whl", "seven-1.0-py3-none-any.whl"),
            "wheel of 'six'",
        ),
        (
            lambda t: t.replace("six-1.17.0-py2.py3-none-any.whl", "six-1.17.0.whl"),
            "wheel of 'six'",
        ),
        (lambda t: t.replace(f'url = "{WHEEL_URL}"', "path = 1"), "wheels path = 1"),
        (lambda t: t.replace("size = 11050", 'size = "11050"'), "size = '11050'"),
        (lambda t: t.replace('"2024-12-04T17:35:26.475Z"', "1"), "upload-time = 1"),
    ],
    ids=[
        "sdist-size-string",
        "sdist-size-bool",
        "sdist-size-negative",
        "sdist-hash-int",
        "sdist-hash-sha1",
        "sdist-hash-empty",
        "sdist-upload-time-int",
        "sdist-upload-time-word",
        "sdist-upload-time-date",
        "sdist-upload-time-naive",
        "sdist-string",
        "wheels-table",
        "wheel-int",
        "wheel-list",
        "wheel-empty",
        "wheel-url-int",
        "wheel-url-not-a-wheel",
        "wheel-other-package",
        "wheel-bad-tags",
        "wheel-path-int",
        "wheel-size-string",
        "wheel-upload-time-int",
    ],
)
def test_a_package_artifact_record_that_uv_cannot_read_is_unknown(
    tmp_path: Path, edit, named: str
) -> None:
    """Each shape is "Failed to parse `uv.lock`", `uv lock --check` exit 2 (measured uv
    0.8.17, Codex P2 on #3763, round 38: `SourceDistWire` / `WheelWire` did not match),
    while the record loop typed only name, source, version and dependencies."""
    lock = UNMARKED_LOCK + ARTIFACTS
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert code == EXIT_OK, message  # the artifacts as uv writes them
    edited = edit(lock)
    assert edited != lock
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, edited)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t.replace('url = "https://files.example/six-1.17.0.tar.gz"', 'url = "bad"'),
        lambda t: t.replace('hash = "sha256:ff70", ', ""),
        lambda t: t.replace('hash = "sha256:ff70"', 'hash = "sha256:"'),
        lambda t: t.replace('hash = "sha256:ff70"', 'hash = "md5:FF70"'),
        lambda t: t.replace('hash = "sha256:ff70"', 'hash = "blake2b:ff70"'),
        lambda t: t.replace("size = 34031, ", ""),
        lambda t: t.replace("size = 34031", "size = 34031, bogus = 1"),
        lambda t: t.replace(SDIST, "sdist = {}"),
        lambda t: t.replace(SDIST + "\n", ""),
        lambda t: t.replace(WHEELS, "wheels = []"),
        lambda t: t.replace('hash = "sha256:4721", ', ""),
        lambda t: t.replace("size = 11050", "size = 11050, bogus = 1"),
        lambda t: t.replace(f'url = "{WHEEL_URL}"', 'path = "six-1.17.0-py2.py3-none-any.whl"'),
        lambda t: t.replace(f'url = "{WHEEL_URL}"', 'url = "six-1.17.0-py2.py3-none-any.whl"'),
        lambda t: t.replace(WHEEL_URL, "file:///x/six-1.17.0-py2.py3-none-any.whl"),
    ],
    ids=[
        "sdist-url-word",
        "sdist-no-hash",
        "sdist-empty-digest",
        "sdist-md5-upper",
        "sdist-blake2b",
        "sdist-no-size",
        "sdist-extra-key",
        "sdist-empty",
        "no-sdist",
        "no-wheels",
        "wheel-no-hash",
        "wheel-extra-key",
        "wheel-path",
        "wheel-relative-url",
        "wheel-file-url",
    ],
)
def test_a_package_artifact_record_uv_reads_keeps_the_verdict(tmp_path: Path, edit) -> None:
    """CONTROLS for the guard above, each read by uv (`uv lock --check` exit 0, measured
    0.8.17, round 38): an sdist url uv does not parse, a missing hash or size, an empty
    or upper-case digest of a known algorithm, an unknown key, `sdist = {}`, no artifacts,
    a `path` wheel, a relative or file url."""
    lock = UNMARKED_LOCK + ARTIFACTS
    edited = edit(lock)
    assert edited != lock
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, edited)
    assert code == EXIT_OK, message


SIX_HEAD = 'name = "six"\nversion = "1.17.0"\nsource = { registry = "https://pypi.org/simple" }'


@pytest.mark.parametrize("site", ["lock", "package"])
@pytest.mark.parametrize("value", ["[true]", "{}", '["bad"]', '[""]', "[1]", "\"os_name == 'x'\""])
def test_a_resolution_marker_that_uv_cannot_read_is_unknown(
    tmp_path: Path, site: str, value: str
) -> None:
    """`resolution-markers` of each shape, on the lock or on a package, is "Failed to
    parse `uv.lock`", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763,
    round 39), while `lock_schema` had read only `version`. Controls: `[]` on either,
    and a list of valid markers (two complementary forks on the lock, one on a
    package) are read (exit 0)."""
    anchor = 'requires-python = ">=3.11"' if site == "lock" else SIX_HEAD
    assert UNMARKED_LOCK.count(anchor) == 1
    lock = UNMARKED_LOCK.replace(anchor, f"{anchor}\nresolution-markers = {value}")
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert "resolution-markers" in message
    forks = "[\"os_name == 'x'\", \"os_name != 'x'\"]" if site == "lock" else "[\"os_name == 'x'\"]"
    for control in ("[]", forks):
        lock = UNMARKED_LOCK.replace(anchor, f"{anchor}\nresolution-markers = {control}")
        code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
        assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("first", "second"),
    [("foo_bar", "foo-bar"), ('"Foo.Bar"', "foo_bar"), ("localdep", "LocalDep")],
)
def test_source_names_that_collide_after_normalization_are_unknown(
    tmp_path: Path, first: str, second: str
) -> None:
    """`foo_bar = ...` beside `foo-bar = ...` in `[tool.uv.sources]`, declared or not,
    is "duplicate sources for package", "Failed to parse: `pyproject.toml`", exit 2
    (measured uv 0.8.17, Codex P2 on #3763, round 39), while the normalized lookup had
    kept one quietly. Control: one of them alone is read (exit 0)."""
    assert MINI_PYPROJECT.endswith('localdep = { path = "dep" }\n')
    entries = [f"{name} = {{ workspace = false }}\n" for name in (first, second)]
    if first == "localdep":  # colliding with the declared source itself
        entries = ["", f'{second} = {{ path = "dep" }}\n']
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    code, message = _run(tmp_path, MINI_PYPROJECT + "".join(entries), MINI_LOCK)
    assert code == EXIT_UNKNOWN, message
    assert "[tool.uv.sources]" in message and "are both" in message
    code, message = _run(tmp_path, MINI_PYPROJECT + entries[0], MINI_LOCK)
    assert code == EXIT_OK, message
