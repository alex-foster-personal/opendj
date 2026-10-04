"""Pure-function tests for :mod:`apps.sync.usb.pioneer.writer_onelibrary`.

[if] write_onelibrary is pointed at a path under a fixture root [then] the guard refuses it before any copy or open, [else stop].

``test_pioneer_writer_onelibrary.py`` carries ``pytest.mark.requires_darwin``,
so every assertion about the writer is skipped off macOS -- and CI runs
``ubuntu-latest`` only. That leaves ``_is_under_fixture_root`` with no
executing coverage anywhere except a developer's Mac, which matters more
than it used to: PR #718 grew it from a one-line lexical check into a
function that reads ``MUX_FIXTURE_HOST`` and walks symlink targets on
disk, and it is the guard standing between ``write_onelibrary`` and an
overwritten fixture library.

These tests need neither macOS nor sqlcipher3, so they live outside
``tests/sync/usb/`` (the required fast lane's ``--ignore=tests/sync/usb``
in ``.github/workflows/ci.yml`` would otherwise drop every assertion here
silently -- PR #941 review) and run on every PR.

They also carry no ``monkeypatch`` of module globals or ``os.environ``, and
never write into the checkout's real ``tests/fixtures/`` tree: AGENTS.md's
fail-closed test contract forbids the former for CAT-06 evidence and
requires the latter stay immutable, so every ``MUX_FIXTURE_HOST`` /
``_TESTS_FIXTURES_DIR`` scenario below runs the guard in a fresh interpreter
against a real environment and a disposable on-disk layout instead (PR #941
review).

Single-line intent, in the repo's regression style:
  - if an ordinary export path is refused then every real USB write breaks
  - if an in-repo tests/fixtures path is allowed then the writer can
    corrupt the committed fixture tree
  - if a path under the resolved external host is allowed then the same
    corruption happens on the LaCie/MUX_FIXTURE_HOST copy
  - if MUX_FIXTURE_HOST is ignored then CI and contributors lose the guard
  - if a contributor symlink target is allowed then case 3 of the
    function's own docstring is unenforced
  - if a packaged install with no tests/ tree raises instead of degrading
    then the guard breaks production, not just fixtures
  - if write_onelibrary stops calling the guard at either call site then
    a real write can overwrite a fixture even though the guard itself
    still works in isolation

Requirement: CAT-06.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.sync.usb.pioneer import writer_onelibrary

pytestmark = pytest.mark.requirement("CAT-06")

_REPO_ROOT = Path(writer_onelibrary.__file__).resolve().parents[4]
_WRITER_PACKAGE_FILES = (
    "apps/__init__.py",
    "apps/sync/__init__.py",
    "apps/sync/usb/__init__.py",
    "apps/sync/usb/pioneer/__init__.py",
    "apps/sync/usb/pioneer/onelibrary.py",
    "apps/sync/usb/pioneer/writer_onelibrary.py",
)


def _env_without_mux_fixture_host() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "MUX_FIXTURE_HOST"}


def _write_isolated_writer_onelibrary_package(root: Path) -> None:
    """Copy just the writer_onelibrary package chain into ``root``, no tests/ sibling.

    A disposable layout, never the checkout's own ``tests/fixtures/``: per
    AGENTS.md, canonical fixture sources are immutable, and a probe that
    writes into the real committed tree would race other test runs sharing
    this checkout (PR #941 review).
    """
    for rel in _WRITER_PACKAGE_FILES:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((_REPO_ROOT / rel).read_bytes())


def _run_guard_probe(target: str, *, env: dict[str, str], cwd: Path | None = None) -> bool:
    """Call ``_is_under_fixture_root(Path(target))`` in a fresh interpreter.

    A subprocess with a real, explicit environment exercises the production
    configuration path (``os.environ.get(...)``) the way a real invocation
    would; nothing in the test process itself is patched.
    """
    code = (
        "import json\n"
        "from pathlib import Path\n"
        "from apps.sync.usb.pioneer import writer_onelibrary as w\n"
        f"print(json.dumps(w._is_under_fixture_root(Path({target!r}))))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd if cwd is not None else _REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(json.loads(completed.stdout))


# -----------------------------------------------------------------------
# Ordinary destinations must stay writable
# -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "destination",
    [
        "/tmp/export/exportLibrary.db",
        "/media/usb/PIONEER/rekordbox/exportLibrary.db",
        "/Volumes/MYUSB/PIONEER/rekordbox/exportLibrary.db",
    ],
)
def test_ordinary_export_paths_are_not_fixture_roots(destination: str) -> None:
    """A real USB target must never trip the fixture guard.

    Runs with ``MUX_FIXTURE_HOST`` explicitly absent so the result cannot
    depend on whatever the invoking machine happens to have configured --
    none of these three destinations sit under the default LaCie host.
    """
    assert _run_guard_probe(destination, env=_env_without_mux_fixture_host()) is False


# -----------------------------------------------------------------------
# The three roots the guard is documented to cover
# -----------------------------------------------------------------------
def test_in_repo_fixture_tree_is_a_fixture_root() -> None:
    target = _REPO_ROOT / "tests" / "fixtures" / "rb-usb-export" / "PIONEER" / "x.db"
    assert writer_onelibrary._is_under_fixture_root(target) is True


def test_default_external_host_is_a_fixture_root() -> None:
    """LaCie is a fixture root even though its path says neither 'tests' nor 'fixtures'."""
    host = writer_onelibrary._DEFAULT_EXTERNAL_FIXTURE_HOST
    target = str(host / "rb-usb-export" / "x.db")
    assert _run_guard_probe(target, env=_env_without_mux_fixture_host()) is True


def test_mux_fixture_host_override_is_honoured(tmp_path: Path) -> None:
    host = tmp_path / "external-host"
    host.mkdir()
    env = dict(os.environ)
    env["MUX_FIXTURE_HOST"] = str(host)

    assert _run_guard_probe(str(host / "rb-usb-export" / "x.db"), env=env) is True
    # A sibling of the host, derived from the same tmp_path, is still an
    # ordinary, writable destination -- never a hardcoded path that could
    # coincide with an ambient host.
    sibling = tmp_path / "somewhere-else" / "x.db"
    assert _run_guard_probe(str(sibling), env=env) is False


def test_contributor_symlink_target_is_a_fixture_root(tmp_path: Path) -> None:
    """Case 3 of the docstring: a tests/fixtures/* symlink can point anywhere.

    Builds a disposable package layout with its own ``tests/fixtures/`` dir
    and creates the symlink there, never in the checkout's real
    ``tests/fixtures/`` (AGENTS.md: canonical fixture sources are immutable;
    PR #941 review flagged the earlier version writing into the shared
    checkout tree).
    """
    isolated_root = tmp_path / "isolated-install"
    _write_isolated_writer_onelibrary_package(isolated_root)
    fixtures_dir = isolated_root / "tests" / "fixtures"
    fixtures_dir.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = fixtures_dir / "rb-usb-export"
    # No try/except-skip here: AGENTS.md forbids silently skipping acceptance
    # because a platform capability is missing (PR #941 review). If this
    # filesystem cannot create a symlink, that is a real failure of the
    # CAT-06 acceptance case, not an UNKNOWN to be hidden behind a skip.
    link.symlink_to(target, target_is_directory=True)  # Windows: directory link type

    env = _env_without_mux_fixture_host()
    assert _run_guard_probe(
        str(target / "PIONEER" / "x.db"), env=env, cwd=isolated_root
    ) is True
    unrelated = tmp_path / "unrelated" / "x.db"
    assert _run_guard_probe(str(unrelated), env=env, cwd=isolated_root) is False


# -----------------------------------------------------------------------
# The guard must actually be wired into write_onelibrary's call sites --
# a direct _is_under_fixture_root assertion above cannot catch a regression
# that stops write_onelibrary from calling it at all (PR #941 review).
# -----------------------------------------------------------------------
def _run_write_onelibrary_probe(
    *, template_path: str, output_path: str, env: dict[str, str]
) -> dict[str, object]:
    """Call the real ``write_onelibrary`` in a fresh interpreter.

    Both paths point under a fixture root, so the guard raises before any
    SQLCipher I/O happens -- this exercises the actual call sites in
    ``write_onelibrary`` (not just the private helper), cross-platform.
    """
    code = (
        "import json\n"
        "from apps.sync.usb.pioneer import writer_onelibrary as w\n"
        "try:\n"
        f"    w.write_onelibrary(template_path={template_path!r}, output_path={output_path!r})\n"
        "except w.OneLibraryWriteError as exc:\n"
        "    print(json.dumps({'raised': True, 'message': str(exc)}))\n"
        "else:\n"
        "    print(json.dumps({'raised': False, 'message': None}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_write_onelibrary_refuses_output_under_fixture_root(tmp_path: Path) -> None:
    template = tmp_path / "template.db"
    # The output guard raises before the template is ever opened, so it
    # only needs to exist as a file, not be a valid OneLibrary DB.
    template.write_bytes(b"guard raises before this is read")
    host = tmp_path / "external-host"
    host.mkdir()
    output = host / "rb-usb-export" / "exportLibrary.db"
    env = dict(os.environ)
    env["MUX_FIXTURE_HOST"] = str(host)

    result = _run_write_onelibrary_probe(
        template_path=str(template), output_path=str(output), env=env
    )
    assert result["raised"] is True
    assert "fixture root" in str(result["message"])


def test_write_onelibrary_refuses_template_under_fixture_root(tmp_path: Path) -> None:
    host = tmp_path / "external-host"
    template = host / "rb-usb-export" / "exportLibrary.db"
    template.parent.mkdir(parents=True)
    template.write_bytes(b"fixture-backed template")
    output = tmp_path / "ordinary-output" / "exportLibrary.db"
    env = dict(os.environ)
    env["MUX_FIXTURE_HOST"] = str(host)

    result = _run_write_onelibrary_probe(
        template_path=str(template), output_path=str(output), env=env
    )
    assert result["raised"] is True
    assert "fixture root" in str(result["message"])


def test_missing_fixtures_dir_does_not_raise(tmp_path: Path) -> None:
    """A packaged install has no tests/ tree; the guard must degrade, not explode.

    Builds a real, isolated copy of just the writer_onelibrary module's package
    chain with no ``tests/`` sibling, and imports it from there in a fresh
    interpreter -- a real layout with no ``tests/fixtures/`` to find, not a
    patched ``_TESTS_FIXTURES_DIR`` constant.
    """
    isolated_root = tmp_path / "isolated-install"
    _write_isolated_writer_onelibrary_package(isolated_root)
    assert not (isolated_root / "tests").exists()

    result = _run_guard_probe(
        "/tmp/export/x.db", env=_env_without_mux_fixture_host(), cwd=isolated_root
    )
    assert result is False
