# ruff: noqa: E501  (TOML inline tables in the fixtures cannot wrap)
"""scripts/lock_metadata_check.py: a stale uv.lock fails closed without resolution.

- [if] pyproject.toml and uv.lock's root requires-dist agree [then] exit 0, [else stop]
- [if] a specifier, extra, marker or requirement differs [then] exit 1 naming it, [else stop]
- [if] the lock has no root package or a requirement is not comparable [then] exit 2
  UNKNOWN, never 0, [else stop]
"""

from __future__ import annotations

from pathlib import Path

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
