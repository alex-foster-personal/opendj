"""CloudSync stem bundle push/hydrate routes (agent-native parity, D13.3).

Sibling of ``routes/stems.py``, which serves validated local bundles under
``/tracks/{stable_id}/stems``. These endpoints drive the migration rail and
the hash-based hydrate path without going through the track-scoped loader.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from apps.lyrics import stems_sync

router = APIRouter(tags=["stems"])


class StemHydrateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_path: str
    dry_run: bool = False
    data_dir: str | None = None


class StemPushMissingIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    dry_run: bool = False
    data_dir: str | None = Field(default=None)


@router.post("/stems/{stable_id}/hydrate")
def hydrate_stem(stable_id: str, body: StemHydrateIn) -> dict[str, str]:
    data_dir = Path(body.data_dir) if body.data_dir else None
    try:
        rc = stems_sync.hydrate(
            stable_id,
            manifest_path=Path(body.manifest_path),
            data_dir=data_dir,
            dry_run=body.dry_run,
        )
    except (ValueError, Exception) as exc:
        from apps.cloud.eviction import HydrationError

        if isinstance(exc, (HydrationError, ValueError)):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise
    if rc != 0:
        raise HTTPException(status_code=500, detail="hydrate failed")
    return {"status": "ok", "stable_id": stable_id}


@router.post("/stems/push-missing")
def push_missing_stems(body: StemPushMissingIn) -> dict[str, str]:
    data_dir = Path(body.data_dir) if body.data_dir else None
    rc = stems_sync.push_missing(data_dir=data_dir, dry_run=body.dry_run)
    if rc != 0:
        raise HTTPException(status_code=500, detail="push-missing failed")
    return {"status": "ok"}
