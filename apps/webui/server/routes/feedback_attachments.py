"""Screenshot attachments for comment pins (issue #1333, part 2 of #928).

Split out of ``feedback.py``/``feedback_pins.py`` the same way (600-line file
budget): this module owns the one thing neither of those needs - storing and
serving the image bytes a pin can carry. ``comments.json`` only ever holds the
small ``AttachmentOut`` record (id, content_type, size_bytes, url); the bytes
themselves live one level down, under ``<data-dir>/feedback/attachments/``.

Endpoints (all under /api/v1/feedback):

    POST /feedback/comments/{comment_id}/attachment   attach one image to a pin
    GET  /feedback/attachments/{attachment_id}         fetch the stored bytes

Size/type policy: PNG/JPEG/GIF/WebP only, up to MAX_ATTACHMENT_BYTES. A
refusal is always an explicit 4xx with a message naming why - never a
silent drop - and nothing is written to disk until the upload has passed
every check, so a rejected upload leaves no orphan file behind.
"""

from __future__ import annotations

import io
import re
import uuid
import warnings
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError

from .feedback import (
    _COMMENTS_FILE,
    _COMMENTS_LOCK,
    AttachmentOut,
    CommentOut,
    _dir,
    _load,
    _now,
    _save,
)

router = APIRouter(prefix="/feedback", tags=["feedback"])

# CFG: the size/type policy an agent or reviewer can rely on without reading
# this file. 8 MiB comfortably covers a full-screen screenshot; PNG/JPEG/GIF/
# WebP cover every format a browser paste or drag-drop actually produces.
# apps/webui/frontend/src/lib/rb/feedback-pin-attachment.ts mirrors both
# numbers for an immediate client-side refusal before a round trip; this
# server copy is the enforced source of truth either way, so a drift between
# the two only costs a slower refusal, never a wrong one.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
_EXT_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}
_CONTENT_TYPE_BY_EXT = {ext: ct for ct, ext in _EXT_BY_CONTENT_TYPE.items()}
# The Pillow `Image.format` string a successfully decoded upload must carry
# for each declared Content-Type (PR #1425 P2): `Image.open(...).verify()`
# alone only proves the bytes decode as SOME image, not the CLAIMED one, so
# real GIF bytes posted with `Content-Type: image/png` would otherwise be
# stored as `<id>.png` and served back as `image/png` - a wrong answer for
# any agent trusting the recorded content_type.
_PIL_FORMAT_BY_CONTENT_TYPE = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}

# Attachment ids are always uuid4().hex[:12], minted below - never taken from
# a caller. A path segment that does not match this shape can never be a real
# attachment, so it is refused as a 404 lookup miss rather than glob()ed.
_ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ----- storage --------------------------------------------------------------
def _attachments_dir(request: Request) -> Path:
    return _dir(request) / "attachments"


def _find_attachment_file(request: Request, attachment_id: str) -> Path | None:
    if not _ATTACHMENT_ID_RE.match(attachment_id):
        return None
    matches = list(_attachments_dir(request).glob(f"{attachment_id}.*"))
    return matches[0] if matches else None


def _delete_attachment_file(attachments_dir: Path, attachment_id: str) -> None:
    # attachment_id here is read back out of comments.json (an old
    # attachment's id), not one just minted like the upload path above -
    # guard its shape the same way _find_attachment_file does before this
    # destructive twin ever reaches glob() (PR #1425 P3). A hand-edited
    # file, a future writer, or a restored backup could otherwise leave a
    # `*`/`?` in the id and unlink unrelated attachments with no bound on
    # the blast radius.
    if not _ATTACHMENT_ID_RE.match(attachment_id):
        return
    for stale in attachments_dir.glob(f"{attachment_id}.*"):
        stale.unlink(missing_ok=True)


# ----- upload -----------------------------------------------------------------
@router.post("/comments/{comment_id}/attachment", response_model=CommentOut, status_code=201)
async def upload_attachment(
    comment_id: str, request: Request, file: Annotated[UploadFile, File()]
) -> CommentOut:
    """Attach one screenshot to an existing pin (issue #1333).

    Type and size are checked, and the bytes are verified to actually decode
    as the claimed image type, BEFORE anything touches disk or comments.json -
    a declared Content-Type header is never trusted alone. Replaces any
    attachment the pin already carried, deleting its old file so attachments
    never accumulate orphans.
    """
    content_type = file.content_type or ""
    ext = _EXT_BY_CONTENT_TYPE.get(content_type)
    if ext is None:
        raise HTTPException(
            status_code=415,
            detail={
                "code": "ATTACHMENT_TYPE_UNSUPPORTED",
                "message": (
                    f"unsupported attachment type {content_type!r}; allowed: "
                    f"{', '.join(sorted(_EXT_BY_CONTENT_TYPE))}"
                ),
            },
        )

    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "ATTACHMENT_TOO_LARGE",
                "message": f"attachment exceeds the {MAX_ATTACHMENT_BYTES} byte limit",
            },
        )
    if not data:
        raise HTTPException(
            status_code=422,
            detail={"code": "ATTACHMENT_EMPTY", "message": "empty upload"},
        )
    try:
        # verify() decodes only metadata, not full pixel data, and is
        # Pillow's documented way to reject a file that merely CLAIMS to be
        # an image via its Content-Type header. Its documented failure modes
        # for bad input are UnidentifiedImageError (unrecognized format) and
        # OSError/SyntaxError/ValueError (recognized but corrupt/truncated).
        #
        # DecompressionBombWarning/-Error (PR #1425 P2) are raised by
        # Image.open() itself, from the declared pixel dimensions alone,
        # before any pixel data is read - a few-KB file can claim a
        # 60000x60000 canvas and pass the byte-size check above outright.
        # DecompressionBombError subclasses Exception directly (not
        # OSError/SyntaxError/ValueError), so it does not get caught by the
        # tuple below on its own; the warning variant (raised for a size
        # between 1x and 2x Image.MAX_IMAGE_PIXELS - the error fires past
        # 2x) is promoted to an exception here so both shapes of bomb are
        # refused with an explicit 4xx instead of a bare 500 or a silent
        # decode of an oversized image.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            decoded_format = image.format
            image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "ATTACHMENT_TOO_LARGE",
                "message": f"upload decodes to an image that is too large: {exc}",
            },
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ATTACHMENT_NOT_AN_IMAGE",
                "message": f"upload does not decode as {content_type}: {exc}",
            },
        ) from exc
    if decoded_format != _PIL_FORMAT_BY_CONTENT_TYPE[content_type]:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ATTACHMENT_TYPE_MISMATCH",
                "message": (
                    f"declared Content-Type {content_type!r} does not match the "
                    f"decoded image format {decoded_format!r}"
                ),
            },
        )

    comments_path = _dir(request) / _COMMENTS_FILE
    attachments_dir = _attachments_dir(request)
    with _COMMENTS_LOCK:
        items = _load(comments_path, "comments")
        for i, item in enumerate(items):
            if item.get("id") != comment_id:
                continue
            attachments_dir.mkdir(parents=True, exist_ok=True)
            old_attachment = item.get("attachment")
            attachment_id = uuid.uuid4().hex[:12]
            (attachments_dir / f"{attachment_id}.{ext}").write_bytes(data)
            attachment = AttachmentOut(
                id=attachment_id,
                content_type=content_type,
                size_bytes=len(data),
                url=f"/api/v1/feedback/attachments/{attachment_id}",
            )
            merged = {**item, "attachment": attachment.model_dump(), "updated_at": _now()}
            validated = CommentOut.model_validate(merged)
            items[i] = validated.model_dump()
            _save(comments_path, "comments", items)
            if old_attachment is not None and old_attachment.get("id"):
                _delete_attachment_file(attachments_dir, old_attachment["id"])
            return validated
    raise HTTPException(
        status_code=404,
        detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
    )


# ----- fetch ------------------------------------------------------------------
@router.get(
    "/attachments/{attachment_id}",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
)
def get_attachment(attachment_id: str, request: Request) -> FileResponse:
    """Stream a stored attachment's bytes back - the agent-native fetch half."""
    path = _find_attachment_file(request, attachment_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ATTACHMENT_NOT_FOUND",
                "message": f"no attachment with id {attachment_id!r}",
            },
        )
    content_type = _CONTENT_TYPE_BY_EXT.get(path.suffix.lstrip("."), "application/octet-stream")
    return FileResponse(path, media_type=content_type)
