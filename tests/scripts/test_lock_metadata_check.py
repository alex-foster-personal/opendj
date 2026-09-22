# ruff: noqa: E501  (TOML inline tables in the fixtures cannot wrap)
"""scripts/lock_metadata_check.py: a stale uv.lock fails closed without resolution.

- [if] pyproject.toml and uv.lock's root requires-dist agree [then] exit 0, [else stop]
- [if] a specifier, extra, marker or requirement differs [then] exit 1 naming it, [else stop]
- [if] the lock has no root package or a requirement is not comparable [then] exit 2
  UNKNOWN, never 0, [else stop]
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.lock_marker_semantics import (
    canonical_version,
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


@pytest.mark.parametrize(
    ("spelled", "recorded", "code"),
    [
        # uv records the tightest bounds (measured with uv 0.8.17, round 12).
        (">=3.10,>=3.11,<4,<5", ">=3.11, <4", EXIT_OK),
        (">3.10,>=3.11", ">=3.11", EXIT_OK),
        (">=3.10,>3.10,<4,<=4", ">3.10, <4", EXIT_OK),
        (">=3.11,<=3.13,<3.13.5", ">=3.11, <=3.13", EXIT_OK),
        (">=3.10.0.1,>3.10", ">=3.10.0.1", EXIT_OK),
        (">=3.11,!=3.12,!=3.12,<4", ">=3.11, !=3.12, <4", EXIT_OK),
        (">=3.11,==3.12.*", "==3.12.*", EXIT_OK),
        # CONTROLS: a bound that is not dominated is still a difference.
        (">=3.10,>=3.11", ">=3.10", EXIT_STALE),
        (">=3.11,<4,<5", ">=3.11, <5", EXIT_STALE),
        (">=3.11,==3.12.*", ">=3.11", EXIT_STALE),
    ],
)
def test_redundant_requires_python_bounds_match_the_tightest_form_uv_writes(
    tmp_path: Path, spelled: str, recorded: str, code: int
) -> None:
    """if pyproject.toml spells requires-python with dominated bounds and uv recorded
    the tightest form (which it does) then 0; a bound uv would keep still separates
    (Codex P2 on #3763, round 12)"""
    pyproject = PYPROJECT.replace('requires-python = ">=3.11"', f'requires-python = "{spelled}"')
    lock = LOCK.replace('requires-python = ">=3.11"', f'requires-python = "{recorded}"')
    assert pyproject != PYPROJECT  # the lock side may legitimately be the fixture's own
    got, message = _run(tmp_path, pyproject, lock)
    assert got == code, (spelled, recorded, message)


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


def test_a_variable_to_variable_marker_matches_the_lock_uv_writes_without_it(
    tmp_path: Path,
) -> None:
    """if pyproject.toml says `os_name != sys_platform` and uv erased the clause from
    the record (which it does, in a conjunction and a disjunction alike) then 0; a clause
    uv keeps is still compared (Codex P2 on #3763, round 10)"""
    quartz = "\"pyobjc-framework-Quartz>=10.0; sys_platform == 'darwin'\""
    recorded_line = "marker = \"sys_platform == 'darwin'\""

    def pair(spelled: str, recorded: str) -> tuple[str, str]:
        pyproject = PYPROJECT.replace(
            quartz, json.dumps(f"pyobjc-framework-Quartz>=10.0; {spelled}")
        )
        assert pyproject != PYPROJECT
        return pyproject, LOCK.replace(recorded_line, "marker = " + json.dumps(recorded))

    for spelled, recorded in (
        ("os_name != sys_platform", ""),
        ("sys_platform == 'darwin' and os_name != sys_platform", "sys_platform == 'darwin'"),
        ("os_name != sys_platform or sys_platform == 'darwin'", "sys_platform == 'darwin'"),
    ):
        code, message = _run(tmp_path, *pair(spelled, recorded))
        assert code == EXIT_OK, (spelled, message)
    # CONTROL: the surviving clause is still compared, so a lock recorded WITHOUT it
    # (or with another platform) is stale.
    for spelled, recorded in (
        ("sys_platform == 'darwin' and os_name != sys_platform", ""),
        ("os_name != sys_platform or sys_platform == 'darwin'", "sys_platform == 'linux'"),
    ):
        code, message = _run(tmp_path, *pair(spelled, recorded))
        assert code == EXIT_STALE, (spelled, message)


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


def test_a_compatible_marker_matches_the_wildcard_uv_writes_for_it(tmp_path: Path) -> None:
    """if pyproject.toml says `python_full_version ~= '3.10.0'` and uv recorded
    `== '3.10.*'` (which it does) then 0; the next minor's wildcard is still stale
    (Codex P2 on #3763, round 11)"""
    code, message = _run(
        tmp_path,
        *_with_marker("python_full_version ~= '3.10.0'", "python_full_version == '3.10.*'"),
    )
    assert code == EXIT_OK, message
    code, message = _run(
        tmp_path,
        *_with_marker("python_full_version ~= '3.10.0'", "python_full_version == '3.11.*'"),
    )
    assert code == EXIT_STALE, message


def test_an_at_sign_inside_a_marker_literal_is_not_a_url_requirement(tmp_path: Path) -> None:
    """if pyproject.toml says `os_name == 'a@b'` and uv recorded it verbatim (which it
    does) then 0, not UNKNOWN; a different literal is stale; a real `name @ url` is
    still UNKNOWN (Codex P2 on #3763, round 11)"""
    code, message = _run(tmp_path, *_with_marker("os_name == 'a@b'", "os_name == 'a@b'"))
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, *_with_marker("os_name == 'a@b'", "os_name == 'a@c'"))
    assert code == EXIT_STALE, message


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


LOCALDEP_TOML = '[project]\nname = "localdep"\nversion = "0.1.0"\n'


def _with_source(
    tmp_path: Path,
    source_line: str | None,
    lock_entry: str,
    target_toml: str | None = LOCALDEP_TOML,
) -> tuple[str, str]:
    """The fixture pair with numpy replaced by a local `localdep` requirement: the
    pyproject side gets `[tool.uv.sources]` (or none), the lock side the entry given,
    and `tmp_path/localdep` the target's pyproject (None: no target directory)."""
    if target_toml is not None:
        for name in ("localdep", "elsewhere"):
            (tmp_path / name).mkdir(exist_ok=True)
            (tmp_path / name / "pyproject.toml").write_text(target_toml, encoding="utf-8")
    pyproject = PYPROJECT.replace('"numpy>=1.26"', '"localdep"')
    if source_line is not None:
        pyproject += f"\n[tool.uv.sources]\nlocaldep = {source_line}\n"
    lock = LOCK.replace('{ name = "numpy", specifier = ">=1.26" }', lock_entry)
    assert pyproject != PYPROJECT and lock != LOCK
    return pyproject, lock


@pytest.mark.parametrize(
    ("source_line", "lock_entry", "code"),
    [
        # Measured with uv 0.8.17: the forms uv records for a path source.
        ('{ path = "./localdep/" }', '{ name = "localdep", directory = "localdep" }', EXIT_OK),
        (
            '{ path = "localdep", editable = true }',
            '{ name = "localdep", editable = "localdep" }',
            EXIT_OK,
        ),
        (
            '{ path = "localdep", package = false }',
            '{ name = "localdep", virtual = "localdep" }',
            EXIT_OK,
        ),
        # A source edited without `uv lock` is stale: path swapped, editability
        # flipped, package-ness flipped, source removed, source added.
        ('{ path = "elsewhere" }', '{ name = "localdep", directory = "localdep" }', EXIT_STALE),
        (
            '{ path = "localdep", editable = true }',
            '{ name = "localdep", directory = "localdep" }',
            EXIT_STALE,
        ),
        (
            '{ path = "localdep", package = false }',
            '{ name = "localdep", directory = "localdep" }',
            EXIT_STALE,
        ),
        (None, '{ name = "localdep", directory = "localdep" }', EXIT_STALE),
        ('{ path = "localdep" }', '{ name = "localdep" }', EXIT_STALE),
        # Sources this check does not model are UNKNOWN, never a verdict.
        ('{ git = "https://example.test/x.git" }', '{ name = "localdep" }', EXIT_UNKNOWN),
        ("{ workspace = true }", '{ name = "localdep", editable = "localdep" }', EXIT_UNKNOWN),
        (
            None,
            '{ name = "localdep", git = "https://example.test/x.git?tag=1" }',
            EXIT_UNKNOWN,
        ),
        (None, '{ name = "localdep", index = "https://pypi.org/simple" }', EXIT_UNKNOWN),
    ],
)
def test_a_path_source_is_compared_with_the_form_uv_records(
    tmp_path: Path, source_line: str | None, lock_entry: str, code: int
) -> None:
    """if `[tool.uv.sources]` names a path source and uv recorded it (directory,
    editable or virtual, which it does) then 0; a source changed without `uv lock` is
    1; a git, url, index or workspace source is 2, not a verdict (Codex P2 on #3763,
    round 13)"""
    got, message = _run(tmp_path, *_with_source(tmp_path, source_line, lock_entry))
    assert got == code, (source_line, lock_entry, message)


def test_a_path_source_is_predicted_from_the_target_and_the_project_dir(tmp_path: Path) -> None:
    """if the source path is absolute then it compares as uv records it, relative to the
    project; if the target's own `[tool.uv] package = false` then uv records `virtual`
    whatever the source says; a target that cannot be read is UNKNOWN, not a verdict
    (Codex P2 on #3763, round 14)"""
    absolute = json.dumps(str(tmp_path / "localdep"))
    entry = '{ name = "localdep", directory = "localdep" }'
    got, message = _run(tmp_path, *_with_source(tmp_path, f"{{ path = {absolute} }}", entry))
    assert got == EXIT_OK, message
    # An absolute path to ANOTHER directory is still a difference.
    other = json.dumps(str(tmp_path / "elsewhere"))
    got, message = _run(tmp_path, *_with_source(tmp_path, f"{{ path = {other} }}", entry))
    assert got == EXIT_STALE, message
    # The target's own package = false makes it virtual; the source need not say so.
    virtual_target = LOCALDEP_TOML + "[tool.uv]\npackage = false\n"
    got, message = _run(
        tmp_path,
        *_with_source(
            tmp_path,
            '{ path = "localdep" }',
            '{ name = "localdep", virtual = "localdep" }',
            virtual_target,
        ),
    )
    assert got == EXIT_OK, message
    got, message = _run(
        tmp_path, *_with_source(tmp_path, '{ path = "localdep" }', entry, virtual_target)
    )
    assert got == EXIT_STALE, message
    # A target without a build system or without [project] is still `directory` (measured).
    got, message = _run(tmp_path, *_with_source(tmp_path, '{ path = "localdep" }', entry, ""))
    assert got == EXIT_OK, message
    # No such directory: the form cannot be inferred, so UNKNOWN.
    got, message = _run(tmp_path, *_with_source(tmp_path, '{ path = "missing" }', entry, None))
    assert got == EXIT_UNKNOWN, message


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
