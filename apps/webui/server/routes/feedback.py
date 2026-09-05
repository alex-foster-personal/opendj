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

router = APIRouter(prefix="/feedback", tags=["feedback"])

_TODOS_FILE = "review-todos.json"
_COMMENTS_FILE = "comments.json"
_GENERAL_FILE = "general-note.json"

# Every read/modify/write of comments.json, in THIS module and feedback_pins.py,
# holds this lock (issue #914 review, Wed 2 Sep 2026: a per-endpoint lock only
# serialized single-pin archives against each other, so a PATCH or follow-on
# could still race a concurrent archive - both report success and one write is
# silently lost, since FastAPI runs sync handlers in a threadpool). One lock
# for one file, held for the whole read-modify-write, not one lock per route.
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
    environment: "PinEnvironmentOut | None" = None
    # Pin lifecycle (issue #858): absent on pins created before Wed 2 Sep 2026.
    status: str | None = None  # open | issued | fixed | merged | archived
    issue_url: str | None = None
    agent_note: str | None = None
    updated_at: str | None = None


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


class PinEnvironmentOut(BaseModel):
    """Non-personal runtime facts needed to reproduce a pinned UI defect.

    ``machine`` and ``release_version`` are already exposed by the running
    daemon's settings/health surfaces. The browser contributes only its UI
    kind and viewport dimensions: no username, user agent, URL query, or
    other new personal data enters the pin store.
    """

    model_config = ConfigDict(frozen=True)

    ui: Literal["chrome-loop", "packaged-app"]
    viewport_width: int
    viewport_height: int
    machine: str
    release_version: str


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


def _load(path: Path, root_key: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get(root_key) if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FEEDBACK_STORE_MALFORMED",
                "message": f"{path} does not hold a {root_key!r} list",
            },
        )
    return items


def _save(path: Path, root_key: str, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({root_key: items}, indent=2) + "\n", encoding="utf-8"
    )


def _load_general(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"text": "", "updated_at": None, "build": None}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        raise HTTPException(
            status_code=500,
            detail={
                "code": "FEEDBACK_STORE_MALFORMED",
                "message": f"{path} does not hold a general note",
            },
        )
    return raw


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
        built_at = (
            datetime.fromisoformat(committed)
            .astimezone(UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        return BuildStampOut(
            git_sha=sha[:8], built_at_utc=built_at, source="repo"
        )
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
    """Combine browser dimensions with daemon facts it already publishes."""

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
        if merged.get("chosen_option") is not None and merged[
            "chosen_option"
        ] not in merged.get("options", []):
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
@router.get("/comments", response_model=CommentListOut)
def list_comments(request: Request) -> CommentListOut:
    items = _load(_dir(request) / _COMMENTS_FILE, "comments")
    return CommentListOut(
        comments=[CommentOut.model_validate(c) for c in items]
    )


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
    )
    with _COMMENTS_LOCK:
        items = _load(path, "comments")
        items.append(comment.model_dump())
        _save(path, "comments", items)
    return comment


# ----- general note -------------------------------------------------------
@router.get("/general", response_model=GeneralNoteOut)
def get_general(request: Request) -> GeneralNoteOut:
    return GeneralNoteOut.model_validate(
        _load_general(_dir(request) / _GENERAL_FILE)
    )


@router.put("/general", response_model=GeneralNoteOut)
def put_general(body: GeneralNotePutIn, request: Request) -> GeneralNoteOut:
    path = _dir(request) / _GENERAL_FILE
    note = GeneralNoteOut(
        text=body.text, updated_at=_now(), build=_build_stamp(request)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(note.model_dump(), indent=2) + "\n", encoding="utf-8"
    )
    return note


# ----- archive (the harvest's server side) --------------------------------
@router.post("/archive", response_model=ArchiveOut)
def archive_feedback(request: Request) -> ArchiveOut:
    root = _dir(request)
    todos_path = root / _TODOS_FILE
    comments_path = root / _COMMENTS_FILE
    general_path = root / _GENERAL_FILE

    with _COMMENTS_LOCK:
        todos = _load(todos_path, "todos")
        comments = _load(comments_path, "comments")
        general = _load_general(general_path)

        kept_todos: list[dict[str, Any]] = []
        archived_todos: list[dict[str, Any]] = []
        archived_feedback: list[dict[str, Any]] = []
        for todo in todos:
            if todo.get("done"):
                archived_todos.append(todo)
            elif todo.get("feedback"):
                archived_feedback.append(
                    {
                        "todo_id": todo.get("id"),
                        "title": todo.get("title"),
                        "feedback": todo["feedback"],
                        "chosen_option": todo.get("chosen_option"),
                    }
                )
                kept_todos.append({**todo, "feedback": "", "updated_at": _now()})
            else:
                kept_todos.append(todo)

        general_archived = bool(general["text"])
        nothing_to_do = (
            not archived_todos
            and not archived_feedback
            and not comments
            and not general_archived
        )
        if nothing_to_do:
            return ArchiveOut(
                archived_to=None,
                todos_archived=0,
                todo_feedback_archived=0,
                comments_archived=0,
                general_archived=False,
            )

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        archive_path = root / f"archive-{stamp}.json"
        if archive_path.exists():
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "FEEDBACK_ARCHIVE_COLLISION",
                    "message": f"{archive_path} already exists; retry in 1s",
                },
            )
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_text(
            json.dumps(
                {
                    "archived_at": _now(),
                    "todos": archived_todos,
                    "todo_feedback": archived_feedback,
                    "comments": comments,
                    "general": general if general_archived else None,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        _save(todos_path, "todos", kept_todos)
        _save(comments_path, "comments", [])
        if general_archived:
            general_path.write_text(
                json.dumps(
                    {"text": "", "updated_at": _now(), "build": general.get("build")},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

    return ArchiveOut(
        archived_to=str(archive_path),
        todos_archived=len(archived_todos),
        todo_feedback_archived=len(archived_feedback),
        comments_archived=len(comments),
        general_archived=general_archived,
    )
