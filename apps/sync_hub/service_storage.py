"""The storage-full half of ``POST /api/v1/sync/push``: models and mapping.

Split out of :mod:`apps.sync_hub.service` for the quality-gate file_size
ratchet, exactly as ``service_enroll`` and ``service_shortfall``. The route
stays in ``service.py``; what lives here is the declared 507 body and the
one place a sqlite-full ``OperationalError`` becomes an HTTP status a spoke
can branch on instead of a bare 500.
"""
from __future__ import annotations

import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel

STORAGE_CODE: str = "SYNC_HUB_STORAGE"
STORAGE_STATUS: int = 507


class HubStorageErrorBody(BaseModel):
    """The ``detail`` object a storage-full push carries."""

    code: str
    message: str


class HubStorageErrorResponse(BaseModel):
    """FastAPI wraps an ``HTTPException`` detail under ``detail``."""

    detail: HubStorageErrorBody


PUSH_STORAGE_RESPONSES: dict[int | str, dict[str, object]] = {
    507: {
        "model": HubStorageErrorResponse,
        "description": (
            "The hub sqlite database cannot take the write. "
            f"code: {STORAGE_CODE}."
        ),
    },
}


def is_hub_storage_full(exc: BaseException) -> bool:
    """True only for sqlite-full ``OperationalError``, not lock or busy."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
        return True
    return "disk is full" in str(exc).lower()


def http_exception_for(exc: sqlite3.OperationalError) -> HTTPException:
    """Build the declared 507 for a sqlite-full push failure."""
    return HTTPException(
        status_code=STORAGE_STATUS,
        detail={"code": STORAGE_CODE, "message": str(exc)},
    )


def _storage_full_operational_error(
    exc: BaseException,
) -> sqlite3.OperationalError | None:
    """Return the sqlite-full error even when rollback masked it."""
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, sqlite3.OperationalError) and is_hub_storage_full(current):
            return current
        current = current.__context__
    return None


def raise_for_operational_error(exc: sqlite3.OperationalError) -> None:
    """Map sqlite-full to 507; re-raise every other ``OperationalError``."""
    storage = _storage_full_operational_error(exc)
    if storage is not None:
        raise http_exception_for(storage) from exc
    raise exc


__all__ = [
    "PUSH_STORAGE_RESPONSES",
    "STORAGE_CODE",
    "STORAGE_STATUS",
    "HubStorageErrorBody",
    "HubStorageErrorResponse",
    "http_exception_for",
    "is_hub_storage_full",
    "raise_for_operational_error",
]
