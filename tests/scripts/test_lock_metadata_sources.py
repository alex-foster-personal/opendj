"""scripts/lock_metadata_check.py: a `[tool.uv.sources]` path source is compared in the
form uv records it (`directory` / `editable` / `virtual`), predicted from the source's
own flags, the target's `[tool.uv] package`, and the project directory (Codex rounds
12-15 on PR #3763). The fixture pair and `_run` live in test_lock_metadata_check.py.

- [if] the source's form and path match the lock's [then] exit 0, [else stop]
- [if] either differs [then] exit 1 naming the entry, [else stop]
- [if] the target is missing or the source is not a path source [then] exit 2 UNKNOWN
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_STALE, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import BUILD_SYSTEM, LOCK, PYPROJECT, _run

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
    # The source's own flags win over the target's package = false (measured, round 15).
    got, message = _run(
        tmp_path,
        *_with_source(tmp_path, '{ path = "localdep", package = true }', entry, virtual_target),
    )
    assert got == EXIT_OK, message
    got, message = _run(
        tmp_path,
        *_with_source(
            tmp_path,
            '{ path = "localdep", editable = true }',
            '{ name = "localdep", editable = "localdep" }',
            virtual_target,
        ),
    )
    assert got == EXIT_OK, message
    # A target without a build system or without [project] is still `directory` (measured).
    got, message = _run(tmp_path, *_with_source(tmp_path, '{ path = "localdep" }', entry, ""))
    assert got == EXIT_OK, message
    # No such directory: the form cannot be inferred, so UNKNOWN.
    got, message = _run(tmp_path, *_with_source(tmp_path, '{ path = "missing" }', entry, None))
    assert got == EXIT_UNKNOWN, message


def test_the_root_source_follows_the_build_system_and_tool_uv_package(tmp_path: Path) -> None:
    """uv records the project itself as `editable = "."` when it is a package (a
    `[build-system]`, or `[tool.uv] package = true` without one) and as `virtual = "."`
    otherwise (`package = false`, or no build-system), and `uv lock --check` rejects the
    lock after either toggle (measured, uv 0.8.17); so does this, naming the root source
    (Codex P2 on #3763, round 17)."""
    virtual_lock = LOCK.replace('source = { editable = "." }', 'source = { virtual = "." }')
    no_build = PYPROJECT.replace(BUILD_SYSTEM, "")
    assert virtual_lock != LOCK and no_build != PYPROJECT
    not_a_package = PYPROJECT + "\n[tool.uv]\npackage = false\n"
    forced_package = no_build + "\n[tool.uv]\npackage = true\n"
    for label, pyproject, lock, expected in (
        ("build-system, editable", PYPROJECT, LOCK, EXIT_OK),
        ("package = false, still editable", not_a_package, LOCK, EXIT_STALE),
        ("package = false, virtual", not_a_package, virtual_lock, EXIT_OK),
        ("no build-system, still editable", no_build, LOCK, EXIT_STALE),
        ("no build-system, virtual", no_build, virtual_lock, EXIT_OK),
        ("no build-system but package = true, editable", forced_package, LOCK, EXIT_OK),
        ("package = true, still virtual", forced_package, virtual_lock, EXIT_STALE),
        ("build-system, still virtual", PYPROJECT, virtual_lock, EXIT_STALE),
    ):
        code, message = _run(tmp_path, pyproject, lock)
        assert code == expected, (label, message)
        if expected == EXIT_STALE:
            assert "root source" in message, (label, message)


def test_a_root_recorded_from_elsewhere_is_unknown_not_a_verdict(tmp_path: Path) -> None:
    """A root source that is not `editable = "."` / `virtual = "."` (another path, a
    registry, two keys) is a lock this check does not model: UNKNOWN, never clean."""
    for source in (
        'source = { editable = "../elsewhere" }',
        'source = { registry = "https://pypi.org/simple" }',
        'source = { editable = ".", virtual = "." }',
    ):
        lock = LOCK.replace('source = { editable = "." }', source)
        assert lock != LOCK
        code, message = _run(tmp_path, PYPROJECT, lock)
        assert code == EXIT_UNKNOWN, (source, message)
        assert "root source" in message, (source, message)


def test_a_path_source_s_specifier_is_ignored_as_uv_ignores_it(tmp_path: Path) -> None:
    """`localdep>=1` with `localdep = { path = ... }`: uv records `{ name, directory }`
    with no specifier, and `uv lock --check` passes even after the specifier is edited
    (measured, uv 0.8.17: the path decides the version), so the specifier is dropped
    from the compare for a path source and only there (Codex P2 on #3763, round 18)."""
    pyproject, lock = _with_source(
        tmp_path, '{ path = "localdep" }', '{ name = "localdep", directory = "localdep" }'
    )
    for spec in (">=1", ">=2,<3", "==0.1.0"):
        pinned = pyproject.replace(
            '"localdep",', f'"localdep{spec}",'
        )  # the dependency, not the source path
        assert pinned != pyproject
        code, message = _run(tmp_path, pinned, lock)
        assert code == EXIT_OK, (spec, message)
    # CONTROL: without a path source the specifier is still compared (a registry
    # requirement's specifier is exactly what a bump without `uv lock` changes).
    unsourced = PYPROJECT.replace('"numpy>=1.26"', '"localdep>=1"')
    code, message = _run(tmp_path, unsourced, lock.replace(', directory = "localdep"', ""))
    assert code == EXIT_STALE, message
    assert "localdep>=1" in message


def test_a_non_boolean_package_or_editable_flag_is_unknown_not_a_verdict(tmp_path: Path) -> None:
    """`package = "false"` (a string) is a file uv refuses to parse (measured: `invalid
    type: string "false", expected a boolean`), on the root's `[tool.uv]`, on a source
    and on a path target alike; inferring a source from it would be a verdict about a
    configuration uv never accepted, so it is UNKNOWN (Codex P2 on #3763, round 18)."""
    code, message = _run(tmp_path, PYPROJECT + '\n[tool.uv]\npackage = "false"\n')
    assert code == EXIT_UNKNOWN, message
    assert "package" in message
    entry = '{ name = "localdep", directory = "localdep" }'
    for source_line in (
        '{ path = "localdep", package = "false" }',
        '{ path = "localdep", editable = 1 }',
    ):
        pyproject, lock = _with_source(tmp_path, source_line, entry)
        code, message = _run(tmp_path, pyproject, lock)
        assert code == EXIT_UNKNOWN, (source_line, message)
    pyproject, lock = _with_source(
        tmp_path,
        '{ path = "localdep" }',
        entry,
        target_toml=LOCALDEP_TOML + '[tool.uv]\npackage = "false"\n',
    )
    code, message = _run(tmp_path, pyproject, lock)
    assert code == EXIT_UNKNOWN, message
