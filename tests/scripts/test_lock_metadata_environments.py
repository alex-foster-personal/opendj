"""scripts/lock_environments.py: `[tool.uv] environments` and `required-environments`
against uv.lock's `supported-markers` and `required-markers`.

Each stale pair is `uv lock --check` exit 1 and each refused shape exit 2 (measured uv
0.8.17, Codex P2 on #3763, round 42); the checker had compared neither pair.

- [if] the two lists differ in length, order or meaning, or one side alone is non-empty
  [then] STALE naming the pair, [else stop]
- [if] they agree element-wise by meaning, in uv's spelling or the project's [then] the
  verdict is unchanged, [else stop]
- [if] either side holds a shape uv refuses [then] exit 2 UNKNOWN naming it, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import UNMARKED_LOCK, UNMARKED_PYPROJECT

LINUX = "sys_platform == 'linux'"
DARWIN = "sys_platform == 'darwin'"
REQUIRES = 'requires-python = ">=3.11"\n'


def _pair(
    environments: str | None, markers: str | None, key: str = "environments"
) -> tuple[str, str]:
    recorded_key = "supported-markers" if key == "environments" else "required-markers"
    pyproject = UNMARKED_PYPROJECT + (
        "" if environments is None else f"[tool.uv]\n{key} = {environments}\n"
    )
    assert UNMARKED_LOCK.count(REQUIRES) == 1
    lock = UNMARKED_LOCK.replace(
        REQUIRES, REQUIRES + ("" if markers is None else f"{recorded_key} = {markers}\n")
    )
    return pyproject, lock


@pytest.mark.parametrize(
    ("environments", "markers", "key"),
    [
        (f'["{LINUX}"]', None, "environments"),
        (f'["{DARWIN}"]', f'["{LINUX}"]', "environments"),
        (f'["{DARWIN}", "{LINUX}"]', f'["{LINUX}", "{DARWIN}"]', "environments"),
        (f'["{LINUX}"]', f'["{LINUX}", "{DARWIN}"]', "environments"),
        (None, f'["{LINUX}"]', "environments"),
        (f'["{LINUX}"]', None, "required-environments"),
        (f'["{DARWIN}"]', f'["{LINUX}"]', "required-environments"),
        (None, f'["{LINUX}"]', "required-environments"),
    ],
    ids=[
        "unlocked",
        "retargeted",
        "reordered",
        "spurious-recorded",
        "removed",
        "required-unlocked",
        "required-retargeted",
        "required-removed",
    ],
)
def test_environments_that_disagree_with_the_recorded_markers_are_stale(
    tmp_path: Path, environments: str | None, markers: str | None, key: str
) -> None:
    """Each pair is `uv lock --check` exit 1 (measured uv 0.8.17, round 42): the lists
    are compared in order, an extra recorded marker is stale, and a side removed
    while the other stays is stale."""
    code, message = _run(tmp_path, *_pair(environments, markers, key))
    assert code == EXIT_STALE, message
    assert f"[tool.uv] {key}:" in message


@pytest.mark.parametrize(
    ("environments", "markers", "key"),
    [
        (f'["{LINUX}"]', f'["{LINUX}"]', "environments"),
        (f'"{LINUX}"', f'["{LINUX}"]', "environments"),
        ("[\"sys_platform=='linux'\"]", f'["{LINUX}"]', "environments"),
        ("[\"platform_system == 'Linux'\"]", f'["{LINUX}"]', "environments"),
        (
            "[\"sys_platform == 'darwin' and python_version >= '3.12'\"]",
            "[\"python_full_version >= '3.12' and sys_platform == 'darwin'\"]",
            "environments",
        ),
        (f'["{LINUX}", "{DARWIN}"]', f'["{LINUX}", "{DARWIN}"]', "environments"),
        ("[]", None, "environments"),
        (None, "[]", "environments"),
        (None, None, "environments"),
        (f'["{LINUX}"]', f'["{LINUX}"]', "required-environments"),
    ],
    ids=[
        "same",
        "single-string",
        "spacing",
        "platform-system",
        "python-version-rewrite",
        "two-in-order",
        "empty-vs-absent",
        "absent-vs-empty",
        "both-absent",
        "required-same",
    ],
)
def test_environments_that_agree_by_meaning_keep_the_verdict(
    tmp_path: Path, environments: str | None, markers: str | None, key: str
) -> None:
    """CONTROLS: each pair is `uv lock --check` exit 0 (measured uv 0.8.17, round 42):
    a single string is one environment, spacing and uv's rewrites (`platform_system`,
    `python_version`, clause order inside one marker) do not matter, `[]` is absent."""
    code, message = _run(tmp_path, *_pair(environments, markers, key))
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("environments", "markers", "named"),
    [
        ("[1]", None, "[tool.uv] environments = [1] is not a list of strings"),
        ('["bad"]', None, "[tool.uv] environments: marker ends early"),
        ('[""]', None, "[tool.uv] environments = '' is blank"),
        ('""', None, "[tool.uv] environments = '' is blank"),
        ("1", None, "[tool.uv] environments = 1 is not a list of strings"),
        (f'["{LINUX}"]', "[true]", "uv.lock supported-markers = [True] is not a list of strings"),
        (f'["{LINUX}"]', '["bad"]', "uv.lock supported-markers: marker ends early"),
    ],
    ids=["int-item", "bad-marker", "blank-marker", "blank-string", "int", "lock-bool", "lock-bad"],
)
def test_an_environment_shape_that_uv_refuses_is_unknown(
    tmp_path: Path, environments: str | None, markers: str | None, named: str
) -> None:
    """`environments = [1]`, `["bad"]`, `[""]`, `""` and `1` are "TOML parse error" and
    `supported-markers = [true]` or `["bad"]` "Failed to parse `uv.lock`", each `uv
    lock --check` exit 2 (measured uv 0.8.17, round 42)."""
    code, message = _run(tmp_path, *_pair(environments, markers))
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
