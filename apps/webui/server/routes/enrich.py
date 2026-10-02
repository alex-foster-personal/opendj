"""HTTP surface of the enrich-on-open card (`apps.webui.server.enrich_prompt`).

  GET /enrich/summary            what the library still lacks, lane by lane,
                                 and which opt-in lanes to ask about
  PUT /enrich/decisions/{lane}   store this library's answer for an opt-in
                                 lane: {"answer": "never" | "ask"}

Starting stems is the existing ``POST /jobs`` (kind stems, scope pending);
retrying failed analysis is ``POST /ahead-analysis/retry``.
CLI twin: ``python -m apps.webui.enrich_cli``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from apps.webui.server import enrich_prompt
from apps.webui.server.routes import ingest as ingest_routes
from apps.webui.server.routes.ingest_coverage_out import read_coverage

router = APIRouter(prefix="/enrich", tags=["enrich"])


class DecisionIn(BaseModel):
    answer: str


@router.get("/summary")
def get_summary(request: Request) -> dict[str, Any]:
    drain = getattr(request.app.state, "ahead_analysis", None)
    analysis = drain.coverage() if drain is not None else None
    analysis_error = None if drain is not None else "the ahead-of-time analysis drain is not running here"
    try:
        coverage: dict[str, Any] | None = read_coverage(
            request.app, True, ingest_routes.build_snapshot
        ).model_dump()
        coverage_error = None
    except Exception as exc:  # noqa: BLE001 - surfaced on the card, never a silent empty
        coverage, coverage_error = None, f"{type(exc).__name__}: {exc}"
    return enrich_prompt.build_summary(
        analysis=analysis,
        analysis_error=analysis_error,
        coverage=coverage,
        coverage_error=coverage_error,
        decisions=enrich_prompt.load_decisions(ingest_routes.COVERAGE_DATA_DIR),
    )


@router.put("/decisions/{lane}")
def put_decision(lane: str, body: DecisionIn) -> dict[str, Any]:
    try:
        decisions = enrich_prompt.save_decision(ingest_routes.COVERAGE_DATA_DIR, lane, body.answer)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "ENRICH_BAD_DECISION", "message": str(exc)}) from exc
    return {"ok": True, "decisions": decisions}
