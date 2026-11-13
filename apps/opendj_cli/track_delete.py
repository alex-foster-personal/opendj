"""``opendj track delete <stable_id> [delete-file] yes``: the two additive delete tiers (LIBM-53).

The CLI twin of ``POST /api/v1/tracks/{id}:delete``. It reads the track's ETag, then posts the
tiers under ``If-Match`` with ``confirm: true``. ``yes`` is required: without it nothing is sent. Words, not
``--options``: the top-level parser refuses options it does not know.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

TIER_WORDS = {"delete-file": "delete_file"}


class TrackDeleteError(Exception):
    """The engine refused or failed a track delete."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _refusal(response: httpx.Response, call: str) -> TrackDeleteError:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    code = detail["code"] if isinstance(detail, dict) and "code" in detail else "track_delete_failed"
    return TrackDeleteError(code, f"{call} returned {response.status_code}: {response.text}")


def delete_track(client: httpx.Client, stable_id: str, tiers: set[str]) -> dict[str, Any]:
    """Remove ``stable_id`` from the collection plus the extra ``tiers`` (``delete_file``)."""
    path = f"/api/v1/tracks/{quote(stable_id, safe='')}"
    read = client.get(path)
    if not read.is_success:
        raise _refusal(read, f"GET {path}")
    etag = read.headers.get("ETag")
    if not etag:
        raise TrackDeleteError("track_delete_failed", f"GET {path} answered without an ETag")
    body = {"remove_from_collection": True, **{tier: tier in tiers for tier in TIER_WORDS.values()}, "confirm": True}
    written = client.post(f"{path}:delete", json=body, headers={"If-Match": etag})
    if not written.is_success:
        raise _refusal(written, f"POST {path}:delete")
    return written.json()
