"""Set one library track's tracklist-hidden flag over the running engine (NOWP-11487).

The agent twin of the library row's Hide in tracklists context menu.
"""

from __future__ import annotations

from typing import Any

import httpx

from apps.opendj_cli.track_comment import TrackCommentError, _refuse, _track_path

REQUEST_TIMEOUT_S = 10.0


def set_tracklist_hidden(client: httpx.Client, stable_id: str, hidden: bool) -> dict[str, Any]:
    """Store ``hidden`` as the track's tracklist_hidden field."""
    path = _track_path(stable_id)
    read = client.get(path)
    if not read.is_success:
        raise _refuse(read, f"GET {path}")
    etag = read.headers.get("ETag")
    if not etag:
        raise TrackCommentError("tracklist_hidden_failed", f"GET {path} answered without an ETag", read.status_code)
    written = client.patch(path, json={"tracklist_hidden": hidden}, headers={"If-Match": etag})
    if not written.is_success:
        code = "track_not_found" if written.status_code == 404 else "tracklist_hidden_failed"
        raise TrackCommentError(
            code, f"PATCH {path} -> {written.status_code}: {written.text[:300]}", written.status_code
        )
    stored = written.json()
    return {
        "stable_id": stable_id,
        "tracklist_hidden": stored.get("tracklist_hidden"),
        "etag": written.headers.get("ETag"),
    }


__all__ = ["set_tracklist_hidden"]
