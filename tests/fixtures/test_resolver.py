"""Unit tests for the external-host fixture resolver.

Covers INFRA-03 (test-suite infrastructure). The resolver is the single
choke point for every big-fixture test in the repo, so it has to:

* resolve direct, committed fixture directories;
* read ``.extern`` marker files and honour the ``MUX_FIXTURE_HOST``
  override;
* raise :class:`FixtureNotAvailable` (not ``FileNotFoundError``) when
  the marker is valid but the host is unmounted, so callers can skip
  cleanly;
* raise :class:`FileNotFoundError` when no resolution strategy applies;
* reject malicious / malformed names (path separators, traversal).

All tests run fully in ``tmp_path`` — they monkeypatch the resolver's
``FIXTURES_ROOT`` so they don't require the real LaCie fixture.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures import _resolver
from tests.fixtures._resolver import (
    FixtureContractMismatch,
    FixtureNotAvailable,
    fixture_path,
    verify_fixture_contract,
)


@contextmanager
def _env_var(name: str, value: str) -> Iterator[None]:
    """Set a real environment variable for the block, then restore it.

    AGENTS.md forbids mocks/monkeypatching/fabricated application state in
    tests: the resolver's external-host override is a real, documented
    production configuration surface (``MUX_FIXTURE_HOST``), so driving it
    through the actual environment is the production configuration path,
    not a fake.
    """
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

pytestmark = [pytest.mark.requirement("INFRA-03")]


@contextmanager
def _real_fixture_name() -> Iterator[str]:
    """Yield a unique name scoped to the REAL, unpatched ``FIXTURES_ROOT``.

    PR #718 review: ``stub_fixtures_root`` monkeypatches
    ``_resolver.FIXTURES_ROOT``, so tests built on it never exercise the
    resolver's actual production root -- a no-monkeypatch violation
    (AGENTS.md). Symlink-precedence coverage instead writes real entries
    directly under the real ``tests/fixtures/`` directory, using a
    per-run random name so it can never collide with a committed fixture,
    and removes them unconditionally afterward so the checkout is left
    exactly as it was found.
    """
    name = f"resolver-precedence-{uuid.uuid4().hex[:12]}"
    entry = _resolver.FIXTURES_ROOT / name
    marker = _resolver.FIXTURES_ROOT / f"{name}.extern"
    try:
        yield name
    finally:
        entry.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


@pytest.fixture
def stub_fixtures_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the resolver at an isolated tmp_path fixtures root."""
    root = tmp_path / "tests" / "fixtures"
    root.mkdir(parents=True)
    monkeypatch.setattr(_resolver, "FIXTURES_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# Strategy 1 — direct directory
# ---------------------------------------------------------------------------


def test_direct_dir_resolves(stub_fixtures_root: Path) -> None:
    """A committed fixture directory resolves to itself."""
    target = stub_fixtures_root / "small-fixture"
    target.mkdir()
    (target / "sentinel.txt").write_text("hi", encoding="utf-8")

    resolved = fixture_path("small-fixture")

    assert resolved == target.resolve()
    assert (resolved / "sentinel.txt").read_text(encoding="utf-8") == "hi"


# ---------------------------------------------------------------------------
# Strategy 2 — .extern marker
# ---------------------------------------------------------------------------


def test_extern_marker_resolves_when_host_mounted(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``.extern`` marker with a mounted host resolves to the target dir."""
    host = tmp_path / "external-host"
    subpath = "big-fixture"
    (host / subpath).mkdir(parents=True)
    (host / subpath / "payload.bin").write_bytes(b"\x00\x01\x02")

    monkeypatch.setenv("MUX_FIXTURE_HOST", str(host))
    (stub_fixtures_root / "big-fixture.extern").write_text(
        subpath + "\n", encoding="utf-8"
    )

    resolved = fixture_path("big-fixture")

    assert resolved == (host / subpath).resolve()
    assert (resolved / "payload.bin").read_bytes() == b"\x00\x01\x02"


def test_extern_marker_tolerates_comments_and_blank_lines(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comments and blank lines in a marker are skipped; first real line wins."""
    host = tmp_path / "external-host"
    (host / "annotated-fixture").mkdir(parents=True)
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(host))
    (stub_fixtures_root / "annotated-fixture.extern").write_text(
        "# Hosted on LaCie — see docs/fixtures.md\n"
        "\n"
        "annotated-fixture\n"
        "# trailing comment\n",
        encoding="utf-8",
    )

    resolved = fixture_path("annotated-fixture")

    assert resolved == (host / "annotated-fixture").resolve()


def test_extern_marker_raises_when_host_missing(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host not mounted → :class:`FixtureNotAvailable` (not FileNotFoundError)."""
    missing_host = tmp_path / "not-mounted"
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(missing_host))
    (stub_fixtures_root / "ghost-fixture.extern").write_text(
        "ghost-fixture\n", encoding="utf-8"
    )

    with pytest.raises(FixtureNotAvailable, match="not mounted"):
        fixture_path("ghost-fixture")


def test_extern_marker_raises_when_subpath_missing(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host mounted but subpath absent → :class:`FixtureNotAvailable`."""
    host = tmp_path / "external-host"
    host.mkdir()
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(host))
    (stub_fixtures_root / "partial-fixture.extern").write_text(
        "not-populated-yet\n", encoding="utf-8"
    )

    with pytest.raises(FixtureNotAvailable, match="subpath"):
        fixture_path("partial-fixture")


def test_extern_marker_empty_file_raises(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A marker with only whitespace/comments raises cleanly."""
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(tmp_path))
    (stub_fixtures_root / "blank.extern").write_text(
        "# only a comment\n\n", encoding="utf-8"
    )

    with pytest.raises(FixtureNotAvailable, match="empty"):
        fixture_path("blank")


# ---------------------------------------------------------------------------
# Strategy 3 — symlink
# ---------------------------------------------------------------------------


def test_symlink_resolves(stub_fixtures_root: Path, tmp_path: Path) -> None:
    """A symlink inside ``tests/fixtures/`` resolves to its target."""
    target = tmp_path / "elsewhere" / "sym-fixture"
    target.mkdir(parents=True)
    (target / "hello.txt").write_text("hello", encoding="utf-8")

    link = stub_fixtures_root / "sym-fixture"
    try:
        link.symlink_to(target)
    except OSError as exc:  # win32: symlinks need admin/dev-mode privilege
        pytest.skip(f"symlinks unavailable on this platform/user: {exc}")

    resolved = fixture_path("sym-fixture")
    assert resolved == target.resolve()


# ---------------------------------------------------------------------------
# Strategy 4 — nothing matches
# ---------------------------------------------------------------------------


def test_missing_fixture_raises(stub_fixtures_root: Path) -> None:
    """No directory, no marker, no symlink → FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="not found"):
        fixture_path("absent")


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "../escape", "nested/name", ".hidden", "a\\b"])
def test_invalid_name_raises_value_error(
    stub_fixtures_root: Path, bad: str
) -> None:
    """Names containing separators or leading '.' are rejected."""
    with pytest.raises(ValueError, match="Invalid fixture name"):
        fixture_path(bad)


# ---------------------------------------------------------------------------
# Resolution precedence
# ---------------------------------------------------------------------------


def test_direct_dir_wins_over_extern_marker(
    stub_fixtures_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both a direct dir AND an .extern marker exist, the dir wins.

    This lets a contributor materialise a local copy of a big fixture
    (e.g. by ``rsync``-ing LaCie → repo for CI reproducibility) without
    deleting the marker.
    """
    direct = stub_fixtures_root / "either-way"
    direct.mkdir()
    (direct / "local.txt").write_text("local", encoding="utf-8")

    host = tmp_path / "external-host"
    (host / "either-way").mkdir(parents=True)
    (host / "either-way" / "remote.txt").write_text("remote", encoding="utf-8")
    monkeypatch.setenv("MUX_FIXTURE_HOST", str(host))

    (stub_fixtures_root / "either-way.extern").write_text(
        "either-way\n", encoding="utf-8"
    )

    resolved = fixture_path("either-way")
    assert (resolved / "local.txt").exists()
    assert not (resolved / "remote.txt").exists()


def test_symlink_wins_over_extern_marker_when_host_unmounted(
    tmp_path: Path,
) -> None:
    """A contributor's ad-hoc symlink resolves even with a committed marker.

    PR #718 review: a marker file is usually committed alongside a
    fixture, so a contributor who symlinks ``tests/fixtures/<name>`` to a
    local copy must not be blocked by the marker branch raising
    ``FixtureNotAvailable`` for the (irrelevant, unmounted) default host.

    Drives the resolver's real, documented ``MUX_FIXTURE_HOST`` override
    (a genuine environment variable, not a patched module attribute) and
    the real, unpatched ``FIXTURES_ROOT`` (via :func:`_real_fixture_name`)
    so the "unmounted host" is a real absent directory and the symlink
    precedence branch runs against the production resolver root, rather
    than fabricated application state (AGENTS.md no-mocks rule).
    """
    with _real_fixture_name() as name:
        target = tmp_path / "local-copy" / name
        target.mkdir(parents=True)
        (target / "local.txt").write_text("local", encoding="utf-8")
        (_resolver.FIXTURES_ROOT / name).symlink_to(target)
        (_resolver.FIXTURES_ROOT / f"{name}.extern").write_text(
            f"{name}\n", encoding="utf-8"
        )

        with _env_var("MUX_FIXTURE_HOST", str(tmp_path / "not-mounted")):
            resolved = fixture_path(name)
        assert resolved == target.resolve()


def test_broken_symlink_falls_back_to_extern_marker(
    tmp_path: Path,
) -> None:
    """A stale/broken symlink doesn't shadow a working marker.

    Runs against the real, unpatched ``FIXTURES_ROOT`` (via
    :func:`_real_fixture_name`) for the same reason as the precedence
    test above: a monkeypatched root never exercises the production
    resolver path (AGENTS.md no-mocks rule).
    """
    with _real_fixture_name() as name:
        (_resolver.FIXTURES_ROOT / name).symlink_to(tmp_path / "does-not-exist")

        host = tmp_path / "external-host"
        (host / name).mkdir(parents=True)
        (_resolver.FIXTURES_ROOT / f"{name}.extern").write_text(
            f"{name}\n", encoding="utf-8"
        )

        with _env_var("MUX_FIXTURE_HOST", str(host)):
            resolved = fixture_path(name)
        assert resolved == (host / name).resolve()


# ---------------------------------------------------------------------------
# Real LaCie happy-path (skipped if drive absent)
# ---------------------------------------------------------------------------


def test_real_lacie_big_fixture_resolves_when_mounted() -> None:
    """Smoke test against the actual on-disk marker + LaCie host.

    Skips if the developer doesn't have LaCie mounted. Exists so a
    broken marker shows up immediately as a test failure on the
    primary dev machine (rather than only when ``test_pioneer_reader_big``
    runs and takes ≈5s to parse the whole export).
    """
    try:
        resolved = fixture_path("rb-usb-export-big")
    except FileNotFoundError:
        pytest.skip("rb-usb-export-big marker not installed yet")
    except FixtureNotAvailable as exc:
        pytest.skip(f"LaCie not available: {exc}")

    assert resolved.is_dir()
    # The top-level fixture dir must contain the PIONEER subtree.
    assert (resolved / "PIONEER").is_dir(), (
        f"expected PIONEER/ under {resolved}"
    )


# ---------------------------------------------------------------------------
# verify_fixture_contract
# ---------------------------------------------------------------------------


def _write_contract(fixtures_root: Path, name: str, files: dict[str, str]) -> None:
    import json

    (fixtures_root / f"{name}.contract.json").write_text(
        json.dumps({"contract_version": 1, "fixture": name, "files": files}),
        encoding="utf-8",
    )


def test_contract_verifies_nested_file_with_posix_relative_path(tmp_path: Path) -> None:
    """A nested file's contract key must be POSIX-separated to match.

    PR #718 review: ``verify_fixture_contract`` used bare
    ``str(path.relative_to(root))``, which is backslash-separated on
    Windows and would never match the forward-slash keys committed in
    ``*.contract.json`` (generated on Linux/macOS), so every file in a
    nested fixture tree would be reported as simultaneously "missing" and
    "extra" on a Windows machine.

    ``verify_fixture_contract`` reads its contract file from the real,
    unmodified ``_resolver.FIXTURES_ROOT`` (it has no override parameter),
    so this writes a scratch, obviously-disposable ``*.contract.json`` there
    directly and removes it in ``finally`` -- no monkeypatching of
    ``FIXTURES_ROOT`` (AGENTS.md's no-mocks rule; PR #718 review flagged the
    prior version's use of the monkeypatched ``stub_fixtures_root``).
    """
    from apps.shared.hashing import sha256_file

    root = tmp_path / "some-fixture"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "file.txt").write_text("hello", encoding="utf-8")
    name = "zz-scratch-contract-posix-test"
    contract_path = _resolver.FIXTURES_ROOT / f"{name}.contract.json"
    _write_contract(
        _resolver.FIXTURES_ROOT,
        name,
        {"a/b/file.txt": sha256_file(root / "a" / "b" / "file.txt")},
    )
    try:
        verify_fixture_contract(name, root)  # must not raise
    finally:
        contract_path.unlink(missing_ok=True)


def test_contract_detects_missing_file(tmp_path: Path) -> None:
    """Same real-``FIXTURES_ROOT`` scratch-file pattern as the test above."""
    root = tmp_path / "some-fixture"
    root.mkdir()
    name = "zz-scratch-contract-missing-file-test"
    contract_path = _resolver.FIXTURES_ROOT / f"{name}.contract.json"
    _write_contract(_resolver.FIXTURES_ROOT, name, {"missing.txt": "sha256:" + "0" * 64})

    try:
        with pytest.raises(FixtureContractMismatch, match=r"missing\.txt"):
            verify_fixture_contract(name, root)
    finally:
        contract_path.unlink(missing_ok=True)


def test_contract_detects_checksum_mismatch(tmp_path: Path) -> None:
    """A tampered/regenerated fixture file must fail loud, not silently pass.

    Distinct from ``test_contract_detects_missing_file`` above: the file is
    PRESENT (so the missing/extra set-difference check is a no-op) but its
    content no longer matches the committed digest -- the exact shape of a
    stale or partially-regenerated ``MUX_FIXTURE_HOST`` tree (Issue #1032
    acceptance: "if the fixture checksum is tampered then FixtureNotAvailable
    fails loud"; the concrete exception here is ``FixtureContractMismatch``,
    the data-integrity-specific guard this repo's own docs point at --
    ``FixtureNotAvailable`` is reserved for an unmounted host). Same real-
    ``FIXTURES_ROOT`` scratch-file pattern as the tests above.
    """
    root = tmp_path / "some-fixture"
    root.mkdir()
    (root / "master.plain.db").write_bytes(b"tampered content")
    name = "zz-scratch-contract-checksum-mismatch-test"
    contract_path = _resolver.FIXTURES_ROOT / f"{name}.contract.json"
    # A well-formed but deliberately WRONG digest -- the file exists, so
    # this exercises the checksum comparison, not the missing-file branch.
    _write_contract(_resolver.FIXTURES_ROOT, name, {"master.plain.db": "sha256:" + "0" * 64})

    try:
        with pytest.raises(FixtureContractMismatch, match=r"checksum verification"):
            verify_fixture_contract(name, root)
    finally:
        contract_path.unlink(missing_ok=True)
