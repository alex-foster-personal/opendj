"""ETag helper: ``sha1(stable_id + ":" + modified_at)`` per D6 / CAT-05.

The ``modified_at`` field is the track's ``updated_at`` RFC3339 timestamp
(or any monotonically-advancing row-version string). Two writers to the
same row at the same instant will collide, but the race is resolved by
the SQLite writer serialising all writes.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from fastapi import HTTPException


def compute_etag(stable_id: str, modified_at: str, variant: str = "") -> str:
    """Return a quoted sha1 etag: ``"<40 hex>"``.

    Quotes match RFC 7232 strong-validator format so clients can compare
    byte-for-byte.

    ``variant`` distinguishes two representations of one resource that share
    a timestamp. A strong validator has to change whenever the bytes do, and
    a track's bytes change when an analysis lane switches from rekordbox to
    own analysis even though nothing about the track was modified. Empty by
    default, and an empty variant hashes the SAME bytes this function has
    always hashed, so every existing etag is unchanged.
    """
    raw = f"{stable_id}:{modified_at}".encode("utf-8")
    if variant:
        raw += b"|" + variant.encode()
    return '"' + hashlib.sha1(raw).hexdigest() + '"'


def strip_quotes(etag: str) -> str:
    """Return the etag value without surrounding quotes (if any).

    Useful when a client sends ``W/"..."`` or the header arrives stripped.
    """
    e = etag.strip()
    if e.startswith("W/"):
        e = e[2:]
    return e.strip('"')


class _TrackETagFields(Protocol):
    stable_id: str
    updated_at: str
    selection_tag: str


def require_current_etag(
    track: _TrackETagFields,
    if_match: str | None,
    *,
    precondition_message: str,
    conflict_code: str,
    conflict_message: str,
) -> None:
    """Reject a write based on a stale track view before mutating state."""
    if not if_match:
        raise HTTPException(status_code=428, detail={
            "code": "PRECONDITION_REQUIRED",
            "message": precondition_message,
        })
    current = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    if strip_quotes(current) != strip_quotes(if_match):
        raise HTTPException(status_code=409, detail={
            "code": conflict_code,
            "message": conflict_message,
        })


__all__ = ["compute_etag", "require_current_etag", "strip_quotes"]
