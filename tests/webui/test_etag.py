"""Unit tests for apps.webui.server.etag.require_current_etag."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

from apps.webui.server.etag import compute_etag, require_current_etag


@dataclass(frozen=True)
class _TrackStub:
    stable_id: str
    updated_at: str
    selection_tag: str


def _track() -> _TrackStub:
    return _TrackStub(stable_id="sid-1", updated_at="2026-01-01T00:00:00Z", selection_tag="")


def test_require_current_etag_missing_none_raises_428() -> None:
    track = _track()
    msg = "POST /relocate/{stable_id}/apply requires If-Match"
    with pytest.raises(HTTPException) as exc:
        require_current_etag(
            track,
            None,
            precondition_message=msg,
            conflict_code="RELOCATE_ETAG_CONFLICT",
            conflict_message="stale",
        )
    assert exc.value.status_code == 428
    assert exc.value.detail == {"code": "PRECONDITION_REQUIRED", "message": msg}


def test_require_current_etag_missing_empty_raises_428() -> None:
    track = _track()
    msg = "track location writes require If-Match (the track ETag)"
    with pytest.raises(HTTPException) as exc:
        require_current_etag(
            track,
            "",
            precondition_message=msg,
            conflict_code="TRACK_ETAG_CONFLICT",
            conflict_message="stale",
        )
    assert exc.value.status_code == 428
    assert exc.value.detail == {"code": "PRECONDITION_REQUIRED", "message": msg}


def test_require_current_etag_stale_relocate_conflict() -> None:
    track = _track()
    with pytest.raises(HTTPException) as exc:
        require_current_etag(
            track,
            '"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"',
            precondition_message="ignored",
            conflict_code="RELOCATE_ETAG_CONFLICT",
            conflict_message="track changed since candidates were selected; refresh and choose again",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "RELOCATE_ETAG_CONFLICT"


def test_require_current_etag_stale_track_conflict() -> None:
    track = _track()
    with pytest.raises(HTTPException) as exc:
        require_current_etag(
            track,
            '"deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"',
            precondition_message="ignored",
            conflict_code="TRACK_ETAG_CONFLICT",
            conflict_message="track changed since it was read; refresh and retry",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "TRACK_ETAG_CONFLICT"


def test_require_current_etag_current_match() -> None:
    track = _track()
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    require_current_etag(
        track,
        etag,
        precondition_message="ignored",
        conflict_code="X",
        conflict_message="y",
    )


def test_require_current_etag_unquoted_and_weak_match() -> None:
    track = _track()
    etag = compute_etag(track.stable_id, track.updated_at, track.selection_tag)
    inner = etag.strip('"')
    for client_value in (inner, f"W/{etag}"):
        require_current_etag(
            track,
            client_value,
            precondition_message="ignored",
            conflict_code="X",
            conflict_message="y",
        )
