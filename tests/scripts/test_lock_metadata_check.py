# ruff: noqa: E501  (TOML inline tables in the fixtures cannot wrap)
"""scripts/lock_metadata_check.py: a stale uv.lock fails closed without resolution.

- [if] pyproject.toml and uv.lock's root requires-dist agree [then] exit 0, [else stop]
- [if] a specifier, extra, marker or requirement differs [then] exit 1 naming it, [else stop]
- [if] the lock has no root package or a requirement is not comparable [then] exit 2
  UNKNOWN, never 0, [else stop]
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import (
    EXIT_OK,
    EXIT_STALE,
    EXIT_UNKNOWN,
    canonical_version,
    check,
    markers_equivalent,
)

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
    ],
)
def test_markers_compare_by_meaning(spelled: str, recorded: str, same: bool) -> None:
    from packaging.markers import Marker

    assert markers_equivalent((spelled,), (recorded,)) is same
    # ORACLE for the true cases: packaging agrees on a hand-picked environment grid.
    if same:
        for full in ("3.9.7", "3.10.0", "3.10.12", "3.11.0", "3.11.3", "3.12.1"):
            for platform in ("darwin", "win32", "linux"):
                env = {
                    "python_full_version": full,
                    "python_version": full.rsplit(".", 1)[0],
                    "sys_platform": platform,
                    "platform_machine": "x86_64",
                    "extra": "dev",
                }
                assert Marker(spelled).evaluate(env) == Marker(recorded).evaluate(env), (
                    spelled,
                    recorded,
                    env,
                )


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
