"""Bulk harvest archive logic (FB-06, issue #3782).

Split from ``feedback.py`` for the 600-line file budget. Marks pins ``harvested``
instead of clearing ``comments.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def mark_comments_harvested(
    comments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mark eligible pins harvested in the live store.

    Returns ``(updated_live_comments, archive_snapshots)``. Skips pins already
    ``harvested`` or ``archived``. Idempotent on re-run.
    """
    to_harvest = [
        c for c in comments if c.get("status") not in {"harvested", "archived"}
    ]
    if not to_harvest:
        return comments, []

    now = _now()
    harvested_ids = {c["id"] for c in to_harvest}
    snapshots = [
        {**c, "status": "harvested", "harvested_at": now, "updated_at": now}
        for c in to_harvest
    ]
    updated = [
        {**c, "status": "harvested", "harvested_at": now, "updated_at": now}
        if c.get("id") in harvested_ids
        else c
        for c in comments
    ]
    return updated, snapshots


def perform_bulk_archive(root: Path) -> Any:
    """Run ``POST /feedback/archive`` under ``_COMMENTS_LOCK``."""
    import json

    from fastapi import HTTPException

    from .feedback import (
        _COMMENTS_FILE,
        _COMMENTS_LOCK,
        _GENERAL_FILE,
        _TODOS_FILE,
        ArchiveOut,
        _load,
        _load_general,
        _save,
        write_atomic,
    )

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
        harvested_comments, comment_snapshots = mark_comments_harvested(comments)
        nothing_to_do = (
            not archived_todos
            and not archived_feedback
            and not comment_snapshots
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
        write_atomic(
            archive_path,
            json.dumps(
                {
                    "archived_at": _now(),
                    "todos": archived_todos,
                    "todo_feedback": archived_feedback,
                    "comments": comment_snapshots,
                    "general": general if general_archived else None,
                },
                indent=2,
            )
            + "\n",
        )

        _save(todos_path, "todos", kept_todos)
        _save(comments_path, "comments", harvested_comments)
        if general_archived:
            write_atomic(
                general_path,
                json.dumps(
                    {"text": "", "updated_at": _now(), "build": general.get("build")},
                    indent=2,
                )
                + "\n",
            )

    return ArchiveOut(
        archived_to=str(archive_path),
        todos_archived=len(archived_todos),
        todo_feedback_archived=len(archived_feedback),
        comments_archived=len(comment_snapshots),
        general_archived=general_archived,
    )
