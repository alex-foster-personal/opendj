"""Pin follow-up replies (issue #905), split out of ``feedback_pins.py``.

Same ``/feedback`` prefix, same tag, same paths: this is a source-file split
for readability and the 600-line file budget, not an API change. ``feedback.py``
still owns the comment store (its files, its load/save helpers) and this module
borrows them.

Endpoints (all under /api/v1/feedback):

    POST /feedback/comments/{id}/replies   append a follow-up on the same pin
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from apps.opendj_pins.http import seed_replies_from_agent_note

from .feedback import (
    _COMMENTS_FILE,
    _COMMENTS_LOCK,
    CommentOut,
    CommentReplyIn,
    _dir,
    _load,
    _now,
    _save,
)

router = APIRouter(prefix="/feedback", tags=["feedback"])
MOUNT_UNDER_API_PREFIX = True


@router.post(
    "/comments/{comment_id}/replies",
    response_model=CommentOut,
    summary="Append a follow-up on the same pin",
    description=(
        "Appends a follow-up comment on the same pin without creating a second "
        "marker. This is not POST /follow-on, which opens a new pin at the same "
        "anchor only after the parent is fixed or merged. The agent_note scalar "
        "remains the latest agent lifecycle note; PATCH of agent_note also appends "
        "an agent reply unless it duplicates the last agent turn."
    ),
)
def add_reply(comment_id: str, body: CommentReplyIn, request: Request) -> CommentOut:
    """Append one follow-up turn on an existing pin (issue #905)."""
    text = body.text.strip()
    if text == "":
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_REPLY", "message": "reply text must not be blank"},
        )

    path = _dir(request) / _COMMENTS_FILE
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        for i, item in enumerate(items):
            if item.get("id") != comment_id:
                continue
            now = _now()
            merged = dict(item)
            seed_replies_from_agent_note(merged, now)
            replies = list(merged.get("replies") or [])
            replies.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "author": body.author,
                    "text": text,
                    "created_at": now,
                    "agent_kind": body.agent_kind,
                }
            )
            merged["replies"] = replies
            if body.author == "agent":
                merged["agent_note"] = text
            merged["updated_at"] = now
            if merged.get("fixed_in_sha") and (
                merged.get("status") in {"fixed", "merged"} or body.author == "operator"
            ):
                merged["status"] = "open"
            validated = CommentOut.model_validate(merged)
            items[i] = validated.model_dump()
            _save(path, "comments", items)
            return validated
    raise HTTPException(
        status_code=404,
        detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
    )
