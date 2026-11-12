"""Mountable FastAPI router for comment pins (OpenAPI feedback tag)."""

from __future__ import annotations

import io
import re
import uuid
import warnings
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from apps.opendj_pins.merge import keep_unknown_fields
from apps.opendj_pins.models import (
    AttachmentOut,
    BuildStampOut,
    CommentCreateIn,
    CommentListOut,
    CommentOut,
    CommentReplyIn,
    CommentUpdateIn,
    PinEnvironmentOut,
)
from apps.opendj_pins.replies import append_agent_note_to_replies, seed_replies_from_agent_note
from apps.opendj_pins.store import PinsStore, filter_comments_by_state, utc_now

router = APIRouter(prefix="/feedback", tags=["feedback"])

_BLOCKED_REQUEST = re.compile(r"^(auth|destructive-action|product-fork): the maintainer must .+[.!?](?:\s|$)")
_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_EXT_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_CONTENT_TYPE_BY_EXT = {ext: ct for ct, ext in _EXT_BY_CONTENT_TYPE.items()}
_PIL_FORMAT_BY_CONTENT_TYPE = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _store(request: Request) -> PinsStore:
    store = getattr(request.app.state, "opendj_pins_store", None)
    if store is None:
        raise RuntimeError("opendj_pins_store is not configured on app.state")
    return store


def default_build_stamp() -> BuildStampOut:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return BuildStampOut(git_sha="opendj-pins", built_at_utc=stamp, source="package")


def build_stamp_for_request(request: Request) -> BuildStampOut:
    factory = getattr(request.app.state, "opendj_pins_build_stamp", None)
    if callable(factory):
        return factory(request)
    return default_build_stamp()


def pin_environment_for_request(body: CommentCreateIn, request: Request) -> PinEnvironmentOut | None:
    factory = getattr(request.app.state, "opendj_pins_environment", None)
    if callable(factory):
        return factory(body, request)
    machine = getattr(request.app.state, "hostname", None) or "mountable-host"
    version = getattr(request.app.state, "version", None) or "opendj-pins"
    return PinEnvironmentOut(
        ui=body.ui,
        viewport_width=body.viewport_width,
        viewport_height=body.viewport_height,
        machine=str(machine),
        release_version=str(version),
        user_email=None,
        ui_config=body.ui_config,
    )


def _has_actionable_blocked_request(agent_note: object) -> bool:
    return isinstance(agent_note, str) and _BLOCKED_REQUEST.match(agent_note) is not None


@router.get("/comments", response_model=CommentListOut)
def list_comments(request: Request, state: str | None = None) -> CommentListOut:
    store = _store(request)
    try:
        items = filter_comments_by_state(store.list_comments(), state)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "FEEDBACK_INVALID_STATE", "message": str(exc)},
        ) from exc
    return CommentListOut(comments=[CommentOut.model_validate(c) for c in items])


@router.post("/comments", response_model=CommentOut, status_code=201)
def create_comment(body: CommentCreateIn, request: Request) -> CommentOut:
    store = _store(request)
    comment = CommentOut(
        id=uuid.uuid4().hex[:12],
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        anchor=body.anchor,
        page=body.page,
        text=body.text,
        created_at=utc_now(),
        build=build_stamp_for_request(request),
        environment=pin_environment_for_request(body, request),
        status="open",
        author=body.author,
        agent_kind=body.agent_kind,
        element_offset=body.element_offset,
        nearby_anchors=body.nearby_anchors,
    )
    store.put_comment(comment.id, comment.model_dump())
    return comment


@router.patch("/comments/{comment_id}", response_model=CommentOut)
def update_comment(comment_id: str, body: CommentUpdateIn, request: Request) -> CommentOut:
    store = _store(request)
    item = store.get_comment(comment_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
        )
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=422,
            detail={"code": "NO_CHANGES", "message": "body carries no field to update"},
        )
    now = utc_now()
    merged = {**item, **changes, "updated_at": now}
    blocked_note = changes.get("agent_note") if changes.get("status") == "blocked" else merged.get("agent_note")
    if merged.get("status") == "blocked" and not _has_actionable_blocked_request(blocked_note):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BLOCKED_PIN_NEEDS_REQUEST",
                "message": "blocked pins need an agent_note naming what the maintainer must provide",
            },
        )
    agent_note = changes.get("agent_note")
    if isinstance(agent_note, str) and agent_note.strip() != "":
        append_agent_note_to_replies(merged, agent_note, now)
    if merged.get("status") in {"fixed", "merged"}:
        if not merged.get("fixed_in_sha"):
            merged["fixed_in_sha"] = changes.get("fixed_in_sha") or build_stamp_for_request(request).git_sha
        if not merged.get("fixed_at"):
            merged["fixed_at"] = now
    validated = CommentOut.model_validate(merged)
    store.put_comment(comment_id, keep_unknown_fields(item, validated.model_dump()))
    return validated


@router.post("/comments/{comment_id}/replies", response_model=CommentOut)
def add_reply(comment_id: str, body: CommentReplyIn, request: Request) -> CommentOut:
    text = body.text.strip()
    if text == "":
        raise HTTPException(
            status_code=422,
            detail={"code": "EMPTY_REPLY", "message": "reply text must not be blank"},
        )
    store = _store(request)
    item = store.get_comment(comment_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
        )
    now = utc_now()
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
    if merged.get("fixed_in_sha") and (merged.get("status") in {"fixed", "merged"} or body.author == "operator"):
        merged["status"] = "open"
    validated = CommentOut.model_validate(merged)
    store.put_comment(comment_id, keep_unknown_fields(item, validated.model_dump()))
    return validated


@router.post("/comments/{comment_id}/attachment", response_model=CommentOut, status_code=201)
async def upload_attachment(comment_id: str, request: Request, file: Annotated[UploadFile, File()]) -> CommentOut:
    content_type = file.content_type or ""
    ext = _EXT_BY_CONTENT_TYPE.get(content_type)
    if ext is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "ATTACHMENT_TYPE_UNSUPPORTED",
                "message": f"unsupported attachment type {content_type!r}",
            },
        )
    data = await file.read(_MAX_ATTACHMENT_BYTES + 1)
    if len(data) > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail={"code": "ATTACHMENT_TOO_LARGE", "message": "too large"})
    if not data:
        raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_EMPTY", "message": "empty upload"})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(
                io.BytesIO(data),
                formats=[_PIL_FORMAT_BY_CONTENT_TYPE[content_type]],
            )
            decoded_format = image.format
            image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(status_code=413, detail={"code": "ATTACHMENT_TOO_LARGE", "message": str(exc)}) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "ATTACHMENT_NOT_AN_IMAGE", "message": str(exc)},
        ) from exc
    if decoded_format != _PIL_FORMAT_BY_CONTENT_TYPE[content_type]:
        raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_TYPE_MISMATCH", "message": "type mismatch"})

    store = _store(request)
    item = store.get_comment(comment_id)
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
        )
    attachment_id = uuid.uuid4().hex[:12]
    path = store.attachments_dir / f"{attachment_id}.{ext}"
    path.write_bytes(data)
    attachment = AttachmentOut(
        id=attachment_id,
        content_type=content_type,
        size_bytes=len(data),
        url=store.attachment_url(attachment_id),
    )
    merged = {**item, "attachment": attachment.model_dump(), "updated_at": utc_now()}
    validated = CommentOut.model_validate(merged)
    store.put_comment(comment_id, validated.model_dump())
    return validated


@router.get(
    "/attachments/{attachment_id}",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
)
def get_attachment(attachment_id: str, request: Request) -> FileResponse:
    if not _ATTACHMENT_ID_RE.match(attachment_id):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND", "message": "not found"})
    store = _store(request)
    matches = list(store.attachments_dir.glob(f"{attachment_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND", "message": "not found"})
    path = matches[0]
    content_type = _CONTENT_TYPE_BY_EXT.get(path.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(path, media_type=content_type)


def mark_pin_harvested(store: PinsStore, comment_id: str, *, when: str | None = None) -> CommentOut:
    """Server-side harvest marker used by tests and future harvest callers."""

    item = store.get_comment(comment_id)
    if item is None:
        raise KeyError(comment_id)
    now = when or utc_now()
    merged = {**item, "status": "harvested", "harvested_at": now, "updated_at": now}
    validated = CommentOut.model_validate(merged)
    store.put_comment(comment_id, validated.model_dump())
    return validated


def mark_anchor_moved(store: PinsStore, comment_id: str, new_anchor: str) -> CommentOut:
    """Persist anchor drift while keeping pixel position canonical."""

    item = store.get_comment(comment_id)
    if item is None:
        raise KeyError(comment_id)
    merged = {
        **item,
        "anchor": new_anchor,
        "updated_at": utc_now(),
    }
    validated = CommentOut.model_validate(merged)
    store.put_comment(comment_id, validated.model_dump())
    return validated
