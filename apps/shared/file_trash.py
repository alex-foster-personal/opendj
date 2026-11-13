"""Move a file to the user's trash instead of unlinking it (LIBM-53, mimicked from Mixxx 2.4).

Every trash here is undoable, and nothing ever copies a file and deletes the original:

* macOS: the system Trash through ``NSFileManager.trashItemAtURL:resultingItemURL:error:``, the
  call Finder itself uses. A file on an external drive goes to that drive's own trash, and
  Finder's "Put Back" returns it to where it was. The path the API reports is returned.
* Linux: the FreeDesktop.org home trash, ``$XDG_DATA_HOME/Trash`` (default ``~/.local/share/Trash``):
  the file goes to ``files/`` and a ``info/<name>.trashinfo`` records where it came from. The
  info file is created first with ``O_EXCL``, which is what reserves the unique name. The move
  is one ``os.rename``; a file on another filesystem (``EXDEV``) is refused, never copied.
* Any other platform raises :class:`NotImplementedError`; nothing is moved.

When the trash cannot take the file, :class:`TrashRefused` is raised and the file is left
where it was. There is no fallback: no copy, no unlink.

The trash is an explicit :class:`TrashBin` (Linux) or :class:`MacTrash` (macOS), so a caller
(and every test) can say exactly where files go; :class:`MacTrash` takes the Foundation call as
a parameter so no test ever reaches a real Trash. :func:`local_trash_bin` resolves this
machine's trash from the platform and ``HOME``.
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


class TrashRefused(OSError):
    """The trash would not take the file; the file is still where it was."""


@dataclass(frozen=True)
class TrashBin:
    """A FreeDesktop.org trash: files land in ``files_dir`` with a record in ``info_dir``."""

    files_dir: Path
    info_dir: Path


def ns_trash_item(path: Path) -> Path:
    """Trash ``path`` with ``NSFileManager.trashItemAtURL:resultingItemURL:error:``; return where it went."""
    try:
        from Foundation import NSURL, NSFileManager  # macOS only
    except ImportError as exc:
        raise TrashRefused(f"the macOS Trash API is unavailable ({exc}); {path} was not moved") from exc
    url = NSURL.fileURLWithPath_(str(path))
    try:
        ok, resulting_url, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            url, None, None
        )
    except Exception as exc:  # an Objective-C exception crosses the bridge as a Python one
        raise TrashRefused(f"the macOS Trash refused {path}: {exc}") from exc
    if not ok or resulting_url is None:
        reason = error.localizedDescription() if error is not None else "no reason given"
        raise TrashRefused(f"the macOS Trash refused {path}: {reason}")
    return Path(str(resulting_url.path()))


@dataclass(frozen=True)
class MacTrash:
    """The macOS system Trash, reached through ``trash_item`` (:func:`ns_trash_item` unless a test injects one)."""

    trash_item: Callable[[Path], Path] = field(default=ns_trash_item)


Trash = TrashBin | MacTrash


def trash_bin_for(platform: str, home: Path, xdg_data_home: str | None) -> Trash:
    """The trash a platform uses for a user whose home is ``home``."""
    if platform == "darwin":
        return MacTrash()
    if platform.startswith("linux"):
        root = Path(xdg_data_home) if xdg_data_home else home / ".local" / "share"
        return TrashBin(files_dir=root / "Trash" / "files", info_dir=root / "Trash" / "info")
    raise NotImplementedError(f"move to trash is not built for platform {platform!r}")


def local_trash_bin() -> Trash:
    """This machine's trash, from ``sys.platform``, ``HOME`` and ``XDG_DATA_HOME``."""
    return trash_bin_for(sys.platform, Path.home(), os.environ.get("XDG_DATA_HOME"))


def _candidate_names(name: str) -> list[str]:
    stem, suffix = os.path.splitext(name)
    return [name, *(f"{stem} {n}{suffix}" for n in range(2, 10_000))]


def _reserve(path: Path) -> bool:
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _trashinfo(original: Path) -> str:
    deleted = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    return f"[Trash Info]\nPath={quote(str(original))}\nDeletionDate={deleted}\n"


def _move_to_freedesktop_trash(source: Path, trash: TrashBin) -> Path:
    trash.files_dir.mkdir(parents=True, exist_ok=True)
    trash.info_dir.mkdir(parents=True, exist_ok=True)
    for name in _candidate_names(source.name):
        target = trash.files_dir / name
        info = trash.info_dir / f"{name}.trashinfo"
        if not _reserve(info):
            continue
        if target.exists():
            info.unlink()
            continue
        try:
            info.write_text(_trashinfo(source), encoding="utf-8")
            os.rename(source, target)
        except OSError as exc:
            info.unlink(missing_ok=True)
            if exc.errno == errno.EXDEV:
                raise TrashRefused(
                    f"{source} is on another filesystem from the trash at {trash.files_dir}; "
                    "it was not moved (a copy would not be undoable from the trash)"
                ) from exc
            raise
        except BaseException:
            info.unlink(missing_ok=True)
            raise
        return target
    raise FileExistsError(f"no free name for {source.name} in {trash.files_dir}")


def move_to_trash(path: Path, trash: Trash) -> Path:
    """Move ``path`` into ``trash``, undoably; returns where it went. Raises :class:`TrashRefused` instead of copying."""
    source = path.absolute()
    if not source.is_file():
        raise FileNotFoundError(f"no file to trash at {source}")
    if isinstance(trash, MacTrash):
        return trash.trash_item(source)
    return _move_to_freedesktop_trash(source, trash)


__all__ = [
    "MacTrash",
    "Trash",
    "TrashBin",
    "TrashRefused",
    "local_trash_bin",
    "move_to_trash",
    "ns_trash_item",
    "trash_bin_for",
]
