"""HTTP surface of the ingest enrichment coordinator (ENRICH-09).

  GET  /ingest/enrichment             every recent import batch: lane counts in
                                      priority order, artwork stage, and the
                                      capability warnings (what cannot run here)
  GET  /ingest/enrichment/{batch_id}  one batch, with every track's lane states
  POST /ingest/enrichment/rerun       {"batch_id": B} or {"playlist_id": P}:
                                      forget failures and cover misses, enrich again

CLI twin: ``opendj enrichment``. MCP: ``ingest_enrichment_status``,
``ingest_enrichment_rerun``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from apps.webui.server import ingest_enrichment

router = APIRouter(prefix="/ingest/enrichment", tags=["ingest"])


class RerunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str | None = None
    playlist_id: str | None = None


@router.get("")
def get_enrichment(request: Request) -> dict[str, Any]:
    return ingest_enrichment.coordinator_for(request.app).status()


@router.get("/{batch_id}")
def get_batch(batch_id: str, request: Request) -> dict[str, Any]:
    try:
        return ingest_enrichment.coordinator_for(request.app).status(batch_id, with_tracks=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "ENRICH_BATCH_NOT_FOUND",
                                                     "message": f"no import batch {batch_id!r}"}) from exc


@router.post("/rerun")
def post_rerun(body: RerunIn, request: Request) -> dict[str, Any]:
    coordinator = ingest_enrichment.coordinator_for(request.app)
    try:
        return coordinator.rerun(batch_id=body.batch_id, playlist_id=body.playlist_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "ENRICH_RERUN_TARGET",
                                                     "message": str(exc)}) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "ENRICH_RERUN_NOT_FOUND",
                                                     "message": f"no batch or playlist {exc.args[0]!r}"}) from exc
