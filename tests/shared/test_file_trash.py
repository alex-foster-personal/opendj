"""LIBM-53: a deleted track file is moved to the trash, undoably, never unlinked or copied.

[if] move_to_trash unlinks, copies, overwrites a trashed file, writes outside the given bin or skips the macOS Trash API [then] fail, [else stop].

No test here reaches a real Trash: the Linux bin is under tmp_path, and the macOS Trash API is
either injected into :class:`MacTrash` or answered by a fake ``Foundation`` module.

Regression one-liners:
  - if a trashed file is unlinked instead of moved then broken
  - if a second file with the same name overwrites the first in the trash then broken
  - if a Linux trash move leaves no .trashinfo naming the original path then broken
  - if an unknown platform moves anything then broken
  - if macOS trashing bypasses NSFileManager.trashItemAtURL (so Finder's Put Back is lost) then broken
  - if a file on another volume is copied and deleted instead of trashed then broken
  - if a refused trash falls back to a copy or an unlink then broken
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import types
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest

from apps.shared.file_trash import MacTrash, TrashBin, TrashRefused, move_to_trash, ns_trash_item, trash_bin_for

pytestmark = pytest.mark.requirement("LIBM-13926")


def _track(folder: Path, name: str, payload: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(payload)
    return path


def _linux_bin(home: Path) -> TrashBin:
    trash = trash_bin_for("linux", home, None)
    assert isinstance(trash, TrashBin)
    return trash


@pytest.fixture
def trash(tmp_path: Path) -> TrashBin:
    return _linux_bin(tmp_path / "home")


@pytest.fixture
def no_copy_or_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way Python could copy, move or delete a file raises, so a test sees any use of one."""

    def _forbidden(name: str) -> Any:
        def _raise(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"{name} was called with {args}: trashing must not copy, move or unlink by hand")

        return _raise

    for module, name in (
        (shutil, "move"),
        (shutil, "copy"),
        (shutil, "copy2"),
        (shutil, "copyfile"),
        (shutil, "copytree"),
        (os, "replace"),
        (os, "unlink"),
        (os, "remove"),
    ):
        monkeypatch.setattr(module, name, _forbidden(f"{module.__name__}.{name}"))


def test_file_is_moved_into_the_trash_not_unlinked(tmp_path: Path, trash: TrashBin) -> None:
    """[if] a file is trashed [then] it is gone from its folder and its bytes sit in the trash files dir, [else stop]."""
    source = _track(tmp_path / "music", "a.mp3", b"first")

    landed = move_to_trash(source, trash)

    assert not source.exists()
    assert landed.parent == trash.files_dir
    assert landed.read_bytes() == b"first"


def test_same_name_in_the_trash_gets_a_unique_name_and_both_survive(tmp_path: Path, trash: TrashBin) -> None:
    """[if] a file with the same name is already in the trash [then] the new one gets a unique name and neither is lost, [else stop]."""
    first = move_to_trash(_track(tmp_path / "one", "a.mp3", b"first"), trash)
    second = move_to_trash(_track(tmp_path / "two", "a.mp3", b"second"), trash)

    assert first.name == "a.mp3"
    assert second.name == "a 2.mp3"
    assert (first.read_bytes(), second.read_bytes()) == (b"first", b"second")


def test_linux_trash_records_the_original_path(tmp_path: Path) -> None:
    """[if] a file is trashed in a FreeDesktop.org trash [then] info/<name>.trashinfo names its original path, [else stop]."""
    trash = trash_bin_for("linux", tmp_path / "home", str(tmp_path / "xdg"))
    assert isinstance(trash, TrashBin)
    source = _track(tmp_path / "music folder", "b c.mp3", b"x")

    landed = move_to_trash(source, trash)

    assert trash.files_dir == tmp_path / "xdg" / "Trash" / "files"
    info = (trash.info_dir / f"{landed.name}.trashinfo").read_text(encoding="utf-8").splitlines()
    assert info[0] == "[Trash Info]"
    assert unquote(info[1].removeprefix("Path=")) == str(source)
    assert info[2].startswith("DeletionDate=")


def test_macos_uses_the_trash_api_and_unknown_platform_refuses(tmp_path: Path) -> None:
    """[if] the platform is macOS or unknown [then] the trash is the NSFileManager call or NotImplementedError is raised, [else stop]."""
    assert trash_bin_for("darwin", tmp_path, None) == MacTrash(trash_item=ns_trash_item)
    with pytest.raises(NotImplementedError):
        trash_bin_for("win32", tmp_path, None)


def test_missing_source_raises_and_trash_stays_empty(tmp_path: Path, trash: TrashBin) -> None:
    """[if] the file to trash does not exist [then] FileNotFoundError is raised and nothing lands in the trash, [else stop]."""
    with pytest.raises(FileNotFoundError):
        move_to_trash(tmp_path / "gone.mp3", trash)

    assert not trash.files_dir.exists() or list(trash.files_dir.iterdir()) == []


@pytest.mark.requirement("LIBM-14825")
@pytest.mark.usefixtures("no_copy_or_unlink")
def test_macos_trash_calls_the_api_with_the_original_path_and_nothing_else_touches_the_file(tmp_path: Path) -> None:
    """[if] a file is trashed on macOS [then] the Trash API alone gets its original path and its answer is returned, [else stop]."""
    source = _track(tmp_path / "music", "a.mp3", b"first")
    calls: list[Path] = []
    reported = tmp_path / "home" / ".Trash" / "a.mp3"

    def _trash_item(path: Path) -> Path:
        calls.append(path)
        return reported

    landed = move_to_trash(source, MacTrash(trash_item=_trash_item))

    assert calls == [source]
    assert landed == reported
    # The fake API moved nothing, so the file is still where it was: file_trash itself never touched it.
    assert source.read_bytes() == b"first"


@pytest.mark.requirement("LIBM-14825")
@pytest.mark.usefixtures("no_copy_or_unlink")
def test_macos_file_on_an_external_drive_goes_to_the_trash_api_not_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the file sits on another volume (rename answers EXDEV) [then] the Trash API is called and no copy is made, [else stop]."""
    source = _track(tmp_path / "Volumes" / "Crate" / "music", "far.mp3", b"far")

    def _cross_device(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Cross-device link", str(src))

    monkeypatch.setattr(os, "rename", _cross_device)
    calls: list[Path] = []

    def _trash_item(path: Path) -> Path:
        calls.append(path)
        return tmp_path / "Volumes" / "Crate" / ".Trashes" / "501" / path.name

    landed = move_to_trash(source, MacTrash(trash_item=_trash_item))

    assert calls == [source]
    assert landed == tmp_path / "Volumes" / "Crate" / ".Trashes" / "501" / "far.mp3"
    assert source.read_bytes() == b"far"


def _fake_foundation(monkeypatch: pytest.MonkeyPatch, answer: tuple[bool, Any, Any]) -> list[str]:
    """Install a ``Foundation`` module whose trashItemAtURL answers ``answer``; return the paths it was given."""
    asked: list[str] = []

    class _URL:
        def __init__(self, path: str) -> None:
            self._path = path

        def path(self) -> str:
            return self._path

    class _Manager:
        def trashItemAtURL_resultingItemURL_error_(self, url: _URL, out: None, err: None) -> tuple[bool, Any, Any]:
            assert (out, err) == (None, None)
            asked.append(url.path())
            return answer

    foundation = types.ModuleType("Foundation")
    foundation.NSURL = types.SimpleNamespace(fileURLWithPath_=_URL)  # type: ignore[attr-defined]
    foundation.NSFileManager = types.SimpleNamespace(defaultManager=_Manager)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    return asked


@pytest.mark.requirement("LIBM-14825")
@pytest.mark.usefixtures("no_copy_or_unlink")
def test_ns_trash_item_passes_the_path_to_nsfilemanager_and_reports_where_it_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] ns_trash_item runs [then] trashItemAtURL gets the file's URL and the resulting URL's path comes back, [else stop]."""
    source = _track(tmp_path / "music", "a.mp3", b"first")
    asked = _fake_foundation(monkeypatch, (True, types.SimpleNamespace(path=lambda: "/Users/x/.Trash/a.mp3"), None))

    assert ns_trash_item(source) == Path("/Users/x/.Trash/a.mp3")
    assert asked == [str(source)]


@pytest.mark.requirement("LIBM-14825")
@pytest.mark.usefixtures("no_copy_or_unlink")
def test_a_refused_macos_trash_raises_and_leaves_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] trashItemAtURL fails [then] TrashRefused is raised with its reason and the file stays put, no fallback, [else stop]."""
    source = _track(tmp_path / "music", "a.mp3", b"first")
    error = types.SimpleNamespace(localizedDescription=lambda: "The volume does not support a trash.")
    _fake_foundation(monkeypatch, (False, None, error))

    with pytest.raises(TrashRefused, match="does not support a trash"):
        move_to_trash(source, MacTrash())

    assert source.read_bytes() == b"first"


@pytest.mark.requirement("LIBM-14825")
def test_no_foundation_refuses_instead_of_moving(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] PyObjC's Foundation cannot be imported [then] TrashRefused is raised and the file stays put, [else stop]."""
    source = _track(tmp_path / "music", "a.mp3", b"first")
    monkeypatch.setitem(sys.modules, "Foundation", None)

    with pytest.raises(TrashRefused, match="unavailable"):
        ns_trash_item(source)

    assert source.read_bytes() == b"first"


@pytest.mark.requirement("LIBM-14825")
def test_linux_file_on_another_filesystem_is_refused_not_copied(
    tmp_path: Path, trash: TrashBin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] the file is on another filesystem from the home trash [then] TrashRefused, the file stays and no record is left, [else stop]."""
    source = _track(tmp_path / "mnt" / "usb", "far.mp3", b"far")
    copies: list[object] = []

    def _cross_device(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))

    monkeypatch.setattr(os, "rename", _cross_device)
    for name in ("copy", "copy2", "copyfile"):
        monkeypatch.setattr(shutil, name, lambda *args, **kwargs: copies.append(args))

    with pytest.raises(TrashRefused, match="another filesystem"):
        move_to_trash(source, trash)

    assert copies == []
    assert source.read_bytes() == b"far"
    assert list(trash.files_dir.iterdir()) == []
    assert list(trash.info_dir.iterdir()) == []
