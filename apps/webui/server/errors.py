"""Shared error response schemas + exception handlers.

``_request`` throughout: FastAPI's exception-handler contract is
``(request, exc)`` and both are passed positionally, so the first parameter has
to exist whether or not a handler reads it. The underscore says "required by
the framework, deliberately unused" instead of leaving four ARG001s that read
as coupling debt.
"""
from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled

from .backend import BackendError, ConflictError, NotFoundError


class ErrorBody(BaseModel):
    error: str
    message: str
    details: dict[str, Any] | None = None


class ConflictBody(BaseModel):
    error: str = "conflict"
    message: str
    current: dict[str, Any]
    etag: str


async def handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=ErrorBody(error="not_found", message=str(exc)).model_dump(),
    )


async def handle_conflict(_request: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        headers={"ETag": exc.etag},
        content=ConflictBody(
            message="If-Match does not match the current row etag",
            current=exc.current, etag=exc.etag,
        ).model_dump(),
    )


async def handle_backend_error(_request: Request, exc: BackendError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorBody(error="invalid_patch", message=str(exc)).model_dump(),
    )


async def handle_rekordbox_writeback_disabled(
    _request: Request, exc: RekordboxWritebackDisabled
) -> JSONResponse:
    """One-way import mode refused this write. 403, never a fake 200.

    The body carries the machine-readable ``rekordbox_writeback_disabled``
    code plus the mapped surface id, so an agent driving the API learns
    exactly which write surface it hit and why nothing happened.
    """
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content=ErrorBody(
            error=exc.code, message=exc.message, details={"surface": exc.surface_id}
        ).model_dump(),
    )


def precondition_required(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED,
        content=ErrorBody(error="precondition_required", message=message).model_dump(),
    )


__all__ = ["ConflictBody", "ErrorBody", "handle_backend_error",
           "handle_conflict", "handle_not_found",
           "handle_rekordbox_writeback_disabled", "precondition_required"]
