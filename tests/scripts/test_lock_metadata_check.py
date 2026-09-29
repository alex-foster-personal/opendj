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

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN, check

BUILD_SYSTEM = """
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
"""

# A [build-system] makes the project a PACKAGE, which is why the lock below records
# it as `editable = "."` (uv records a project without one as `virtual = "."`).
PYPROJECT = (
    BUILD_SYSTEM
    + """
[project]
name = "Demo_Project"
version = "0.1.0"
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
)

LOCK = """
version = 1
requires-python = ">=3.11"

[[package]]
name = "numpy"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

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
provides-extras = ["dev", "all"]
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


def test_requires_python_removed_without_uv_lock_is_never_clean(tmp_path: Path) -> None:
    """if pyproject.toml drops requires-python and the lock still carries `>=3.11` then
    UNKNOWN, never 0: that is also the default uv fills in from a 3.11 interpreter,
    which this check does not see (tests/scripts/test_lock_metadata_defaults.py)"""
    code, message = _run(tmp_path, PYPROJECT.replace('requires-python = ">=3.11"\n', ""))
    assert code == EXIT_UNKNOWN
    assert "omits requires-python" in message and "'>=3.11'" in message


def test_an_extra_named_with_underscores_matches_its_normalized_marker(tmp_path: Path) -> None:
    """if the optional-dependency key is `foo_bar` then uv's `extra == 'foo-bar'` marker matches"""
    pyproject = PYPROJECT.replace("dev = [", "dev_tools = [").replace("[dev]", "[dev_tools]")
    lock = (
        LOCK.replace("extra == 'dev'", "extra == 'dev-tools'")
        .replace('extras = ["dev"]', 'extras = ["dev-tools"]')
        .replace('provides-extras = ["dev", "all"]', 'provides-extras = ["dev-tools", "all"]')
    )
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
    """uv keeps a dependency's `~=` verbatim (`six~=1.16` records `~=1.16`), and
    `uv lock --check` fails when the SPELLING changes between `~=1.3` and `>=1.3,<2` in
    either direction (measured uv 0.8.17, Codex P2 on #3763, round 21): the same
    spelling (a noncanonical operand included) is 0, the equivalent bounds form is 1."""
    compatible = PYPROJECT.replace('"numpy>=1.26"', '"numpy~=1.26"')
    lock_compatible = LOCK.replace('specifier = ">=1.26"', 'specifier = "~=1.26"')
    lock_bounds = LOCK.replace('specifier = ">=1.26"', 'specifier = ">=1.26,<2"')
    for pyproject, lock, expected in (
        (compatible, lock_compatible, EXIT_OK),
        (PYPROJECT.replace('"numpy>=1.26"', '"numpy~=01.26"'), lock_compatible, EXIT_OK),
        (compatible, lock_bounds, EXIT_STALE),
        (PYPROJECT.replace('"numpy>=1.26"', '"numpy>=1.26,<2"'), lock_compatible, EXIT_STALE),
    ):
        code, message = _run(tmp_path, pyproject, lock)
        assert code == expected, (pyproject[pyproject.index("numpy") :][:20], message)


def test_a_dependency_free_project_matches_a_lock_with_no_requires_dist(tmp_path: Path) -> None:
    """if the project declares no dependencies and no extras then uv writes no
    `requires-dist` (and no `[package.metadata]`) at all, and `uv lock --check` passes:
    0, not UNKNOWN; the same lock under a pyproject.toml that does declare dependencies
    is stale (`uv lock --check` exits 1), not UNKNOWN (measured uv 0.8.17, round 21)"""
    start, end = LOCK.index("[package.metadata]"), LOCK.index('provides-extras = ["dev", "all"]')
    bare_lock = LOCK[:start] + LOCK[end + len('provides-extras = ["dev", "all"]\n') :]
    assert "requires-dist" not in bare_lock and "provides-extras" not in bare_lock
    start, end = PYPROJECT.index("dependencies = ["), PYPROJECT.index('all = ["Demo_Project[dev]"]')
    bare_pyproject = (
        PYPROJECT[:start]
        + "dependencies = []\n"
        + PYPROJECT[end + len('all = ["Demo_Project[dev]"]') :]
    )
    assert "optional-dependencies" not in bare_pyproject
    code, message = _run(tmp_path, bare_pyproject, bare_lock)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, PYPROJECT, bare_lock)
    assert code == EXIT_STALE, message
    assert "numpy" in message


def test_a_requires_python_bound_deeper_than_four_components_is_compared_not_unknown(
    tmp_path: Path,
) -> None:
    """if requires-python is `>=3.10,>=3.11.15,<3.11.15.0.0.0.0.1` (valid; uv records
    `>=3.11.15, <3.11.15.0.0.0.0.1` and `uv lock --check` passes, measured uv 0.8.17)
    then 0: the only release strictly between the bounds needs more than four zero
    components, and the probe finds it rather than reporting UNKNOWN (Codex P2 on
    #3763, round 21); a lock recording a different deep bound is stale"""
    pyproject = PYPROJECT.replace(
        'requires-python = ">=3.11"', 'requires-python = ">=3.10,>=3.11.15,<3.11.15.0.0.0.0.1"'
    )
    lock = LOCK.replace(
        'requires-python = ">=3.11"', 'requires-python = ">=3.11.15, <3.11.15.0.0.0.0.1"'
    )
    assert pyproject != PYPROJECT and lock != LOCK
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, pyproject, lock.replace("0.0.0.0.1", "0.0.0.0.2"))
    assert code == EXIT_STALE, message


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
        return pyproject, LOCK.replace(  # uv writes a dropped marker as no key (round 38)
            ", " + recorded_line, ", marker = " + json.dumps(recorded) if recorded else ""
        )

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
    # uv writes a requirement whose marker it dropped WITHOUT the key: `marker = ""`
    # is "Expected marker value", `uv lock --check` exit 2 (round 38).
    assert LOCK.count(", " + recorded) == LOCK.count(recorded)
    lock = LOCK.replace(
        ", " + recorded, ", marker = " + json.dumps(lock_marker) if lock_marker else ""
    )
    assert pyproject != PYPROJECT
    assert lock != LOCK or lock_marker == "sys_platform == 'darwin'", "no-op lock edit"
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


def test_an_extra_added_or_removed_without_uv_lock_is_stale_even_when_empty(
    tmp_path: Path,
) -> None:
    """if pyproject.toml declares `Empty_Group = []` and uv.lock's provides-extras does
    not name it (uv records every declared extra there, normalized, and `uv lock --check`
    rejects the stale lock even though an empty group adds no requires-dist entry) then 1
    naming both lists; the lock uv writes for it reads 0; the group removed again against
    that lock is stale the same way (Codex P2 on #3763, round 16)"""
    added = PYPROJECT.replace(
        'all = ["Demo_Project[dev]"]', 'all = ["Demo_Project[dev]"]\nEmpty_Group = []'
    )
    assert added != PYPROJECT
    code, message = _run(tmp_path, added)
    assert code == EXIT_STALE, message
    assert "provides-extras" in message and "empty-group" in message
    relocked = LOCK.replace(
        'provides-extras = ["dev", "all"]', 'provides-extras = ["dev", "all", "empty-group"]'
    )
    assert relocked != LOCK
    code, message = _run(tmp_path, added, relocked)
    assert code == EXIT_OK, message
    code, message = _run(tmp_path, PYPROJECT, relocked)
    assert code == EXIT_STALE, message
    assert "empty-group" in message


def test_a_project_without_extras_matches_a_lock_without_provides_extras(tmp_path: Path) -> None:
    """uv writes no provides-extras key for a project that declares no extras (measured),
    so that pair is 0, not a stale list against a missing one."""
    pyproject = PYPROJECT.split("[project.optional-dependencies]", maxsplit=1)[0]
    lock = LOCK.replace('provides-extras = ["dev", "all"]\n', "")
    for entry in (
        '    { name = "demo-project", extras = ["dev"], marker = "extra == \'all\'" },\n',
        '    { name = "pytest", marker = "extra == \'dev\'", specifier = ">=9,<10" },\n',
        '    { name = "python-rtmidi", marker = "sys_platform != \'win32\' and extra == \'dev\'", specifier = ">=1.5,<2" },\n',
    ):
        assert entry in lock
        lock = lock.replace(entry, "")
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_OK, message
    assert "3 requirements" in message


def test_a_disjunctive_marker_under_an_extra_matches_the_distributed_form_uv_writes(
    tmp_path: Path,
) -> None:
    """if pyproject.toml lists `dep; a or b` under extra `dev` and uv recorded
    `(a and extra == 'dev') or (b and extra == 'dev')` (which it does: the extra guards
    BOTH branches) then 0; a record guarding only one branch means something else and is
    stale (Codex P2 on #3763, round 16)"""
    spelled = "\"python-rtmidi>=1.5,<2; os_name == 'posix' or sys_platform == 'win32'\""
    pyproject = PYPROJECT.replace("\"python-rtmidi>=1.5,<2; sys_platform != 'win32'\"", spelled)
    assert pyproject != PYPROJECT
    recorded = "marker = \"sys_platform != 'win32' and extra == 'dev'\""
    assert recorded in LOCK
    both = "(os_name == 'posix' and extra == 'dev') or (sys_platform == 'win32' and extra == 'dev')"
    code, message = _run(tmp_path, pyproject, LOCK.replace(recorded, f'marker = "{both}"'))
    assert code == EXIT_OK, message
    one = "os_name == 'posix' or (sys_platform == 'win32' and extra == 'dev')"
    code, message = _run(tmp_path, pyproject, LOCK.replace(recorded, f'marker = "{one}"'))
    assert code == EXIT_STALE, message


def test_a_wildcard_uv_cannot_compare_matches_the_lock_uv_writes_without_it(
    tmp_path: Path,
) -> None:
    """if pyproject.toml puts the wildcard where it is not a PEP 440 comparison
    (`'3.11.*' == python_full_version`, `python_full_version < '3.11.*'`) and uv erased
    the clause from the record (which it does: alone, in a conjunction and in a
    disjunction alike) then 0, never a crash; the wildcard equality uv keeps is still
    compared, so a record without it is stale (Codex P2 on #3763, round 16)"""
    for spelled, recorded in (
        ("'3.11.*' == python_full_version", ""),
        ("'3.11.*' != python_full_version", ""),
        ("python_full_version < '3.11.*'", ""),
        ("python_full_version ~= '3.11.*'", ""),
        (
            "'3.11.*' == python_full_version and sys_platform == 'darwin'",
            "sys_platform == 'darwin'",
        ),
        ("'3.11.*' == python_full_version or sys_platform == 'darwin'", "sys_platform == 'darwin'"),
    ):
        code, message = _run(tmp_path, *_with_marker(spelled, recorded))
        assert code == EXIT_OK, (spelled, message)
    for spelled, recorded in (
        ("python_full_version == '3.11.*'", ""),
        ("python_full_version != '3.11.*'", ""),
        ("'3.11.*' == python_full_version and sys_platform == 'darwin'", ""),
        ("os_name == '3.11.*'", ""),
    ):
        code, message = _run(tmp_path, *_with_marker(spelled, recorded))
        assert code == EXIT_STALE, (spelled, message)


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


def test_a_compatible_release_on_a_string_variable_matches_the_lock_uv_writes_without_it(
    tmp_path: Path,
) -> None:
    """if pyproject.toml puts `~=` on a string variable (`os_name ~= 'posix'`) and uv
    erased the clause from the record (which it does: alone, in a conjunction and in a
    disjunction alike, measured round 20) then 0, never stale; the literal-left form
    uv cannot lock at all is UNKNOWN (Codex P2 on #3763, round 20)"""
    for spelled, recorded in (
        ("os_name ~= 'posix'", ""),
        ("os_name ~= 'posix' and sys_platform == 'darwin'", "sys_platform == 'darwin'"),
        ("sys_platform == 'darwin' or os_name ~= 'posix'", "sys_platform == 'darwin'"),
    ):
        code, message = _run(tmp_path, *_with_marker(spelled, recorded))
        assert code == EXIT_OK, (spelled, message)
    code, message = _run(tmp_path, *_with_marker("'posix' ~= os_name", ""))
    assert code == EXIT_UNKNOWN, message
