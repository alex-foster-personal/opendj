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

from pathlib import Path

import pytest

from tests.fixtures import _resolver
from tests.fixtures._resolver import (
    FixtureNotAvailable,
    fixture_path,
)


pytestmark = [pytest.mark.requirement("INFRA-03")]


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
    link.symlink_to(target)

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
