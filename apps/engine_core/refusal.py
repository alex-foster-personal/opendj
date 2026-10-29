"""Shared HTTP 503 unavailable envelope for engine-core health routes."""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse

__all__ = ["unavailable_response"]


def unavailable_response(
    error: str,
    message: str | None,
    *,
    details: dict[str, str] | None = None,
) -> JSONResponse:
    """Return the standard engine-core unavailable JSON body with status 503."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": error, "message": message, "details": details},
    )
