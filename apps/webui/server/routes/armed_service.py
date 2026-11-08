"""Read a background service the daemon may not have armed, or answer 503.

The coverage drain, the ahead-analysis drain and the grid-phase precompute
are built only when the daemon arms them (app_wiring), so a missing
``app.state`` attribute means "not running here", never "idle".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request


def armed_service(attribute: str, *, code: str, message: str) -> Callable[[Request], Any]:
    """Return a lookup of ``app.state.<attribute>`` that answers 503 {code, message} when absent."""

    def lookup(request: Request) -> Any:
        service = getattr(request.app.state, attribute, None)
        if service is None:
            raise HTTPException(status_code=503, detail={"code": code, "message": message})
        return service

    return lookup


__all__ = ["armed_service"]
