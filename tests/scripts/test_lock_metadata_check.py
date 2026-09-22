# ruff: noqa: E501  (TOML inline tables in the fixtures cannot wrap)
"""scripts/lock_metadata_check.py: a stale uv.lock fails closed without resolution.

- [if] pyproject.toml and uv.lock's root requires-dist agree [then] exit 0, [else stop]
- [if] a specifier, extra, marker or requirement differs [then] exit 1 naming it, [else stop]
- [if] the lock has no root package or a requirement is not comparable [then] exit 2
  UNKNOWN, never 0, [else stop]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.lock_marker_semantics import (
    _eval,
    _grid,
    _MarkerParser,
    canonical_version,
    markers_equivalent,
    release_literal,
)
from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN, check

PYPROJECT = """
[project]
name = "Demo_Project"
requires-python = ">=3.11"
dependencies = [
    "numpy>=1.26",
    "uvicorn[standard]>=0.30",
    "pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'",
]
[project.optional-dependencies]
dev = [
    "pytest>=9,<10",
    "python-rtmidi>=1.5,<2; sys_platform != 'win32'",
]
all = ["Demo_Project[dev]"]
"""

LOCK = """
version = 1
requires-python = ">=3.11"

[[package]]
name = "numpy"
version = "2.0.0"

[[package]]
name = "demo-project"
version = "0.1.0"
source = { editable = "." }

[package.metadata]
requires-dist = [
    { name = "demo-project", extras = ["dev"], marker = "extra == 'all'" },
    { name = "numpy", specifier = ">=1.26" },
    { name = "pyobjc-framework-quartz", marker = "sys_platform == 'darwin'", specifier = ">=10.0" },
    { name = "pytest", marker = "extra == 'dev'", specifier = ">=9,<10" },
    { name = "python-rtmidi", marker = "sys_platform != 'win32' and extra == 'dev'", specifier = ">=1.5,<2" },
    { name = "uvicorn", extras = ["standard"], specifier = ">=0.30" },
]
"""


def _run(tmp_path: Path, pyproject: str = PYPROJECT, lock: str = LOCK) -> tuple[int, str]:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(lock, encoding="utf-8")
    return check(tmp_path / "pyproject.toml", tmp_path / "uv.lock")


def test_matching_metadata_passes_across_name_extra_and_marker_spelling(tmp_path: Path) -> None:
    """if names, extras and marker order are spelled differently but mean the same then 0"""
    code, message = _run(tmp_path)
    assert code == EXIT_OK, message
    assert "6 requirements" in message


def test_a_specifier_bumped_in_pyproject_only_is_stale_and_named(tmp_path: Path) -> None:
    code, message = _run(tmp_path, PYPROJECT.replace('"numpy>=1.26"', '"numpy>=2.1"'))
    assert code == EXIT_STALE
    assert "in pyproject.toml, not in uv.lock: numpy>=2.1" in message
    assert "in uv.lock, not in pyproject.toml: numpy>=1.26" in message


def test_a_requirement_added_to_an_extra_without_uv_lock_is_stale(tmp_path: Path) -> None:
    """the PR #2740 shape: a dev dependency edited, the lock left behind"""
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('"pytest>=9,<10",', '"pytest>=9,<10",\n    "pytest-timeout>=2.3,<3",'),
    )
    assert code == EXIT_STALE
    assert "pytest-timeout<3,>=2.3; extra == 'dev'" in message


def test_a_marker_change_is_stale(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path, PYPROJECT.replace("sys_platform != 'win32'", "sys_platform != 'linux'")
    )
    assert code == EXIT_STALE
    assert "python-rtmidi" in message


def test_requires_python_drift_is_stale(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path, PYPROJECT.replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"')
    )
    assert code == EXIT_STALE
    assert "requires-python" in message


def test_requires_python_removed_without_uv_lock_is_stale(tmp_path: Path) -> None:
    """if pyproject.toml drops requires-python and the lock still carries it then 1, not 0"""
    code, message = _run(tmp_path, PYPROJECT.replace('requires-python = ">=3.11"\n', ""))
    assert code == EXIT_STALE
    assert "requires-python: pyproject.toml None, uv.lock '>=3.11'" in message


def test_a_version_only_bump_without_uv_lock_is_stale(tmp_path: Path) -> None:
    """if [project].version moves and the lock's root package version does not then 1"""
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('name = "Demo_Project"', 'name = "Demo_Project"\nversion = "0.2.0"'),
    )
    assert code == EXIT_STALE
    assert "[project] version: pyproject.toml '0.2.0', uv.lock root '0.1.0'" in message
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('name = "Demo_Project"', 'name = "Demo_Project"\nversion = "0.1.0"'),
    )
    assert code == EXIT_OK, message


def test_an_extra_named_with_underscores_matches_its_normalized_marker(tmp_path: Path) -> None:
    """if the optional-dependency key is `foo_bar` then uv's `extra == 'foo-bar'` marker matches"""
    pyproject = PYPROJECT.replace("dev = [", "dev_tools = [").replace("[dev]", "[dev_tools]")
    lock = LOCK.replace("extra == 'dev'", "extra == 'dev-tools'").replace(
        'extras = ["dev"]', 'extras = ["dev-tools"]'
    )
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("spelled", "canonical"),
    [
        ("01.026", "1.26"),
        ("v1.0", "1.0"),
        ("1.0.0-rc1", "1.0.0rc1"),
        ("1.0alpha", "1.0a0"),
        ("1.0.preview.2", "1.0rc2"),
        ("1.0-1", "1.0.post1"),
        ("1.0.rev2", "1.0.post2"),
        ("1.0-dev", "1.0.dev0"),
        ("0!1.0", "1.0"),
        ("2!1.0", "2!1.0"),
        ("1.0+Ubuntu_1", "1.0+ubuntu.1"),
        ("1.0+01", "1.0+1"),
        ("1.0+abc.007.x01", "1.0+abc.7.x01"),
        ("1.0.1", "1.0.1"),
    ],
)
def test_canonical_version_matches_pep_440_normalization(spelled: str, canonical: str) -> None:
    assert canonical_version(spelled) == canonical


def test_a_non_version_is_unknown() -> None:
    with pytest.raises(Exception, match="not a PEP 440 version"):
        canonical_version("latest")


def test_noncanonical_version_spellings_match_the_canonical_lock(tmp_path: Path) -> None:
    """if pyproject.toml spells `>=01.026`, `>=03.011` and `1.0.0-rc1` and uv wrote the
    canonical forms then 0: a fresh lock must never read stale over spelling"""
    pyproject = (
        PYPROJECT.replace('"numpy>=1.26"', '"numpy>=01.026"')
        .replace('requires-python = ">=3.11"', 'requires-python = ">=03.011"')
        .replace('name = "Demo_Project"', 'name = "Demo_Project"\nversion = "0.1.0-rc1"')
    )
    lock = LOCK.replace('version = "0.1.0"\nsource', 'version = "0.1.0rc1"\nsource')
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message


def test_a_marker_without_operator_spacing_matches_the_spaced_lock(tmp_path: Path) -> None:
    """if pyproject.toml writes `python_full_version<'3.11'` and uv wrote it with spaces then 0"""
    pyproject = PYPROJECT.replace(
        "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\"",
        "\"pyobjc-framework-Quartz>=10.0; sys_platform=='darwin' and python_full_version<'3.13'\"",
    )
    lock = LOCK.replace(
        "marker = \"sys_platform == 'darwin'\"",
        "marker = \"python_full_version < '3.13' and sys_platform == 'darwin'\"",
    )
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, pyproject.replace("<'3.13'", "<'3.12'"), lock)
    assert code == EXIT_STALE


def test_a_wildcard_and_arbitrary_equality_clause_still_compare(tmp_path: Path) -> None:
    pyproject = PYPROJECT.replace('"numpy>=1.26"', '"numpy==01.026.*,!=1.26.3"')
    lock = LOCK.replace('specifier = ">=1.26" }', 'specifier = "==1.26.*,!=1.26.3" }')
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, pyproject.replace("!=1.26.3", "!=1.26.4"), lock)
    assert code == EXIT_STALE


def test_an_unparseable_project_version_is_unknown_not_clean(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('name = "Demo_Project"', 'name = "Demo_Project"\nversion = "latest"'),
    )
    assert code == EXIT_UNKNOWN
    assert "not a PEP 440 version" in message


def test_a_python_version_marker_matches_uv_s_python_full_version_rewrite(tmp_path: Path) -> None:
    """if pyproject.toml says `python_version < '3.11'` and uv recorded
    `python_full_version < '3.11'` then 0: same meaning, different spelling"""
    pyproject = PYPROJECT.replace(
        "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\"",
        "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin' and python_version < '3.11'\"",
    )
    lock = LOCK.replace(
        "marker = \"sys_platform == 'darwin'\"",
        "marker = \"python_full_version < '3.11' and sys_platform == 'darwin'\"",
    )
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    # CONTROL: a marker that MEANS something else is still stale, spelled either way.
    code, message = _run(tmp_path, pyproject.replace("< '3.11'", "< '3.12'"), lock)
    assert code == EXIT_STALE
    assert "python_version < '3.12'" in message


def test_a_parenthesized_specifier_matches_the_bare_lock_specifier(tmp_path: Path) -> None:
    """if pyproject.toml writes `numpy (>=1.26)` and uv recorded `>=1.26` then 0"""
    code, message = _run(tmp_path, PYPROJECT.replace('"numpy>=1.26"', '"numpy (>=1.26)"'))
    assert code == EXIT_OK, message


def _orders_a_string(marker: str) -> bool:
    ordered = re.findall(r"([a-z_]+)\s*(?:<=|>=|<|>)\s*'", marker)
    return any(
        var not in ("python_version", "python_full_version", "implementation_version")
        for var in ordered
    )


def _as_uv_stores_it(marker: str) -> str:
    """The marker with every version literal cut to its release, which is the spelling
    uv records (scripts/lock_marker_semantics.py, measured table). Over release-only
    environments packaging's PEP 440 evaluation of THAT spelling is uv's semantics."""
    return re.sub(
        r"(python_full_version|python_version|implementation_version)\s*"
        r"(===|==|!=|<=|>=|<|>|~=)\s*'([^']*)'",
        lambda m: f"{m.group(1)} {m.group(2)} '{release_literal(m.group(3))}'",
        marker,
    )


@pytest.mark.parametrize(
    ("spelled", "recorded", "same"),
    [
        ("python_version < '3.11'", "python_full_version < '3.11'", True),
        ("python_version >= '3.10'", "python_full_version >= '3.10'", True),
        ("python_version <= '3.10'", "python_full_version < '3.11'", True),
        ("python_version > '3.10'", "python_full_version >= '3.11'", True),
        ("python_version == '3.10'", "python_full_version == '3.10.*'", True),
        (
            "python_version == '3.10'",
            "python_full_version >= '3.10' and python_full_version < '3.11'",
            True,
        ),
        ("python_version != '3.10'", "python_full_version != '3.10.*'", True),
        (
            "(sys_platform == 'win32' or sys_platform == 'darwin') and extra == 'dev'",
            "(extra == 'dev' and sys_platform == 'darwin') or (extra == 'dev' and sys_platform == 'win32')",
            True,
        ),
        ("python_version < '3.11'", "python_full_version < '3.12'", False),
        ("sys_platform == 'darwin'", "sys_platform != 'darwin'", False),
        (
            "sys_platform == 'darwin'",
            "sys_platform == 'darwin' or platform_machine == 'arm64'",
            False,
        ),
        ("python_full_version < '3.11.3'", "python_full_version < '3.11.4'", False),
        # Only a NEIGHBOR of a mentioned literal separates these two: every literal
        # itself evaluates the same on both sides, 3.10.100 does not.
        ("python_full_version < '3.11'", "python_full_version <= '3.10.99'", False),
        ("python_full_version >= '3.11'", "python_full_version > '3.10.99'", False),
        # uv cuts every version literal to its release before storing it (measured
        # against uv 0.8.17, table in scripts/lock_marker_semantics.py), so a fresh lock
        # spells `<= '3.11rc1'` as `<= '3.11'`: still not `< '3.11'`, they part at 3.11.
        ("python_full_version < '3.11'", "python_full_version <= '3.11rc1'", False),
        ("python_full_version < '3.11'", "python_full_version < '3.11rc0'", True),
        ("python_full_version < '3.11rc1'", "python_full_version < '3.11'", True),
        ("python_full_version > '3.13.0b2'", "python_full_version > '3.13'", True),
        ("python_full_version < '3.11.post1'", "python_full_version < '3.11'", True),
        ("python_full_version == '3.11rc1'", "python_full_version == '3.11'", True),
        ("python_full_version == '3.11.0'", "python_full_version == '3.11'", True),
        # A wildcard keeps its width: uv records `3.11.0.*` as the 3.11.0.x range
        # (Codex P2 on #3763, round 7), and it is NOT `3.11.*`.
        (
            "python_full_version == '3.11.0.*'",
            "python_full_version >= '3.11.0' and python_full_version < '3.11.1'",
            True,
        ),
        (
            "python_full_version != '3.11.0.*'",
            "python_full_version < '3.11.0' or python_full_version >= '3.11.1'",
            True,
        ),
        ("python_full_version == '3.11.0.*'", "python_full_version == '3.11.*'", False),
        ("python_version <= '3.11rc1'", "python_full_version < '3.12'", True),
        ("implementation_version < '3.11rc1'", "implementation_version < '3.11'", True),
        # uv drops an epoch too (measured, same table).
        ("python_full_version <= '1!3'", "python_full_version <= '3'", True),
        ("python_full_version >= '1!3.11'", "python_full_version >= '3.11'", True),
        (
            "python_full_version ~= '3.11.1'",
            "python_full_version >= '3.11.1' and python_full_version < '3.12'",
            True,
        ),
        # Strings order lexically too (packaging and uv both compare them so): only
        # a value strictly BETWEEN 'linux' and 'win32' separates these, e.g. 'netbsd'.
        ("sys_platform <= 'linux'", "sys_platform < 'win32'", False),
        # uv's own rewrites of ordered string markers (measured, same table):
        ("sys_platform < 'win32'", "sys_platform < 'win32' and sys_platform != 'win32'", True),
        ("sys_platform > 'linux'", "sys_platform >= 'linux' and sys_platform != 'linux'", True),
        ("sys_platform <= 'linux'", "sys_platform < 'linux' or sys_platform == 'linux'", True),
        ("sys_platform < 'win32' or sys_platform >= 'win32'", "", True),
        ("sys_platform < 'win32' or sys_platform > 'win32'", "", False),
        # Membership is substring membership: 'linux' satisfies `in` but not `==`.
        ("sys_platform in 'linux,darwin'", "sys_platform == 'linux,darwin'", False),
        (
            "sys_platform in 'linux,darwin'",
            "sys_platform == 'linux' or sys_platform == 'darwin'",
            False,
        ),
        ("sys_platform not in 'win32'", "sys_platform != 'win32'", False),
        ("python_full_version >= '3.10'", "python_full_version >= '3.10.dev0'", True),
        ("python_full_version > '3.10'", "python_full_version > '3.10.post1'", True),
    ],
)
def test_markers_compare_by_meaning(spelled: str, recorded: str, same: bool) -> None:
    from packaging.markers import Marker

    assert markers_equivalent((spelled,), (recorded,)) is same
    if _orders_a_string(spelled) or _orders_a_string(recorded):
        # packaging 26 evaluates `<`/`>` on strings as always false and `<=`/`>=` as
        # equality; uv orders them lexically (measured table in the module docstring),
        # and a uv.lock check follows uv. No packaging oracle for these rows; the
        # lexical semantics has its own test below.
        return
    uv_spelled, uv_recorded = _as_uv_stores_it(spelled), _as_uv_stores_it(recorded)
    # ORACLE over the checker's own probe grid, both directions: packaging, on the
    # spelling uv stores, must agree with our evaluator on EVERY probe environment (so
    # the evaluator is right), and for the false cases separate the two markers on at
    # least one (so the grid is complete).
    ast_a, ast_b = _MarkerParser(spelled).parse(), _MarkerParser(recorded).parse()
    envs = _grid(ast_a, ast_b)
    for env in envs:
        env.setdefault("platform_machine", "arm64")
        env.setdefault("extra", "dev")
        env.setdefault("sys_platform", "linux")
        env.setdefault("python_full_version", "3.11.0")
        env.setdefault("python_version", "3.11")
        for text, ast in ((uv_spelled, ast_a), (uv_recorded, ast_b)):
            assert _eval(ast, env) == Marker(text).evaluate(env), (text, env)
    if not same:
        assert any(
            Marker(uv_spelled).evaluate(env) != Marker(uv_recorded).evaluate(env) for env in envs
        ), (spelled, recorded, envs)
    # ORACLE for the true cases: packaging agrees on a hand-picked environment grid.
    if same:
        for full in ("3.9.7", "3.10.0", "3.10.12", "3.11.0", "3.11.3", "3.12.1", "3.13.0"):
            for platform in ("darwin", "win32", "linux", "netbsd"):
                env = {
                    "python_full_version": full,
                    "python_version": full.rsplit(".", 1)[0],
                    "implementation_version": full,
                    "sys_platform": platform,
                    "platform_machine": "x86_64",
                    "extra": "dev",
                }
                assert Marker(uv_spelled).evaluate(env) == Marker(uv_recorded).evaluate(env), (
                    spelled,
                    recorded,
                    env,
                )


def test_a_prerelease_bounded_marker_matches_the_lock_uv_writes_for_it(tmp_path: Path) -> None:
    """if pyproject.toml says `python_full_version < '3.11rc1'` and uv recorded
    `python_full_version < '3.11'` (which uv 0.8.17 does) then 0: a freshly regenerated
    lock must never read stale (Codex P2 on #3763, round 6)"""
    pyproject = PYPROJECT.replace(
        "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\"",
        "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin' and python_full_version < '3.11rc1'\"",
    )
    lock = LOCK.replace(
        "marker = \"sys_platform == 'darwin'\"",
        "marker = \"python_full_version < '3.11' and sys_platform == 'darwin'\"",
    )
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    # CONTROL: the same edit one minor version over is still stale.
    code, message = _run(tmp_path, pyproject.replace("< '3.11rc1'", "< '3.12rc1'"), lock)
    assert code == EXIT_STALE
    assert "python_full_version < '3.12rc1'" in message


@pytest.mark.parametrize(
    ("value", "below_win32", "at_most_linux"),
    [
        ("darwin", True, True),
        ("linux", True, True),
        ("netbsd", True, False),
        ("win32", False, False),
    ],
)
def test_string_ordering_is_lexical_as_uv_ranges_it(
    value: str, below_win32: bool, at_most_linux: bool
) -> None:
    """`sys_platform < 'win32'` holds for every platform that sorts before it, which is
    how uv's marker algebra ranges strings (it folds `< 'win32' and != 'win32'` to
    `< 'win32'`); 'netbsd' is the value between 'linux' and 'win32' that separates
    `<= 'linux'` from `< 'win32'`."""
    env = {"sys_platform": value}
    assert _eval(_MarkerParser("sys_platform < 'win32'").parse(), env) is below_win32
    assert _eval(_MarkerParser("sys_platform <= 'linux'").parse(), env) is at_most_linux


def test_a_compatible_requires_python_matches_the_bounds_uv_writes(tmp_path: Path) -> None:
    """if pyproject.toml says `requires-python = "~=3.11"` and uv recorded `>=3.11, <4`
    (which uv does; `~=3.11.2` becomes `>=3.11.2, <3.12`) then 0 (Codex P2 on #3763,
    round 7)"""
    pyproject = PYPROJECT.replace('requires-python = ">=3.11"', 'requires-python = "~=3.11"')
    lock = LOCK.replace('requires-python = ">=3.11"', 'requires-python = ">=3.11, <4"')
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    pyproject = PYPROJECT.replace('requires-python = ">=3.11"', 'requires-python = "~=3.11.2"')
    lock = LOCK.replace('requires-python = ">=3.11"', 'requires-python = ">=3.11.2, <3.12"')
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    # CONTROL: the next minor is still stale, and so is a wider upper bound.
    code, message = _run(tmp_path, pyproject.replace("~=3.11.2", "~=3.12.0"), lock)
    assert code == EXIT_STALE and "requires-python" in message
    code, message = _run(tmp_path, pyproject, lock.replace("<3.12", "<4"))
    assert code == EXIT_STALE and "requires-python" in message


def test_a_compatible_dependency_specifier_matches_uv_s_verbatim_record(tmp_path: Path) -> None:
    """uv keeps a dependency's `~=` verbatim (`six~=1.16` records `~=1.16`); both sides
    expand the same way, so that still matches, and the bounds form matches too."""
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('"numpy>=1.26"', '"numpy~=1.26"'),
        LOCK.replace('specifier = ">=1.26"', 'specifier = "~=1.26"'),
    )
    assert code == EXIT_OK, message
    code, message = _run(
        tmp_path,
        PYPROJECT.replace('"numpy>=1.26"', '"numpy~=1.26"'),
        LOCK.replace('specifier = ">=1.26"', 'specifier = ">=1.26,<2"'),
    )
    assert code == EXIT_OK, message


def test_a_quoted_and_or_inside_a_marker_literal_is_one_clause(tmp_path: Path) -> None:
    """if pyproject.toml says `os_name == 'posix and stuff'` and uv recorded it verbatim
    (which it does) then 0: the `and` inside the quotes is not a conjunction (Codex P2 on
    #3763, round 8)"""
    for literal in ("posix and stuff", "a or b"):
        pyproject = PYPROJECT.replace(
            "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\"",
            f"\"pyobjc-framework-Quartz>=10.0; os_name == '{literal}' and sys_platform == 'darwin'\"",
        )
        lock = LOCK.replace(
            "marker = \"sys_platform == 'darwin'\"",
            f"marker = \"os_name == '{literal}' and sys_platform == 'darwin'\"",
        )
        code, message = _run(tmp_path, pyproject, lock)
        assert code == EXIT_OK, message
        # CONTROL: a different literal is still stale.
        code, message = _run(tmp_path, pyproject, lock.replace(f"'{literal}'", "'posix'"))
        assert code == EXIT_STALE, message


def _with_marker(pyproject_marker: str, lock_marker: str) -> tuple[str, str]:
    """The fixture pair with the Quartz requirement's marker swapped on each side; JSON
    strings are valid TOML basic strings, so either quote form survives the fixture."""
    quartz = "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\""
    recorded = "marker = \"sys_platform == 'darwin'\""
    pyproject = PYPROJECT.replace(
        quartz, json.dumps(f"pyobjc-framework-Quartz>=10.0; {pyproject_marker}")
    )
    lock = LOCK.replace(recorded, "marker = " + json.dumps(lock_marker))
    assert pyproject != PYPROJECT and lock != LOCK
    return pyproject, lock


def test_a_double_quoted_marker_literal_matches_its_single_quoted_record(tmp_path: Path) -> None:
    """if pyproject.toml says `os_name == "posix"` and uv recorded `os_name == 'posix'`
    (which it does) then 0; a literal holding a double quote is recorded verbatim in
    single quotes and matches too (Codex P2 on #3763, round 9)"""
    code, message = _run(tmp_path, *_with_marker('os_name == "posix"', "os_name == 'posix'"))
    assert code == EXIT_OK, message
    pyproject, lock = _with_marker("os_name == 'say \"hi\"'", "os_name == 'say \"hi\"'")
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    # CONTROL: a different literal is still stale.
    code, message = _run(tmp_path, pyproject, lock.replace('say \\"hi\\"', "posix"))
    assert code == EXIT_STALE, message


def test_uv_s_malformed_record_of_an_apostrophe_literal_is_unknown(tmp_path: Path) -> None:
    """`os_name == "posix's"` is valid PEP 508, and uv 0.8.17 records it as
    `os_name == 'posix's'`, which is not (a single-quoted literal cannot hold `'`).
    The lock side cannot be parsed, so the check says UNKNOWN naming the marker,
    never a verdict either way."""
    pyproject, lock = _with_marker('os_name == "posix\'s"', "os_name == 'posix's'")
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_UNKNOWN, message
    assert "posix's" in message


def test_a_local_version_in_a_marker_is_unknown_not_a_verdict() -> None:
    """uv dropped the marker outright for `>= '3.11.2+local'` (measured); that rewrite is
    not modeled, so the compare must say so rather than guess either way."""
    with pytest.raises(Exception, match="local version"):
        markers_equivalent(
            ("python_full_version >= '3.11.2+local'",), ("python_full_version >= '3.11.2'",)
        )


def test_ordering_mixed_with_membership_is_unknown_not_a_verdict() -> None:
    """`sys_platform in 'linux' and sys_platform < 'linux'` needs a probe per (interval,
    substring) pair; the grid does not build those, so it must not render a verdict."""
    with pytest.raises(Exception, match="membership and ordering"):
        markers_equivalent(
            ("sys_platform in 'linux' and sys_platform < 'linux'",), ("sys_platform < 'linux'",)
        )


def test_membership_with_the_variable_on_the_right_is_unknown_not_a_verdict() -> None:
    """`'lin' in sys_platform` holds for every value CONTAINING the literal, a class the
    probe grid cannot enumerate; the compare must say so rather than guess either way."""
    with pytest.raises(Exception, match="variable on the right"):
        markers_equivalent(("'lin' in sys_platform",), ("sys_platform == 'linux'",))


def test_a_lock_without_the_root_package_is_unknown_not_clean(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path, lock=LOCK.replace('name = "demo-project"', 'name = "someone-else"')
    )
    assert code == EXIT_UNKNOWN
    assert "UNKNOWN" in message and "expected the root" in message


def test_a_url_requirement_is_unknown_not_clean(tmp_path: Path) -> None:
    code, message = _run(
        tmp_path, PYPROJECT.replace('"numpy>=1.26"', '"numpy @ https://example.invalid/numpy.whl"')
    )
    assert code == EXIT_UNKNOWN
    assert "URL requirement" in message


def test_an_unreadable_lock_is_unknown(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    code, message = check(tmp_path / "pyproject.toml", tmp_path / "missing.lock")
    assert code == EXIT_UNKNOWN
    assert "cannot read" in message


def test_the_real_tree_matches(tmp_path: Path) -> None:
    """The committed pair is the positive control: a repo whose own lock reads stale here
    would make every PR red, and a script that cannot parse the real file is no guard."""
    root = Path(__file__).resolve().parents[2]
    code, message = check(root / "pyproject.toml", root / "uv.lock")
    assert code == EXIT_OK, message
