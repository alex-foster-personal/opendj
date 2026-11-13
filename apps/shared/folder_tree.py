"""Folder tree of the collection's stored file paths, the rekordbox Explorer view (mimicked, issue #9747).

Pure: built from ``{stable_id: file_path}`` alone, it never touches the disk. POSIX paths split on ``/``; a path with
a drive letter or a backslash splits as Windows. Children and tracks sort by name, case-folded, like a file browser.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePath, PurePosixPath, PureWindowsPath

_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|\\")


@dataclass
class FolderNode:
    """One folder: its own tracks (by file path) and its child folders by name."""

    name: str
    path: str
    tracks: dict[str, str] = field(default_factory=dict)  # stable_id -> file_path, directly in this folder
    children: dict[str, FolderNode] = field(default_factory=dict)

    def track_count(self) -> int:
        """Tracks in this folder and every folder below it."""
        return len(self.tracks) + sum(child.track_count() for child in self.children.values())

    def sorted_children(self) -> list[FolderNode]:
        """Child folders in name order, case-folded."""
        return sorted(self.children.values(), key=lambda node: (node.name.casefold(), node.name))

    def tracks_in_order(self, *, recursive: bool) -> list[tuple[str, str]]:
        """``(stable_id, file_path)`` in file-name order, then (``recursive``) each child folder's in turn."""
        rows = sorted(self.tracks.items(), key=lambda item: (item[1].casefold(), item[0]))
        if recursive:
            for child in self.sorted_children():
                rows.extend(child.tracks_in_order(recursive=True))
        return rows


@dataclass
class FolderForest:
    """Every root folder (``/``, ``C:\\``) plus the tracks that have no stored path."""

    roots: dict[str, FolderNode]
    unlocated_ids: list[str]

    def sorted_roots(self) -> list[FolderNode]:
        """Root folders in name order."""
        return sorted(self.roots.values(), key=lambda node: node.path)

    def find(self, path: str) -> FolderNode | None:
        """The folder at exactly ``path`` (a trailing separator is ignored), or ``None`` when no track lives under it."""
        parts = _pure(path).parts
        if not parts:
            return None
        node = self.roots.get(parts[0])
        for name in parts[1:]:
            if node is None:
                return None
            node = node.children.get(name)
        return node


def _pure(path: str) -> PurePath:
    return PureWindowsPath(path) if _WINDOWS_PATH.search(path) else PurePosixPath(path)


def build_folder_forest(file_paths: Mapping[str, str | None]) -> FolderForest:
    """The folder tree of every track's parent directory; tracks with no path or a bare file name are unlocated."""
    roots: dict[str, FolderNode] = {}
    unlocated: list[str] = []
    for stable_id, file_path in file_paths.items():
        pure = _pure(file_path) if file_path else None
        if pure is None or not pure.anchor or len(pure.parts) < 2:
            unlocated.append(stable_id)
            continue
        anchor, *folders = pure.parent.parts
        node = roots.setdefault(anchor, FolderNode(name=anchor, path=anchor))
        current = pure.parent.__class__(anchor)
        for name in folders:
            current = current / name
            node = node.children.setdefault(name, FolderNode(name=name, path=str(current)))
        node.tracks[stable_id] = str(file_path)
    return FolderForest(roots=roots, unlocated_ids=sorted(unlocated))


__all__ = ["FolderForest", "FolderNode", "build_folder_forest"]
