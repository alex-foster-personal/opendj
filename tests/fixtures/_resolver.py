"""Fixture path resolver with external-host support (LaCie).

Fixtures under ``tests/fixtures/`` can be stored in one of three ways:

    1. Committed in-repo as a directory -- small fixtures (<100MB)
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
    2. ``<FIXTURES_ROOT>/<name>`` exists as a symlink resolving to a
       real directory  →  return it. Checked before the ``.extern``
       marker so a contributor's ad-hoc symlink works even when a
       marker for the same name is also committed and its default
       external host isn't mounted.
    3. ``<FIXTURES_ROOT>/<name>.extern`` exists          →  read the
       one-line subpath, resolve to ``<external_host>/<subpath>``,
       return that directory (or raise ``FixtureNotAvailable`` if the
       host directory is missing).
    4. ``<FIXTURES_ROOT>/<name>`` exists as a symlink but its target is
       missing, and no marker matched  →  raise
       :class:`FixtureNotAvailable`.
    5. Otherwise raise :class:`FileNotFoundError`.

The resolver is intentionally small and has zero test-framework
dependency: it can be imported from anywhere in the repo, not only
under pytest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from apps.shared.hashing import sha256_file

__all__ = [
    "DEFAULT_EXTERNAL_HOST",
    "FIXTURES_ROOT",
    "REPO_ROOT",
    "FixtureContractMismatch",
    "FixtureNotAvailable",
    "external_host",
    "fixture_path",
    "verify_fixture_contract",
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
    hard failure -- cloning the repo on a machine without LaCie must not
    break the suite.
    """


class FixtureContractMismatch(Exception):
    """Raised when a resolved fixture's content does not match its contract.

    Always a hard failure for every caller, pytest or not: this is a
    data-integrity problem (stale, regenerated, or partially copied
    ``MUX_FIXTURE_HOST`` tree), never an availability problem, so nothing
    should catch it and fall back to "missing" behavior.
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
    # 1. Plain directory (not a symlink).
    if direct.is_dir() and not direct.is_symlink():
        return direct.resolve()

    # 2. Symlink resolving to a real directory. Checked before the
    #    ``.extern`` marker below: a marker for this name is usually also
    #    committed, and would otherwise shadow a contributor's ad-hoc
    #    symlink whenever the default external host isn't mounted.
    if direct.is_symlink():
        resolved = direct.resolve()
        if resolved.is_dir():
            return resolved

    # 3. ``.extern`` marker.
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

    # 4. Symlink exists but its target is missing, and no marker matched.
    if direct.is_symlink():
        raise FixtureNotAvailable(
            f"Symlinked fixture target missing: {direct} -> {direct.resolve()}"
        )

    raise FileNotFoundError(
        f"Fixture {name!r} not found under {FIXTURES_ROOT}. "
        f"Expected one of: a directory, a '{name}.extern' marker, or a "
        f"symlink."
    )


def verify_fixture_contract(name: str, root: Path) -> None:
    """Verify every file in a resolved fixture tree against its contract.

    Reads ``tests/fixtures/<name>.contract.json`` (built by
    ``scripts/make_usb_fixture_contract.py``) when one exists and raises
    :class:`FixtureContractMismatch` on any mismatch: wrong contract
    identity, a missing or extra file, or a checksum mismatch. Framework
    agnostic, like the rest of this module, so both pytest callers (which
    translate the exception into ``pytest.fail``) and plain scripts (which
    let it propagate as a normal hard crash) can share one check rather than
    duplicating it (AGENTS.md: "Verify canonical fixtures by version,
    manifest, and checksum before use").

    A fixture name with no ``.contract.json`` on disk is left unverified
    (returns immediately) rather than treated as an error, so this can be
    adopted fixture-by-fixture.

    Not cached: AGENTS.md requires verification "before use", every use, and
    a process-lifetime cache would hand out a stale "verified" result if the
    fixture tree changed on disk after the first call -- exactly the
    mutation this check exists to catch (PR #718 review).
    """
    contract_path = FIXTURES_ROOT / f"{name}.contract.json"
    if not contract_path.is_file():
        return
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("contract_version") != 1 or contract.get("fixture") != name:
        raise FixtureContractMismatch(
            f"Unsupported fixture contract at {contract_path}: expected "
            f"contract_version=1, fixture={name!r}, got "
            f"contract_version={contract.get('contract_version')!r}, "
            f"fixture={contract.get('fixture')!r}. A contract from an "
            "unsupported revision must not be trusted just because it "
            "carries the same key."
        )
    expected_files: dict[str, str] = contract["files"]
    actual_paths = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    expected_names = set(expected_files)
    missing = sorted(expected_names - actual_paths)
    extra = sorted(actual_paths - expected_names)
    if missing or extra:
        raise FixtureContractMismatch(
            f"Fixture {name!r} at {root} does not match its contract "
            f"{contract_path}: missing={missing!r}, extra={extra!r}. A "
            "stale, regenerated, or partially copied MUX_FIXTURE_HOST tree "
            "must fail closed rather than be accepted unchanged."
        )
    mismatched = sorted(
        relpath
        for relpath, expected_digest in expected_files.items()
        if sha256_file(root / relpath) != expected_digest
    )
    if mismatched:
        raise FixtureContractMismatch(
            f"Fixture {name!r} at {root} failed checksum verification "
            f"against {contract_path} for: {mismatched!r}. A stale or "
            "corrupted fixture tree must fail closed rather than be "
            "accepted unchanged."
        )
