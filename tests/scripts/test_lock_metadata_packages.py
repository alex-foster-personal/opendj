"""scripts/lock_metadata_check.py: every `[[package]]` typed as uv types it.

Continues tests/scripts/test_lock_metadata_shapes.py (Codex P2 round 40 on #3763): a
non-root package's `[package.metadata]` and the names inside dependency records. Each
shape is `uv lock --check` exit 2 (measured uv 0.8.17), so the checker must be UNKNOWN
there, never a verdict, and each test carries the shape uv DOES read as its control.

- [if] a package's metadata or a dependency record holds a shape or name uv refuses
  [then] exit 2 UNKNOWN naming it, never a verdict, [else stop]
- [if] the same site holds a shape uv reads [then] the verdict is unchanged, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import UNMARKED_LOCK, UNMARKED_PYPROJECT

SIX_DEP = 'dependencies = [\n    { name = "six" },\n]'


@pytest.mark.parametrize(
    ("appended", "named"),
    [
        ("metadata = 1\n", "'six' metadata = 1 is not a table"),
        ("[package.metadata]\nrequires-dist = {}\n", "requires-dist = {} is not a list"),
        ("[package.metadata]\nrequires-dist = [1]\n", "record 1 is not a table"),
        ("[package.metadata]\nrequires-dist = [{}]\n", "record {} is not a table"),
        ("[package.metadata]\nrequires-dist = [{ name = 1 }]\n", "name = 1 is not a string"),
        (
            '[package.metadata]\nrequires-dist = [{ name = "bad space" }]\n',
            "'bad space' is not a valid package or extra name",
        ),
        (
            '[package.metadata]\nrequires-dist = [{ name = "seven", marker = "" }]\n',
            "requires-dist 'seven' marker = '' is blank",
        ),
        (
            '[package.metadata]\nrequires-dist = [{ name = "seven", specifier = 1 }]\n',
            "'seven' specifier = 1 is not a string",
        ),
        (
            '[package.metadata]\nrequires-dist = [{ name = "seven", specifier = ">=1," }]\n',
            "empty specifier clause in '>=1,'",
        ),
        (
            '[package.metadata]\nrequires-dist = [{ name = "seven", extras = "x" }]\n',
            "'seven' extras = 'x' is not a list of strings",
        ),
        (
            '[package.metadata]\nrequires-dist = [{ name = "seven", extras = ["bad space"] }]\n',
            "'bad space' is not a valid package or extra name",
        ),
        ('[package.metadata]\nrequires-dev = "bad"\n', "requires-dev = 'bad' is not a table"),
        ("[package.metadata.requires-dev]\ndev = {}\n", "requires-dev dev = {} is not a list"),
        ("[package.metadata.requires-dev]\ndev = [1]\n", "record 1 is not a table"),
        (
            '[package.metadata.requires-dev]\ndev = [{ name = "bad space" }]\n',
            "'bad space' is not a valid package or extra name",
        ),
        (
            '[package.metadata.requires-dev]\n"bad space" = []\n',
            "'bad space' is not a valid package or extra name",
        ),
        ("[package.metadata]\nprovides-extras = [1]\n", "provides-extras = [1] is not a list"),
        (
            '[package.metadata]\nprovides-extras = ["bad space"]\n',
            "'bad space' is not a valid package or extra name",
        ),
    ],
    ids=[
        "metadata-int",
        "requires-dist-table",
        "requires-dist-int-record",
        "requires-dist-nameless",
        "requires-dist-name-int",
        "requires-dist-name-invalid",
        "requires-dist-marker-blank",
        "requires-dist-specifier-int",
        "requires-dist-specifier-empty-clause",
        "requires-dist-extras-string",
        "requires-dist-extras-invalid",
        "requires-dev-string",
        "requires-dev-group-table",
        "requires-dev-group-int-record",
        "requires-dev-name-invalid",
        "requires-dev-group-invalid",
        "provides-extras-int",
        "provides-extras-invalid",
    ],
)
def test_a_non_root_package_metadata_that_uv_cannot_read_is_unknown(
    tmp_path: Path, appended: str, named: str
) -> None:
    """Each shape, appended to the non-root package `six`, is "Failed to parse
    `uv.lock`", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round
    40), while the validators had read only the root's metadata."""
    assert UNMARKED_LOCK.rstrip("\n").endswith('source = { registry = "https://pypi.org/simple" }')
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + appended)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "appended",
    [
        "[package.metadata]\nrequires-dist = []\n",
        '[package.metadata]\nrequires-dist = [{ name = "seven" }]\n',
        '[package.metadata]\nrequires-dist = [{ name = "seven", specifier = ">=1", '
        "marker = \"os_name == 'x'\" }]\n",
        '[package.metadata]\nrequires-dist = [{ name = "seven", git = "https://example.com/x" }]\n',
        '[package.metadata]\nrequires-dist = [{ name = "seven", bogus = 1 }]\n',
        '[package.metadata]\nrequires-dist = [{ name = "d", directory = "x", specifier = "," }]\n',
        '[package.metadata]\nrequires-dist = [{ name = "seven", extras = ["Bad_Extra"] }]\n',
        "[package.metadata.requires-dev]\ndev = []\n",
        '[package.metadata]\nprovides-extras = ["Bad_Extra"]\n',
        "[package.metadata]\nx = 1\n",
    ],
    ids=[
        "empty-requires-dist",
        "plain-record",
        "specifier-and-marker",
        "git-record",
        "unknown-key",
        "path-record-specifier-unread",
        "unnormalized-extra",
        "empty-group",
        "unnormalized-provides-extra",
        "unknown-metadata-key",
    ],
)
def test_a_non_root_package_metadata_uv_reads_keeps_the_verdict(
    tmp_path: Path, appended: str
) -> None:
    """CONTROLS for the guard above, each read by uv (`uv lock --check` exit 0,
    measured 0.8.17, round 40)."""
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + appended)
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("record", "named"),
    [
        ('{ name = "bad space" }', "'bad space' is not a valid package or extra name"),
        ('{ name = "-lead" }', "'-lead' is not a valid package or extra name"),
        ('{ name = "seven" }', "names 'seven', which no [[package]] of the lock carries"),
        ('{ name = "six", extra = ["bad space"] }', "'bad space' is not a valid package"),
    ],
    ids=["name-space", "name-leading-dash", "name-unknown-package", "extra-invalid"],
)
def test_a_dependency_record_name_that_uv_refuses_is_unknown(
    tmp_path: Path, record: str, named: str
) -> None:
    """`name = "bad space"` or `"-lead"` in a dependency record is "Not a valid package
    or extra name", a valid name no `[[package]]` carries is "has missing `source` field
    but has more than one matching package", and `extra = ["bad space"]` is refused too,
    each `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 40);
    the record read had typed the strings only. Controls: `name = "SIX"` (normalized to
    the package) and an unnormalized or unprovided extra are read (exit 0)."""
    assert UNMARKED_LOCK.count(SIX_DEP) == 1
    lock = UNMARKED_LOCK.replace(SIX_DEP, f"dependencies = [\n    {record},\n]")
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
    for control in ('{ name = "SIX" }', '{ name = "six", extra = ["Bad_Extra", "nope"] }'):
        lock = UNMARKED_LOCK.replace(SIX_DEP, f"dependencies = [\n    {control},\n]")
        code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
        assert code == EXIT_OK, message


@pytest.mark.parametrize("value", ['"3"', "true", "-1", "1.5", str(2**32)])
def test_a_lock_revision_that_is_not_a_u32_is_unknown(tmp_path: Path, value: str) -> None:
    """`revision = "3"`, `true`, `-1`, `1.5` and 2**32 are "invalid type ... expected
    u32", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 41),
    while `lock_schema` had read only `version`. Controls: absent, 0, 3 and 2**32 - 1
    are read (exit 0)."""
    assert UNMARKED_LOCK.count("revision = 3\n") == 1
    code, message = _run(
        tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK.replace("revision = 3", f"revision = {value}")
    )
    assert code == EXIT_UNKNOWN, message
    assert "uv.lock revision" in message and "is not a u32" in message
    for control in ("", "revision = 0\n", "revision = 3\n", f"revision = {2**32 - 1}\n"):
        code, message = _run(
            tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK.replace("revision = 3\n", control)
        )
        assert code == EXIT_OK, (control, message)


PYPI = 'source = { registry = "https://pypi.org/simple" }'
GHOST = f'\n[[package]]\nname = "ghost"\nversion = "1.0"\n{PYPI}\n'
SIX_16 = f'\n[[package]]\nname = "six"\nversion = "1.16.0"\n{PYPI}\n'


def test_a_package_identity_recorded_twice_is_unknown(tmp_path: Path) -> None:
    """A second `[[package]]` with the same name, version and source is refused whether
    anything depends on it (`six`) or not (`ghost`), `uv lock --check` exit 2 (measured
    uv 0.8.17, Codex P2 on #3763, round 41); the name set had collapsed it. Controls:
    one `ghost`, a second version of it, or the same version from another registry
    are read (exit 0)."""
    six = UNMARKED_LOCK[UNMARKED_LOCK.rindex("\n[[package]]") :]
    assert 'name = "six"' in six
    for twice in (six, GHOST + GHOST):
        code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + twice)
        assert code == EXIT_UNKNOWN, message
        assert "is recorded twice" in message
    other_source = GHOST.replace("pypi.org", "example.com")
    for once in (GHOST, GHOST + GHOST.replace('"1.0"', '"2.0"'), GHOST + other_source):
        code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + once)
        assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("record", "appended", "named"),
    [
        ('{ name = "six" }', SIX_16, "2 [[package]] records match"),
        ('{ name = "six", version = "9.9" }', "", "no [[package]] of the lock carries"),
        (
            '{ name = "six", source = { registry = "https://example.com/simple" } }',
            "",
            "no [[package]] of the lock carries",
        ),
        ('{ name = "six", version = 1 }', "", "'six' version = 1 is not a string"),
        ('{ name = "six", source = "bad" }', "", "source = 'bad' is not a source table"),
    ],
    ids=["ambiguous", "version-unmatched", "source-unmatched", "version-int", "source-string"],
)
def test_a_dependency_record_that_matches_no_single_package_is_unknown(
    tmp_path: Path, record: str, appended: str, named: str
) -> None:
    """A bare record naming a package the lock records in two versions, a `version` or
    `source` no record of that name carries, `version = 1` and `source = "bad"` are
    each `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round 41).
    Controls: `version` and `source` that pick one of the two, and `version` alone
    against one record, are read (exit 0); `source` alone against two of one registry
    is still ambiguous (exit 2, covered by the bare case's guard)."""
    lock = UNMARKED_LOCK.replace(SIX_DEP, f"dependencies = [\n    {record},\n]") + appended
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
    picked = (
        '{ name = "six", version = "1.17.0", source = { registry = "https://pypi.org/simple" } }'
    )
    for control, extra in (
        (picked, SIX_16),
        ('{ name = "six", version = "1.17.0" }', ""),
    ):
        lock = UNMARKED_LOCK.replace(SIX_DEP, f"dependencies = [\n    {control},\n]") + extra
        code, message = _run(tmp_path, UNMARKED_PYPROJECT, lock)
        assert code == EXIT_OK, (control, message)


@pytest.mark.parametrize(
    ("appended", "named"),
    [
        ('optional-dependencies = "bad"\n', "optional-dependencies = 'bad' is not a table"),
        (
            "[package.optional-dependencies]\nformat = {}\n",
            "optional-dependencies format = {} is not a list",
        ),
        ("[package.optional-dependencies]\nformat = [1]\n", "record 1 is not a table with a name"),
        (
            "[package.optional-dependencies]\nformat = [{}]\n",
            "record {} is not a table with a name",
        ),
        ("[package.optional-dependencies]\nformat = [{ name = 1 }]\n", "name = 1 is not a string"),
        (
            '[package.optional-dependencies]\nformat = [{ name = "bad space" }]\n',
            "'bad space' is not a valid package or extra name",
        ),
        (
            '[package.optional-dependencies]\nformat = [{ name = "seven" }]\n',
            "names 'seven', which no [[package]] of the lock carries",
        ),
        (
            '[package.optional-dependencies]\nformat = [{ name = "demo", marker = "bad" }]\n',
            "optional-dependencies format 'demo' marker: marker ends early",
        ),
        (
            '[package.optional-dependencies]\n"bad space" = []\n',
            "'bad space' is not a valid package or extra name",
        ),
        ('dev-dependencies = "bad"\n', "dev-dependencies = 'bad' is not a table"),
        ("[package.dev-dependencies]\ndev = {}\n", "dev-dependencies dev = {} is not a list"),
        (
            '[package.dev-dependencies]\ndev = [{ name = "seven" }]\n',
            "names 'seven', which no [[package]] of the lock carries",
        ),
        ('[package.dev-dependencies]\n"bad space" = []\n', "'bad space' is not a valid package"),
    ],
    ids=[
        "optional-string",
        "optional-group-table",
        "optional-int-record",
        "optional-nameless",
        "optional-name-int",
        "optional-name-invalid",
        "optional-unknown-package",
        "optional-marker-bad",
        "optional-group-invalid",
        "dev-string",
        "dev-group-table",
        "dev-unknown-package",
        "dev-group-invalid",
    ],
)
def test_a_package_optional_or_dev_dependency_map_that_uv_cannot_read_is_unknown(
    tmp_path: Path, appended: str, named: str
) -> None:
    """`[package.optional-dependencies]` and `[package.dev-dependencies]` on any
    package hold dependency records per extra or group; each shape is "Failed to parse
    `uv.lock`", `uv lock --check` exit 2 (measured uv 0.8.17, Codex P2 on #3763, round
    44), while the record loop had read only `dependencies`."""
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + appended)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "appended",
    [
        "[package.optional-dependencies]\nformat = []\n",
        '[package.optional-dependencies]\nformat = [{ name = "demo" }]\n',
        '[package.optional-dependencies]\nformat = [{ name = "SIX" }]\n',
        "[package.dev-dependencies]\ndev = []\n",
        '[package.dev-dependencies]\ndev = [{ name = "demo" }]\n',
    ],
    ids=["optional-empty", "optional-record", "optional-unnormalized", "dev-empty", "dev-record"],
)
def test_a_package_optional_or_dev_dependency_map_uv_reads_keeps_the_verdict(
    tmp_path: Path, appended: str
) -> None:
    """CONTROLS for the guard above, each `uv lock --check` exit 0 (measured 0.8.17)."""
    code, message = _run(tmp_path, UNMARKED_PYPROJECT, UNMARKED_LOCK + appended)
    assert code == EXIT_OK, message
