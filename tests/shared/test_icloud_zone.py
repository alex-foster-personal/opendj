"""iCloud zone classification for path heal."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from apps.shared import icloud_zone


@pytest.mark.skipif(sys.platform != "darwin", reason="iCloud zone is Darwin-scoped")
def test_music_path_is_not_icloud_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    music = tmp_path / "Music"
    music.mkdir()
    track = music / "a.mp3"
    track.write_bytes(b"x")
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", tmp_path)
    assert icloud_zone.is_icloud_zone(track) is False
    assert icloud_zone.icloud_zone_reason(track) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="iCloud zone is Darwin-scoped")
def test_mobile_documents_path_is_zone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mobile = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "lib"
    mobile.mkdir(parents=True)
    track = mobile / "a.mp3"
    track.write_bytes(b"x")
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", tmp_path)
    assert icloud_zone.is_icloud_zone(track) is True
    assert icloud_zone.icloud_zone_reason(track) == "mobile-documents"


@pytest.mark.skipif(sys.platform != "darwin", reason="iCloud zone is Darwin-scoped")
def test_documents_is_zone_when_cloud_docs_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    cloud.mkdir(parents=True)
    docs = tmp_path / "Documents" / "lib"
    docs.mkdir(parents=True)
    track = docs / "a.mp3"
    track.write_bytes(b"x")
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", tmp_path)
    assert icloud_zone.icloud_zone_reason(track) == "documents-desktop-sync"


@pytest.mark.skipif(sys.platform != "darwin", reason="iCloud zone is Darwin-scoped")
def test_missing_mobile_documents_path_classifies_lexically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", tmp_path)
    missing = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "gone.mp3"
    assert icloud_zone.is_icloud_zone(missing) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="iCloud zone is Darwin-scoped")
def test_music_root_inside_zone_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Music"
    cloud.mkdir(parents=True)
    monkeypatch.setattr(icloud_zone.platform_paths, "HOME", tmp_path)
    with pytest.raises(RuntimeError, match="inside iCloud zone"):
        icloud_zone.assert_music_roots_outside_icloud_zone([cloud])


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="off-Darwin returns None before the absolute-path check runs",
)
def test_relative_path_rejected() -> None:
    # icloud_zone_reason short-circuits on `sys.platform != "darwin"` BEFORE it
    # validates the argument, by design: the iCloud zone is a macOS-only problem
    # domain and the module is deliberately inert elsewhere. So the ValueError is
    # a Darwin-only contract, and asserting it unconditionally fails on Linux CI.
    # Gated the same way as test_off_darwin_never_zone below, in reverse.
    with pytest.raises(ValueError, match="absolute"):
        icloud_zone.is_icloud_zone("relative/a.mp3")


@pytest.mark.skipif(sys.platform == "darwin", reason="off-Darwin always false")
def test_off_darwin_never_zone(tmp_path: Path) -> None:
    p = tmp_path / "Library" / "Mobile Documents" / "x.mp3"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    assert icloud_zone.is_icloud_zone(p) is False
