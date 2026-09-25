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


@pytest.mark.parametrize(
    ("entry", "named"),
    [
        ('path = "x", extra = "bad space"', "'bad space' is not a valid package or extra name"),
        ('path = "x", extra = "-bad"', "'-bad' is not a valid package or extra name"),
        ('path = "x", group = "b@d"', "'b@d' is not a valid package or extra name"),
        ('path = "x", extra = "café"', "is not a valid package or extra name"),
        ('path = "x", extra = ""', "is not a valid package or extra name"),
        ('path = "x", extra = "notdeclared"', "the 'notdeclared' extra does not exist"),
        ('path = "x", group = "notdeclared"', "the 'notdeclared' group does not exist"),
        ('path = "x", extra = "1"', "'unused' is not listed under that extra"),
        ('path = "x", group = "foo"', "'unused' is not listed under that group"),
        ('path = "x", group = "Foo"', "'unused' is not listed under that group"),
    ],
    ids=[
        "extra-space",
        "extra-leading-dash",
        "group-at-sign",
        "extra-non-ascii",
        "extra-empty",
        "extra-missing",
        "group-missing",
        "extra-without-package",
        "group-without-package",
        "group-without-package-spelled-differently",
    ],
)
def test_a_scoped_source_naming_no_listing_extra_or_group_is_unknown(
    tmp_path: Path, entry: str, named: str
) -> None:
    """Round 53: uv 0.8.17 refuses each (an invalid name at parse, "Not a valid package
    or extra name", exit 2; a valid one whose extra/group is absent or does not list the
    package at metadata generation, exit 2), measured on an entry no requirement uses,
    so the check cannot answer clean on the unchanged lock."""
    code, message = _run_sources(tmp_path, _with(entry))
    assert code == EXIT_UNKNOWN, message
    assert named in message, message
    assert "[tool.uv.sources] unused" in message or "is not a valid" in message, message


def test_a_scoped_source_listed_in_a_list_entry_is_checked_too(tmp_path: Path) -> None:
    """Round 53: uv 0.8.17 checks every entry of a source LIST (`[{ path, extra = "dev"
    }, { path }]` with the package only in `dependencies` is refused, measured); the
    second, plain entry does not excuse the first."""
    scoped = 'localdep = [{ path = "dep", extra = "1" }, { path = "dep" }]'
    pyproject = MINI_PYPROJECT.replace('localdep = { path = "dep" }', scoped)
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "'localdep' is not listed under that extra" in message, message


@pytest.mark.parametrize(
    ("declared", "scope"),
    [
        ('"1" = []', 'extra = "1"'),
        ('Dev_X = ["LocalDep"]', 'extra = "dev-x"'),
        ("dev-x = [\"localdep[x]; python_version > '3'\"]", 'extra = "Dev_X"'),
    ],
    ids=["extra-exact", "extra-normalized-both-sides", "extra-with-extras-and-marker"],
)
def test_an_extra_scoped_source_the_extra_lists_passes_the_scope_check(
    tmp_path: Path, declared: str, scope: str
) -> None:
    """CONTROLS: uv 0.8.17 reads each (exit 1, the moved requirement changes the lock,
    measured round 53), names matched normalized on both sides and a requirement's
    extras and marker ignored; the verdict is the existing refusal to compare a used
    source scoped by extra, never the round-53 scope refusal."""
    pyproject = (
        MINI_PYPROJECT.replace('dependencies = ["localdep"]', "dependencies = []")
        .replace('"1" = []', declared.replace('"1" = []', '"1" = ["localdep"]'))
        .replace('localdep = { path = "dep" }', f'localdep = {{ path = "dep", {scope} }}')
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "has keys not compared" in message, message
    assert "does not exist" not in message and "not listed" not in message, message
    assert "is not a valid" not in message, message


CHAIN = (
    'base = ["localdep"]',
    'mid = [{include-group = "base"}]',
    'dev = [{include-group = "mid"}]',
)


def test_a_group_scoped_source_reached_through_include_group_passes_the_scope_check(
    tmp_path: Path,
) -> None:
    """CONTROL: `group = "dev"` where `dev` reaches `localdep` only through a two-step
    `include-group` chain is read by uv 0.8.17 (exit 1, measured round 53)."""
    pyproject = (
        MINI_PYPROJECT.replace('dependencies = ["localdep"]', "dependencies = []")
        .replace("foo = []", "\n".join(CHAIN))
        .replace('localdep = { path = "dep" }', 'localdep = { path = "dep", group = "dev" }')
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "has keys not compared" in message, message
    assert "does not exist" not in message and "not listed" not in message, message


L = "marker = \"sys_platform == 'linux'\""
D = "marker = \"sys_platform == 'darwin'\""
PY = "marker = \"python_version < '3.12'\""
W = "marker = \"sys_platform == 'win32'\""
NT = "marker = \"os_name == 'nt'\""
POSIX = "marker = \"os_name == 'posix'\""
IMPL = "marker = \"implementation_name == 'cpython'\""


def _list(entries: str) -> str:
    assert MINI_PYPROJECT.endswith('localdep = { path = "dep" }\n')
    return MINI_PYPROJECT + f"unused = [{entries}]\n"


@pytest.mark.parametrize(
    ("entries", "named"),
    [
        ('{ path = "x" }, { path = "y" }', "each source must include a platform marker"),
        ('{ path = "x" }, { path = "x" }', "each source must include a platform marker"),
        (f'{{ path = "x", {L} }}, {{ path = "y" }}', "each source must include a platform marker"),
        (f'{{ path = "x", {L} }}, {{ path = "y", {L} }}', "must be disjoint"),
        (
            f'{{ path = "x", {L} }}, {{ path = "y", marker = "sys_platform==\'linux\'" }}',
            "must be disjoint",
        ),
        (f'{{ path = "x", {L} }}, {{ path = "y", {PY} }}', "must be disjoint"),
        (
            f'{{ path = "x", {L} }}, {{ path = "y", {D} }}, {{ path = "z", {PY} }}',
            "must be disjoint",
        ),
        (
            f'{{ path = "x", {L} }}, '
            "{ path = \"y\", marker = \"sys_platform == 'linux' and python_version < '3.12'\" }",
            "must be disjoint",
        ),
        (
            '{ path = "x", marker = "python_version < \'3.11\'" }, '
            '{ path = "y", marker = "python_version < \'3.10\'" }',
            "must be disjoint",
        ),
        (
            f'{{ path = "x", marker = "python_version < \'3.11\'" }}, {{ path = "y", {L} }}',
            "must be disjoint",
        ),
        (
            f'{{ path = "x", marker = "platform_system == \'Linux\'" }}, {{ path = "y", {L} }}',
            "must be disjoint",
        ),
        (
            "{ path = \"x\", marker = \"sys_platform == 'linux' or sys_platform == 'darwin'\" }, "
            f'{{ path = "y", {D} }}',
            "must be disjoint",
        ),
        (
            f'{{ path = "x", {IMPL} }}, {{ path = "y", {L} }}',
            "must be disjoint",
        ),
        (
            '{ path = "x", marker = "extra == \'a\'" }, { path = "y", marker = "extra == \'b\'" }',
            "name an extra or group; their overlap is not compared",
        ),
    ],
    ids=[
        "two-unmarked",
        "two-unmarked-same-path",
        "one-unmarked",
        "same-marker",
        "same-marker-respelled",
        "linux-and-python",
        "third-overlaps",
        "subset",
        "both-empty-under-requires-python",
        "empty-and-linux",
        "platform-system-alias",
        "or-marker-overlaps",
        "implementation-and-platform",
        "extra-markers",
    ],
)
def test_a_source_list_uv_refuses_at_list_level_is_unknown(
    tmp_path: Path, entries: str, named: str
) -> None:
    """Round 56: uv 0.8.17 refuses each list at parse (exit 2) on a rule no single entry
    shows: every entry in a shared scope needs a marker, and the markers must be
    pairwise disjoint under uv's marker algebra; `extra == ...` markers overlap for uv
    (extras are a set) and are UNKNOWN here as not modeled, never clean."""
    code, message = _run_sources(tmp_path, _list(entries))
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


@pytest.mark.parametrize(
    "entries",
    [
        f'{{ path = "x", {L} }}, {{ path = "y", {D} }}',
        f'{{ path = "x", {L} }}, {{ path = "x", {D} }}',
        f'{{ path = "x", {L} }}, {{ path = "y", marker = "sys_platform != \'linux\'" }}',
        f'{{ path = "x", {L} }}',
        f'{{ path = "x", {PY} }}, {{ path = "y", marker = "python_version >= \'3.12\'" }}',
        f'{{ path = "x", marker = "python_full_version >= \'3.12\'" }}, {{ path = "y", {PY} }}',
        f'{{ git = "https://example.com/x", {L} }}, {{ path = "y", {D} }}',
        f'{{ workspace = true, {L} }}, {{ path = "y", {D} }}',
        "{ path = \"x\", marker = \"sys_platform == 'linux' and python_version < '3.12'\" }, "
        "{ path = \"y\", marker = \"sys_platform == 'linux' and python_version >= '3.12'\" }",
        f'{{ path = "x", {L} }}, {{ path = "y", {D} }}, {{ path = "z", {W} }}',
        f'{{ path = "x", {NT} }}, {{ path = "y", {L} }}',
        f'{{ path = "x", {POSIX} }}, {{ path = "y", {W} }}',
        "{ path = \"x\", marker = \"sys_platform == 'linux' or sys_platform == 'darwin'\" }, "
        "{ path = \"y\", marker = \"sys_platform != 'linux' and sys_platform != 'darwin'\" }",
    ],
    ids=[
        "linux-darwin",
        "same-path-disjoint",
        "complement",
        "single-marked",
        "python-split",
        "full-version-split",
        "mixed-kinds",
        "workspace-and-path",
        "and-split",
        "three-pairwise-disjoint",
        "os-name-vs-sys-platform",
        "posix-vs-win32",
        "or-marker-and-complement",
    ],
)
def test_a_source_list_uv_reads_keeps_the_verdict(tmp_path: Path, entries: str) -> None:
    """CONTROLS: uv 0.8.17 reads each list (exit 0, unused so the lock is unchanged,
    measured round 56), including disjointness it derives from its marker algebra
    (`os_name == 'nt'` against `sys_platform == 'linux'`, a python split across
    `python_version` and `python_full_version`)."""
    code, message = _run_sources(tmp_path, _list(entries))
    assert code == EXIT_OK, message


@pytest.mark.parametrize(
    ("entries", "named"),
    [
        (
            '{ path = "dep", extra = "1" }, { path = "dep", extra = "1" }',
            "each source must include",
        ),
        (
            '{ path = "dep", extra = "1" }, { path = "dep", extra = "1" }, { path = "dep" }',
            "each source must include",
        ),
        (
            '{ path = "dep", extra = "1" }, { path = "dep" }, { path = "dep" }',
            "each source must include",
        ),
        (
            f'{{ path = "dep", extra = "1", {L} }}, {{ path = "dep", extra = "1", {L} }}',
            "must be disjoint",
        ),
    ],
    ids=[
        "same-extra-twice",
        "same-extra-twice-beside-plain",
        "extra-beside-two-plain",
        "same-extra-same-marker",
    ],
)
def test_a_source_list_sharing_a_scope_is_checked_within_it(
    tmp_path: Path, entries: str, named: str
) -> None:
    """Round 56: the marker rule applies among entries of ONE scope (uv 0.8.17 refuses
    `extra = "dev"` twice without markers, and twice with the same marker, exit 2)."""
    pyproject = MINI_PYPROJECT.replace('"1" = []', '"1" = ["localdep"]').replace(
        'localdep = { path = "dep" }', f"localdep = [{entries}]"
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert named in message, message


def test_a_source_list_scope_is_the_normalized_extra(tmp_path: Path) -> None:
    """Round 56: `extra = "Dev"` beside `extra = "dev"` is ONE scope for uv 0.8.17
    ("each source must include a platform marker", exit 2, measured), so the scope is
    the normalized name; spelled scopes had let the pair pass as two."""
    pyproject = MINI_PYPROJECT.replace('"1" = []', 'Dev_X = ["localdep"]').replace(
        'localdep = { path = "dep" }',
        'localdep = [{ path = "dep", extra = "Dev_X" }, { path = "dep", extra = "dev-x" }]',
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "each source must include a platform marker" in message, message


@pytest.mark.parametrize(
    "entries",
    [
        '{ path = "dep", extra = "1" }, { path = "dep" }',
        '{ path = "dep", group = "foo" }, { path = "dep" }',
        '{ path = "dep", extra = "1" }, { path = "dep", group = "foo" }',
        f'{{ path = "dep", extra = "1", {L} }}, {{ path = "dep" }}',
        f'{{ path = "dep", extra = "1" }}, {{ path = "dep", {L} }}',
        f'{{ path = "dep", {L} }}, {{ path = "dep", {L}, extra = "1" }}',
        f'{{ path = "dep", extra = "1", {L} }}, {{ path = "dep", extra = "1", {D} }}',
    ],
    ids=[
        "extra-beside-plain",
        "group-beside-plain",
        "extra-beside-group",
        "extra-marked-beside-plain",
        "extra-beside-marked",
        "same-marker-different-scopes",
        "same-extra-disjoint-markers",
    ],
)
def test_a_source_list_across_scopes_needs_no_marker(tmp_path: Path, entries: str) -> None:
    """CONTROLS: uv 0.8.17 reads each (exit 1, the scoped source changes the lock,
    measured round 56): entries in different scopes need no marker and may share one;
    here the verdict is the existing refusal to compare a list source downstream, never
    the list-level refusal."""
    pyproject = (
        MINI_PYPROJECT.replace('"1" = []', '"1" = ["localdep"]')
        .replace("foo = []", 'foo = ["localdep"]')
        .replace('localdep = { path = "dep" }', f"localdep = [{entries}]")
    )
    assert pyproject != MINI_PYPROJECT
    code, message = _run_sources(tmp_path, pyproject)
    assert code == EXIT_UNKNOWN, message
    assert "is not a path source, not compared" in message, message
    assert "must include" not in message and "disjoint" not in message, message
