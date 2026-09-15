"""Refuse NFC/case-fold path collisions before ingest writes any rows.

Two distinct files whose paths normalize to the same collision key must not
share one inferred ``stable_id``. Duplicate spellings of one physical file
(same device plus inode) are allowed.
"""
from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable, Sequence

_STREAMING_PREFIXES = ("spotify:", "tidal:", "http://", "https://")


class PathCollisionError(ValueError):
    """Two or more distinct local paths share one normalized collision key."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = sorted(dict.fromkeys(paths))
        joined = "; ".join(self.paths)
        super().__init__(
            "path collision: distinct files share the same normalized path "
            f"key: {joined}"
        )


def path_collision_key(raw_path: str) -> str:
    """NFC-normalize then case-fold for collision grouping only."""
    return unicodedata.normalize("NFC", raw_path).casefold()


def is_streaming_path(path_str: str) -> bool:
    return path_str.startswith(_STREAMING_PREFIXES)


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


def _file_identity(path_str: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path_str)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _all_same_physical_file(paths: Sequence[str]) -> bool:
    identities = [_file_identity(path) for path in paths]
    if any(identity is None for identity in identities):
        return False
    first = identities[0]
    return all(identity == first for identity in identities[1:])


__all__ = [
    "PathCollisionError",
    "assert_no_path_collisions",
    "is_streaming_path",
    "path_collision_key",
]
