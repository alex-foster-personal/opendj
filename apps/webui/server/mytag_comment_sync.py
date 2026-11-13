"""The ``mytag_to_comments`` setting and the writes that keep each track's comment block in sync with its tags.

The setting lives in ``<data>/state/mytag-comments.json`` beside ``ui-prefs.json`` (same root resolution), so a
``PUT /api/v1/ui-prefs`` that rewrites the prefs file can never drop it. The stored ``Track.comments`` IS the merged
comment while the setting is on, so every reader (the table, the API, an export) carries the block with no extra step.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI

from apps.shared.atomic_text import write_atomic
from apps.shared.mytag_comments import merge_mytag_comments
from apps.shared.paths import DATA_DIR

from .backend import Source, StateBackend, Track, TrackFilter, TrackUpdate
from .etag import compute_etag

_FILENAME = "mytag-comments.json"
_SCAN_PAGE_LIMIT = 1000
_WRITE_BATCH = 100


# ----- setting store ---------------------------------------------------------


def setting_path(app: FastAPI) -> Path:
    """Where this app keeps the setting: ``<data_dir>/state/mytag-comments.json``, the ui-prefs root rule."""
    configured = getattr(app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "state" / _FILENAME


def read_mytag_to_comments(app: FastAPI) -> bool:
    """True when the setting is on; a missing file is off (rekordbox ships the option off)."""
    path = setting_path(app)
    if not path.is_file():
        return False
    enabled = json.loads(path.read_text(encoding="utf-8"))["enabled"]
    if not isinstance(enabled, bool):
        raise TypeError(f"{path}: enabled must be a boolean, got {enabled!r}")
    return enabled


def write_mytag_to_comments(app: FastAPI, enabled: bool) -> None:
    """Persist the setting atomically (apps.shared.atomic_text.write_atomic)."""
    write_atomic(setting_path(app), json.dumps({"enabled": enabled}))


# ----- sync ------------------------------------------------------------------


def _etag(track: Track) -> str:
    return compute_etag(track.stable_id, track.updated_at, track.selection_tag)


def _rewrite(backend: StateBackend, tracks: Sequence[Track], *, enabled: bool, source: Source) -> dict[str, Track]:
    stale = [
        (track, merged)
        for track in tracks
        if (merged := merge_mytag_comments(track.comments, track.tags or [], enabled=enabled)) != track.comments
    ]
    rewritten: dict[str, Track] = {}
    for start in range(0, len(stale), _WRITE_BATCH):
        batch = stale[start : start + _WRITE_BATCH]
        updates = [TrackUpdate(track.stable_id, {"comments": merged}, _etag(track)) for track, merged in batch]
        rewritten.update({track.stable_id: track for track in backend.update_tracks(updates, source=source)})
    return rewritten


def sync_mytag_comments(app: FastAPI, backend: StateBackend, tracks: Sequence[Track], *, source: Source) -> list[Track]:
    """After a tag or comment write: re-render each track's block when the setting is on; the tracks as now stored."""
    if not read_mytag_to_comments(app):
        return list(tracks)
    rewritten = _rewrite(backend, tracks, enabled=True, source=source)
    return [rewritten.get(track.stable_id, track) for track in tracks]


def sweep_mytag_comments(backend: StateBackend, *, enabled: bool, source: Source) -> list[str]:
    """Add (``enabled``) or strip every track's block in one pass; the ids rewritten (none when already in sync)."""
    tracks: list[Track] = []
    cursor: str | None = None
    while True:
        page = backend.list_tracks(TrackFilter(cursor=cursor, limit=_SCAN_PAGE_LIMIT))
        tracks.extend(page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return sorted(_rewrite(backend, tracks, enabled=enabled, source=source))


__all__ = [
    "read_mytag_to_comments",
    "setting_path",
    "sweep_mytag_comments",
    "sync_mytag_comments",
    "write_mytag_to_comments",
]
