"""scripts/lock_metadata_check.py: `[dependency-groups]` (PEP 735) against the
`requires-dev` table uv records for the root package (Codex P2 on #3763, round 22).

Measured with uv 0.8.17: each group is recorded under its normalized name as the SET
of its entries (an `include-group` expanded inline, a duplicate listed once), an
empty group as `[]`, and `uv lock --check` fails when a group is emptied, added or
removed (empty ones included), an entry's marker changes, an include is dropped, or
the whole table goes; a cycle or an include of a missing group is a uv error.

- [if] every group in pyproject.toml has a matching requires-dev group in uv.lock
  with the same entries and vice versa [then] exit 0, [else] exit 1 naming the group
  and entry, [else stop] ✔︎ ✅ 🎯
- [if] a group includes a missing group, or includes itself through any chain
  [then] exit 2 UNKNOWN, never a verdict, [else stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

from pathlib import Path

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import LOCK, PYPROJECT, _run

GROUPS = """
[dependency-groups]
dev = ["six>=1.16", {include-group = "lint"}]
lint = ["packaging>=24; sys_platform == 'linux'"]
Empty_Group = []
"""
REQUIRES_DEV = """
[package.metadata.requires-dev]
dev = [
    { name = "packaging", marker = "sys_platform == 'linux'", specifier = ">=24" },
    { name = "six", specifier = ">=1.16" },
]
empty-group = []
lint = [{ name = "packaging", marker = "sys_platform == 'linux'", specifier = ">=24" }]
"""
GROUPS_PYPROJECT = PYPROJECT + GROUPS
GROUPS_LOCK = LOCK + REQUIRES_DEV


def test_dependency_groups_match_the_requires_dev_uv_records(tmp_path: Path) -> None:
    code, message = _run(tmp_path, GROUPS_PYPROJECT, GROUPS_LOCK)
    assert code == EXIT_OK, message
    # A duplicate, listed and included, is recorded once: still the same set.
    duplicated = GROUPS_PYPROJECT.replace(
        'dev = ["six>=1.16",', 'dev = ["six>=1.16", "packaging>=24; sys_platform == \'linux\'",'
    )
    assert duplicated != GROUPS_PYPROJECT
    code, message = _run(tmp_path, duplicated, GROUPS_LOCK)
    assert code == EXIT_OK, message


def test_every_dependency_group_edit_uv_rejects_is_stale(tmp_path: Path) -> None:
    for label, pyproject in (
        (
            "group emptied",
            GROUPS_PYPROJECT.replace('dev = ["six>=1.16", {include-group = "lint"}]', "dev = []"),
        ),
        ("empty group removed", GROUPS_PYPROJECT.replace("Empty_Group = []\n", "")),
        ("empty group added", GROUPS_PYPROJECT + "another = []\n"),
        (
            "marker changed",
            GROUPS_PYPROJECT.replace("sys_platform == 'linux'", "sys_platform == 'darwin'"),
        ),
        ("include dropped", GROUPS_PYPROJECT.replace(', {include-group = "lint"}', "")),
        ("table removed", PYPROJECT),
    ):
        assert pyproject != GROUPS_PYPROJECT, label
        code, message = _run(tmp_path, pyproject, GROUPS_LOCK)
        assert code == EXIT_STALE, (label, message)
        assert "dependency group" in message, (label, message)
    code, message = _run(tmp_path, GROUPS_PYPROJECT, LOCK)
    assert code == EXIT_STALE and "dependency group" in message, message


def test_a_malformed_dependency_group_is_unknown_not_a_verdict(tmp_path: Path) -> None:
    for label, pyproject in (
        (
            "cycle",
            GROUPS_PYPROJECT.replace(
                "lint = [\"packaging>=24; sys_platform == 'linux'\"]",
                'lint = [{include-group = "dev"}]',
            ),
        ),
        (
            "missing include",
            GROUPS_PYPROJECT.replace('{include-group = "lint"}', '{include-group = "nope"}'),
        ),
        (
            "unknown entry shape",
            GROUPS_PYPROJECT.replace('{include-group = "lint"}', '{group = "lint"}'),
        ),
    ):
        assert pyproject != GROUPS_PYPROJECT, label
        code, message = _run(tmp_path, pyproject, GROUPS_LOCK)
        assert code == EXIT_UNKNOWN, (label, message)
