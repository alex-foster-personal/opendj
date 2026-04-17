"""Fixture path resolver with external-host support (LaCie).

Fixtures under ``tests/fixtures/`` can be stored in one of three ways:

    1. Committed in-repo as a directory — small fixtures (<100MB)
       live alongside the tests and are checked in.
    2. Hosted on an external drive (e.g. LaCie) and referenced via a
       ``<name>.extern`` marker file committed in the repo. The marker
       file's contents are a single-line subpath under the external
       host root.
    3. A plain symlink inside ``tests/fixtures/`` pointing at an
       out-of-tree directory (useful for ad-hoc developer setups that
       don't want a committed marker).

Tests call :func:`fixture_path` with a logical name and receive a real
:class:`pathlib.Path`. If the external host is not mounted for an
``.extern``-backed fixture, the resolver raises
:class:`FixtureNotAvailable` so callers (or the pytest helper in
``tests/fixtures/conftest.py``) can ``pytest.skip`` cleanly rather than
surface a confusing ``FileNotFoundError``.

Design notes
------------
* ``DEFAULT_EXTERNAL_HOST`` points at LaCie. The env var
  ``MUX_FIXTURE_HOST`` overrides it (handy for CI or for contributors
  who keep their big fixtures elsewhere).
* Resolution order for a given name:

    1. ``<FIXTURES_ROOT>/<name>`` exists as a directory  →  return it.
    2. ``<FIXTURES_ROOT>/<name>.extern`` exists          →  read the
       one-line subpath, resolve to ``<external_host>/<subpath>``,
       return that directory (or raise ``FixtureNotAvailable`` if the
       host directory is missing).
    3. ``<FIXTURES_ROOT>/<name>`` exists as a symlink    →  resolve
       and return.
    4. Otherwise raise :class:`FileNotFoundError`.

The resolver is intentionally small and has zero test-framework
dependency: it can be imported from anywhere in the repo, not only
under pytest.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "FIXTURES_ROOT",
    "REPO_ROOT",
    "DEFAULT_EXTERNAL_HOST",
    "FixtureNotAvailable",
    "external_host",
    "fixture_path",
]


# ``tests/fixtures/_resolver.py`` → parents[2] is the repo root.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]
FIXTURES_ROOT: Path = REPO_ROOT / "tests" / "fixtures"

# Primary external host. Overridable via ``MUX_FIXTURE_HOST``.
DEFAULT_EXTERNAL_HOST: Path = Path("/Volumes/LaCie/music-dj-tools-fixtures")


class FixtureNotAvailable(Exception):
    """Raised when a fixture's external host (or its target dir) is missing.

    Distinct from :class:`FileNotFoundError`: this means the marker is
    wired correctly but the underlying storage isn't mounted right now.
    Tests should translate this into a ``pytest.skip`` rather than a
    hard failure — cloning the repo on a machine without LaCie must not
    break the suite.
    """


def external_host() -> Path:
    """Return the external host root, honouring ``MUX_FIXTURE_HOST``."""
    return Path(os.environ.get("MUX_FIXTURE_HOST", str(DEFAULT_EXTERNAL_HOST)))


def _read_extern_marker(marker: Path) -> str:
    """Return the first non-empty, non-comment line from a marker file.

    ``.extern`` markers are plain text. We tolerate blank lines and
    ``#``-prefixed comments so contributors can annotate them, but the
    effective payload is a single relative subpath.
    """
    for raw in marker.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line
    raise FixtureNotAvailable(
        f"Marker file is empty (no subpath line): {marker}"
    )


def fixture_path(name: str) -> Path:
    """Resolve a fixture ``name`` to a concrete :class:`Path`.

    See module docstring for the full resolution order. The returned
    path is always an absolute, existing directory.

    Raises
    ------
    FixtureNotAvailable
        When an ``.extern`` marker exists but the external host (or
        target subpath) is not mounted / present.
    FileNotFoundError
        When no resolution strategy matches the name.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(
            f"Invalid fixture name {name!r}: must be a single path component."
        )

    direct = FIXTURES_ROOT / name
    # 1. Plain directory.
    if direct.is_dir() and not direct.is_symlink():
        return direct.resolve()

    # 2. ``.extern`` marker.
    marker = FIXTURES_ROOT / f"{name}.extern"
    if marker.is_file():
        subpath = _read_extern_marker(marker)
        host = external_host()
        if not host.is_dir():
            raise FixtureNotAvailable(
                f"External fixture host not mounted: {host} "
                f"(marker: {marker}). Mount the drive or override via "
                f"MUX_FIXTURE_HOST."
            )
        target = (host / subpath).resolve()
        if not target.is_dir():
            raise FixtureNotAvailable(
                f"External fixture target missing: {target} "
                f"(host is mounted at {host}, but subpath {subpath!r} "
                f"is absent). Repopulate the external host."
            )
        return target

    # 3. Symlink.
    if direct.is_symlink():
        resolved = direct.resolve()
        if not resolved.is_dir():
            raise FixtureNotAvailable(
                f"Symlinked fixture target missing: {direct} → {resolved}"
            )
        return resolved

    raise FileNotFoundError(
        f"Fixture {name!r} not found under {FIXTURES_ROOT}. "
        f"Expected one of: a directory, a '{name}.extern' marker, or a "
        f"symlink."
    )
