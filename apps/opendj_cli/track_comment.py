"""Set one library track's comment over the running engine (LIBUX-56).

The agent twin of the library's Comment cell editor: the CLI verb
``opendj track comment`` and the MCP tool ``track_set_comment`` both call
:func:`set_track_comment`, which does what the cell does: read the track's
ETag, then ``PATCH /api/v1/tracks/{id}`` with ``{"comments": ...}`` under
``If-Match``. The edit is stored as the user's own ``comments`` field (source
``webui``), which wins over the rekordbox comment in the library listing; an
empty string clears the comment the library shows.

-Claude
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

TRACKS_PATH = "/api/v1/tracks"
REQUEST_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class TrackCommentError(Exception):
    code: str
    message: str
    status_code: int

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "status_code": self.status_code}


# ---------------------------------------------------------------- helpers


def _refuse(response: httpx.Response, what: str) -> TrackCommentError:
    code = "track_not_found" if response.status_code == 404 else "track_comment_failed"
    return TrackCommentError(code, f"{what} -> {response.status_code}: {response.text[:300]}", response.status_code)


def _track_path(stable_id: str) -> str:
    return f"{TRACKS_PATH}/{quote(stable_id, safe='')}"


# ---------------------------------------------------------------- public


def set_track_comment(client: httpx.Client, stable_id: str, comment: str) -> dict[str, Any]:
    """Store ``comment`` as the track's comment; returns the stored value and the new ETag.

    ``client`` carries the engine's base URL (tests pass the app's own
    ``TestClient``). A 404, a stale ETag (409) or any other non-2xx answer is
    a :class:`TrackCommentError`, never a silent no-op.
    """
    path = _track_path(stable_id)
    read = client.get(path)
    if not read.is_success:
        raise _refuse(read, f"GET {path}")
    etag = read.headers.get("ETag")
    if not etag:
        raise TrackCommentError("track_comment_failed", f"GET {path} answered without an ETag", read.status_code)
    written = client.patch(path, json={"comments": comment}, headers={"If-Match": etag})
    if not written.is_success:
        raise _refuse(written, f"PATCH {path}")
    stored = written.json().get("provenance", {}).get("comments", {})
    return {"stable_id": stable_id, "comments": stored.get("value"), "etag": written.headers.get("ETag")}


def engine_client(base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT_S, trust_env=False)
