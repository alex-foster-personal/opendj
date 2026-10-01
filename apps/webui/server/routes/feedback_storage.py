"""JSON storage for the feedback routes: load, atomic save, forward-compatible merge.

Split out of ``feedback.py`` to keep that module under the file-size limit
with room to grow. ``feedback.py`` re-exports every name here, so the sibling
modules that import them from it are unchanged. No behavior change.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def _load(path: Path, root_key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get(root_key) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FEEDBACK_STORE_MALFORMED",
                "message": f"{path} does not hold a {root_key!r} list",
            },
        )
    return items


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so a reader sees the old file or the new one.

    Temp file in the same directory, fsync, then ``os.replace`` (atomic on one
    filesystem), then fsync the directory so the rename itself is durable. A
    plain ``write_text`` truncates first, so a crash or a full disk mid-write
    leaves a torn ``comments.json`` that every later read refuses (PR #1978
    review). The temp name starts with a dot, so no ``archive-*.json`` glob
    can mistake a leftover for an archive.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _save(path: Path, root_key: str, items: list[dict[str, Any]]) -> None:
    write_atomic(path, json.dumps({root_key: items}, indent=2) + "\n")


def keep_unknown_fields(raw: dict[str, Any], known: dict[str, Any]) -> dict[str, Any]:
    """``known`` (a model dump) laid over ``raw``, so fields this build lacks survive.

    A newer build may add a pin field this one's ``CommentOut`` does not know.
    Validating and dumping drops it; overlaying the dump on the raw doc keeps
    it, at every nesting level, while still filling this build's defaults.
    """
    merged = dict(raw)
    for key, value in known.items():
        prior = raw.get(key)
        if isinstance(value, dict) and isinstance(prior, dict):
            merged[key] = keep_unknown_fields(prior, value)
        else:
            merged[key] = value
    return merged


def _load_general(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"text": "", "updated_at": None, "build": None}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FEEDBACK_STORE_MALFORMED",
                "message": f"{path} does not hold a general note",
            },
        )
    return raw
