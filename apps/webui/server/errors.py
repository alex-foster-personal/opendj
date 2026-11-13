"""Shared error response schemas + exception handlers.

``_request`` throughout: FastAPI's exception-handler contract is
``(request, exc)`` and both are passed positionally, so the first parameter has
to exist whether or not a handler reads it. The underscore says "required by
the framework, deliberately unused" instead of leaving four ARG001s that read
as coupling debt.
"""

from __future__ import annotations

import http
import math
import sqlite3
from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled
from apps.shared.state.db import StateStoreBusyError, is_sqlite_busy

from .backend import BackendError, ConflictError, NotFoundError
from .playlist_add import AlreadyExistsError, BulkLimitError, SmartlistImmutableError
from .playlist_move import SliceNotContiguousError, TargetInsideSliceError

PROBLEM_DETAILS_MEDIA_TYPE = "application/problem+json"
_PROBLEM_TYPE_PREFIX = "https://opendj.dev/problems/"


class ErrorBody(BaseModel):
    error: str
    message: str
    details: dict[str, Any] | None = None
    type: str = ""
    title: str = ""
    status: int = 0
    detail: str = ""


class ConflictBody(BaseModel):
    error: str = "conflict"
    message: str
    current: dict[str, Any]
    etag: str
    type: str = ""
    title: str = ""
    status: int = 0
    detail: str = ""


def _http_title(status_code: int) -> str:
    try:
        return http.HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


def _problem_type(error_code: str) -> str:
    slug = error_code.replace("_", "-")
    return f"{_PROBLEM_TYPE_PREFIX}{slug}"


def _enrich_error_body(
    status_code: int,
    *,
    error: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ErrorBody(
        error=error,
        message=message,
        details=details,
        type=_problem_type(error),
        title=_http_title(status_code),
        status=status_code,
        detail=message,
    ).model_dump()


def _enrich_conflict_body(
    status_code: int,
    *,
    message: str,
    current: dict[str, Any],
    etag: str,
) -> dict[str, Any]:
    return ConflictBody(
        message=message,
        current=current,
        etag=etag,
        type=_problem_type("conflict"),
        title=_http_title(status_code),
        status=status_code,
        detail=message,
    ).model_dump()


def _error_response(
    status_code: int,
    *,
    error: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type=PROBLEM_DETAILS_MEDIA_TYPE,
        content=_enrich_error_body(status_code, error=error, message=message, details=details),
    )


async def handle_not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
    return _error_response(
        status.HTTP_404_NOT_FOUND,
        error="not_found",
        message=str(exc),
    )


async def handle_conflict(_request: Request, exc: ConflictError) -> JSONResponse:
    message = "If-Match does not match the current row etag"
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        media_type=PROBLEM_DETAILS_MEDIA_TYPE,
        headers={"ETag": exc.etag},
        content=_enrich_conflict_body(
            status.HTTP_409_CONFLICT,
            message=message,
            current=exc.current,
            etag=exc.etag,
        ),
    )


async def handle_already_exists(
    _request: Request,
    exc: AlreadyExistsError,
) -> JSONResponse:
    return _error_response(
        status.HTTP_409_CONFLICT,
        error="already_exists",
        message=str(exc),
    )


async def handle_smartlist_immutable(
    _request: Request,
    exc: SmartlistImmutableError,
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="smartlist_immutable",
        message=str(exc),
    )


async def handle_slice_not_contiguous(
    _request: Request,
    exc: SliceNotContiguousError,
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="slice_not_contiguous",
        message=str(exc),
    )


async def handle_target_inside_slice(
    _request: Request,
    exc: TargetInsideSliceError,
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="target_inside_slice",
        message=str(exc),
    )


async def handle_bulk_limit(_request: Request, exc: BulkLimitError) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="bulk_limit",
        message=str(exc),
    )


def _json_safe(value: Any) -> Any:
    """Replace NaN and Infinity, which JSON cannot encode, with their string form."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


async def handle_request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422, but still a 422 when the rejected input is non-finite (PREF-2163)."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(_json_safe(list(exc.errors())))},
    )


async def handle_backend_error(_request: Request, exc: BackendError) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        error="invalid_patch",
        message=str(exc),
    )


STATE_STORE_BUSY_CODE = "STATE_STORE_BUSY"


def _state_store_busy_body(exc: BaseException) -> dict[str, Any]:
    message = str(exc).strip() or "database is locked"
    if "busy" not in message.lower() and "locked" not in message.lower():
        message = f"state store is busy: {message}"
    elif not message.lower().startswith("state store"):
        message = f"state store is busy: {message}"
    return {
        "detail": {
            "code": STATE_STORE_BUSY_CODE,
            "message": message,
        },
    }


async def handle_state_store_busy(
    _request: Request,
    exc: StateStoreBusyError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_state_store_busy_body(exc),
    )


async def handle_sqlite_busy_operational_error(
    _request: Request,
    exc: sqlite3.OperationalError,
) -> JSONResponse:
    if not is_sqlite_busy(exc):
        raise exc
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=_state_store_busy_body(exc),
    )


async def handle_rekordbox_writeback_disabled(_request: Request, exc: RekordboxWritebackDisabled) -> JSONResponse:
    """One-way import mode refused this write. 403, never a fake 200.

    The body carries the machine-readable ``rekordbox_writeback_disabled``
    code plus the mapped surface id, so an agent driving the API learns
    exactly which write surface it hit and why nothing happened.
    """
    return _error_response(
        status.HTTP_403_FORBIDDEN,
        error=exc.code,
        message=exc.message,
        details={"surface": exc.surface_id},
    )


def precondition_required(message: str) -> JSONResponse:
    return _error_response(
        status.HTTP_428_PRECONDITION_REQUIRED,
        error="precondition_required",
        message=message,
    )


__all__ = [
    "ConflictBody",
    "ErrorBody",
    "PROBLEM_DETAILS_MEDIA_TYPE",
    "STATE_STORE_BUSY_CODE",
    "handle_already_exists",
    "handle_backend_error",
    "handle_bulk_limit",
    "handle_conflict",
    "handle_not_found",
    "handle_rekordbox_writeback_disabled",
    "handle_slice_not_contiguous",
    "handle_smartlist_immutable",
    "handle_sqlite_busy_operational_error",
    "handle_state_store_busy",
    "handle_target_inside_slice",
    "precondition_required",
]
