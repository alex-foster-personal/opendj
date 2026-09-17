"""Convert CLI-parity SystemExit leaks into HTTP errors (issue #2957, STEM-34)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from apps.vocals.errors import UnknownPlaylistError

T = TypeVar("T")


def unknown_playlist_response(exc: UnknownPlaylistError) -> JSONResponse:
    """404 body shape required by STEM-34 acceptance (flat detail + known)."""
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"unknown playlist '{exc.name}'",
            "known": exc.known,
        },
    )


def guard_stems_parity_call(fn: Callable[[], T]) -> T:
    """Run parity logic; never let SystemExit escape to uvicorn."""
    try:
        return fn()
    except UnknownPlaylistError:
        raise
    except SystemExit as exc:
        message = str(exc) or "invalid request"
        raise HTTPException(status_code=422, detail=message) from exc
