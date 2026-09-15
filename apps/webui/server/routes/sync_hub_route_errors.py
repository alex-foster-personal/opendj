"""Shared sync-hub transport error mapping for stems HTTP routes (issue #2978).

Route audit (sync_hub transport failures):
- stems_assets.py: bulk-hydrate -> 503 SYNC_HUB_UNREACHABLE
- stems.py: manifest/part hydrate -> 503 via _raise_hydrate_error
- cloudsync_ops.py: 502 CLOUDSYNC_HUB_UNREACHABLE (unchanged)
- feedback_sync.py: 503 FEEDBACK_SYNC_HUB_UNREACHABLE (unchanged)
"""

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
