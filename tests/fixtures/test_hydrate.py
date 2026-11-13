"""Acceptance tests for :mod:`tests.fixtures.hydrate` (INFRA-03).

[if] fixture hydration is not platform-safe [then] locked fixtures may be mis-resolved, [else stop].
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

import apps.shared.platform_paths as platform_paths
from tests.fixtures.hydrate import classify_stored_path, hydrate_file, is_any_absolute as hydrate_is_any_absolute

pytestmark = [pytest.mark.requirement("INFRA-03")]


def test_hydrate_file_posix_path(tmp_path: Path) -> None:
    """[if] hydration runs on a POSIX path [then] the copy exists and the source is unchanged, [else stop]."""
    payload = b"fixture-bytes-13107"
    source = tmp_path / "canonical" / "payload.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    size_before = source.stat().st_size

    dest = tmp_path / "hydrated" / "payload.bin"
    result = hydrate_file(source, dest)

    assert result == dest
    assert dest.read_bytes() == payload
    assert source.stat().st_size == size_before


@pytest.mark.parametrize(
    "path_str",
    [
        r"C:\Music\dj\song.mp3",
        "D:/dj/track.flac",
        r"\\server\share\dj\song.mp3",
    ],
)
def test_classify_windows_style_fixture_path(
    path_str: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[if] a Windows-style path is classified [then] foreign absolute yields unmapped reason, [else stop]."""
    monkeypatch.setattr(platform_paths, "IS_DARWIN", True)
    monkeypatch.setattr(platform_paths, "IS_WINDOWS", False)

    assert hydrate_is_any_absolute(path_str) is True
    assert PureWindowsPath(path_str).is_absolute()

    mapped = classify_stored_path(path_str)
    assert mapped.original == path_str
    assert mapped.resolved is None
    assert mapped.mapped is False
    assert mapped.reason.startswith("unmapped")


def test_hydrate_file_missing_source_fails(tmp_path: Path) -> None:
    """[if] a fixture source is missing [then] hydrate raises FileNotFoundError, [else stop]."""
    missing = tmp_path / "nope" / "file.bin"
    missing.parent.mkdir(parents=True, exist_ok=True)
    dest = tmp_path / "out.bin"

    with pytest.raises(FileNotFoundError, match="fixture source") as exc_info:
        hydrate_file(missing, dest)

    assert str(missing) in str(exc_info.value)
