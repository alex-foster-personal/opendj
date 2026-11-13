"""Refuse NFC/case-fold path collisions before ingest writes any rows.

Two distinct files whose paths normalize to the same collision key must not
share one inferred ``stable_id``. Duplicate spellings of one physical file
(same device plus inode) are allowed.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable, Sequence

STREAMING_URI_PREFIXES = ("spotify:", "tidal:", "http://", "https://")


class PathCollisionError(ValueError):
    """Two or more distinct local paths share one normalized collision key."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = sorted(dict.fromkeys(paths))
        joined = "; ".join(self.paths)
        super().__init__(f"path collision: distinct files share the same normalized path key: {joined}")


class AmbiguousAnalysisPathError(PathCollisionError):
    """Two or more tracks resolve to one normalized audio path for native analysis."""

    def __init__(self, stable_ids: Sequence[str], paths: Sequence[str]) -> None:
        self.stable_ids = sorted(dict.fromkeys(stable_ids))
        super().__init__(paths)
        self.args = (
            "ambiguous analysis path: tracks "
            + ", ".join(self.stable_ids)
            + " resolve to one normalized audio path: "
            + "; ".join(self.paths),
        )


def path_collision_key(raw_path: str) -> str:
    """NFC-normalize then case-fold for collision grouping only."""
    return unicodedata.normalize("NFC", raw_path).casefold()


def is_streaming_path(path_str: str) -> bool:
    return path_str.startswith(STREAMING_URI_PREFIXES)


def assert_no_path_collisions(paths: Iterable[str]) -> None:
    """Raise :class:`PathCollisionError` when distinct files share one key."""
    by_key: dict[str, list[str]] = {}
    for raw in paths:
        if not raw or is_streaming_path(raw):
            continue
        by_key.setdefault(path_collision_key(raw), []).append(raw)

    for group in by_key.values():
        unique = list(dict.fromkeys(group))
        if len(unique) < 2:
            continue
        if _all_same_physical_file(unique):
            continue
        raise PathCollisionError(unique)


def assert_unambiguous_analysis_paths(targets: Iterable[tuple[str, str | None]]) -> None:
    """Raise :class:`AmbiguousAnalysisPathError` when distinct tracks share one path key.

    ``targets`` is ``(stable_id, resolved_path)`` pairs. Unlike ingest, a
    shared physical file is still refused: one file analyzed for two tracks
    cannot say whose analysis it is. Missing and streaming paths are skipped.
    """
    by_key: dict[str, dict[str, list[str]]] = {}
    for stable_id, raw in targets:
        if not raw or is_streaming_path(raw):
            continue
        by_key.setdefault(path_collision_key(raw), {}).setdefault(stable_id, []).append(raw)

    for key in sorted(by_key):
        group = by_key[key]
        if len(group) < 2:
            continue
        raise AmbiguousAnalysisPathError(list(group), [path for paths in group.values() for path in paths])


def _file_identity(path_str: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path_str)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def paths_are_same_physical_file(paths: Sequence[str]) -> bool:
    """True when every path resolves to the same device and inode."""
    if len(paths) < 2:
        return len(paths) == 1
    return _all_same_physical_file(paths)


def _all_same_physical_file(paths: Sequence[str]) -> bool:
    identities = [_file_identity(path) for path in paths]
    if any(identity is None for identity in identities):
        return False
    first = identities[0]
    return all(identity == first for identity in identities[1:])


__all__ = [
    "AmbiguousAnalysisPathError",
    "PathCollisionError",
    "assert_no_path_collisions",
    "assert_unambiguous_analysis_paths",
    "is_streaming_path",
    "path_collision_key",
    "paths_are_same_physical_file",
]
