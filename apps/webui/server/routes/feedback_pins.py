"""Pin lifecycle routes (issue #858), split out of ``feedback.py``.

Same ``/feedback`` prefix, same tag, same paths: this is a source-file split
for readability and the 600-line file budget, not an API change. ``feedback.py``
still owns the comment store (its files, its load/save helpers and the build
stamp) and this module borrows them, because two modules writing
``comments.json`` through two copies of the same helpers is how the two copies
drift apart.

Endpoints (all under /api/v1/feedback):

    PATCH  /feedback/comments/{id}             pin lifecycle: status/issue_url/note
    POST   /feedback/comments/{id}/archive     archive ONE pin, with history
    POST   /feedback/comments/{id}/follow-on   new open pin at the same anchor
                                                (409 unless parent fixed/merged)
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .feedback import (
    _COMMENTS_FILE,
    _COMMENTS_LOCK,
    CommentOut,
    _build_stamp,
    _dir,
    _load,
    _now,
    _save,
    keep_unknown_fields,
    write_atomic,
)
from .feedback_replies import append_agent_note_to_replies

router = APIRouter(prefix="/feedback", tags=["feedback"])

# `archived` is only ever reached through POST /comments/{id}/archive, which
# performs the archive-file transaction alongside the removal. Allowing it
# here would let a plain PATCH make a pin vanish from the board with no
# corresponding archive-file entry.
_PATCHABLE_STATUSES = "^(open|issued|blocked|fixed|merged)$"
_BLOCKED_REQUEST = re.compile(
    r"^(auth|destructive-action|product-fork): the maintainer must .+[.!?](?:\s|$)"
)


# ----- models -------------------------------------------------------------
class CommentUpdateIn(BaseModel):
    text: str = Field(default=None, min_length=1)
    status: str | None = Field(default=None, pattern=_PATCHABLE_STATUSES)
    issue_url: str | None = None
    agent_note: str | None = None
    fixed_in_sha: str | None = Field(default=None, min_length=7, max_length=40)


class CommentFollowOnIn(BaseModel):
    """Optional extra text appended after the generated parent reference."""

    model_config = ConfigDict(frozen=True)

    text: str | None = None


class CommentArchiveOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    archived_to: str
    comment: CommentOut


# ----- storage ------------------------------------------------------------
def _append_to_archive(root: Path, comment: dict[str, Any]) -> Path:
    """Put one archived pin in the newest archive file, or start one.

    Archive files are named ``archive-<UTC stamp>.json``, so newest by name IS
    newest by time. A malformed newest file raises rather than being replaced:
    archives are never deleted, and quietly starting a fresh one beside a
    broken file is how history goes missing.
    """
    existing = sorted(root.glob("archive-*.json"))
    if existing:
        path = existing[-1]
        payload = json.loads(path.read_text(encoding="utf-8"))
        comments = payload.get("comments") if isinstance(payload, dict) else None
        if not isinstance(comments, list):
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "FEEDBACK_ARCHIVE_MALFORMED",
                    "message": f"{path} does not hold a 'comments' list",
                },
            )
        comments.append(comment)
        payload["comments"] = comments
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = root / f"archive-{stamp}.json"
        payload = {
            "archived_at": _now(),
            "todos": [],
            "todo_feedback": [],
            "comments": [comment],
            "general": None,
        }
    write_atomic(path, json.dumps(payload, indent=2) + "\n")
    return path


# ----- pin lifecycle ------------------------------------------------------
def _has_actionable_blocked_request(agent_note: object) -> bool:
    """Require a permitted category and a first sentence naming the maintainer's action."""
    return isinstance(agent_note, str) and _BLOCKED_REQUEST.match(agent_note) is not None


@router.patch("/comments/{comment_id}", response_model=CommentOut)
def update_comment(comment_id: str, body: CommentUpdateIn, request: Request) -> CommentOut:
    """Agent-native half of the pin lifecycle (issue #858, first slice).

    A pin is never deleted by a fix. The agent that acts on it records what it
    did here so the pin itself shows progress: ``status`` moves
    open -> issued -> fixed -> merged, ``issue_url`` links the queue item, and
    ``agent_note`` is the one-paragraph reply the widget renders under the
    original text. ``blocked`` is deliberately narrow: use it only when the maintainer
    must supply credentials/auth, make a destructive-action decision, or choose
    a genuine product fork. "I could not work out what you meant" is a question
    in the note, never blocked. A blocked pin's note starts with one sentence
    saying exactly what the maintainer needs, before any supporting detail. Its first
    sentence is `auth: the maintainer must ...`, `destructive-action: the maintainer must ...`, or
    `product-fork: the maintainer must ...`. Every write is a partial update; unset fields
    are untouched.

    Partial-fix convention (pin 58a16ac781db, follow-on to #907): when only
    PART of a pin's defect is fixed, do NOT invent a new ``status`` value
    (there isn't one, and there is no plan to add one - see that pin's PR
    body for the sizing call). Leave ``status`` at ``open`` or ``issued`` and
    write an ``agent_note`` that starts with the literal prefix ``PARTIAL:``
    and names where the remaining work went (a ``#123`` issue/PR reference,
    or a URL) - e.g. ``"PARTIAL: markers fixed, remainder tracked in #1150"``.
    The frontend (``pinVisualState`` in ``feedback.ts``) recognises that
    convention and paints the pin's marker half orange/half green instead of
    plain amber. A ``PARTIAL:`` note with nothing to point at is NOT
    recognised - a partial pin must always say where the rest of the work is.
    """
    path = _dir(request) / _COMMENTS_FILE
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        for i, item in enumerate(items):
            if item.get("id") != comment_id:
                continue
            changes = body.model_dump(exclude_unset=True)
            if not changes:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "NO_CHANGES", "message": "body carries no field to update"},
                )
            # Validate BEFORE persisting, as patch_todo does. A rejected patch
            # must leave the store exactly as it was: comments.json is read
            # back through CommentOut on every list, so one unvalidated write
            # would make GET /comments fail for every pin, not just this one.
            now = _now()
            merged = {**item, **changes, "updated_at": now}
            requested_blocked = changes.get("status") == "blocked"
            blocked_note = (
                changes.get("agent_note") if requested_blocked else merged.get("agent_note")
            )
            blocked_without_request = (
                merged.get("status") == "blocked"
                and not _has_actionable_blocked_request(blocked_note)
            )
            if blocked_without_request:
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
            new_status = merged.get("status")
            if new_status in {"fixed", "merged"}:
                if not merged.get("fixed_in_sha"):
                    stamp = _build_stamp(request)
                    merged["fixed_in_sha"] = changes.get("fixed_in_sha") or stamp.git_sha
                if not merged.get("fixed_at"):
                    merged["fixed_at"] = now
            validated = CommentOut.model_validate(merged)
            # A synced pin may carry fields a newer build added (ADR-0013):
            # an edit here must not strip them from every machine.
            items[i] = keep_unknown_fields(merged, validated.model_dump())
            _save(path, "comments", items)
            return validated
    raise HTTPException(
        status_code=404,
        detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
    )


@router.post("/comments/{comment_id}/archive", response_model=CommentArchiveOut)
def archive_comment(comment_id: str, request: Request) -> CommentArchiveOut:
    """Archive ONE pin, with its full history (issue #858).

    The bulk ``POST /feedback/archive`` empties the board at harvest time.
    This is the button on a pin whose work is done: it moves that pin alone
    into the archive, keeping every field it accumulated (status, issue_url,
    agent_note, build stamp, created_at) so the archive answers "what was
    this, and what happened to it" without the live board.

    It APPENDS to the newest existing archive file rather than writing one
    file per press, which would scatter a review pass across dozens of files.

    Rejects an `open` or `issued` pin: this endpoint is agent-facing (not
    just the widget, which already hides the button before `fixed`/`merged`),
    so the lifecycle invariant - only DONE work is archived - has to be
    enforced here too, or an agent PATCHing straight to this route can drop
    an unfinished item off the live board.

    The whole read/append/delete/save sequence holds `_COMMENTS_LOCK`, the
    same lock every other comments.json writer holds, so this cannot
    interleave with a concurrent archive, PATCH, follow-on, or new pin.
    """
    root = _dir(request)
    path = root / _COMMENTS_FILE
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        for i, item in enumerate(items):
            if item.get("id") != comment_id:
                continue
            status = item.get("status") or "open"
            if status not in {"fixed", "merged"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PIN_NOT_DONE",
                        "message": f"pin {comment_id!r} is {status!r}; archive needs fixed/merged",
                    },
                )
            archived = {**item, "status": "archived", "updated_at": _now()}
            archive_path = _append_to_archive(root, archived)
            del items[i]
            _save(path, "comments", items)
            return CommentArchiveOut(
                archived_to=str(archive_path),
                comment=CommentOut.model_validate(archived),
            )
    raise HTTPException(
        status_code=404,
        detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
    )


@router.post("/comments/{comment_id}/follow-on", response_model=CommentOut, status_code=201)
def follow_on_comment(
    comment_id: str, request: Request, body: CommentFollowOnIn | None = None
) -> CommentOut:
    """Open a new pin at the same anchor, referencing the parent (issue #858).

    "More work is needed here" is a new pin, not an edit: the parent keeps its
    own status and history. The text opens with the parent's issue URL when
    one was filed and its pin id otherwise, so a follow-on is never an orphan.
    """
    path = _dir(request) / _COMMENTS_FILE
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        for parent in items:
            if parent.get("id") != comment_id:
                continue
            status = parent.get("status") or "open"
            # open/issued parents are refused with 409 PIN_NOT_DONE, mirroring archive
            if status not in {"fixed", "merged"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PIN_NOT_DONE",
                        "message": (
                            f"pin {comment_id!r} is {status!r}; follow-on needs fixed/merged"
                        ),
                    },
                )
            reference = parent.get("issue_url") or parent.get("id")
            extra = (body.text if body is not None else None) or ""
            child = CommentOut(
                id=uuid.uuid4().hex[:12],
                x_pct=parent["x_pct"],
                y_pct=parent["y_pct"],
                anchor=parent.get("anchor"),
                page=parent["page"],
                text=f"Follow-on to {reference}: {extra}".rstrip(),
                created_at=_now(),
                build=_build_stamp(request),
                status="open",
                author=parent.get("author", "operator"),
                agent_kind=parent.get("agent_kind"),
                element_offset=parent.get("element_offset"),
                nearby_anchors=parent.get("nearby_anchors") or [],
            )
            items.append(child.model_dump())
            _save(path, "comments", items)
            return child
    raise HTTPException(
        status_code=404,
        detail={"code": "COMMENT_NOT_FOUND", "message": f"no comment with id {comment_id!r}"},
    )
