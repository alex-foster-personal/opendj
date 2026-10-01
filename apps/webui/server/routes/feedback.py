"""In-app review/feedback store (FB-01..FB-06).

the maintainer reviews the running app; agents queue what needs reviewing and harvest
what he wrote back. Everything lives in plain JSON under
``<data-dir>/feedback/`` so no engine store schema migration is involved:

    review-todos.json   {"todos": [...]}          things to review
    comments.json       {"comments": [...]}       comment-anywhere pins
    general-note.json   {"text", "updated_at", "build"}
    archive-<utc>.json  what a harvest moved out; NEVER deleted

Endpoints (all under /api/v1/feedback, agent-native parity with the widget):

    GET    /feedback/todos            list review todos
    POST   /feedback/todos            create one (agents queue reviews here)
    PATCH  /feedback/todos/{id}       done flag / chosen option / feedback text
    GET    /feedback/comments         list pins
    POST   /feedback/comments         drop a pin
    GET    /feedback/general          the general note
    PUT    /feedback/general          replace it (debounced client auto-save)
    POST   /feedback/archive          move harvested feedback to an archive file

The pin lifecycle routes (PATCH a pin, archive one, open a follow-on) live in
``feedback_pins.py`` on the same ``/feedback`` prefix; this module stays the
owner of the comment store they write through.

Build stamping: every write is stamped with the serving build's identity so
feedback maps to the version it was given on. On an engine boot the identity
is read from ``app.state`` (mounted by apps/engine_core/build_info.py under
BUILD_IDENTITY_STATE_ATTR = "build_identity"; the constant is not imported
because webui must not import the engine chassis). On a legacy dev boot the
checkout is described from live git, once. A failed resolution is recorded as
an explicit ``{"error": ...}`` stamp: feedback capture must never be refused
over provenance, but a missing stamp must never look like a clean one.

Archive semantics ("persist until next version release" = never auto-delete):
done todos move wholesale; open todos stay but their harvested feedback text
moves into the archive entry and resets, so a second harvest never re-prints
it; all comments move; a non-empty general note moves and resets.
"""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.paths import DATA_DIR, PROJECT_ROOT
from apps.webui.server.routes.auth import signed_in_user
from apps.webui.server.routes.feedback_pin_ui_config import PinUiConfig

# Re-exported: the sibling feedback modules import these from here.
from .feedback_storage import (  # noqa: F401
    _load,
    _load_general,
    _save,
    keep_unknown_fields,
    write_atomic,
)

router = APIRouter(prefix="/feedback", tags=["feedback"])

_COMMENT_STATE_FILTERS = frozenset({"regressed", "harvested"})

_TODOS_FILE = "review-todos.json"
_COMMENTS_FILE = "comments.json"
_GENERAL_FILE = "general-note.json"

# Every read/modify/write of comments.json, in THIS module, feedback_pins.py,
# and feedback_replies.py, holds this lock (issue #914 review, Wed 2 Sep 2026:
# a per-endpoint lock only serialized single-pin archives against each other,
# so a PATCH or follow-on could still race a concurrent archive - both report
# success and one write is silently lost, since FastAPI runs sync handlers in a
# threadpool). One lock for one file, held for the whole read-modify-write, not
# one lock per route.
_COMMENTS_LOCK = threading.Lock()

# Mirrors apps.engine_core.build_info.BUILD_IDENTITY_STATE_ATTR. Spelled here
# because importing the engine chassis from a legacy router is the package
# cycle the architecture gate exists to prevent.
_BUILD_IDENTITY_ATTR = "build_identity"


# ----- models -------------------------------------------------------------
class BuildStampOut(BaseModel):
    """Provenance of the build that recorded a piece of feedback.

    Exactly one of the two shapes: a resolved (git_sha, built_at_utc, source)
    triple, or an explicit error naming why resolution failed. Never blank.
    """

    model_config = ConfigDict(frozen=True)

    git_sha: str | None = None
    built_at_utc: str | None = None
    source: str | None = None
    error: str | None = None


class TodoOut(BaseModel):
    """Response model: every field required, so the generated client types
    say what the server always sends (no `possibly undefined` on the UI)."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    detail: str | None
    options: list[str]
    done: bool
    chosen_option: str | None
    feedback: str
    created_at: str
    updated_at: str
    build: BuildStampOut


class TodoListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    todos: list[TodoOut]


class TodoCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1)
    detail: str | None = None
    options: list[str] = Field(default_factory=list)


class TodoPatchIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(default=None, min_length=1)
    detail: str | None = None
    options: list[str] | None = None
    done: bool | None = None
    chosen_option: str | None = None
    feedback: str | None = None


class AttachmentOut(BaseModel):
    """A screenshot pasted into a comment pin (issue #1333, part 2 of #928).

    ``url`` is the GET route that streams the stored bytes back (relative,
    same convention as ``issue_url`` on ``CommentOut``); ``content_type`` and
    ``size_bytes`` are recorded once, at upload time, so a caller can decide
    whether to fetch the bytes without a HEAD round trip first. The bytes
    themselves live under ``<data-dir>/feedback/attachments/`` - see
    ``feedback_attachments.py``, the module that actually writes and serves
    them; this file only owns the small record ``comments.json`` carries.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    content_type: str
    size_bytes: int
    url: str


class CommentReplyOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    author: Literal["operator", "agent"]
    text: str
    created_at: str
    agent_kind: str | None = None


class CommentReplyIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1)
    author: Literal["operator", "agent"] = "operator"
    agent_kind: str | None = None


class CommentOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    x_pct: float
    y_pct: float
    anchor: str | None
    page: str
    text: str
    created_at: str
    build: BuildStampOut
    # Pins created before issue #904 have no environment record.
    environment: PinEnvironmentOut | None = None
    # Pin lifecycle (issue #858): absent on pins created before Wed 2 Sep 2026.
    # `blocked` is ONLY for credentials/auth, a destructive-action decision,
    # or a genuine product fork that needs the maintainer. Unclear instructions are a
    # question in agent_note, never blocked. Its agent_note starts with
    # `auth: the maintainer must ...`, `destructive-action: the maintainer must ...`, or
    # `product-fork: the maintainer must ...` to say what the maintainer must provide.
    status: str | None = None  # open | issued | blocked | fixed | merged | harvested | archived
    issue_url: str | None = None
    agent_note: str | None = None
    updated_at: str | None = None
    fixed_in_sha: str | None = None
    fixed_at: str | None = None
    harvested_at: str | None = None
    # Set when an agent bulk-harvest snapshots the pin without UI archive (#3981).
    agent_snapshot_at: str | None = None
    # PIN-AGENT-01: older operator pins retain their original identity when
    # read through this newer contract.
    author: Literal["operator", "agent"] = Field(default_factory=lambda: "operator")
    agent_kind: str | None = None
    # Screenshot pasted into the pin (issue #1333): absent until one is
    # uploaded through POST /feedback/comments/{id}/attachment.
    attachment: AttachmentOut | None = None
    # Pin follow-up thread (issue #905): absent or empty on pins before FB-13.
    replies: list[CommentReplyOut] = Field(default_factory=list)


class CommentListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    comments: list[CommentOut]


class CommentCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    x_pct: float = Field(ge=0, le=100)
    y_pct: float = Field(ge=0, le=100)
    anchor: str | None = None
    page: str = Field(min_length=1)
    text: str = Field(min_length=1)
    ui: Literal["chrome-loop", "packaged-app"]
    viewport_width: int = Field(ge=1, le=100_000)
    viewport_height: int = Field(ge=1, le=100_000)
    author: Literal["operator", "agent"] = Field(default_factory=lambda: "operator")
    agent_kind: str | None = None
    # Pin 49f9d217: optional so an agent posting a pin over HTTP need not
    # invent a UI it does not have.
    ui_config: PinUiConfig | None = None


class PinEnvironmentOut(BaseModel):
    """Runtime facts needed to reproduce a pinned UI defect.

    ``machine`` and ``release_version`` are already exposed by the running
    daemon's settings/health surfaces. The browser contributes its UI kind,
    viewport dimensions and a closed ``ui_config`` snapshot (see
    ``PinUiConfig``): no user agent, URL query, file path or track title.

    ``user_email`` is the one personal field (pin 49f9d217). The daemon stamps
    it from the session cookie, the same identity ``GET /api/v1/auth/me``
    already returns to this browser; a request body cannot set it. It is null
    when nobody is signed in, and absent on pins older than this field.
    """

    model_config = ConfigDict(frozen=True)

    ui: Literal["chrome-loop", "packaged-app"]
    viewport_width: int
    viewport_height: int
    machine: str
    release_version: str
    user_email: str | None = None
    ui_config: PinUiConfig | None = None


class GeneralNoteOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    updated_at: str | None
    build: BuildStampOut | None


class GeneralNotePutIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str


class ArchiveOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    archived_to: str | None
    todos_archived: int
    todo_feedback_archived: int
    comments_archived: int
    general_archived: bool


# ----- storage ------------------------------------------------------------
def _dir(request: Request) -> Path:
    configured = getattr(request.app.state, "data_dir", None)
    root = Path(configured) if configured is not None else DATA_DIR
    return root / "feedback"


def _now() -> str:
    # Microsecond resolution, not just seconds: two agent writes to the same
    # pin inside one second must still produce a strictly later `updated_at`,
    # or the frontend's string-order unread comparison cannot tell the second
    # write happened at all.
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ----- build stamp --------------------------------------------------------
@lru_cache(maxsize=1)
def _repo_stamp() -> BuildStampOut:
    """Describe the checkout once per process; legacy dev boots land here."""

    def _git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    try:
        sha = _git("rev-parse", "HEAD")
        committed = _git("log", "-1", "--format=%cI")
        built_at = datetime.fromisoformat(committed).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return BuildStampOut(git_sha=sha[:8], built_at_utc=built_at, source="repo")
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        return BuildStampOut(error=f"git describe of {PROJECT_ROOT} failed: {exc}")


def _build_stamp(request: Request) -> BuildStampOut:
    identity = getattr(request.app.state, _BUILD_IDENTITY_ATTR, None)
    if identity is None:
        return _repo_stamp()
    info = getattr(identity, "info", None)
    if info is not None:
        return BuildStampOut(
            git_sha=info.git_sha,
            built_at_utc=info.built_at_utc,
            source=info.source,
        )
    failure = getattr(identity, "failure", None)
    return BuildStampOut(error=str(failure))


def _pin_environment(body: CommentCreateIn, request: Request) -> PinEnvironmentOut:
    """Combine browser facts with daemon facts, including who is signed in."""

    user = signed_in_user(request)
    machine = getattr(request.app.state, "hostname", None)
    release_version = getattr(request.app.state, "version", None)
    if not isinstance(machine, str) or machine == "":
        raise RuntimeError("feedback pin environment has no published machine name")
    if not isinstance(release_version, str) or release_version == "":
        raise RuntimeError("feedback pin environment has no published release version")
    return PinEnvironmentOut(
        ui=body.ui,
        viewport_width=body.viewport_width,
        viewport_height=body.viewport_height,
        machine=machine,
        release_version=release_version,
        user_email=None if user is None else user.email,
        ui_config=body.ui_config,
    )


# ----- todos --------------------------------------------------------------
@router.get("/todos", response_model=TodoListOut)
def list_todos(request: Request) -> TodoListOut:
    items = _load(_dir(request) / _TODOS_FILE, "todos")
    return TodoListOut(todos=[TodoOut.model_validate(t) for t in items])


@router.post("/todos", response_model=TodoOut, status_code=201)
def create_todo(body: TodoCreateIn, request: Request) -> TodoOut:
    path = _dir(request) / _TODOS_FILE
    items = _load(path, "todos")
    now = _now()
    todo = TodoOut(
        id=uuid.uuid4().hex[:12],
        title=body.title,
        detail=body.detail,
        options=body.options,
        done=False,
        chosen_option=None,
        feedback="",
        created_at=now,
        updated_at=now,
        build=_build_stamp(request),
    )
    items.append(todo.model_dump())
    _save(path, "todos", items)
    return todo


@router.patch("/todos/{todo_id}", response_model=TodoOut)
def patch_todo(todo_id: str, body: TodoPatchIn, request: Request) -> TodoOut:
    path = _dir(request) / _TODOS_FILE
    items = _load(path, "todos")
    for i, item in enumerate(items):
        if item.get("id") != todo_id:
            continue
        patch = body.model_dump(exclude_unset=True)
        merged = {**item, **patch, "updated_at": _now()}
        if merged.get("chosen_option") is not None and merged["chosen_option"] not in merged.get(
            "options", []
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "FEEDBACK_INVALID",
                    "message": (
                        f"chosen_option {merged['chosen_option']!r} is not one "
                        f"of this todo's options {merged.get('options', [])}"
                    ),
                },
            )
        validated = TodoOut.model_validate(merged)
        items[i] = validated.model_dump()
        _save(path, "todos", items)
        return validated
    raise HTTPException(
        status_code=404,
        detail={
            "code": "FEEDBACK_TODO_NOT_FOUND",
            "message": f"no review todo with id {todo_id!r}",
        },
    )


# ----- comments -----------------------------------------------------------
def _filter_comments_by_state(
    items: list[dict[str, Any]], state: str | None
) -> list[dict[str, Any]]:
    if state is None:
        return items
    if state not in _COMMENT_STATE_FILTERS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "FEEDBACK_INVALID_STATE",
                "message": f"state must be one of {sorted(_COMMENT_STATE_FILTERS)!r}, not {state!r}",
            },
        )
    if state == "harvested":
        return [c for c in items if c.get("status") == "harvested"]
    return [
        c
        for c in items
        if c.get("fixed_in_sha")
        and (c.get("updated_at") or "") > (c.get("fixed_at") or "")
    ]


@router.get("/comments", response_model=CommentListOut)
def list_comments(request: Request, state: str | None = None) -> CommentListOut:
    items = _load(_dir(request) / _COMMENTS_FILE, "comments")
    filtered = _filter_comments_by_state(items, state)
    return CommentListOut(comments=[CommentOut.model_validate(c) for c in filtered])


@router.post("/comments", response_model=CommentOut, status_code=201)
def create_comment(body: CommentCreateIn, request: Request) -> CommentOut:
    path = _dir(request) / _COMMENTS_FILE
    comment = CommentOut(
        id=uuid.uuid4().hex[:12],
        x_pct=body.x_pct,
        y_pct=body.y_pct,
        anchor=body.anchor,
        page=body.page,
        text=body.text,
        created_at=_now(),
        build=_build_stamp(request),
        environment=_pin_environment(body, request),
        status="open",
        author=body.author,
        agent_kind=body.agent_kind,
    )
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        items.append(comment.model_dump())
        _save(path, "comments", items)
    return comment


# ----- general note -------------------------------------------------------
@router.get("/general", response_model=GeneralNoteOut)
def get_general(request: Request) -> GeneralNoteOut:
    return GeneralNoteOut.model_validate(_load_general(_dir(request) / _GENERAL_FILE))


@router.put("/general", response_model=GeneralNoteOut)
def put_general(body: GeneralNotePutIn, request: Request) -> GeneralNoteOut:
    path = _dir(request) / _GENERAL_FILE
    note = GeneralNoteOut(text=body.text, updated_at=_now(), build=_build_stamp(request))
    write_atomic(path, json.dumps(note.model_dump(), indent=2) + "\n")
    return note


# ----- archive (the harvest's server side) --------------------------------
@router.post("/archive", response_model=ArchiveOut)
def archive_feedback(request: Request) -> ArchiveOut:
    from .feedback_archive import perform_bulk_archive

    return perform_bulk_archive(_dir(request))
