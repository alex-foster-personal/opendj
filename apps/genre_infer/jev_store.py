"""What JEV is shown about each track, and where its guesses are kept.

``<data>/state/genre/jev_suggestions.json``::

    {"schema": "jev-suggestions/v1", "created_at": ..., "model": ...,
     "min_confidence": 0.8, "tags": [{"name", "question"}],
     "suggestions": {stable_id: {"status": "ok", "family", "confidence",
                                 "probabilities", "tags": {name: P(yes)}}}}

Machine-local and regenerable, like ``suggestions.json`` beside it. Reading it
is read-only and cached by modification time, so a library listing pays one
stat per request, not a JSON parse.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath
from typing import Any

from . import store

# track_fields that can help a guess. A genre tag is deliberately absent.
_FIELD_NAMES = ("bpm", "key", "comments", "label", "year")


def suggestions_path(data_dir: Path) -> Path:
    return store.genre_dir(data_dir) / "jev_suggestions.json"


def tags_path(data_dir: Path) -> Path:
    return store.genre_dir(data_dir) / "jev_tags.json"


def _scalar(value_json: str | None) -> Any:
    if not value_json:
        return None
    try:
        value = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return None


def _artists(artists_json: str | None) -> str | None:
    if not artists_json:
        return None
    try:
        value = json.loads(artists_json)
    except (json.JSONDecodeError, TypeError):
        return artists_json.strip() or None
    if isinstance(value, list):
        names = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(names) or None
    return str(value).strip() or None


def local_genre_ids(conn: sqlite3.Connection) -> set[str]:
    """stable_ids with a non-empty local genre field (file, MIK or web edit).

    The library row shows this field even when rekordbox has no genre, so a
    track carrying one is tagged and must never be asked about (GENRE-02).
    """
    out: set[str] = set()
    for sid, value_json in conn.execute(
        "SELECT stable_id, value_json FROM track_fields WHERE field_name = 'genre' AND deleted_at IS NULL"
    ):
        try:
            value = json.loads(value_json) if value_json else None
        except ValueError:
            value = value_json
        if isinstance(value, str) and value.strip():
            out.add(str(sid))
    return out


def track_facts(conn: sqlite3.Connection, stable_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """stable_id -> the fields :func:`apps.genre_infer.jev.build_state` reads."""
    ids = list(stable_ids)
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for sid, title, artists_json, album, file_path in conn.execute(
            f"SELECT stable_id, title, artists_json, album, file_path FROM tracks WHERE deleted_at IS NULL AND stable_id IN ({marks})",
            chunk,
        ):
            out[sid] = {
                "title": title,
                "artist": _artists(artists_json),
                "album": album,
                # Windows parsing splits on both / and \\, so a Windows-authored path
                # sends only its basename, never the user's folders.
                "file_name": PureWindowsPath(file_path).name if file_path else None,
            }
        names = ",".join("?" * len(_FIELD_NAMES))
        for sid, name, value_json in conn.execute(
            f"SELECT stable_id, field_name, value_json FROM track_fields "
            f"WHERE deleted_at IS NULL AND field_name IN ({names}) AND stable_id IN ({marks})",
            [*_FIELD_NAMES, *chunk],
        ):
            if sid in out:
                value = _scalar(value_json)
                if value is not None:
                    out[sid][name] = value
    return out


_cache_lock = threading.Lock()
_cache: dict[Path, tuple[float, dict[str, Any]]] = {}


def load_suggestions(data_dir: Path) -> dict[str, Any]:
    """The suggestions document, or an empty one when there is none (cached by mtime)."""
    path = suggestions_path(data_dir)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {"suggestions": {}}
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"suggestions": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("suggestions"), dict):
        return {"suggestions": {}}
    with _cache_lock:
        _cache[path] = (mtime, doc)
    return doc


def genre_guess(doc: dict[str, Any], stable_id: str) -> dict[str, Any] | None:
    """The row's ``genre_guess``: an ``ok`` suggestion at or above the stored floor, else None.

    ``other`` is never served: it means JEV saw nothing to go on.
    """
    s = doc.get("suggestions", {}).get(stable_id)
    if not isinstance(s, dict) or s.get("status") != "ok" or s.get("family") == "other":
        return None
    confidence = s.get("confidence")
    floor = doc.get("min_confidence", 0.0)
    if not isinstance(confidence, int | float) or confidence < floor:
        return None
    return {"family": s["family"], "confidence": float(confidence), "source": "jev"}


__all__ = [
    "genre_guess",
    "load_suggestions",
    "suggestions_path",
    "tags_path",
    "track_facts",
]
