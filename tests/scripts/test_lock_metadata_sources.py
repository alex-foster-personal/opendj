"""Per-kind keys on a `[tool.uv.sources]` entry, measured on uv 0.8.17 (Codex P2 on
#3763, round 51) with an entry no requirement uses, so nothing but the parse decides:
every UNKNOWN case here is "Failed to parse: `pyproject.toml`", exit 2, and every control
is read (exit 0, or exit 1 where the entry changes the resolution). The shape check had
stopped at "one recognized kind" and let any extra key ride."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.lock_metadata_check import EXIT_OK, EXIT_UNKNOWN
from tests.scripts.test_lock_metadata_check import _run
from tests.scripts.test_lock_metadata_types import MINI_DEP_PYPROJECT, MINI_LOCK, MINI_PYPROJECT

GIT = 'git = "https://example.com/x"'
URL = 'url = "https://example.com/x.whl"'


def _run_sources(tmp_path: Path, pyproject: str) -> tuple[int, str]:
    (tmp_path / "dep").mkdir(exist_ok=True)
    (tmp_path / "dep" / "pyproject.toml").write_text(MINI_DEP_PYPROJECT, encoding="utf-8")
    return _run(tmp_path, pyproject, MINI_LOCK)


def _with(entry: str) -> str:
    assert MINI_PYPROJECT.endswith('localdep = { path = "dep" }\n')
    return MINI_PYPROJECT + f"unused = {{ {entry} }}\n"


@pytest.mark.parametrize(
    ("entry", "named"),
    [
        ('path = "x", bogus = "y"', "unknown field 'bogus'"),
        (f"{GIT}, editable = true", "cannot specify both 'git' and 'editable'"),
        (f"{GIT}, package = false", "cannot specify both 'git' and 'package'"),
        (f'{GIT}, rev = "a", tag = "b"', "at most one of rev, tag, or branch"),
        (f"{GIT}, rev = 1", "rev = 1 is not a string"),
        (f"{GIT}, subdirectory = 1", "subdirectory = 1 is not a string"),
        (f"{URL}, editable = true", "cannot specify both 'url' and 'editable'"),
        (f"{URL}, package = true", "cannot specify both 'url' and 'package'"),
        (f'{URL}, rev = "r"', "cannot specify both 'url' and 'rev'"),
        (f"{URL}, marker = 1", "marker = 1 is not a string"),
        ('path = "x", rev = "a"', "cannot specify both 'path' and 'rev'"),
        ('path = "x", tag = "t"', "cannot specify both 'path' and 'tag'"),
        ('path = "x", editable = 1', "editable = 1 is not a boolean"),
        ('path = "x", editable = true, package = false', "editable = true and package = false"),
        ('path = "x", extra = 1', "extra = 1 is not a string"),
        ('path = "x", extra = "1", group = "foo"', "cannot specify both extra and group"),
        ('index = "i", editable = true', "cannot specify both 'index' and 'editable'"),
        ('index = "i", package = false', "cannot specify both 'index' and 'package'"),
        ('index = "i", rev = "r"', "cannot specify both 'index' and 'rev'"),
        ("workspace = true, package = false", "cannot specify both 'workspace' and 'package'"),
    ],
    ids=[
        "path-bogus",
        "git-editable",
        "git-package",
        "git-rev-and-tag",
        "git-rev-int",
        "git-subdirectory-int",
        "url-editable",
        "url-package",
        "url-rev",
        "url-marker-int",
        "path-rev",
        "path-tag",
        "path-editable-int",
        "path-editable-package-conflict",
        "path-extra-int",
        "path-extra-and-group",
        "index-editable",
        "index-package",
        "index-rev",
        "workspace-package",
    ],
)
def test_a_source_key_uv_rejects_for_its_kind_is_unknown(
    tmp_path: Path, entry: str, named: str
) -> None:
    code, message = _run_sources(tmp_path, _with(entry))
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "entry",
    [
        'path = "x"',
        'path = "x", subdirectory = "s"',
        'path = "x", editable = true, package = true',
        f'{GIT}, branch = "b", subdirectory = "s"',
        f'{URL}, subdirectory = "s"',
        'index = "i", subdirectory = "s"',
        "workspace = true, editable = true",
        "workspace = false, editable = true",
        "workspace = true, marker = \"os_name == 'posix'\"",
    ],
    ids=[
        "path",
        "path-subdirectory",
        "path-editable-package",
        "git-branch-subdirectory",
        "url-subdirectory",
        "index-subdirectory",
        "workspace-editable",
        "workspace-false-editable",
        "workspace-marker",
    ],
)
def test_a_source_key_uv_reads_for_its_kind_keeps_the_verdict(tmp_path: Path, entry: str) -> None:
    """CONTROLS: each entry is read by uv 0.8.17 (exit 0, unused so the lock is unchanged)."""
    code, message = _run_sources(tmp_path, _with(entry))
    assert code == EXIT_OK, message


def test_a_group_scoped_source_parses_and_stops_downstream(tmp_path: Path) -> None:
    """CONTROL: `group = "foo"` on the requirement's own source parses (uv 0.8.17 reads
    it and reports the moved requirement as exit 1, measured, round 51); here the parse
    passes and the verdict is the existing refusal to compare a used source scoped by
    group, never a parse rejection."""
    pyproject = (
        MINI_PYPROJECT.replace('dependencies = ["localdep"]', "dependencies = []")
        .replace("foo = []", 'foo = ["localdep"]')
        .replace('localdep = { path = "dep" }', 'localdep = { path = "dep", group = "foo" }')
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "has keys not compared" in message, message
    assert "cannot specify" not in message and "unknown field" not in message, message
