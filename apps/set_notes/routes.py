"""Set-notes HTTP endpoints (SETNOTE-6340c), mounted under ``/api/v1``.

No default note is fabricated: GET on an unsaved scope is a 404 and the
client creates it with ``base_rev`` 0.

The write guard (the web UI's cloud-lock check) is passed in rather than
imported, because ``.importlinter`` keeps every domain package below
``apps.webui``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from apps.shared.events import publish

from .connection import open_set_notes
from .revision import StaleRevision, stale_rev_detail
from .store import load_set_note, save_set_note, validate_scope
from .wire import geometry_from_optional, note_to_wire


class SetNoteOut(BaseModel):
    scope: str
    body_md: str
    x: int | None
    y: int | None
    w: int | None
    h: int | None
    visible: bool
    rev: int
    updated_at: str


class SetNotePutIn(BaseModel):
    body_md: str
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None
    visible: bool
    base_rev: int = Field(ge=0)


def _checked_scope(scope: str) -> str:
    try:
        return validate_scope(scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": "invalid_scope", "message": str(exc)},
        ) from exc


def _open_conn(request: Request) -> sqlite3.Connection:
    db_path = Path(request.app.state.state_db_path)
    try:
        return open_set_notes(db_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "state_db_missing", "message": str(exc)},
        ) from exc


def create_router(write_guard: Callable[..., object]) -> APIRouter:
    router = APIRouter(prefix="/set-notes", tags=["set-notes"])

    @router.get("/{scope}", response_model=SetNoteOut, operation_id="get_set_note")
    def get_set_note(scope: str, request: Request) -> SetNoteOut:
        scope = _checked_scope(scope)
        conn = _open_conn(request)
        try:
            return SetNoteOut(**note_to_wire(load_set_note(conn, scope)))
        except LookupError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found"},
            ) from None
        finally:
            conn.close()

    @router.put(
        "/{scope}",
        response_model=SetNoteOut,
        operation_id="put_set_note",
        responses={409: {"description": "base_rev is stale; body carries current_rev"}},
    )
    def put_set_note(
        scope: str,
        body: SetNotePutIn,
        request: Request,
        _guard: object = Depends(write_guard),  # FastAPI DI
    ) -> SetNoteOut:
        scope = _checked_scope(scope)
        conn = _open_conn(request)
        try:
            try:
                note = save_set_note(
                    conn,
                    scope,
                    body.body_md,
                    geometry_from_optional(body.x, body.y, body.w, body.h),
                    body.visible,
                    body.base_rev,
                )
                conn.commit()
            except StaleRevision as exc:
                conn.rollback()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=stale_rev_detail(exc.current_rev),
                ) from None
            publish("library.changed", {"kind": "set_notes", "ids": [scope]})
            return SetNoteOut(**note_to_wire(note))
        finally:
            conn.close()

    return router


__all__ = ["SetNoteOut", "SetNotePutIn", "create_router"]
