"""Record, list and clear "this track has no stems to make" (HEALTH-09).

  GET    /coverage-outcomes/stems/no-source              every mark, with why
  POST   /coverage-outcomes/stems/no-source              {"stable_id", "reason"}
  DELETE /coverage-outcomes/stems/no-source/{stable_id}  clear a mark

A marked track counts as finished for the Stems (and so the Vocals) light and
is shown separately in the hover. Clearing a mark also stops the automatic
check from putting it back. CLI twin: ``python -m apps.webui.coverage_drain_cli
no-source-list | no-source-mark | no-source-clear``.

The mark is keyed on the audio file's signature, so the track must be present
on this machine: a track with no audio here answers 404.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.webui.server import coverage_outcomes as outcomes_mod
from apps.webui.server import coverage_stems_terminal as stems_terminal
from apps.webui.server.routes import ingest as ingest_routes
from apps.webui.server.routes.ingest_job import tracks_on_disk

router = APIRouter(prefix="/coverage-outcomes", tags=["coverage-drain"])


class NoSourceMarkOut(BaseModel):
    stable_id: str
    reason: str
    recorded_at: float
    #: False when the audio file changed since the mark: it no longer applies.
    applies: bool | None


class NoSourceListOut(BaseModel):
    marks: list[NoSourceMarkOut]
    #: Ids a user cleared; the automatic check leaves them pending.
    keep_pending: list[str]


class NoSourceMarkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stable_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=500)


class NoSourceClearOut(BaseModel):
    stable_id: str
    cleared: bool
    keep_pending: bool


def _stores(request: Request) -> tuple[outcomes_mod.OutcomeStore, stems_terminal.KeepPending]:
    data_dir = ingest_routes.COVERAGE_DATA_DIR
    drain = getattr(request.app.state, "coverage_drain", None)
    # The drain's own store when there is one: its lock serializes both writers.
    outcomes = getattr(drain, "outcomes", None) or outcomes_mod.OutcomeStore(
        outcomes_mod.store_path(data_dir)
    )
    return outcomes, stems_terminal.KeepPending(stems_terminal.keep_pending_path(data_dir))


def _refresh(request: Request) -> None:
    drain = getattr(request.app.state, "coverage_drain", None)
    if drain is not None and hasattr(drain, "invalidate"):
        drain.invalidate()


def _present_paths() -> dict[str, Path]:
    present, _unreachable = tracks_on_disk(ingest_routes.open_ro)
    return {stable_id: Path(path) for stable_id, path in present}


def _out(outcome: outcomes_mod.Outcome, paths: dict[str, Path]) -> NoSourceMarkOut:
    path = paths.get(outcome.stable_id)
    return NoSourceMarkOut(
        stable_id=outcome.stable_id,
        reason=outcome.reason,
        recorded_at=outcome.recorded_at,
        applies=None if path is None else outcomes_mod.is_no_source(
            outcome, outcomes_mod.audio_token(path)
        ),
    )


@router.get("/stems/no-source", response_model=NoSourceListOut)
def list_stems_no_source(request: Request) -> NoSourceListOut:
    outcomes, keep = _stores(request)
    paths = _present_paths()
    return NoSourceListOut(
        marks=[_out(outcome, paths) for outcome in stems_terminal.list_no_source(outcomes)],
        keep_pending=sorted(keep.load()),
    )


@router.post("/stems/no-source", response_model=NoSourceMarkOut)
def mark_stems_no_source(request: Request, body: NoSourceMarkIn) -> NoSourceMarkOut:
    outcomes, keep = _stores(request)
    paths = _present_paths()
    path = paths.get(body.stable_id)
    if path is None:
        raise HTTPException(
            404, f"stable_id {body.stable_id!r} has no audio present on this machine"
        )
    outcome = stems_terminal.mark_no_source(
        outcomes, keep, body.stable_id, path, body.reason, now=time.time()
    )
    _refresh(request)
    return _out(outcome, paths)


@router.delete("/stems/no-source/{stable_id}", response_model=NoSourceClearOut)
def clear_stems_no_source(request: Request, stable_id: str) -> NoSourceClearOut:
    outcomes, keep = _stores(request)
    cleared = stems_terminal.clear_no_source(outcomes, keep, stable_id)
    _refresh(request)
    return NoSourceClearOut(stable_id=stable_id, cleared=cleared, keep_pending=True)


__all__ = ["router"]
