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
