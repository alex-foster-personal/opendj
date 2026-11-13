"""HTTP read and write for the Add My Tag to Comments setting (rekordbox option, issue #10025).

``GET /api/v1/mytag-comments`` reads the setting; ``PUT`` sets it and sweeps every track in the same call: on adds
each track's ``/* a / b */`` block, off strips it, and the answer counts the tracks rewritten (0 when in sync).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, StrictBool

from apps.shared.events import publish

from ..backend import BatchConflictError, StateBackend
from ..deps import get_write_state
from ..mytag_comment_sync import read_mytag_to_comments, sweep_mytag_comments, write_mytag_to_comments

router = APIRouter(prefix="/mytag-comments", tags=["mytags"])
MOUNT_UNDER_API_PREFIX = True


class MyTagCommentsSettingOut(BaseModel):
    enabled: bool


class MyTagCommentsSettingIn(BaseModel):
    enabled: StrictBool


class MyTagCommentsSweepOut(BaseModel):
    enabled: bool
    tracks_updated: int


@router.get("", response_model=MyTagCommentsSettingOut)
def get_mytag_comments_setting(request: Request) -> MyTagCommentsSettingOut:
    """Whether My Tags are mirrored into comments."""
    return MyTagCommentsSettingOut(enabled=read_mytag_to_comments(request.app))


@router.put("", response_model=MyTagCommentsSweepOut)
def put_mytag_comments_setting(
    body: MyTagCommentsSettingIn,
    request: Request,
    backend: StateBackend = Depends(get_write_state),  # noqa: B008  # FastAPI DI
) -> MyTagCommentsSweepOut:
    """Set the setting, then add or strip every track's block in one sweep."""
    write_mytag_to_comments(request.app, body.enabled)
    try:
        rewritten = sweep_mytag_comments(backend, enabled=body.enabled, source="webui")
    except BatchConflictError as exc:
        raise HTTPException(status_code=409, detail={"error": "conflict", "conflicts": exc.conflicts}) from exc
    if rewritten:
        publish("library.changed", {"kind": "tracks", "ids": rewritten})
    return MyTagCommentsSweepOut(enabled=body.enabled, tracks_updated=len(rewritten))


__all__ = ["router"]
