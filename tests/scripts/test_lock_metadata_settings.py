"""scripts/lock_settings.py: `[tool.uv]` settings that shape a resolution against what
uv.lock records of them (`[options]`, `[manifest]`, `conflicts`).

Each stale pair is `uv lock --check` exit 1 and each refused shape exit 2 (measured uv
0.8.17, Codex P2 on #3763, round 43); the checker had compared none of them. A scalar
setting uv cannot read is a WARNING that drops the whole settings table, not a parse
failure, so it is UNKNOWN here rather than a verdict against the defaults.

- [if] a setting and its record differ, or one side alone is set [then] STALE naming
  the pair, [else stop]
- [if] they agree, in uv's spelling or the project's, or both are the default [then]
  the verdict is unchanged, [else stop]
- [if] either side holds a shape uv refuses or silently drops [then] exit 2 UNKNOWN
  naming it, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import UNMARKED_LOCK, UNMARKED_PYPROJECT

REQUIRES = 'requires-python = ">=3.11"\n'
STAMP = "2025-01-01T00:00:00Z"
CONFLICT = '[[{ extra = "a" }, { extra = "b" }]]'
RECORDED_CONFLICT = (
    'conflicts = [[\n    { package = "demo", extra = "a" },\n'
    '    { package = "demo", extra = "b" },\n]]\n'
)


def _pair(settings: str | None, recorded: str | None) -> tuple[str, str]:
    pyproject = UNMARKED_PYPROJECT + ("" if settings is None else f"[tool.uv]\n{settings}\n")
    assert UNMARKED_LOCK.count(REQUIRES) == 1
    lock = UNMARKED_LOCK.replace(REQUIRES, REQUIRES + ("" if recorded is None else recorded))
    return pyproject, lock


@pytest.mark.parametrize(
    ("settings", "recorded", "named"),
    [
        ('resolution = "lowest-direct"', None, "[tool.uv] resolution:"),
        (None, '[options]\nresolution-mode = "lowest-direct"\n', "[tool.uv] resolution:"),
        ('prerelease = "allow"', None, "[tool.uv] prerelease:"),
        ('fork-strategy = "fewest"', None, "[tool.uv] fork-strategy:"),
        (f'exclude-newer = "{STAMP}"', None, "[tool.uv] exclude-newer:"),
        ('constraint-dependencies = ["six<2"]', None, "[tool.uv] constraint-dependencies: not in"),
        (None, '[manifest]\nconstraints = [{ name = "six", specifier = "<2" }]\n', "manifest only"),
        (
            'constraint-dependencies = ["six<2"]',
            '[manifest]\nconstraints = [{ name = "six", specifier = "<3" }]\n',
            "[tool.uv] constraint-dependencies:",
        ),
        ('override-dependencies = ["six>=1.16"]', None, "[tool.uv] override-dependencies:"),
        (
            'build-constraint-dependencies = ["setuptools<80"]',
            None,
            "build-constraint-dependencies:",
        ),
        (f"conflicts = {CONFLICT}", None, "[tool.uv] conflicts:"),
        (None, RECORDED_CONFLICT, "[tool.uv] conflicts:"),
    ],
    ids=[
        "resolution-unlocked",
        "resolution-removed",
        "prerelease",
        "fork-strategy",
        "exclude-newer",
        "constraints-unlocked",
        "constraints-removed",
        "constraints-changed",
        "overrides",
        "build-constraints",
        "conflicts-unlocked",
        "conflicts-removed",
    ],
)
def test_a_setting_that_disagrees_with_the_lock_is_stale(
    tmp_path: Path, settings: str | None, recorded: str | None, named: str
) -> None:
    """Each pair is `uv lock --check` exit 1 (measured uv 0.8.17, round 43)."""
    code, message = _run(tmp_path, *_pair(settings, recorded))
    assert code == EXIT_STALE, message
    assert named in message, message


@pytest.mark.parametrize(
    ("settings", "recorded"),
    [
        ('resolution = "highest"', None),
        ('prerelease = "if-necessary-or-explicit"', None),
        ('fork-strategy = "requires-python"', None),
        ('resolution = "lowest-direct"', '[options]\nresolution-mode = "lowest-direct"\n'),
        ('prerelease = "allow"', '[options]\nprerelease-mode = "allow"\n'),
        ('fork-strategy = "fewest"', '[options]\nfork-strategy = "fewest"\n'),
        (f'exclude-newer = "{STAMP}"', f'[options]\nexclude-newer = "{STAMP}"\n'),
        ('exclude-newer = "2025-01-01T01:00:00+01:00"', f'[options]\nexclude-newer = "{STAMP}"\n'),
        (None, '[options]\nbogus = "x"\n'),
        (
            'constraint-dependencies = ["six<2"]',
            '[manifest]\nconstraints = [{ name = "six", specifier = "<2" }]\n',
        ),
        (
            'constraint-dependencies = ["six < 2.0"]',
            '[manifest]\nconstraints = [{ name = "six", specifier = "<2" }]\n',
        ),
        (
            "constraint-dependencies = [\"seven>=1; os_name == 'x'\"]",
            "[manifest]\nconstraints = [\n"
            '    { name = "seven", marker = "os_name == \'x\'", specifier = ">=1" },\n]\n',
        ),
        (
            "constraint-dependencies = [\"six<2; python_version < '3.12'\"]",
            "[manifest]\nconstraints = [\n"
            '    { name = "six", marker = "python_full_version < \'3.12\'", '
            'specifier = "<2" },\n]\n',
        ),
        (
            'constraint-dependencies = ["six<2", "seven>=1"]',
            '[manifest]\nconstraints = [\n    { name = "seven", specifier = ">=1" },\n'
            '    { name = "six", specifier = "<2" },\n]\n',
        ),
        (
            'override-dependencies = ["six>=1.16"]',
            '[manifest]\noverrides = [{ name = "six", specifier = ">=1.16" }]\n',
        ),
        (
            'build-constraint-dependencies = ["setuptools<80"]',
            '[manifest]\nbuild-constraints = [{ name = "setuptools", specifier = "<80" }]\n',
        ),
        (None, "[manifest]\nbogus = 1\n"),
        (f"conflicts = {CONFLICT}", RECORDED_CONFLICT),
        ('conflicts = [[{ extra = "b" }, { extra = "a" }]]', RECORDED_CONFLICT),
    ],
    ids=[
        "resolution-default-explicit",
        "prerelease-default-explicit",
        "fork-strategy-default-explicit",
        "resolution-same",
        "prerelease-same",
        "fork-strategy-same",
        "exclude-newer-same",
        "exclude-newer-same-instant",
        "options-unknown-key",
        "constraints-same",
        "constraints-spelling",
        "constraints-marker",
        "constraints-marker-rewritten",
        "constraints-reordered",
        "overrides-same",
        "build-constraints-same",
        "manifest-unknown-key",
        "conflicts-same",
        "conflicts-reordered",
    ],
)
def test_a_setting_that_agrees_with_the_lock_keeps_the_verdict(
    tmp_path: Path, settings: str | None, recorded: str | None
) -> None:
    """CONTROLS: each pair is `uv lock --check` exit 0 (measured uv 0.8.17, round 43):
    an explicit default records nothing, spelling, order and uv's marker rewrite
    (`python_version` -> `python_full_version`) do not matter, an unknown key beside a
    table is read. (The same-instant and re-ordered conflict spellings
    follow from how uv compares, not from a separate measurement.)"""
    code, message = _run(tmp_path, *_pair(settings, recorded))
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("settings", "recorded", "named"),
    [
        ("resolution = 1", None, "[tool.uv] resolution = 1 is not one of"),
        ('resolution = "bogus"', None, "[tool.uv] resolution = 'bogus' is not one of"),
        ("exclude-newer = 1", None, "[tool.uv] exclude-newer = 1 is not a zoned timestamp"),
        (
            'exclude-newer = "2025-01-01"',
            None,
            "exclude-newer = '2025-01-01' is not a zoned timestamp",
        ),
        (None, 'options = "bad"\n', "uv.lock options = 'bad' is not a table"),
        (
            None,
            "[options]\nresolution-mode = 1\n",
            "uv.lock options resolution-mode = 1 is not one of",
        ),
        (None, '[options]\nresolution-mode = "bogus"\n', "resolution-mode = 'bogus' is not one of"),
        (
            "constraint-dependencies = [1]",
            None,
            "[tool.uv] constraint-dependencies = [1] is not a list",
        ),
        (
            'constraint-dependencies = "six<2"',
            None,
            "constraint-dependencies = 'six<2' is not a list",
        ),
        ('constraint-dependencies = ["bad space<2"]', None, "unparseable specifier clause"),
        (None, 'manifest = "bad"\n', "uv.lock manifest = 'bad' is not a table"),
        (None, "[manifest]\nconstraints = {}\n", "uv.lock manifest constraints = {} is not a list"),
        (None, "[manifest]\nconstraints = [{ name = 1 }]\n", "requires-dist entry without a name"),
        ('conflicts = "bad"', None, "[tool.uv] conflicts = 'bad' is not a list"),
        (None, 'conflicts = "bad"\n', "uv.lock conflicts = 'bad' is not a list"),
        (None, '[manifest]\nmembers = ["demo"]\n', "uv.lock manifest members is not compared"),
        (
            'dependency-metadata = [{ name = "six", version = "1.17.0", requires-dist = [] }]',
            None,
            "[tool.uv] dependency-metadata is not compared",
        ),
    ],
    ids=[
        "resolution-int",
        "resolution-bogus",
        "exclude-newer-int",
        "exclude-newer-date-only",
        "lock-options-string",
        "lock-resolution-int",
        "lock-resolution-bogus",
        "constraints-int",
        "constraints-string",
        "constraints-bad-name",
        "lock-manifest-string",
        "lock-constraints-table",
        "lock-constraint-nameless",
        "conflicts-string",
        "lock-conflicts-string",
        "lock-members",
        "dependency-metadata",
    ],
)
def test_a_setting_shape_uv_refuses_or_drops_is_unknown(
    tmp_path: Path, settings: str | None, recorded: str | None, named: str
) -> None:
    """`resolution = 1` / `"bogus"`, `exclude-newer = 1` / a date with no time make uv
    DROP the settings table with a warning and resolve with the defaults (exit 0 on a
    lock that matches them; measured uv 0.8.17, round 43): a verdict resting on
    settings uv did not read, so UNKNOWN. `constraint-dependencies = [1]` / a string /
    a bad name, `conflicts = "bad"`, and the lock's `options = "bad"`, `resolution-mode
    = 1` / `"bogus"`, `manifest = "bad"`, `constraints = {}` / `[{ name = 1 }]`,
    `conflicts = "bad"` are exit 2. Workspace members and dependency-metadata are
    stale on either side alone (exit 1) and not modeled: UNKNOWN."""
    code, message = _run(tmp_path, *_pair(settings, recorded))
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
