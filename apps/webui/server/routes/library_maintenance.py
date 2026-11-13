"""Library maintenance route: explicit, confirmed SQLite optimize (LIBM-126, issue #12332).

- ``POST /library/maintenance/optimize {data_dir, confirm}`` runs ``PRAGMA optimize`` and
  ``VACUUM`` on ``<data_dir>/state/state.db`` only when ``confirm`` is true.
- No confirmation: 400 ``CONFIRM_REQUIRED`` with the warning, nothing runs.
- No state.db in ``data_dir``: 404 ``LIBRARY_DB_MISSING``; no library schema: 409 ``NOT_A_LIBRARY``.
- SQLite failure (locked, disk full): 503 ``OPTIMIZE_FAILED`` naming the error.

The core lives in :mod:`apps.shared.state.db_optimize`; ``opendj library optimize`` calls
the same function. Mounted from ``app_wiring.py`` under the API prefix.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.shared.state import db_optimize

router = APIRouter(tags=["library"])
MOUNT_UNDER_API_PREFIX = True

_REFUSAL_STATUS = {"LIBRARY_DB_MISSING": 404, "NOT_A_LIBRARY": 409}


class LibraryOptimizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str = Field(min_length=1, description="Data directory whose state/state.db is optimized")
    confirm: bool = Field(description="Must be true; VACUUM blocks writers while it runs")


class LibraryOptimizeOut(BaseModel):
    status: str = Field(description="Always 'completed'; failures answer with an error status")
    db_path: str
    bytes_before: int
    bytes_after: int
    elapsed_ms: int
    warning: str


@router.post("/library/maintenance/optimize", response_model=LibraryOptimizeOut)
def optimize_library(body: LibraryOptimizeIn) -> LibraryOptimizeOut:
    """Optimize the named library DB only when confirmed; report completion or the failure."""
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONFIRM_REQUIRED", "message": f"set confirm=true to optimize. {db_optimize.WARNING}"},
        )
    try:
        result = db_optimize.optimize_library_db(Path(body.data_dir))
    except db_optimize.OptimizeRefused as exc:
        raise HTTPException(
            status_code=_REFUSAL_STATUS[exc.code], detail={"code": exc.code, "message": exc.message}
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=503, detail={"code": "OPTIMIZE_FAILED", "message": f"optimize failed: {exc}"}
        ) from exc
    return LibraryOptimizeOut(
        status="completed",
        db_path=str(result.db_path),
        bytes_before=result.bytes_before,
        bytes_after=result.bytes_after,
        elapsed_ms=result.elapsed_ms,
        warning=db_optimize.WARNING,
    )
