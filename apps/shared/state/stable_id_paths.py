"""Confine stable-id JSON cache filenames under a resolved directory."""
from __future__ import annotations

from pathlib import Path


def confined_stable_id_json_path(
    directory: Path,
    stable_id: str,
    *,
    directory_name: str,
    error_type: type[ValueError] = ValueError,
) -> Path:
    root = directory.resolve()
    path = (root / f"{stable_id}.json").resolve()
    if path.parent != root:
        raise error_type(f"stable_id escapes {directory_name} directory: {stable_id!r}")
    return path
