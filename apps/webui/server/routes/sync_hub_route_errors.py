"""Shared sync-hub transport error mapping for stems HTTP routes (issue #2978)."""

from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import HTTPException

log = logging.getLogger(__name__)


def raise_sync_hub_unreachable(endpoint: str, exc: BaseException) -> NoReturn:
    """Map an unreachable hub to HTTP 503 with one WARNING, no ASGI traceback."""
    log.warning("sync hub unreachable at %s: %s", endpoint, exc)
    raise HTTPException(
        status_code=503,
        detail={
            "code": "SYNC_HUB_UNREACHABLE",
            "endpoint": endpoint,
            "message": str(exc),
        },
    ) from exc
