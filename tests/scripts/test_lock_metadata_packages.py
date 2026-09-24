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
